"""异动扫描：在维度和时间上找“值得提假设”的信号。

只用数据本身（时间序列突变、基期尖峰、小时级骤降、断货、渠道结构变化、价格和费率变化），不读标准答案。
扫描结果交给假设生成环节：每个信号对应一条候选假设，再加上事件日历里落在窗口内的事件。
"""
import numpy as np
import pandas as pd

from .domains import shift_date


def _runs(dates):
    """连续日期分段。"""
    out, cur = [], []
    for d in sorted(dates):
        if cur and shift_date(cur[-1], 1) != d:
            out.append(cur)
            cur = []
        cur.append(d)
    if cur:
        out.append(cur)
    return out


def level_shift(series, search_start, min_seg=5, pre_days=14, exclude=()):
    """在 search_start 之后找一个突变点 τ，使“τ 之后均值 / τ 之前 pre_days 天均值”变化最显著。返回 (τ, 变化率, t 值)。"""
    s = series[~series.index.isin(exclude)]
    best = None
    idx = list(s.index)
    for i, tau in enumerate(idx):
        if tau < search_start:
            continue
        pre = s[(s.index < tau) & (s.index >= shift_date(tau, -pre_days))]
        post = s[s.index >= tau]
        if len(pre) < min_seg or len(post) < min_seg:
            continue
        diff = post.mean() - pre.mean()
        se = np.sqrt(pre.var(ddof=1) / len(pre) + post.var(ddof=1) / len(post)) or 1e-9
        t = diff / se
        if best is None or abs(t) > abs(best[2]):
            best = (tau, post.mean() / pre.mean() - 1, t)
    return best


def ecommerce_signals(dom, p0, p1):
    sig = []
    # 1. 基期 / 现期的日 GMV 尖峰（大促等）
    d = dom.daily(p0["start"], p1["end"]).set_index("dt")
    spike_days = []
    for p, which in ((p0, "base"), (p1, "current")):
        g = d.loc[p["start"]:p["end"], "gmv"]
        med = g.median()
        days = list(g[g > med * 1.25].index)
        for run in _runs(days):
            excess = float((g.loc[run] - med).sum())
            sig.append(dict(type="spike", period=which, start=run[0], end=run[-1], excess=excess, ratio=float(g.loc[run].mean() / med),
                            text=f"{'基期' if which == 'base' else '现期'} {run[0]} 至 {run[-1]} 日 GMV 为中位数的 {g.loc[run].mean() / med:.2f} 倍"))
            spike_days += run
    # 2. 分渠道访客数突变
    t = dom.q("SELECT f.dt, f.channel_id, ch.channel_name, f.uv FROM fact_traffic_daily f JOIN dim_channel ch ON ch.channel_id = f.channel_id WHERE f.dt BETWEEN ? AND ?",
              (shift_date(p1["start"], -21), p1["end"]))
    for (cid, cname), g in t.groupby(["channel_id", "channel_name"]):
        s = g.set_index("dt").uv.sort_index()
        r = level_shift(s, p1["start"], exclude=spike_days)
        if r and r[1] <= -0.25 and abs(r[2]) >= 4:
            sig.append(dict(type="traffic_shift", channel_id=cid, channel_name=cname, start=r[0], end=p1["end"], change=float(r[1]), t=float(r[2]),
                            text=f"{cname}访客数自 {r[0]} 起较此前 14 天均值变化 {r[1]:+.0%}（t = {r[2]:.1f}）"))
    # 3. 现期断货
    oos = dom.q("""SELECT i.product_id, p.product_name, p.category_id, c.category_name, MIN(i.dt) s, MAX(i.dt) e, COUNT(*) n
                   FROM fact_inventory_daily i JOIN dim_product p ON p.product_id = i.product_id JOIN dim_category c ON c.category_id = p.category_id
                   WHERE i.is_out_of_stock = 1 AND i.dt BETWEEN ? AND ? GROUP BY i.product_id""", (p1["start"], p1["end"]))
    if len(oos):
        for cat, g in oos.groupby("category_id"):
            sig.append(dict(type="stockout", category_id=cat, category_name=g.category_name.iloc[0], products=list(g.product_id),
                            product_names=list(g.product_name), start=g.s.min(), end=g.e.max(), days=int(g.n.max()),
                            text=f"{g.category_name.iloc[0]}类目 {len(g)} 个商品在 {g.s.min()} 至 {g.e.max()} 断货（{', '.join(g.product_name)}）"))
    # 4. 小时级骤降（系统故障等）
    h = dom.q("SELECT dt, CAST(substr(pay_time, 12, 2) AS INTEGER) hr, COUNT(*) n FROM fact_orders WHERE dt BETWEEN ? AND ? GROUP BY dt, hr",
              (p1["start"], p1["end"]))
    pv = h.pivot(index="dt", columns="hr", values="n").fillna(0)
    med = pv.median()
    hits = [(dt, hr, pv.loc[dt, hr], med[hr]) for dt in pv.index for hr in pv.columns if med[hr] >= 50 and pv.loc[dt, hr] < 0.4 * med[hr]]
    for dt in sorted({x[0] for x in hits}):
        hrs = [x for x in hits if x[0] == dt]
        lost = sum(m - a for _, _, a, m in hrs)
        sig.append(dict(type="hourly_drop", date=dt, start=dt, end=dt, hours=[int(x[1]) for x in hrs], lost_orders=float(lost),
                        text=f"{dt} {','.join(str(int(x[1])) for x in hrs)} 点订单量只有同时段中位数的 {sum(x[2] for x in hrs) / sum(x[3] for x in hrs):.0%}"))
    return sig, spike_days


