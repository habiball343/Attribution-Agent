"""切片下钻、透视表、桑基图。

drill_slice  追问时锁定一个切片（某渠道/类目/区域/新老客/产品线），给出切片自身的指标对比、切片内部再按其他维度拆开、
             日趋势，以及与之相关的原因和事件
pivot        两个维度的层级透视表（父行可折叠），每行带基期、现期、差额、贡献率
sankey       现期结构的桑基图数据（电商：渠道 → 类目 GMV；快消：渠道 → 营收去向）
"""
from .domains import shift_date

E_DIM = {
    "channel": ("ch.channel_name", "o.channel_id", "JOIN dim_channel ch ON ch.channel_id = o.channel_id"),
    "region": ("r.region_name", "o.region_id", "JOIN dim_region r ON r.region_id = o.region_id"),
    "user_type": ("CASE WHEN o.is_new_user = 1 THEN '新客' ELSE '老客' END", "o.is_new_user", ""),
    "category": ("c.category_name", "p.category_id", "JOIN dim_product p ON p.product_id = i.product_id JOIN dim_category c ON c.category_id = p.category_id"),
    "product": ("p.product_name", "p.product_id", "JOIN dim_product p ON p.product_id = i.product_id"),
}
F_DIM = {"channel": ("c.channel_name", "JOIN dim_channel c ON c.channel_id = s.channel_id"),
         "product_line": ("d.product_line", "JOIN dim_sku d ON d.sku_id = s.sku_id"),
         "sku": ("d.sku_name", "JOIN dim_sku d ON d.sku_id = s.sku_id")}


def resolve_slice(dom, text):
    """在问题里找切片取值（渠道名、类目名、区域名、新/老客、产品线），返回 [(dim, key, name)]。"""
    found = []
    if dom.id == "ecommerce":
        for dim, sql in (("channel", "SELECT channel_id, channel_name FROM dim_channel"), ("category", "SELECT category_id, category_name FROM dim_category"),
                         ("region", "SELECT region_id, region_name FROM dim_region")):
            for k, n in dom.q(sql).values:
                if n in text or (dim == "channel" and n.replace("广告", "") in text and len(n.replace("广告", "")) >= 2):
                    found.append((dim, k, n))
        if "新客" in text:
            found.append(("user_type", 1, "新客"))
        elif "老客" in text:
            found.append(("user_type", 0, "老客"))
    else:
        for k, n in dom.q("SELECT channel_id, channel_name FROM dim_channel").values:
            short = n.split("（")[0]
            if n in text or short in text:
                found.append(("channel", k, n))
        for (n,) in dom.q("SELECT DISTINCT product_line FROM dim_sku").values:
            if n in text:
                found.append(("product_line", n, n))
    return found


# ------------------------------------------------------------ 电商

def _e_where(dim, key):
    if dim == "category":
        return ("EXISTS (SELECT 1 FROM fact_order_items i JOIN dim_product p ON p.product_id = i.product_id WHERE i.order_id = o.order_id AND p.category_id = ?)", (key,))
    col = E_DIM[dim][1]
    return (f"{col} = ?", (key,))


