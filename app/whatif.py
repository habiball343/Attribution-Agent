"""What-If 推演：基于归因结果锁定可干预的驱动因子，按弹性系数推演目标指标的变动和区间。

build(dom, intent, analysis)  从数据和归因结果算出推演模型的参数（只算一次，存进会话快照）
simulate(model, values)       给定各杠杆取值，返回预测值、变动、区间、各杠杆贡献、敏感度曲线
推演口径：以现期（月）水平为基线，推演“下个月”在各杠杆调整后的指标；不改变的部分保持现期水平。
区间：参数不确定性（弹性系数、恢复比例 ±30%）+ 历史波动（日 GMV 或日毛利率的波动折算到月度），取 95% 区间。
"""
import math

import numpy as np

from .domains import shift_date


def _lever(key, name, lo, hi, step, value, unit, desc, cause=None):
    return dict(key=key, name=name, min=lo, max=hi, step=step, value=value, default=value, unit=unit, desc=desc, cause=cause)


# ============================================================ 电商：GMV = Σ 渠道 访客 × 转化率 × 客单价

def _build_ecommerce(dom, intent, analysis):
    p1 = intent["current"]
    ch = dom.q("""SELECT c.channel_id, c.channel_name, c.channel_type,
                  (SELECT SUM(uv) FROM fact_traffic_daily t WHERE t.channel_id = c.channel_id AND t.dt BETWEEN ? AND ?) AS uv,
                  (SELECT COUNT(*) FROM fact_orders o WHERE o.channel_id = c.channel_id AND o.dt BETWEEN ? AND ?) AS orders,
                  (SELECT SUM(pay_amount) FROM fact_orders o WHERE o.channel_id = c.channel_id AND o.dt BETWEEN ? AND ?) AS gmv
                  FROM dim_channel c""", (p1["start"], p1["end"]) * 3)
    days = (np.datetime64(p1["end"]) - np.datetime64(p1["start"])).astype(int) + 1
    base = float(ch.gmv.sum()) / days * 30
    paid = ch[ch.channel_type == "付费投放"]
    paid_gmv = float(paid.gmv.sum()) / days * 30
    levers, recov = [], []
    causes = analysis["conclusion"]["causes"]
    for c in causes:
        if c["type"] == "traffic_drop" and c["rank"] in ("主要", "次要"):
            cid = next((h.get("channel_id") for h in analysis["hypotheses"] if h["id"] == c["id"]), None)
            row = ch[ch.channel_id == cid].iloc[0]
            tau = c["start"]
            d = dom.q("SELECT dt, SUM(uv) uv FROM fact_traffic_daily WHERE channel_id = ? AND dt BETWEEN ? AND ? GROUP BY dt",
                      (cid, shift_date(tau, -14), p1["end"]))
            pre, post = d[d.dt < tau].uv.median(), d[d.dt >= tau].uv.mean()
            gain = (pre - post) * 30 * row.orders / row.uv * row.gmv / row.orders
            recov.append(dict(key=f"rec_{c['id']}", full=float(gain)))
            levers.append(_lever(f"rec_{c['id']}", f"{row.channel_name}访客恢复比例", 0, 100, 5, 0, "%",
                                 f"把限流后少掉的日均 {pre - post:,.0f} 访客恢复回来（按该渠道现期转化率和客单价）", c["id"]))
        elif c["type"] == "stockout" and c["rank"] in ("主要", "次要"):
            s, e = max(c["start"], p1["start"]), min(c["end"], p1["end"])
            n = (np.datetime64(e) - np.datetime64(s)).astype(int) + 1
            gain = abs(c["effect"]) / n * 30
            recov.append(dict(key=f"rec_{c['id']}", full=float(gain)))
            levers.append(_lever(f"rec_{c['id']}", "断货商品补货到位比例", 0, 100, 5, 0, "%",
                                 f"断货期间日均损失 {abs(c['effect']) / n / 1e4:,.1f} 万元，补货后按比例恢复", c["id"]))
    levers += [
        _lever("paid_budget", "付费投放预算调整", -30, 50, 5, 0, "%", "搜索广告 + 信息流广告预算；访客对预算的弹性 0.8（边际递减）"),
        _lever("cvr", "全站转化率提升", -10, 10, 1, 0, "%", "例如优化详情页、结算流程带来的转化率相对变化"),
        _lever("price", "商品价格调整", -10, 10, 1, 0, "%", "价格变动对订单量的影响按价格弹性计算"),
        _lever("elasticity", "价格弹性系数", -3, -0.5, 0.1, -1.5, "", "订单量变化% ÷ 价格变化%，默认 −1.5（电商大盘经验值）"),
    ]
    daily = dom.daily(p1["start"], p1["end"])
    sigma_month = float(daily.gmv.std() * math.sqrt(30))
    return dict(domain="ecommerce", metric="gmv", metric_name="GMV", unit="元", base=base, paid_gmv=paid_gmv, recov=recov,
                sigma=sigma_month, levers=levers,
                note=f"基线：现期（{p1['label']}）日均 GMV × 30 天 = {base / 1e4:,.1f} 万元")