def fmcg_signals(dom, p0, p1):
    sig = []
    a, b = dom.by_channel(p0), dom.by_channel(p1)
    sa, sb = a.rev / a.rev.sum(), b.rev / b.rev.sum()
    for k in b.index:
        ds = sb[k] - sa.get(k, 0)
        if abs(ds) >= 0.03:
            sig.append(dict(type="channel_mix", channel_id=k, channel_name=b.channel_name[k], share0=float(sa.get(k, 0)), share1=float(sb[k]),
                            margin0=float(a.margin.get(k, np.nan)), margin1=float(b.margin[k]),
                            text=f"{b.channel_name[k]}营收占比由 {sa.get(k, 0):.1%} 变为 {sb[k]:.1%}，该渠道净利率 {b.margin[k]:.1%}（整体 {b.net.sum() / b.rev.sum():.1%}）"))
    # 产品线折扣率变化
    def disc(p):
        return dom.q("""SELECT d.product_line k, SUM(s.discount_amount) / SUM(s.gross_amount) r FROM fact_sales_daily s JOIN dim_sku d ON d.sku_id = s.sku_id
                        WHERE s.dt BETWEEN ? AND ? GROUP BY k""", (p["start"], p["end"])).set_index("k").r
    d0, d1 = disc(p0), disc(p1)
    for k in d1.index:
        if d1[k] - d0.get(k, d1[k]) >= 0.03:
            sig.append(dict(type="discount", product_line=k, rate0=float(d0[k]), rate1=float(d1[k]),
                            text=f"{k}折扣率由 {d0[k]:.1%} 升到 {d1[k]:.1%}"))
    # 基期异常：基期月份和相邻月份比，折扣率是否偏高
    months = dom.periods_available()
    i0 = months.index(p0["start"][:7])
    neighbors = [m for m in months[max(0, i0 - 2): i0 + 2] if m != p0["start"][:7] and m != p1["start"][:7]]
    if neighbors:
        from .domains import month_period
        nb = [dom.measures(month_period(m))["discount_rate"] for m in neighbors]
        m0 = dom.measures(p0)
        if m0["discount_rate"] - np.mean(nb) >= 0.01:
            sig.append(dict(type="base_discount", month=p0["start"][:7], rate=float(m0["discount_rate"]), neighbors=neighbors, neighbor_rate=float(np.mean(nb)),
                            text=f"基期 {p0['start'][:7]} 折扣率 {m0['discount_rate']:.1%}，高于相邻月份均值 {np.mean(nb):.1%}"))
    # 原材料价格变化
    mp = dom.q("""SELECT m.material_id, d.material_name, m.month, m.unit_price FROM fact_material_price_monthly m JOIN dim_material d ON d.material_id = m.material_id
                  WHERE m.month IN (?, ?)""", (p0["start"][:7], p1["start"][:7]))
    pv = mp.pivot_table(index=["material_id", "material_name"], columns="month", values="unit_price")
    for (mid, mname), r in pv.iterrows():
        ch = r[p1["start"][:7]] / r[p0["start"][:7]] - 1
        if abs(ch) >= 0.1:
            sig.append(dict(type="material_price", material_id=mid, material_name=mname, change=float(ch),
                            text=f"{mname}采购单价 {p0['start'][:7]} → {p1['start'][:7]} 变化 {ch:+.0%}"))
    # 佣金费率变化
    cm = dom.q("""SELECT c.channel_id, ch.channel_name, c.effective_from, c.commission_rate FROM dim_channel_commission c JOIN dim_channel ch ON ch.channel_id = c.channel_id
                  WHERE c.effective_from > ? AND c.effective_from <= ?""", (p0["end"], p1["end"]))
    for _, r in cm.iterrows():
        prev = dom.q("SELECT commission_rate FROM dim_channel_commission WHERE channel_id = ? AND effective_from < ? ORDER BY effective_from DESC LIMIT 1",
                     (r.channel_id, r.effective_from))
        r0 = float(prev.commission_rate[0]) if len(prev) else 0.0
        sig.append(dict(type="commission", channel_id=r.channel_id, channel_name=r.channel_name, start=r.effective_from, rate0=r0, rate1=float(r.commission_rate),
                        text=f"{r.channel_name}佣金率自 {r.effective_from} 由 {r0:.0%} 调整为 {r.commission_rate:.0%}"))
    return sig, []