def _e_breakdown(dom, p0, p1, dim, key, by):
    """切片内按 by 维度拆开（GMV 差额）。类目切片或按类目/商品拆时走明细表。"""
    if dim == "category" or by in ("category", "product"):
        frm = ("FROM fact_order_items i JOIN fact_orders o ON o.order_id = i.order_id JOIN dim_product p ON p.product_id = i.product_id "
               "JOIN dim_category c ON c.category_id = p.category_id")
        if by in ("channel", "region"):
            frm += " " + E_DIM[by][2]
        amt = "i.pay_amount"
    else:
        frm = "FROM fact_orders o " + E_DIM[by][2]
        amt = "o.pay_amount"
    where = "p.category_id = ?" if dim == "category" else f"{E_DIM[dim][1]} = ?"
    sql = f"""SELECT {E_DIM[by][0]} AS k, SUM(CASE WHEN o.dt BETWEEN ? AND ? THEN {amt} ELSE 0 END) AS v0,
              SUM(CASE WHEN o.dt BETWEEN ? AND ? THEN {amt} ELSE 0 END) AS v1 {frm} WHERE {where} AND o.dt BETWEEN ? AND ? GROUP BY k"""
    df = dom.q(sql, (p0["start"], p0["end"], p1["start"], p1["end"], key, p0["start"], p1["end"]))
    df["change"] = df.v1 - df.v0
    tot = df.change.sum()
    df["contrib_share"] = df.change / tot if tot else 0
    df["change_rate"] = df.v1 / df.v0.where(df.v0 != 0) - 1
    df = df.sort_values("change")
    if by == "product":
        df = df.head(10)
    return dict(by=by, by_name=dom.DIMS.get(by, "商品"), rows=df.fillna(0).to_dict("records"), total=float(tot))


def _e_slice(dom, p0, p1, dim, key, name):
    where, params = _e_where(dim, key)
    if dim == "category":
        def meas(p):
            r = dom.q("""SELECT SUM(i.pay_amount) gmv, COUNT(DISTINCT o.order_id) orders, SUM(i.quantity) items FROM fact_order_items i
                         JOIN fact_orders o ON o.order_id = i.order_id JOIN dim_product p ON p.product_id = i.product_id
                         WHERE p.category_id = ? AND o.dt BETWEEN ? AND ?""", (key, p["start"], p["end"])).iloc[0]
            g, n, it = float(r.gmv or 0), float(r.orders or 0), float(r["items"] or 0)
            return dict(gmv=g, orders=n, aov=g / n if n else 0, asp=g / it if it else 0)
        m0, m1 = meas(p0), meas(p1)
        subs = ["channel", "product"]
        oos = dom.q("""SELECT v.dt, SUM(v.is_out_of_stock) oos FROM fact_inventory_daily v JOIN dim_product p ON p.product_id = v.product_id
                       WHERE p.category_id = ? AND v.dt BETWEEN ? AND ? GROUP BY v.dt""", (key, p0["start"], p1["end"]))
    else:
        if dim == "channel":
            m0 = dom.measures(p0, "AND channel_id = ?", (key,))
            m1 = dom.measures(p1, "AND channel_id = ?", (key,))
        else:
            col = "region_id" if dim == "region" else "is_new_user"

            def meas(p):
                r = dom.q(f"SELECT SUM(pay_amount) gmv, COUNT(*) orders, SUM(item_qty) items FROM fact_orders WHERE dt BETWEEN ? AND ? AND {col} = ?",
                          (p["start"], p["end"], key)).iloc[0]
                g, n, it = float(r.gmv or 0), float(r.orders or 0), float(r["items"] or 0)
                return dict(gmv=g, orders=n, aov=g / n if n else 0, asp=g / it if it else 0)
            m0, m1 = meas(p0), meas(p1)
        subs = {"channel": ["region", "user_type", "category"], "region": ["channel", "category"], "user_type": ["channel", "region"]}[dim]
        oos = None
    keys = [k for k in ("gmv", "uv", "orders", "cvr", "aov", "asp") if k in m0]
    names = dict(gmv="GMV", uv="访客数", orders="订单数", cvr="支付转化率", aov="客单价", asp="件单价")
    metrics = [dict(key=k, name=names[k], v0=m0[k], v1=m1[k], change_rate=(m1[k] / m0[k] - 1) if m0[k] else None) for k in keys]
    breakdowns = [_e_breakdown(dom, p0, p1, dim, key, b) for b in subs]
    if dim == "category":
        d = dom.q("""SELECT o.dt, SUM(i.pay_amount) gmv FROM fact_order_items i JOIN fact_orders o ON o.order_id = i.order_id
                     JOIN dim_product p ON p.product_id = i.product_id WHERE p.category_id = ? AND o.dt BETWEEN ? AND ? GROUP BY o.dt ORDER BY o.dt""",
                  (key, p0["start"], p1["end"]))
        series = [dict(name=f"{name} 日 GMV", x=list(d.dt), y=list(d.gmv))]
        if oos is not None and oos.oos.sum() > 0:
            series.append(dict(name="断货商品数", x=list(oos.dt), y=[float(x) for x in oos.oos], axis=1))
    else:
        if dim == "channel":
            d = dom.daily(p0["start"], p1["end"], "AND channel_id = ?", (key,))
        else:
            d = dom.q(f"SELECT dt, SUM(pay_amount) gmv FROM fact_orders WHERE dt BETWEEN ? AND ? AND {col} = ? GROUP BY dt ORDER BY dt", (p0["start"], p1["end"], key))
        series = [dict(name=f"{name} 日 GMV", x=list(d.dt), y=[float(x) for x in d.gmv])]
        if dim == "channel":
            series.append(dict(name=f"{name} 日访客数", x=list(d.dt), y=[float(x) for x in d.uv], axis=1))
    return metrics, breakdowns, series