def _sim_ecommerce(m, v, k_unc=1.0):
    base = m["base"]
    parts = {}
    for r in m["recov"]:
        parts[r["key"]] = r["full"] * v.get(r["key"], 0) / 100 * k_unc
    b = v.get("paid_budget", 0) / 100
    parts["paid_budget"] = m["paid_gmv"] * ((1 + b) ** (0.8 * k_unc) - 1) if b > -1 else -m["paid_gmv"]
    sub = base + sum(parts.values())
    cvr = v.get("cvr", 0) / 100
    parts["cvr"] = sub * cvr * k_unc
    sub2 = sub + parts["cvr"]
    p, eps = v.get("price", 0) / 100, v.get("elasticity", -1.5) * (2 - k_unc)
    parts["price"] = sub2 * ((1 + p) ** (1 + eps) - 1)
    return base, parts


# ============================================================ 快消：净利率 =（营收 − 成本 − 物流 − 佣金 − 营销）÷ 营收

def _build_fmcg(dom, intent, analysis):
    p1 = intent["current"]
    df = dom.by_channel(p1)
    ch = {k: dict(name=r.channel_name, rev=float(r.rev), gross=float(r.gross), disc=float(r.disc), cogs=float(r.cogs), logi=float(r.logi),
                  comm=float(r.comm), mkt=float(r.mkt)) for k, r in df.iterrows()}
    rev = sum(c["rev"] for c in ch.values())
    sugar = dom.q("""SELECT SUM(s.sales_qty * b.qty_per_unit * p.unit_price) AS v FROM fact_sales_daily s JOIN bom b ON b.sku_id = s.sku_id
                     JOIN fact_material_price_monthly p ON p.material_id = b.material_id AND p.month = substr(s.dt, 1, 7)
                     JOIN dim_material m ON m.material_id = b.material_id WHERE m.material_name = '白砂糖' AND s.dt BETWEEN ? AND ?""",
                  (p1["start"], p1["end"])).v[0] or 0
    levers = []
    causes = {c["type"]: c for c in analysis["conclusion"]["causes"] if c["rank"] in ("主要", "次要")}
    hyps = {h["id"]: h for h in analysis["hypotheses"]}
    gb = None
    if "channel_mix" in causes:
        gb = hyps[causes["channel_mix"]["id"]].get("channel_id")
    model = dict(domain="fmcg", metric="net_margin", metric_name="净利率", unit="pp", ch=ch, sugar=float(sugar), gb=gb, levers=levers)
    if gb:
        c = ch[gb]
        share = c["rev"] / rev
        levers.append(_lever("gb_share", f"{c['name']}营收占比", 0, 25, 0.5, round(share * 100, 1), "%",
                             f"现期 {share:.1%}；按该渠道现期利润结构等比例缩放", causes["channel_mix"]["id"]))
        d = c["disc"] / c["gross"]
        levers.append(_lever("gb_disc", f"{c['name']}供货折扣率", 15, 40, 1, round(d * 100), "%",
                             f"现期 {d:.0%}；降低折扣即提高该渠道实收（假设销量不变）", causes["channel_mix"]["id"]))
    if "discount" in causes:
        h = hyps[causes["discount"]["id"]]
        X = None
        for ev in (h.get("result") or {}).get("finding", "").split("多让利 ")[1:]:
            try:
                X = float(ev.split(" 万元")[0].replace(",", "")) * 1e4
            except ValueError:
                pass
        if X:
            model["promo_x"] = X
            levers.append(_lever("promo_keep", f"{h.get('product_line', '')}额外让利保留比例", 0, 100, 5, 100, "%",
                                 f"现期额外让利 {X / 1e4:,.1f} 万元；改为满赠/组合装后可收回一部分", causes["discount"]["id"]))
    if sugar:
        levers.append(_lever("sugar", "白砂糖采购价变化", -40, 20, 5, 0, "%", f"现期白砂糖成本 {sugar / 1e4:,.1f} 万元；锁价、替代供应商或配方减糖",
                             causes.get("material_price", {}).get("id")))
    comm_ch = None
    if "commission" in causes:
        comm_ch = hyps[causes["commission"]["id"]].get("channel_id")
        c = ch[comm_ch]
        r = c["comm"] / c["rev"]
        model["comm_ch"] = comm_ch
        levers.append(_lever("comm_rate", f"{c['name']}佣金率", 3, 12, 0.5, round(r * 100, 1), "%", f"现期 {r:.1%}；谈判阶梯返佣", causes["commission"]["id"]))
    mk = sum(c["mkt"] for c in ch.values()) / rev
    levers.append(_lever("mkt_rate", "营销费用率", 0, 10, 0.1, round(mk * 100, 1), "%", f"现期 {mk:.1%}"))
    daily = dom.daily(p1["start"], p1["end"])
    model["sigma"] = float(daily.gross_margin.std() / math.sqrt(len(daily)))
    model["note"] = f"基线：现期（{p1['label']}）分渠道营收与成本结构"
    model["base"] = _fmcg_margin(model, {l["key"]: l["default"] for l in levers})
    return model


