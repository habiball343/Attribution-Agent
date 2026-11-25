"""场景二：快消品利润率异动归因的模拟数仓（禾悦食品，2026-04-01 至 2026-09-30）。

净利率 =（营收 − 产品成本 − 物流费用 − 渠道佣金 − 营销费用）÷ 营收
毛利率 =（营收 − 产品成本）÷ 营收
埋入的原因可逐个关闭，用反事实计算每个原因对“9 月相对 6 月”利润率变化的真实影响。

用法：python gen_fmcg.py 输出目录
"""
import datetime as dt
import json
import random
import sqlite3
import sys
import zlib
from pathlib import Path

SEED = "heyue-2026"
START, END = dt.date(2026, 4, 1), dt.date(2026, 9, 30)
DAYS = [START + dt.timedelta(d) for d in range((END - START).days + 1)]
MONTHS = ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"]

MATERIALS = [  # 编码, 名称, 单位, 4-6 月单价
    ("M01", "白砂糖", "kg", 6.0), ("M02", "浓缩果汁", "kg", 18.0), ("M03", "茶叶提取物", "kg", 120.0),
    ("M04", "PET瓶", "个", 0.35), ("M05", "铝罐", "个", 0.55), ("M06", "坚果原料", "kg", 48.0),
    ("M07", "复合包装袋", "个", 0.25), ("M08", "纸箱", "个", 2.4), ("M09", "奶粉", "kg", 32.0),
    ("M10", "玉米粉", "kg", 4.0), ("M11", "食用油", "kg", 11.0), ("M12", "礼盒包装", "个", 6.0),
]
SUGAR_UP = 1.50  # 原因 C1：7 月起白砂糖采购价上涨 50%
PROCESS_MULT = 3.3  # 加工及制造费用系数

