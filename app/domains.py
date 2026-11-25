"""指标域：每个数仓一套指标定义、指标树、维度和下钻口径。

指标树和拆解方法以 meta_metric 中的口径为准，计算一律通过只读 SQL 完成。
- 电商（ecommerce）：GMV = 访客数 × 支付转化率 × 件单价 × 每单件数（乘法模型，LMDI）
- 快消（fmcg）：净利率 = 1 − 产品成本率 − 物流费用率 − 佣金率 − 营销费用率（加法模型，差额分析）
"""
import calendar
import re
from datetime import date, timedelta

import pandas as pd

from . import decompose as dc
from .warehouse import query


def month_period(ym):
    y, m = map(int, ym.split("-"))
    last = calendar.monthrange(y, m)[1]
    return dict(start=f"{y:04d}-{m:02d}-01", end=f"{y:04d}-{m:02d}-{last:02d}", label=f"{y} 年 {m} 月", ym=f"{y:04d}-{m:02d}")


def days_of(p):
    return (date.fromisoformat(p["end"]) - date.fromisoformat(p["start"])).days + 1


def shift_date(d, n):
    return (date.fromisoformat(d) + timedelta(days=n)).isoformat()


class Domain:
    id = ""
    title = ""
    db = ""
    data_start = data_end = ""
    main_metric = ""
    default_question = ""

    def q(self, sql, params=()):
        return query(self.db, sql, params)

    def events(self):
        return self.q("SELECT * FROM event_calendar ORDER BY start_date")

    def event_records(self, extra=()):
        """数仓事件 + 用户在事件日历页补录的事件，空值统一成 None。"""
        df = self.events()
        recs = [{k: (None if (v is None or (isinstance(v, float) and v != v)) else v) for k, v in r.items()} for r in df.to_dict("records")]
        for r in recs:
            r["source"] = "数仓"
        for e in extra:
            if e.get("domain", self.id) != self.id:
                continue
            r = {k: (e.get(k) or None) for k in ("event_id", "start_date", "end_date", "event_type", "title", "description", "channel_id",
                                                  "category_id" if self.id == "ecommerce" else "product_line")}
            r["description"] = r["description"] or ""
            r["source"] = "补录"
            recs.append(r)
        for r in recs:
            r.setdefault("category_id", None)
            r.setdefault("product_line", None)
            r.setdefault("channel_id", None)
        return sorted(recs, key=lambda r: r["start_date"])

    def metric_dict(self):
        return self.q("SELECT * FROM meta_metric")


# ============================================================ 电商