def _fmcg_margin(m, v, k_unc=1.0):
    ch = {k: dict(c) for k, c in m["ch"].items()}
    gb = m.get("gb")
    if gb:
        c = ch[gb]
        d0 = c["disc"] / c["gross"]
        d1 = v.get("gb_disc", d0 * 100) / 100
        new_rev = c["gross"] * (1 - d1)
        delta = (new_rev - c["rev"]) * k_unc
        c["comm"] *= (c["rev"] + delta) / c["rev"]
        c["rev"] += delta
        others = sum(x["rev"] for k, x in ch.items() if k != gb)
        target = v.get("gb_share")
        if target is not None:
            t = target / 100
            want = t / (1 - t) * others if t < 1 else c["rev"]
            k = want / c["rev"]
            for f in ("rev", "gross", "disc", "cogs", "logi", "comm", "mkt"):
                c[f] *= k
    rev = sum(c["rev"] for c in ch.values())
    cogs = sum(c["cogs"] for c in ch.values())
    logi = sum(c["logi"] for c in ch.values())
    comm = sum(c["comm"] for c in ch.values())
    if m.get("comm_ch") and "comm_rate" in v:
        c = ch[m["comm_ch"]]
        comm += (v["comm_rate"] / 100 * c["rev"] - c["comm"]) * k_unc
    if "promo_keep" in v:
        back = m["promo_x"] * (1 - v["promo_keep"] / 100) * k_unc
        rev += back
    if "sugar" in v:
        cogs += m["sugar"] * v["sugar"] / 100 * k_unc
    mkt = v.get("mkt_rate", sum(c["mkt"] for c in ch.values()) / rev * 100) / 100 * rev
    return (rev - cogs - logi - comm - mkt) / rev