# ------------------------------------------------------------ 快消

def _f_cost(dom, p, where, params):
    a = dom.q(f"""SELECT SUM(s.net_revenue) rev, SUM(s.gross_amount) gross, SUM(s.discount_amount) disc, SUM(s.sales_qty * c.unit_cogs) cogs
                  FROM fact_sales_daily s JOIN fact_sku_cost_monthly c ON c.sku_id = s.sku_id AND c.month = substr(s.dt, 1, 7)
                  JOIN dim_sku d ON d.sku_id = s.sku_id WHERE s.dt BETWEEN ? AND ? {where}""", (p["start"], p["end"], *params)).iloc[0]
    return {k: float(a[k] or 0) for k in ("rev", "gross", "disc", "cogs")}


def _f_slice(dom, p0, p1, dim, key, name):
    metrics = []
    if dim == "channel":
        a, b = dom.by_channel(p0), dom.by_channel(p1)
        r0, r1 = a.loc[key], b.loc[key]
        rows = [("rev", "营收", r0.rev, r1.rev, False), ("share", "营收占比", r0.rev / a.rev.sum(), r1.rev / b.rev.sum(), True),
                ("disc_rate", "折扣率", r0.disc / r0.gross, r1.disc / r1.gross, True), ("cogs_rate", "产品成本率", r0.cogs / r0.rev, r1.cogs / r1.rev, True),
                ("logi_rate", "物流费用率", r0.logi / r0.rev, r1.logi / r1.rev, True), ("comm_rate", "佣金率", r0.comm / r0.rev, r1.comm / r1.rev, True),
                ("mkt_rate", "营销费用率", r0.mkt / r0.rev, r1.mkt / r1.rev, True), ("margin", "净利率", r0.margin, r1.margin, True)]
        subs = ["product_line"]
        where, params = "AND s.channel_id = ?", (key,)
    else:
        c0, c1 = _f_cost(dom, p0, "AND d.product_line = ?", (key,)), _f_cost(dom, p1, "AND d.product_line = ?", (key,))
        t0, t1 = _f_cost(dom, p0, "", ()), _f_cost(dom, p1, "", ())
        rows = [("rev", "营收", c0["rev"], c1["rev"], False), ("share", "营收占比", c0["rev"] / t0["rev"], c1["rev"] / t1["rev"], True),
                ("disc_rate", "折扣率", c0["disc"] / c0["gross"], c1["disc"] / c1["gross"], True),
                ("gross_margin", "毛利率", 1 - c0["cogs"] / c0["rev"], 1 - c1["cogs"] / c1["rev"], True)]
        subs = ["channel", "sku"]
        where, params = "AND d.product_line = ?", (key,)
    for k, n, v0, v1, ratio in rows:
        metrics.append(dict(key=k, name=n, v0=float(v0), v1=float(v1), ratio=ratio, change_pp=float(v1 - v0) if ratio else None,
                            change_rate=None if ratio else float(v1 / v0 - 1)))
    breakdowns = []
    for by in subs:
        label, join = F_DIM[by]
        joins = join if by != "sku" or dim != "product_line" else ""
        if dim == "product_line" and "dim_sku" not in joins:
            joins += " JOIN dim_sku d ON d.sku_id = s.sku_id"
        df = dom.q(f"""SELECT {label} AS k,
                       SUM(CASE WHEN s.dt BETWEEN ? AND ? THEN s.net_revenue ELSE 0 END) AS rev0,
                       SUM(CASE WHEN s.dt BETWEEN ? AND ? THEN s.net_revenue ELSE 0 END) AS rev1,
                       SUM(CASE WHEN s.dt BETWEEN ? AND ? THEN s.discount_amount ELSE 0 END) / SUM(CASE WHEN s.dt BETWEEN ? AND ? THEN s.gross_amount ELSE 0 END) AS disc0,
                       SUM(CASE WHEN s.dt BETWEEN ? AND ? THEN s.discount_amount ELSE 0 END) / SUM(CASE WHEN s.dt BETWEEN ? AND ? THEN s.gross_amount ELSE 0 END) AS disc1,
                       1 - SUM(CASE WHEN s.dt BETWEEN ? AND ? THEN s.sales_qty * c.unit_cogs ELSE 0 END) / SUM(CASE WHEN s.dt BETWEEN ? AND ? THEN s.net_revenue ELSE 0 END) AS gm0,
                       1 - SUM(CASE WHEN s.dt BETWEEN ? AND ? THEN s.sales_qty * c.unit_cogs ELSE 0 END) / SUM(CASE WHEN s.dt BETWEEN ? AND ? THEN s.net_revenue ELSE 0 END) AS gm1
                       FROM fact_sales_daily s JOIN fact_sku_cost_monthly c ON c.sku_id = s.sku_id AND c.month = substr(s.dt, 1, 7) {joins}
                       {'' if 'dim_sku d' in joins else 'JOIN dim_sku d ON d.sku_id = s.sku_id'}
                       WHERE 1 = 1 {where} GROUP BY k""",
                    tuple(x for pp in (p0, p1, p0, p0, p1, p1, p0, p0, p1, p1) for x in (pp["start"], pp["end"])) + params)
        df["change"] = df.rev1 - df.rev0
        df["gm_change_pp"] = df.gm1 - df.gm0
        df["disc_change_pp"] = df.disc1 - df.disc0
        df = df.sort_values("gm_change_pp").fillna(0)
        if by == "sku":
            df = df.head(10)
        breakdowns.append(dict(by=by, by_name={"channel": "渠道", "product_line": "产品线", "sku": "SKU"}[by], rows=df.to_dict("records")))
    d = dom.q(f"""SELECT s.dt, SUM(s.net_revenue) rev, SUM(s.discount_amount) / SUM(s.gross_amount) disc,
                  1 - SUM(s.sales_qty * c.unit_cogs) / SUM(s.net_revenue) gm FROM fact_sales_daily s
                  JOIN fact_sku_cost_monthly c ON c.sku_id = s.sku_id AND c.month = substr(s.dt, 1, 7) JOIN dim_sku d ON d.sku_id = s.sku_id
                  WHERE s.dt BETWEEN ? AND ? {where} GROUP BY s.dt ORDER BY s.dt""", (dom.data_start, dom.data_end, *params))
    series = [dict(name=f"{name} 日毛利率", x=list(d.dt), y=[float(x) for x in d.gm], pct=True),
              dict(name=f"{name} 日折扣率", x=list(d.dt), y=[float(x) for x in d.disc], pct=True)]
    return metrics, breakdowns, series