SKUS = [  # 编码, 名称, 产品线, 规格, 建议零售价, 加工费, 日均基础销量, BOM {材料: 用量}, 上市日期
    ("S01", "鲜榨感橙汁", "果汁饮料", "500ml", 5.5, 0.45, 900, {"M01": 0.045, "M02": 0.06, "M04": 1, "M08": 1 / 24}, "2024-03-01"),
    ("S02", "苹果汁", "果汁饮料", "500ml", 5.0, 0.45, 700, {"M01": 0.045, "M02": 0.055, "M04": 1, "M08": 1 / 24}, "2024-03-01"),
    ("S03", "混合果蔬汁", "果汁饮料", "1L", 9.9, 0.70, 400, {"M01": 0.08, "M02": 0.11, "M04": 1, "M08": 1 / 12}, "2024-06-01"),
    ("S04", "葡萄汁", "果汁饮料", "300ml", 4.0, 0.35, 500, {"M01": 0.03, "M02": 0.04, "M04": 1, "M08": 1 / 24}, "2025-04-01"),
    ("S05", "冰红茶", "茶饮料", "500ml", 3.5, 0.30, 1300, {"M01": 0.05, "M03": 0.002, "M04": 1, "M08": 1 / 24}, "2024-03-01"),
    ("S06", "茉莉绿茶", "茶饮料", "500ml", 3.5, 0.30, 1000, {"M01": 0.035, "M03": 0.002, "M04": 1, "M08": 1 / 24}, "2024-03-01"),
    ("S07", "无糖乌龙茶", "茶饮料", "500ml", 4.5, 0.30, 800, {"M03": 0.003, "M04": 1, "M08": 1 / 24}, "2025-03-01"),
    ("S08", "柠檬茶", "茶饮料", "250ml", 3.0, 0.25, 700, {"M01": 0.03, "M03": 0.001, "M02": 0.01, "M05": 1, "M08": 1 / 24}, "2024-09-01"),
    ("S09", "原味气泡水", "气泡水", "330ml", 4.0, 0.25, 900, {"M05": 1, "M08": 1 / 24}, "2024-05-01"),
    ("S10", "白桃气泡水", "气泡水", "330ml", 4.5, 0.28, 800, {"M01": 0.02, "M02": 0.005, "M05": 1, "M08": 1 / 24}, "2024-05-01"),
    ("S11", "青柠气泡水", "气泡水", "330ml", 4.5, 0.28, 600, {"M01": 0.02, "M02": 0.005, "M05": 1, "M08": 1 / 24}, "2025-05-01"),
    ("S12", "轻甜气泡水", "气泡水", "480ml", 5.0, 0.30, 500, {"M01": 0.015, "M02": 0.005, "M04": 1, "M08": 1 / 24}, "2026-09-05"),
    ("S13", "每日坚果", "坚果零食", "25g×7袋", 29.9, 1.20, 260, {"M06": 0.16, "M07": 8, "M08": 1 / 12}, "2024-03-01"),
    ("S14", "盐焗腰果", "坚果零食", "200g", 25.9, 0.90, 180, {"M06": 0.21, "M11": 0.01, "M07": 1, "M08": 1 / 20}, "2024-03-01"),
    ("S15", "蜂蜜扁桃仁", "坚果零食", "180g", 22.9, 0.90, 160, {"M06": 0.18, "M01": 0.02, "M07": 1, "M08": 1 / 20}, "2024-08-01"),
    ("S16", "中秋坚果礼盒", "坚果零食", "1.2kg", 128.0, 6.00, 15, {"M06": 1.15, "M07": 12, "M12": 1}, "2025-08-15"),
    ("S17", "玉米片", "膨化零食", "100g", 7.9, 0.40, 600, {"M10": 0.07, "M11": 0.02, "M01": 0.005, "M07": 1, "M08": 1 / 30}, "2024-03-01"),
    ("S18", "薯条脆", "膨化零食", "80g", 6.9, 0.40, 550, {"M10": 0.05, "M11": 0.02, "M07": 1, "M08": 1 / 30}, "2024-03-01"),
    ("S19", "海苔卷", "膨化零食", "60g", 9.9, 0.50, 350, {"M10": 0.03, "M01": 0.01, "M11": 0.01, "M07": 1, "M08": 1 / 30}, "2025-02-01"),
    ("S20", "焦糖爆米花", "膨化零食", "120g", 8.9, 0.40, 400, {"M10": 0.08, "M01": 0.03, "M11": 0.015, "M07": 1, "M08": 1 / 30}, "2024-10-01"),
    ("S21", "原味酸奶饮", "乳饮", "250ml", 4.5, 0.40, 800, {"M01": 0.025, "M09": 0.02, "M04": 1, "M08": 1 / 24}, "2024-03-01"),
    ("S22", "草莓乳饮", "乳饮", "250ml", 4.5, 0.40, 600, {"M01": 0.03, "M09": 0.018, "M02": 0.005, "M04": 1, "M08": 1 / 24}, "2024-03-01"),
    ("S23", "高钙奶饮", "乳饮", "1L", 12.9, 0.80, 250, {"M01": 0.06, "M09": 0.08, "M04": 1, "M08": 1 / 12}, "2024-07-01"),
    ("S24", "乳酸菌饮品", "乳饮", "100ml×5", 9.9, 0.60, 450, {"M01": 0.04, "M09": 0.01, "M04": 5, "M07": 1, "M08": 1 / 10}, "2024-03-01"),
]
DRINK_LINES = {"果汁饮料", "茶饮料", "气泡水", "乳饮"}

CHANNELS = [  # 编码, 名称, 类型, 销量占比, 渠道折扣, 每件物流成本, 佣金率(8月前)
    ("F01", "综合电商（青禾优选）", "电商平台", 0.20, 0.10, 0.45, 0.04),
    ("F02", "内容电商平台", "电商平台", 0.18, 0.12, 0.50, 0.05),
    ("F03", "大型商超", "线下零售", 0.22, 0.08, 0.20, 0.0),
    ("F04", "便利店", "线下零售", 0.12, 0.02, 0.30, 0.0),
    ("F05", "社区团购", "社区团购", 0.08, 0.35, 0.60, 0.12),
    ("F06", "经销商批发", "批发", 0.20, 0.30, 0.08, 0.0),
]
F02_NEW_RATE, F02_RATE_FROM = 0.10, dt.date(2026, 8, 1)  # 原因 C2
GROUP_BUY_FROM = dt.date(2026, 7, 15)  # 原因 C3：社区团购扩张