def _sim_fmcg(m, v, k_unc=1.0):
    base = m["base"]
    parts = {}
    cur = {l["key"]: l["default"] for l in m["levers"]}
    for l in m["levers"]:
        one = dict(cur)
        one[l["key"]] = v.get(l["key"], l["default"])
        parts[l["key"]] = _fmcg_margin(m, one, k_unc) - base
    total = _fmcg_margin(m, {**cur, **v}, k_unc) - base
    inter = total - sum(parts.values())
    if abs(inter) > 1e-9:
        parts["_interaction"] = inter
    return base, parts


# ============================================================ 通用

BUILD = {"ecommerce": _build_ecommerce, "fmcg": _build_fmcg}
SIM = {"ecommerce": _sim_ecommerce, "fmcg": _sim_fmcg}


def build(dom, intent, analysis):
    m = BUILD[dom.id](dom, intent, analysis)
    m["presets"] = presets(m)
    return m


def presets(m):
    rec = [l for l in m["levers"] if l.get("cause")]
    if m["domain"] == "ecommerce":
        return [
            dict(name="保守：主要原因恢复一半", values={l["key"]: 50 for l in rec if l["key"].startswith("rec_")}),
            dict(name="基准：主要原因恢复 80%", values={l["key"]: 80 for l in rec if l["key"].startswith("rec_")}),
            dict(name="积极：全部恢复 + 付费预算 +20%", values={**{l["key"]: 100 for l in rec if l["key"].startswith("rec_")}, "paid_budget": 20}),
        ]
    lv = {l["key"]: l for l in m["levers"]}
    base = {}
    if "gb_disc" in lv:
        base["gb_disc"] = lv["gb_disc"]["default"] - 5
    if "promo_keep" in lv:
        base["promo_keep"] = 50
    if "comm_rate" in lv:
        base["comm_rate"] = round(lv["comm_rate"]["default"] - 1.5, 1)
    agg = dict(base)
    if "gb_share" in lv:
        agg["gb_share"] = 10
    if "sugar" in lv:
        agg["sugar"] = -15
    return [dict(name="保守：只谈社区团购折扣", values={k: v for k, v in base.items() if k == "gb_disc"}),
            dict(name="基准：折扣 + 促销 + 佣金三项整改", values=base),
            dict(name="积极：再控社区团购占比到 10%、糖价锁价 −15%", values=agg)]


def simulate(m, values, curves=True):
    v = {l["key"]: values.get(l["key"], l["value"] if "value" in l else l["default"]) for l in m["levers"]}
    base, parts = SIM[m["domain"]](m, v)
    delta = sum(parts.values())
    _, lo_parts = SIM[m["domain"]](m, v, 0.7)
    _, hi_parts = SIM[m["domain"]](m, v, 1.3)
    d_lo, d_hi = sorted([sum(lo_parts.values()), sum(hi_parts.values())])
    z = 1.96 * m["sigma"]
    band = (base + d_lo - z, base + d_hi + z)
    names = {l["key"]: l["name"] for l in m["levers"]}
    names["_interaction"] = "交互项"
    contrib = [dict(key=k, name=names.get(k, k), delta=float(x)) for k, x in parts.items() if abs(x) > 1e-12 and k != "elasticity"]
    res = dict(base=base, predicted=base + delta, delta=delta, band=band, contrib=contrib, values=v)
    if curves:
        res["curves"] = {}
        for l in m["levers"]:
            if l["key"] == "elasticity":
                continue
            xs = list(np.linspace(l["min"], l["max"], 11))
            ys = []
            for x in xs:
                b2, p2 = SIM[m["domain"]](m, {**v, l["key"]: x})
                ys.append(float(b2 + sum(p2.values())))
            res["curves"][l["key"]] = dict(x=[float(x) for x in xs], y=ys, name=l["name"])
    return res


def compare(m, scenarios):
    out = []
    for s in scenarios:
        r = simulate(m, s.get("values", {}), curves=False)
        out.append(dict(name=s["name"], values=r["values"], predicted=r["predicted"], delta=r["delta"], band=r["band"]))
    return out