class Ecommerce(Domain):
    id = "ecommerce"
    title = "青禾优选电商"
    db = "ecommerce"
    data_start, data_end = "2026-07-01", "2026-09-30"
    main_metric = "gmv"
    default_question = "9 月 GMV 比 8 月下降了多少？为什么？"
    unit = "元"
    DIMS = {"channel": "渠道", "category": "类目", "region": "区域", "user_type": "新老客"}
    METRICS = {
        "gmv": "GMV", "uv": "访客数", "order_cnt": "订单数", "cvr": "支付转化率", "aov": "客单价",
        "asp": "件单价", "ipo": "每单件数", "net_gmv": "净GMV", "refund_rate": "退款率", "new_user_share": "新客占比",
    }
    RATIO = {"cvr", "refund_rate", "new_user_share"}

    def tree(self):
        return dict(code="gmv", name="GMV", method="乘法模型（LMDI）", formula="GMV = 访客数 × 支付转化率 × 客单价", children=[
            dict(code="uv", name="访客数", formula="SUM(fact_traffic_daily.uv)"),
            dict(code="cvr", name="支付转化率", formula="订单数 ÷ 访客数"),
            dict(code="aov", name="客单价", formula="客单价 = 件单价 × 每单件数", children=[
                dict(code="asp", name="件单价", formula="GMV ÷ 件数"),
                dict(code="ipo", name="每单件数", formula="件数 ÷ 订单数")])])

    def measures(self, p, where="", params=()):
        t = self.q(f"SELECT SUM(uv) uv FROM fact_traffic_daily WHERE dt BETWEEN ? AND ? {where}", (p["start"], p["end"], *params))
        o = self.q(f"""SELECT COUNT(*) orders, SUM(pay_amount) gmv, SUM(item_qty) items, SUM(is_new_user) new_orders,
                       SUM(is_refunded) refunds, SUM(CASE WHEN is_refunded = 0 THEN pay_amount ELSE 0 END) net_gmv
                       FROM fact_orders WHERE dt BETWEEN ? AND ? {where}""", (p["start"], p["end"], *params))
        r = dict(uv=float(t.uv[0] or 0), **{k: float(o[k][0] or 0) for k in o.columns})
        r["cvr"] = r["orders"] / r["uv"] if r["uv"] else 0
        r["aov"] = r["gmv"] / r["orders"] if r["orders"] else 0
        r["asp"] = r["gmv"] / r["items"] if r["items"] else 0
        r["ipo"] = r["items"] / r["orders"] if r["orders"] else 0
        r["order_cnt"] = r["orders"]
        r["refund_rate"] = r["refunds"] / r["orders"] if r["orders"] else 0
        r["new_user_share"] = r["new_orders"] / r["orders"] if r["orders"] else 0
        r["days"] = days_of(p)
        return r

    def value(self, metric, p):
        return self.measures(p)[metric]

    def decompose(self, metric, p0, p1):
        m0, m1 = self.measures(p0), self.measures(p1)
        total = m1[metric] - m0[metric]
        if metric in ("gmv",):
            keys = ["uv", "cvr", "asp", "ipo"]
        elif metric == "aov":
            keys = ["asp", "ipo"]
        elif metric == "order_cnt":
            keys = ["uv", "cvr"]
        else:
            keys = []
        factors = []
        if keys:
            c = dc.lmdi({k: m0[k] for k in keys}, {k: m1[k] for k in keys})
            names = dict(uv="访客数", cvr="支付转化率", asp="件单价", ipo="每单件数")
            for k in keys:
                factors.append(dict(key=k, name=names[k], v0=m0[k], v1=m1[k], change_rate=m1[k] / m0[k] - 1, contrib=c[k]))
            if metric == "gmv":  # 第二层：客单价 = 件单价 × 每单件数，合并成一项展示，子项保留
                aov = dict(key="aov", name="客单价", v0=m0["aov"], v1=m1["aov"], change_rate=m1["aov"] / m0["aov"] - 1,
                           contrib=c["asp"] + c["ipo"], children=[f for f in factors if f["key"] in ("asp", "ipo")])
                factors = [f for f in factors if f["key"] in ("uv", "cvr")] + [aov]
        chk = dc.check_sum([f["contrib"] for f in factors], total) if factors else None
        for f in factors:
            f["share"] = f["contrib"] / total if total else 0
            for ch in f.get("children", []):
                ch["share"] = ch["contrib"] / total if total else 0
        return dict(metric=metric, name=self.METRICS.get(metric, metric), method="LMDI 乘法分解" if factors else "无拆解",
                    v0=m0[metric], v1=m1[metric], change=total, change_rate=(m1[metric] / m0[metric] - 1) if m0[metric] else None,
                    daily_change_rate=(m1[metric] / m1["days"]) / (m0[metric] / m0["days"]) - 1 if metric in ("gmv", "uv", "order_cnt", "net_gmv") else None,
                    factors=factors, check=chk, m0=m0, m1=m1, ratio=metric in self.RATIO)

    # ---------------------------------------------------------------- 维度下钻（GMV 为可加指标，差额即贡献）
    def _dim_gmv(self, dim, p):
        if dim == "category":
            sql = """SELECT c.category_name AS k, SUM(i.pay_amount) AS gmv, COUNT(DISTINCT i.order_id) AS orders FROM fact_order_items i
                     JOIN fact_orders o ON o.order_id = i.order_id JOIN dim_product pr ON pr.product_id = i.product_id
                     JOIN dim_category c ON c.category_id = pr.category_id WHERE o.dt BETWEEN ? AND ? GROUP BY k"""
        elif dim == "channel":
            sql = """SELECT ch.channel_name AS k, SUM(o.pay_amount) AS gmv, COUNT(*) AS orders FROM fact_orders o
                     JOIN dim_channel ch ON ch.channel_id = o.channel_id WHERE o.dt BETWEEN ? AND ? GROUP BY k"""
        elif dim == "region":
            sql = """SELECT r.region_name AS k, SUM(o.pay_amount) AS gmv, COUNT(*) AS orders FROM fact_orders o
                     JOIN dim_region r ON r.region_id = o.region_id WHERE o.dt BETWEEN ? AND ? GROUP BY k"""
        elif dim == "user_type":
            sql = """SELECT CASE WHEN is_new_user = 1 THEN '新客' ELSE '老客' END AS k, SUM(pay_amount) AS gmv, COUNT(*) AS orders
                     FROM fact_orders WHERE dt BETWEEN ? AND ? GROUP BY k"""
        else:
            raise ValueError(f"不支持的维度：{dim}")
        return self.q(sql, (p["start"], p["end"])).set_index("k")

    def drilldown(self, metric, dim, p0, p1):
        a, b = self._dim_gmv(dim, p0), self._dim_gmv(dim, p1)
        total0, total1 = a.gmv.sum(), b.gmv.sum()
        change = total1 - total0
        rows = []
        uv = None
        if dim == "channel":
            t = self.q("""SELECT ch.channel_name AS k, SUM(CASE WHEN dt BETWEEN ? AND ? THEN uv END) uv0, SUM(CASE WHEN dt BETWEEN ? AND ? THEN uv END) uv1
                          FROM fact_traffic_daily f JOIN dim_channel ch ON ch.channel_id = f.channel_id GROUP BY k""",
                       (p0["start"], p0["end"], p1["start"], p1["end"])).set_index("k")
            uv = t
        for k in sorted(set(a.index) | set(b.index)):
            g0 = float(a.gmv.get(k, 0)); g1 = float(b.gmv.get(k, 0))
            o0 = float(a.orders.get(k, 0)); o1 = float(b.orders.get(k, 0))
            row = dict(key=k, v0=g0, v1=g1, change=g1 - g0, change_rate=(g1 / g0 - 1) if g0 else None,
                       contrib_share=(g1 - g0) / change if change else 0, pull_rate=(g1 - g0) / total0 if total0 else 0,
                       share0=g0 / total0, share1=g1 / total1, orders0=o0, orders1=o1,
                       aov0=g0 / o0 if o0 else None, aov1=g1 / o1 if o1 else None)
            if uv is not None and k in uv.index:
                row.update(uv0=float(uv.uv0[k]), uv1=float(uv.uv1[k]), cvr0=o0 / uv.uv0[k], cvr1=o1 / uv.uv1[k])
            rows.append(row)
        rows.sort(key=lambda r: r["change"])
        return dict(metric="gmv", dim=dim, dim_name=self.DIMS[dim], method="可加指标差额（各项之和等于总变动）",
                    total0=total0, total1=total1, change=change, rows=rows, check=dc.check_sum([r["change"] for r in rows], change))

    def cross_drill(self, p0, p1, dims=("channel", "region")):
        """两个维度交叉，找变动最集中的组合。"""
        col = dict(channel="ch.channel_name", region="r.region_name", user_type="CASE WHEN o.is_new_user = 1 THEN '新客' ELSE '老客' END")
        a, b = col[dims[0]], col[dims[1]]
        df = self.q(f"""SELECT {a} AS d1, {b} AS d2, SUM(CASE WHEN o.dt BETWEEN ? AND ? THEN o.pay_amount ELSE 0 END) AS v0,
                        SUM(CASE WHEN o.dt BETWEEN ? AND ? THEN o.pay_amount ELSE 0 END) AS v1
                        FROM fact_orders o JOIN dim_channel ch ON ch.channel_id = o.channel_id JOIN dim_region r ON r.region_id = o.region_id
                        WHERE o.dt BETWEEN ? AND ? GROUP BY d1, d2""",
                    (p0["start"], p0["end"], p1["start"], p1["end"], p0["start"], p1["end"]))
        df["change"] = df.v1 - df.v0
        total = df.change.sum()
        df["contrib_share"] = df.change / total
        df = df.sort_values("change")
        return dict(dims=[self.DIMS[d] for d in dims], total_change=float(total), rows=df.head(8).to_dict("records"))

    def daily(self, start, end, where="", params=()):
        t = self.q(f"SELECT dt, SUM(uv) uv FROM fact_traffic_daily WHERE dt BETWEEN ? AND ? {where} GROUP BY dt", (start, end, *params))
        o = self.q(f"""SELECT dt, COUNT(*) orders, SUM(pay_amount) gmv, SUM(item_qty) items, SUM(is_new_user) new_orders
                       FROM fact_orders WHERE dt BETWEEN ? AND ? {where} GROUP BY dt""", (start, end, *params))
        df = t.merge(o, on="dt", how="outer").fillna(0).sort_values("dt")
        df["cvr"] = df.orders / df.uv.where(df.uv > 0)
        df["aov"] = df.gmv / df.orders.where(df.orders > 0)
        return df

    def kpis(self):
        return [("gmv", "GMV", "元"), ("uv", "访客数", "人"), ("orders", "订单数", "单"), ("cvr", "支付转化率", "%"), ("aov", "客单价", "元")]

    def kpi_daily(self):
        return self.daily(self.data_start, self.data_end)

    def periods_available(self):
        return ["2026-07", "2026-08", "2026-09"]