def drill_slice(dom, intent, analysis, dim, key, name):
    p0, p1 = intent["base"], intent["current"]
    if dom.id == "ecommerce":
        metrics, breakdowns, series = _e_slice(dom, p0, p1, dim, key, name)
    else:
        metrics, breakdowns, series = _f_slice(dom, p0, p1, dim, key, name)
    idk = {"channel": "channel_id", "category": "category_id", "product_line": "product_line"}.get(dim)
    related = []
    for h in analysis.get("hypotheses", []):
        hit = (idk and h.get(idk) == key) or (name in (h.get("slice") or "")) or (name in h["title"])
        if hit and h.get("result"):
            related.append(dict(id=h["id"], title=h["title"], verdict=h["result"]["verdict"], effect=h["result"]["effect"], finding=h["result"]["finding"]))
    evs = [e for e in dom.event_records(analysis.get("custom_events", [])) if (idk and e.get(idk) == key)
           and e["start_date"] <= p1["end"] and e["end_date"] >= shift_date(p0["start"], -3)]
    # 文字要点：切片内变动最集中的子维度
    points = []
    for b in breakdowns:
        rows = b["rows"]
        if not rows:
            continue
        if dom.id == "ecommerce":
            worst = rows[0]
            if b["total"]:
                points.append(f"按{b['by_name']}拆：{worst['k']}变动 {worst['change'] / 1e4:+,.1f} 万元，占该切片变动的 {worst['contrib_share']:.0%}")
        else:
            worst = rows[0]
            points.append(f"按{b['by_name']}拆：{worst['k']}毛利率变动 {worst['gm_change_pp'] * 100:+.2f} 个百分点、折扣率 {worst['disc_change_pp'] * 100:+.2f} 个百分点")
    return dict(dim=dim, dim_name=dom.DIMS.get(dim, dim), key=key, name=name, metrics=metrics, breakdowns=breakdowns, series=series,
                related=related, events=[dict(event_id=e["event_id"], title=e["title"], start=e["start_date"], end=e["end_date"], event_type=e["event_type"]) for e in evs],
                points=points)