# 营销费用：按渠道营收的固定比例投入
MARKETING_RATE = {"F01": 0.06, "F02": 0.09, "F03": 0.04, "F04": 0.02, "F05": 0.03, "F06": 0.01}

SEASON = {"果汁饮料": [0.85, 0.95, 1.10, 1.22, 1.18, 1.00], "茶饮料": [0.85, 0.95, 1.12, 1.25, 1.20, 1.00],
          "气泡水": [0.80, 0.95, 1.15, 1.28, 1.22, 0.98], "乳饮": [1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
          "坚果零食": [1.0, 0.98, 0.96, 0.95, 0.98, 1.05], "膨化零食": [1.0, 1.0, 1.0, 1.02, 1.03, 1.0]}

CAUSES_ALL = {
    "C1_白砂糖涨价": True,
    "C2_内容电商佣金上调": True,
    "C3_社区团购扩张": True,
    "C4_中秋促销让利": True,
    "C5_618大促基数": True,
}


def rand(*keys):
    return random.Random(zlib.crc32(("|".join(map(str, (SEED,) + keys))).encode()))


def material_price(mid, month, causes):
    base = dict((m[0], m[3]) for m in MATERIALS)[mid]
    r = rand("mat", mid, month)
    p = base * (1 + r.uniform(-0.01, 0.01))
    if mid == "M01" and month >= "2026-07" and causes["C1_白砂糖涨价"]:
        p = base * SUGAR_UP * (1 + r.uniform(-0.01, 0.01))
    return round(p, 4)


def unit_cost(sku, month, causes):
    mat = sum(q * material_price(m, month, causes) for m, q in sku[7].items())
    return round(mat, 4), round(sku[5] * PROCESS_MULT, 4)


def generate(causes):
    sales, logistics = [], []
    agg = {m: dict(rev=0.0, cogs=0.0, logi=0.0, comm=0.0, mkt=0.0) for m in MONTHS}
    ch_rev = {(m, c[0]): 0.0 for m in MONTHS for c in CHANNELS}
    for day in DAYS:
        month = day.strftime("%Y-%m")
        mi = MONTHS.index(month)
        wk = 1.08 if day.weekday() >= 5 else 1.0
        trend = 1 + 0.0004 * (day - START).days
        ch_ship = {c[0]: 0 for c in CHANNELS}
        for sku in SKUS:
            if dt.date.fromisoformat(sku[8]) > day:
                continue
            m_cost, p_cost = unit_cost(sku, month, causes)
            for ch in CHANNELS:
                cid, share, ch_disc, logi_unit, rate = ch[0], ch[3], ch[4], ch[5], ch[6]
                r = rand("sale", day, sku[0], cid)
                vol = sku[6] * share * SEASON[sku[2]][mi] * wk * trend * r.lognormvariate(0, 0.08)
                disc = ch_disc
                if causes["C5_618大促基数"] and dt.date(2026, 6, 1) <= day <= dt.date(2026, 6, 18) and cid in ("F01", "F02"):
                    vol *= 1.5
                    disc += 0.08
                if causes["C3_社区团购扩张"] and cid == "F05" and day >= GROUP_BUY_FROM:
                    ramp = min(1.0, (day - GROUP_BUY_FROM).days / 60)
                    vol *= 1 + 1.6 * ramp
                if sku[2] == "坚果零食" and dt.date(2026, 9, 10) <= day <= dt.date(2026, 9, 25) and cid != "F06":
                    vol *= 1.8 if sku[0] != "S16" else 6.0
                    if causes["C4_中秋促销让利"]:
                        disc += 0.12
                    else:
                        disc += 0.02
                if sku[0] == "S16" and not (dt.date(2026, 8, 20) <= day <= dt.date(2026, 9, 25)):
                    vol *= 0.1
                qty = int(round(vol))
                if qty <= 0:
                    continue
                gross = qty * sku[4]
                discount = gross * disc
                net = gross - discount
                if cid == "F02" and day >= F02_RATE_FROM and causes["C2_内容电商佣金上调"]:
                    rate = F02_NEW_RATE
                sales.append((day.isoformat(), sku[0], cid, qty, round(gross, 2), round(discount, 2), round(net, 2)))
                ch_ship[cid] += qty
                a = agg[month]
                a["rev"] += net
                a["cogs"] += qty * (m_cost + p_cost)
                a["comm"] += net * rate
                ch_rev[(month, cid)] += net
        for ch in CHANNELS:
            qty = ch_ship[ch[0]]
            r = rand("logi", day, ch[0])
            cost = qty * ch[5] * (1 + r.uniform(-0.02, 0.02))
            logistics.append((day.isoformat(), ch[0], qty, max(1, qty // 400), round(cost, 2)))
            agg[month]["logi"] += cost
    mkt_rows = []
    for m in MONTHS:
        for c in CHANNELS:
            amt = round(ch_rev[(m, c[0])] * MARKETING_RATE[c[0]], -2)
            agg[m]["mkt"] += amt
            mkt_rows.append((m, c[0], amt))
    agg["_mkt_rows"] = mkt_rows
    return sales, logistics, agg


def margins(a):
    gp = a["rev"] - a["cogs"]
    np_ = gp - a["logi"] - a["comm"] - a["mkt"]
    return dict(revenue=round(a["rev"], 2), cogs=round(a["cogs"], 2), logistics=round(a["logi"], 2),
                commission=round(a["comm"], 2), marketing=round(a["mkt"], 2),
                gross_margin=round(gp / a["rev"], 6), net_margin=round(np_ / a["rev"], 6))


SCHEMA = """
CREATE TABLE dim_sku (sku_id TEXT PRIMARY KEY, sku_name TEXT, product_line TEXT, spec TEXT, list_price REAL, process_cost REAL, launch_date TEXT);
CREATE TABLE dim_channel (channel_id TEXT PRIMARY KEY, channel_name TEXT, channel_type TEXT, base_discount_rate REAL);
CREATE TABLE dim_material (material_id TEXT PRIMARY KEY, material_name TEXT, unit TEXT);
CREATE TABLE bom (sku_id TEXT, material_id TEXT, qty_per_unit REAL, PRIMARY KEY (sku_id, material_id));
CREATE TABLE fact_material_price_monthly (month TEXT, material_id TEXT, unit_price REAL, PRIMARY KEY (month, material_id));
CREATE TABLE fact_sku_cost_monthly (month TEXT, sku_id TEXT, unit_material_cost REAL, unit_process_cost REAL, unit_cogs REAL, PRIMARY KEY (month, sku_id));
CREATE TABLE fact_sales_daily (dt TEXT, sku_id TEXT, channel_id TEXT, sales_qty INTEGER, gross_amount REAL, discount_amount REAL, net_revenue REAL);
CREATE TABLE fact_logistics_daily (dt TEXT, channel_id TEXT, shipped_qty INTEGER, shipments INTEGER, logistics_cost REAL, PRIMARY KEY (dt, channel_id));
CREATE TABLE dim_channel_commission (channel_id TEXT, effective_from TEXT, effective_to TEXT, commission_rate REAL);
CREATE TABLE fact_commission_monthly (month TEXT, channel_id TEXT, net_revenue REAL, commission_amount REAL, PRIMARY KEY (month, channel_id));
CREATE TABLE fact_marketing_monthly (month TEXT, channel_id TEXT, spend_type TEXT, amount REAL);
CREATE TABLE event_calendar (event_id TEXT PRIMARY KEY, start_date TEXT, end_date TEXT, event_type TEXT, title TEXT, description TEXT, channel_id TEXT, product_line TEXT);
CREATE TABLE meta_metric (metric_code TEXT PRIMARY KEY, metric_name TEXT, aliases TEXT, definition TEXT, decomposition TEXT);
CREATE INDEX idx_sales_dt ON fact_sales_daily(dt);
CREATE INDEX idx_sales_sku ON fact_sales_daily(sku_id);
"""

EVENTS = [
    ("FE01", "2026-06-01", "2026-06-18", "营销活动", "618 大促", "综合电商和内容电商渠道额外让利 8%，销量明显放大", None, None),
    ("FE02", "2026-07-01", "2026-09-30", "采购成本", "白砂糖采购价上调", "主要供应商通知白砂糖采购价自 7 月起上调", None, None),
    ("FE03", "2026-07-15", "2026-09-30", "渠道拓展", "社区团购渠道扩张", "社区团购新增 3 个城市，渠道销量快速增长；该渠道供货价低、团长佣金高、末端配送成本高", "F05", None),
    ("FE04", "2026-08-01", "2026-08-01", "物流调整", "华南物流服务商更换", "华南区域干线物流服务商更换，合同单价与原服务商持平", None, None),
    ("FE05", "2026-08-01", "2026-12-31", "渠道政策", "内容电商平台佣金调整", "内容电商平台技术服务费率由 5% 调整为 10%", "F02", None),
    ("FE06", "2026-09-05", "2026-09-05", "新品上市", "轻甜气泡水上市", "新品 480ml 轻甜气泡水上市", None, "气泡水"),
    ("FE07", "2026-09-10", "2026-09-25", "营销活动", "中秋礼盒促销", "坚果零食线全渠道（批发除外）加大让利，礼盒主推", None, "坚果零食"),
]

METRICS = [
    ("revenue", "营收", "销售收入,净收入,收入", "SUM(fact_sales_daily.net_revenue)，已扣除折扣", ""),
    ("cogs", "产品成本", "销售成本,商品成本", "SUM(sales_qty × fact_sku_cost_monthly.unit_cogs)，按销售月份匹配成本", "单位成本 = 材料成本 + 加工费；材料成本 = Σ BOM 用量 × 当月材料单价"),
    ("gross_profit", "毛利", "", "营收 − 产品成本", ""),
    ("gross_margin", "毛利率", "", "毛利 ÷ 营收", ""),
    ("logistics", "物流费用", "履约费用,运费", "SUM(fact_logistics_daily.logistics_cost)", ""),
    ("commission", "渠道佣金", "平台扣点,技术服务费", "SUM(fact_commission_monthly.commission_amount)", ""),
    ("marketing", "营销费用", "推广费,市场费用", "SUM(fact_marketing_monthly.amount)", ""),
    ("net_profit", "净利润", "贡献利润,经营利润", "毛利 − 物流费用 − 渠道佣金 − 营销费用", "加法模型"),
    ("net_margin", "净利率", "利润率,经营利润率", "净利润 ÷ 营收", ""),
    ("discount_rate", "折扣率", "让利率", "SUM(discount_amount) ÷ SUM(gross_amount)", ""),
]


def build_db(path, sales, logistics, causes, mkt_rows):
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    cur = con.cursor()
    cur.executescript(SCHEMA)
    cur.executemany("INSERT INTO dim_sku VALUES (?,?,?,?,?,?,?)", [(s[0], s[1], s[2], s[3], s[4], s[5], s[8]) for s in SKUS])
    cur.executemany("INSERT INTO dim_channel VALUES (?,?,?,?)", [(c[0], c[1], c[2], c[4]) for c in CHANNELS])
    cur.executemany("INSERT INTO dim_material VALUES (?,?,?)", [(m[0], m[1], m[2]) for m in MATERIALS])
    cur.executemany("INSERT INTO bom VALUES (?,?,?)", [(s[0], m, round(q, 6)) for s in SKUS for m, q in s[7].items()])
    cur.executemany("INSERT INTO fact_material_price_monthly VALUES (?,?,?)",
                    [(mo, m[0], material_price(m[0], mo, causes)) for mo in MONTHS for m in MATERIALS])
    rows = []
    for mo in MONTHS:
        for s in SKUS:
            mc, pc = unit_cost(s, mo, causes)
            rows.append((mo, s[0], mc, pc, round(mc + pc, 4)))
    cur.executemany("INSERT INTO fact_sku_cost_monthly VALUES (?,?,?,?,?)", rows)
    cur.executemany("INSERT INTO fact_sales_daily VALUES (?,?,?,?,?,?,?)", sales)
    cur.executemany("INSERT INTO fact_logistics_daily VALUES (?,?,?,?,?)", logistics)
    comm = []
    for c in CHANNELS:
        if c[0] == "F02":
            comm += [("F02", "2025-01-01", "2026-07-31", 0.05), ("F02", "2026-08-01", "9999-12-31", F02_NEW_RATE)]
        else:
            comm.append((c[0], "2025-01-01", "9999-12-31", c[6]))
    cur.executemany("INSERT INTO dim_channel_commission VALUES (?,?,?,?)", comm)
    cur.execute("""INSERT INTO fact_commission_monthly
        SELECT substr(s.dt,1,7), s.channel_id, ROUND(SUM(s.net_revenue),2), ROUND(SUM(s.net_revenue * r.commission_rate),2)
        FROM fact_sales_daily s JOIN dim_channel_commission r
          ON r.channel_id = s.channel_id AND s.dt BETWEEN r.effective_from AND r.effective_to
        GROUP BY 1, 2""")
    mk = []
    for mo, cid, amt in mkt_rows:
        t = {"F03": "商超进场与陈列费", "F04": "终端陈列费", "F06": "经销商激励"}.get(cid, "平台推广投放")
        mk.append((mo, cid, t, float(amt)))
    cur.executemany("INSERT INTO fact_marketing_monthly VALUES (?,?,?,?)", mk)
    cur.executemany("INSERT INTO event_calendar VALUES (?,?,?,?,?,?,?,?)", EVENTS)
    cur.executemany("INSERT INTO meta_metric VALUES (?,?,?,?,?)", METRICS)
    con.commit()
    cur.execute("VACUUM")
    con.close()


def main(out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    sales, logistics, agg = generate(CAUSES_ALL)
    build_db(out / "fmcg.db", sales, logistics, CAUSES_ALL, agg["_mkt_rows"])
    jun, sep = margins(agg["2026-06"]), margins(agg["2026-09"])
    res = dict(jun=jun, sep=sep, causes={})
    for name in CAUSES_ALL:
        c = dict(CAUSES_ALL)
        c[name] = False
        _, _, a2 = generate(c)
        j2, s2 = margins(a2["2026-06"]), margins(a2["2026-09"])
        res["causes"][name] = dict(
            net_margin_pp=round(((sep["net_margin"] - jun["net_margin"]) - (s2["net_margin"] - j2["net_margin"])) * 100, 3),
            gross_margin_pp=round(((sep["gross_margin"] - jun["gross_margin"]) - (s2["gross_margin"] - j2["gross_margin"])) * 100, 3),
            jun_without=j2, sep_without=s2)
    _, _, a0 = generate({k: False for k in CAUSES_ALL})
    res["no_causes"] = dict(jun=margins(a0["2026-06"]), sep=margins(a0["2026-09"]))
    res["monthly"] = {m: margins(agg[m]) for m in MONTHS}
    (out / "_impacts_fmcg.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    for m in MONTHS:
        mm = res["monthly"][m]
        print(m, round(mm["revenue"] / 1e4), "毛利率", round(mm["gross_margin"] * 100, 2), "净利率", round(mm["net_margin"] * 100, 2))
    for k, v in res["causes"].items():
        print(k, "净利率影响", v["net_margin_pp"], "pp  毛利率影响", v["gross_margin_pp"], "pp")
    nc = res["no_causes"]
    print("无埋入原因时 6月/9月 净利率", round(nc["jun"]["net_margin"] * 100, 2), round(nc["sep"]["net_margin"] * 100, 2))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/home/claude/wh/out")