# ============================================================ 快消

class Fmcg(Domain):
    id = "fmcg"
    title = "禾悦食品快消"
    db = "fmcg"
    data_start, data_end = "2026-04-01", "2026-09-30"
    main_metric = "net_margin"
    default_question = "9 月净利率比 6 月下降了多少个百分点？为什么？"
    unit = "百分点"
    DIMS = {"channel": "渠道", "product_line": "产品线"}
    METRICS = {"net_margin": "净利率", "gross_margin": "毛利率", "revenue": "营收", "net_profit": "净利润", "discount_rate": "折扣率",
               "cogs_rate": "产品成本率", "logistics_rate": "物流费用率", "commission_rate": "佣金率", "marketing_rate": "营销费用率"}
    RATIO = {"net_margin", "gross_margin", "discount_rate", "cogs_rate", "logistics_rate", "commission_rate", "marketing_rate"}

    def tree(self):
        return dict(code="net_margin", name="净利率", method="加法模型（差额分析）",
                    formula="净利率 = 1 − 产品成本率 − 物流费用率 − 佣金率 − 营销费用率", children=[
                        dict(code="cogs_rate", name="产品成本率", formula="产品成本 ÷ 营收", children=[
                            dict(code="mix", name="结构（SKU × 渠道组合）"), dict(code="price", name="价格（折扣）"),
                            dict(code="cost", name="单位成本（材料与加工）")]),
                        dict(code="logistics_rate", name="物流费用率", formula="物流费用 ÷ 营收"),
                        dict(code="commission_rate", name="佣金率", formula="渠道佣金 ÷ 营收"),
                        dict(code="marketing_rate", name="营销费用率", formula="营销费用 ÷ 营收")])

    def _months(self, p):
        return p["start"][:7], p["end"][:7]

    def by_channel(self, p):
        m0, m1 = self._months(p)
        s = self.q("""SELECT s.channel_id k, SUM(s.net_revenue) rev, SUM(s.gross_amount) gross, SUM(s.discount_amount) disc,
                      SUM(s.sales_qty * c.unit_cogs) cogs, SUM(s.sales_qty) qty FROM fact_sales_daily s
                      JOIN fact_sku_cost_monthly c ON c.sku_id = s.sku_id AND c.month = substr(s.dt, 1, 7)
                      WHERE s.dt BETWEEN ? AND ? GROUP BY k""", (p["start"], p["end"])).set_index("k")
        lg = self.q("SELECT channel_id k, SUM(logistics_cost) logi, SUM(shipped_qty) shipped FROM fact_logistics_daily WHERE dt BETWEEN ? AND ? GROUP BY k",
                    (p["start"], p["end"])).set_index("k")
        cm = self.q("SELECT channel_id k, SUM(commission_amount) comm FROM fact_commission_monthly WHERE month BETWEEN ? AND ? GROUP BY k", (m0, m1)).set_index("k")
        mk = self.q("SELECT channel_id k, SUM(amount) mkt FROM fact_marketing_monthly WHERE month BETWEEN ? AND ? GROUP BY k", (m0, m1)).set_index("k")
        names = self.q("SELECT channel_id k, channel_name FROM dim_channel").set_index("k")
        df = s.join(lg, how="outer").join(cm, how="outer").join(mk, how="outer").join(names, how="left").fillna(0)
        df["net"] = df.rev - df.cogs - df.logi - df.comm - df.mkt
        df["margin"] = df.net / df.rev
        return df

    def measures(self, p):
        df = self.by_channel(p)
        r = {k: float(df[k].sum()) for k in ["rev", "gross", "disc", "cogs", "qty", "logi", "comm", "mkt", "net", "shipped"]}
        rev = r["rev"]
        r.update(revenue=rev, net_profit=r["net"], net_margin=r["net"] / rev, gross_margin=(rev - r["cogs"]) / rev,
                 cogs_rate=r["cogs"] / rev, logistics_rate=r["logi"] / rev, commission_rate=r["comm"] / rev,
                 marketing_rate=r["mkt"] / rev, discount_rate=r["disc"] / r["gross"], days=days_of(p))
        return r

    def value(self, metric, p):
        return self.measures(p)[metric]

    def _cogs_split(self, p0, p1):
        """产品成本率变化 = 结构 + 价格 + 单位成本（按 SKU × 渠道组合依次替换，三项之和等于总变化）。"""
        def grid(p):
            return self.q("""SELECT s.sku_id || '|' || s.channel_id k, SUM(s.sales_qty) q, SUM(s.net_revenue) rev,
                             SUM(s.sales_qty * c.unit_cogs) cogs FROM fact_sales_daily s
                             JOIN fact_sku_cost_monthly c ON c.sku_id = s.sku_id AND c.month = substr(s.dt, 1, 7)
                             WHERE s.dt BETWEEN ? AND ? GROUP BY k""", (p["start"], p["end"])).set_index("k")
        g0, g1 = grid(p0), grid(p1)
        g = g1.join(g0, lsuffix="1", rsuffix="0", how="left")
        g["p0"] = (g.rev0 / g.q0).fillna(g.rev1 / g.q1)   # 新组合用当期价格和成本代替
        g["c0"] = (g.cogs0 / g.q0).fillna(g.cogs1 / g.q1)
        g["p1"] = g.rev1 / g.q1
        g["c1"] = g.cogs1 / g.q1
        r0 = g0.cogs.sum() / g0.rev.sum()
        r_a = (g.q1 * g.c0).sum() / (g.q1 * g.p0).sum()
        r_b = (g.q1 * g.c0).sum() / (g.q1 * g.p1).sum()
        r1 = g1.cogs.sum() / g1.rev.sum()
        return dict(mix=r_a - r0, price=r_b - r_a, cost=r1 - r_b, total=r1 - r0)

    def decompose(self, metric, p0, p1):
        m0, m1 = self.measures(p0), self.measures(p1)
        total = m1[metric] - m0[metric]
        factors = []
        names = dict(cogs_rate="产品成本率", logistics_rate="物流费用率", commission_rate="佣金率", marketing_rate="营销费用率")
        keys = {"net_margin": ["cogs_rate", "logistics_rate", "commission_rate", "marketing_rate"], "gross_margin": ["cogs_rate"]}.get(metric, [])
        for k in keys:
            factors.append(dict(key=k, name=names[k], v0=m0[k], v1=m1[k], change_rate=m1[k] - m0[k], contrib=-(m1[k] - m0[k])))
        if keys:
            sp = self._cogs_split(p0, p1)
            factors[0]["children"] = [dict(key="mix", name="结构（SKU × 渠道组合）", contrib=-sp["mix"]),
                                      dict(key="price", name="价格（折扣）", contrib=-sp["price"]),
                                      dict(key="cost", name="单位成本（材料与加工）", contrib=-sp["cost"])]
        chk = dc.check_sum([f["contrib"] for f in factors], total) if factors else None
        for f in factors:
            f["share"] = f["contrib"] / total if total else 0
            for ch in f.get("children", []):
                ch["share"] = ch["contrib"] / total if total else 0
        return dict(metric=metric, name=self.METRICS.get(metric, metric), method="加法模型差额分析" if factors else "无拆解",
                    v0=m0[metric], v1=m1[metric], change=total, change_rate=None, daily_change_rate=None,
                    factors=factors, check=chk, m0=m0, m1=m1, ratio=metric in self.RATIO)

    def drilldown(self, metric, dim, p0, p1):
        if dim == "channel":
            a, b = self.by_channel(p0), self.by_channel(p1)
            col = "margin" if metric == "net_margin" else None
            if metric == "gross_margin":
                a["gm"] = 1 - a.cogs / a.rev; b["gm"] = 1 - b.cogs / b.rev; col = "gm"
            res, (y0, y1) = dc.mix_rate({k: (a.rev[k], a[col][k]) for k in a.index}, {k: (b.rev[k], b[col][k]) for k in b.index})
            nm = {**a.channel_name.to_dict(), **b.channel_name.to_dict()}
        elif dim == "product_line":
            def pl(p):
                return self.q("""SELECT d.product_line k, SUM(s.net_revenue) rev, SUM(s.sales_qty * c.unit_cogs) cogs, SUM(s.discount_amount) disc,
                                 SUM(s.gross_amount) gross FROM fact_sales_daily s JOIN dim_sku d ON d.sku_id = s.sku_id
                                 JOIN fact_sku_cost_monthly c ON c.sku_id = s.sku_id AND c.month = substr(s.dt, 1, 7)
                                 WHERE s.dt BETWEEN ? AND ? GROUP BY k""", (p["start"], p["end"])).set_index("k")
            a, b = pl(p0), pl(p1)
            a["gm"] = 1 - a.cogs / a.rev; b["gm"] = 1 - b.cogs / b.rev
            res, (y0, y1) = dc.mix_rate({k: (a.rev[k], a.gm[k]) for k in a.index}, {k: (b.rev[k], b.gm[k]) for k in b.index})
            nm = {k: k for k in res}
            metric = "gross_margin"
        else:
            raise ValueError(f"不支持的维度：{dim}")
        rows = []
        for k, v in res.items():
            rows.append(dict(key=nm.get(k, k), code=k, share0=v["s0"], share1=v["s1"], v0=v["m0"], v1=v["m1"],
                             mix=v["mix"], rate=v["rate"], change=v["mix"] + v["rate"]))
        rows.sort(key=lambda r: r["change"])
        total = y1 - y0
        return dict(metric=metric, dim=dim, dim_name=self.DIMS[dim], method="结构 × 水平两因子 Shapley（结构效应 + 水平效应 = 总变动）",
                    total0=y0, total1=y1, change=total, rows=rows, check=dc.check_sum([r["change"] for r in rows], total))

    def monthly(self):
        out = []
        for ym in self.periods_available():
            m = self.measures(month_period(ym))
            out.append(dict(month=ym, **{k: m[k] for k in ["revenue", "net_margin", "gross_margin", "discount_rate", "cogs_rate", "logistics_rate", "commission_rate", "marketing_rate"]}))
        return pd.DataFrame(out)

    def daily(self, start, end, where="", params=()):
        df = self.q(f"""SELECT s.dt, SUM(s.net_revenue) revenue, SUM(s.gross_amount) gross, SUM(s.discount_amount) disc,
                        SUM(s.sales_qty * c.unit_cogs) cogs FROM fact_sales_daily s
                        JOIN fact_sku_cost_monthly c ON c.sku_id = s.sku_id AND c.month = substr(s.dt, 1, 7)
                        WHERE s.dt BETWEEN ? AND ? {where} GROUP BY s.dt ORDER BY s.dt""", (start, end, *params))
        df["gross_margin"] = 1 - df.cogs / df.revenue
        df["discount_rate"] = df.disc / df.gross
        return df

    def kpis(self):
        return [("revenue", "营收", "元"), ("gross_margin", "毛利率", "%"), ("discount_rate", "折扣率", "%")]

    def kpi_daily(self):
        return self.daily(self.data_start, self.data_end)

    def periods_available(self):
        return ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"]