# ------------------------------------------------------------ 透视表

def pivot(dom, intent, d1, d2=None):
    p0, p1 = intent["base"], intent["current"]
    if dom.id == "ecommerce":
        item = "category" in (d1, d2)
        cols = [E_DIM[d][0] for d in (d1, d2) if d]
        joins = " ".join(dict.fromkeys(E_DIM[d][2] for d in (d1, d2) if d and E_DIM[d][2]))
        if item:
            base = f"FROM fact_order_items i JOIN fact_orders o ON o.order_id = i.order_id {joins}"
            amt = "i.pay_amount"
        else:
            base = f"FROM fact_orders o {joins}"
            amt = "o.pay_amount"
        sel = ", ".join(f"{c} AS d{i}" for i, c in enumerate(cols))
        grp = ", ".join(f"d{i}" for i in range(len(cols)))
        df = dom.q(f"""SELECT {sel}, SUM(CASE WHEN o.dt BETWEEN ? AND ? THEN {amt} ELSE 0 END) v0, SUM(CASE WHEN o.dt BETWEEN ? AND ? THEN {amt} ELSE 0 END) v1
                       {base} WHERE o.dt BETWEEN ? AND ? GROUP BY {grp}""", (p0["start"], p0["end"], p1["start"], p1["end"], p0["start"], p1["end"]))
        measure = "GMV"
    else:
        cols = [F_DIM[d][0] for d in (d1, d2) if d]
        joins = " ".join(dict.fromkeys(F_DIM[d][1] for d in (d1, d2) if d))
        sel = ", ".join(f"{c} AS d{i}" for i, c in enumerate(cols))
        grp = ", ".join(f"d{i}" for i in range(len(cols)))
        df = dom.q(f"""SELECT {sel}, SUM(CASE WHEN s.dt BETWEEN ? AND ? THEN s.net_revenue ELSE 0 END) v0, SUM(CASE WHEN s.dt BETWEEN ? AND ? THEN s.net_revenue ELSE 0 END) v1,
                       SUM(CASE WHEN s.dt BETWEEN ? AND ? THEN s.sales_qty * c.unit_cogs ELSE 0 END) c0, SUM(CASE WHEN s.dt BETWEEN ? AND ? THEN s.sales_qty * c.unit_cogs ELSE 0 END) c1
                       FROM fact_sales_daily s JOIN fact_sku_cost_monthly c ON c.sku_id = s.sku_id AND c.month = substr(s.dt, 1, 7) {joins}
                       WHERE s.dt BETWEEN ? AND ? GROUP BY {grp}""", (p0["start"], p0["end"], p1["start"], p1["end"]) * 2 + (p0["start"], p1["end"]))
        measure = "营收"
    total = float(df.v1.sum() - df.v0.sum())

    def row(key, g, rid):
        v0, v1 = float(g.v0.sum()), float(g.v1.sum())
        r = dict(id=rid, key=key, v0=v0, v1=v1, change=v1 - v0, change_rate=(v1 / v0 - 1) if v0 else None, contrib_share=(v1 - v0) / total if total else 0)
        if "c0" in g:
            c0, c1 = float(g.c0.sum()), float(g.c1.sum())
            r.update(gm0=1 - c0 / v0 if v0 else None, gm1=1 - c1 / v1 if v1 else None)
            r["gm_change_pp"] = (r["gm1"] - r["gm0"]) if r["gm0"] is not None and r["gm1"] is not None else None
        return r
    rows = []
    for i, (k, g) in enumerate(df.groupby("d0")):
        r = row(k, g, f"r{i}")
        if d2:
            r["children"] = sorted([row(k2, g2, f"r{i}_{j}") for j, (k2, g2) in enumerate(g.groupby("d1"))], key=lambda x: x["change"])
        rows.append(r)
    rows.sort(key=lambda x: x["change"])
    return dict(d1=d1, d2=d2, d1_name=dom.DIMS.get(d1, d1), d2_name=dom.DIMS.get(d2, d2) if d2 else None, measure=measure, total=total, rows=rows,
                has_margin=dom.id == "fmcg")