DOMAINS = {"ecommerce": Ecommerce(), "fmcg": Fmcg()}


# ============================================================ 意图解析（规则兜底；在线时由大模型解析后再用这里校验）

METRIC_ALIASES = {
    "ecommerce": [("gmv", ["gmv", "成交额", "销售额", "交易额", "支付金额"]), ("net_gmv", ["净gmv", "扣退"]), ("aov", ["客单价", "aov"]),
                  ("cvr", ["转化率", "cvr"]), ("uv", ["访客", "uv", "流量"]), ("order_cnt", ["订单数", "单量"]),
                  ("refund_rate", ["退款率", "退货率"]), ("new_user_share", ["新客占比"])],
    "fmcg": [("net_margin", ["净利率", "利润率", "经营利润率"]), ("gross_margin", ["毛利率"]), ("revenue", ["营收", "收入"]),
             ("discount_rate", ["折扣率", "让利"])],
}


def guess_domain(text):
    t = text.lower()
    if any(k in t for k in ["净利率", "毛利率", "利润率", "快消", "禾悦", "白砂糖", "社区团购"]):
        return "fmcg"
    return "ecommerce"


def parse_intent(text, domain_id=None):
    d = DOMAINS[domain_id or guess_domain(text)]
    t = text.lower()
    metric = d.main_metric
    for code, words in METRIC_ALIASES[d.id]:
        if any(w in t for w in words):
            metric = code
            break
    months = [int(m) for m in re.findall(r"(\d{1,2})\s*月", text)]
    avail = d.periods_available()
    if len(months) >= 2:
        cur, base = months[0], months[1]
        if "比" in text and text.find(f"{months[0]}") > text.find(f"{months[1]}"):
            cur, base = months[1], months[0]
    elif len(months) == 1:
        cur, base = months[0], months[0] - 1
    else:
        cur, base = int(avail[-1][5:]), int(avail[-2][5:])
    p1, p0 = month_period(f"2026-{cur:02d}"), month_period(f"2026-{base:02d}")
    compare = "环比" if cur - base == 1 else "指定基期"
    return dict(domain=d.id, metric=metric, metric_name=d.METRICS.get(metric, metric), base=p0, current=p1, compare=compare)