# ------------------------------------------------------------ 桑基图

def sankey(dom, p):
    if dom.id == "ecommerce":
        df = dom.q("""SELECT ch.channel_name a, c.category_name b, SUM(i.pay_amount) v FROM fact_order_items i JOIN fact_orders o ON o.order_id = i.order_id
                      JOIN dim_channel ch ON ch.channel_id = o.channel_id JOIN dim_product p ON p.product_id = i.product_id
                      JOIN dim_category c ON c.category_id = p.category_id WHERE o.dt BETWEEN ? AND ? GROUP BY a, b""", (p["start"], p["end"]))
        links = [dict(source=r.a, target=r.b, value=round(float(r.v) / 1e4, 1)) for r in df.itertuples() if r.v > 0]
        nodes = sorted({l["source"] for l in links}) + sorted({l["target"] for l in links})
        return dict(title=f"{p['label']} GMV 流向：渠道 → 类目（万元）", nodes=[dict(name=n) for n in nodes], links=links)
    b = dom.by_channel(p)
    links = []
    for k, r in b.iterrows():
        nm = r.channel_name
        for col, name in (("cogs", "产品成本"), ("logi", "物流费用"), ("comm", "渠道佣金"), ("mkt", "营销费用")):
            links.append(dict(source=nm, target=name, value=round(float(r[col]) / 1e4, 1)))
        if r.net > 0:
            links.append(dict(source=nm, target="贡献利润", value=round(float(r.net) / 1e4, 1)))
    nodes = list(dict.fromkeys([l["source"] for l in links] + [l["target"] for l in links]))
    return dict(title=f"{p['label']} 营收去向：渠道 → 成本与利润（万元）", nodes=[dict(name=n) for n in nodes], links=[l for l in links if l["value"] > 0])
