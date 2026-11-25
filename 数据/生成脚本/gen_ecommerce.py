"""场景一：电商 GMV 下滑归因的模拟数仓（青禾优选，2026-07-01 至 2026-09-30）。

埋入的原因可以逐个关闭（CAUSES），关闭后重新生成即可得到“没有这个原因时”的反事实结果，
据此计算每个原因对 9 月 GMV 的真实影响，写入标准答案。
每笔订单用独立的随机种子生成，关闭一个原因只影响它波及的订单，不会打乱其他订单。

用法：python gen_ecommerce.py 输出目录
"""
import datetime as dt
import json
import sqlite3
import sys
import zlib
import random
from pathlib import Path

SEED = "qinghe-2026"
START, END = dt.date(2026, 7, 1), dt.date(2026, 9, 30)
DAYS = [START + dt.timedelta(d) for d in range((END - START).days + 1)]

# ---------------- 维度数据 ----------------
CATEGORIES = [  # 编码, 名称, 订单占比权重, 成本率, 退款率
    ("C01", "家电", 0.10, 0.82, 0.03),
    ("C02", "手机数码", 0.12, 0.88, 0.04),
    ("C03", "生鲜", 0.20, 0.70, 0.04),
    ("C04", "个护美妆", 0.14, 0.55, 0.06),
    ("C05", "食品饮料", 0.16, 0.68, 0.03),
    ("C06", "家居日用", 0.14, 0.60, 0.05),
    ("C07", "母婴", 0.07, 0.72, 0.04),
    ("C08", "服饰鞋包", 0.07, 0.50, 0.12),
]
CAT_IDS = [c[0] for c in CATEGORIES]

PRODUCTS = {  # 类目: [(名称, 品牌, 价格, 热度)]
    "C01": [("1.5匹新一级变频空调 KFR-35GW", "岭风", 2899, 0.22), ("大1匹变频空调 KFR-26GW", "岭风", 2399, 0.16),
            ("510升十字对开门冰箱 BCD-510", "冰原", 3699, 0.15), ("10公斤滚筒洗衣机 XQG100", "净川", 1899, 0.10),
            ("65英寸4K智能电视 Q65", "明幕", 3299, 0.08), ("60升储水式电热水器 F60", "暖源", 1299, 0.07),
            ("侧吸式油烟机 CXW-260", "灶安", 2199, 0.06), ("218升三门冰箱 BCD-218", "冰原", 1599, 0.06),
            ("10公斤洗烘一体机 XQG100-HB", "净川", 2999, 0.05), ("3匹立柜式空调 KFR-72LW", "岭风", 5999, 0.05)],
    "C02": [("星曜 X9 手机 12+256G", "星曜", 3999, 0.20), ("星曜 X9 Lite 手机 8+256G", "星曜", 1999, 0.18),
            ("拾音 Pro 降噪耳机", "拾音", 899, 0.14), ("轻薄笔记本 Air14", "朗格", 4599, 0.10),
            ("11英寸平板 Pad S", "星曜", 2299, 0.10), ("智能手表 W3", "星曜", 999, 0.09),
            ("20000mAh 移动电源", "电驰", 129, 0.10), ("65W 氮化镓充电器", "电驰", 99, 0.09)],
    "C03": [("智利车厘子 JJ级 2斤装", "鲜禾", 129, 0.14), ("烟台红富士苹果 5斤", "鲜禾", 39, 0.16),
            ("海南贵妃芒 5斤", "鲜禾", 49, 0.12), ("鲜活基围虾 1斤", "海泽", 79, 0.10),
            ("冷鲜猪肋排 1kg", "牧原鲜", 69, 0.12), ("散养土鸡蛋 30枚", "禾农", 45, 0.12),
            ("有机蔬菜组合 3kg", "禾农", 59, 0.12), ("巴氏鲜牛奶 950ml×2", "牧云", 32, 0.12)],
    "C04": [("氨基酸洁面乳 120g", "清研", 79, 0.16), ("保湿精华液 30ml", "清研", 259, 0.14),
            ("防晒乳 SPF50 50ml", "曦护", 129, 0.12), ("洗发水 750ml", "丝语", 69, 0.16),
            ("沐浴露 1L", "丝语", 59, 0.14), ("口红 3.5g", "绯色", 199, 0.10), ("面膜 10片装", "清研", 99, 0.18)],
    "C05": [("每日坚果 30包", "禾悦", 99, 0.16), ("NFC 橙汁 1L×6", "禾悦", 89, 0.12),
            ("中秋月饼礼盒 8枚", "禾悦", 168, 0.08), ("五常大米 5kg", "禾农", 69, 0.16),
            ("特级初榨橄榄油 1L", "橄情", 99, 0.10), ("气泡水 330ml×24", "禾悦", 59, 0.14),
            ("挂耳咖啡 50包", "焙香", 89, 0.12), ("东北黑木耳 500g", "禾农", 49, 0.12)],
    "C06": [("全棉四件套 1.8m", "棉语", 299, 0.12), ("收纳箱 3只装", "整居", 89, 0.14),
            ("不粘锅炒锅 32cm", "炊匠", 159, 0.12), ("抽纸 24包", "柔点", 49, 0.20),
            ("洗衣凝珠 100颗", "净川", 69, 0.18), ("记忆棉枕", "棉语", 129, 0.12), ("LED 台灯", "明幕", 99, 0.12)],
    "C07": [("婴儿配方奶粉 3段 800g", "贝启", 268, 0.24), ("纸尿裤 L码 120片", "贝柔", 159, 0.26),
            ("婴儿推车 轻便款", "贝行", 899, 0.10), ("儿童安全座椅", "贝行", 1299, 0.08),
            ("婴儿湿巾 80抽×10", "贝柔", 59, 0.20), ("益智积木 300片", "童趣", 129, 0.12)],
    "C08": [("男士速干T恤", "行野", 99, 0.18), ("女士连衣裙", "素衣", 299, 0.14), ("运动跑鞋", "行野", 459, 0.16),
            ("双肩背包", "途行", 199, 0.14), ("纯棉袜 10双", "棉语", 49, 0.20), ("冲锋衣", "行野", 699, 0.18)],
}
OOS_SKUS = {"C01-01", "C01-02", "C01-03"}  # 埋入原因 C2：断货的三款家电

CHANNELS = [  # 编码, 名称, 类型, 基础日均访客, 基础转化率, 新客比例, 区域分布(华南,华东,华北,华中,西南,西北,东北)
    ("CH01", "App自然流量", "自然流量", 20000, 0.032, 0.12, (0.22, 0.26, 0.16, 0.12, 0.12, 0.06, 0.06)),
    ("CH02", "搜索广告", "付费投放", 7500, 0.038, 0.30, (0.22, 0.26, 0.16, 0.12, 0.12, 0.06, 0.06)),
    ("CH03", "信息流广告", "付费投放", 10000, 0.022, 0.45, (0.14, 0.18, 0.35, 0.12, 0.10, 0.06, 0.05)),
    ("CH04", "直播", "内容", 4000, 0.045, 0.25, (0.24, 0.26, 0.14, 0.12, 0.12, 0.06, 0.06)),
    ("CH05", "小程序", "自然流量", 5000, 0.028, 0.15, (0.24, 0.24, 0.16, 0.12, 0.12, 0.06, 0.06)),
    ("CH06", "联盟推广", "付费投放", 2500, 0.025, 0.40, (0.22, 0.26, 0.16, 0.12, 0.12, 0.06, 0.06)),
]
REGIONS = [("R01", "华南"), ("R02", "华东"), ("R03", "华北"), ("R04", "华中"), ("R05", "西南"), ("R06", "西北"), ("R07", "东北")]

PROMOS = [  # 编码, 名称, 开始, 结束, 类型, 适用类目(None=全场), 访客倍数, 转化倍数, 类目需求倍数, 额外折扣
    ("P01", "暑期家电节", dt.date(2026, 7, 10), dt.date(2026, 7, 16), "类目活动", "C01", 1.05, 1.0, 1.8, 0.08),
    ("P02", "818年中返场", dt.date(2026, 8, 15), dt.date(2026, 8, 18), "全场大促", None, 1.25, 1.15, 1.0, 0.12),
    ("P03", "开学季", dt.date(2026, 8, 25), dt.date(2026, 9, 3), "类目活动", "C02", 1.0, 1.0, 1.3, 0.06),
    ("P04", "中秋礼盒节", dt.date(2026, 9, 15), dt.date(2026, 9, 24), "类目活动", "C05", 1.0, 1.0, 1.6, 0.08),
    ("P05", "国庆预热", dt.date(2026, 9, 26), dt.date(2026, 9, 30), "全场活动", None, 1.15, 1.05, 1.0, 0.04),
]

HOUR_W = [0.6, 0.3, 0.2, 0.1, 0.1, 0.2, 0.6, 1.5, 2.6, 3.6, 5.2, 5.6, 4.8, 4.0, 4.2, 4.4, 4.6, 4.8, 5.0, 5.8, 7.4, 8.6, 7.8, 3.9]

# 埋入的原因（True 表示生效）
CAUSES_ALL = {
    "C1_信息流限流": True,       # 9/8 起信息流广告访客减半
    "C2_家电断货": True,         # 9/10-9/25 三款热销家电断货
    "C3_818大促基数": True,      # 8 月有 818 大促（关闭则 8 月没有这场大促）
    "C4_支付故障": True,         # 9/20 21:00-23:00 支付通道故障
}
FEED_CUT_FROM = dt.date(2026, 9, 8)
OOS_FROM, OOS_TO = dt.date(2026, 9, 10), dt.date(2026, 9, 25)
PAY_OUTAGE_DAY, PAY_OUTAGE_HOURS = dt.date(2026, 9, 20), (21, 22)


def rand(*keys):
    return random.Random(zlib.crc32(("|".join(map(str, (SEED,) + keys))).encode()))


def promos_on(day, causes):
    out = []
    for p in PROMOS:
        if p[2] <= day <= p[3]:
            if p[0] == "P02" and not causes["C3_818大促基数"]:
                continue
            out.append(p)
    return out


def generate(causes, collect=True):
    """返回 (表数据字典, 按日按渠道的汇总)。collect=False 时只算汇总，用于反事实。"""
    prod_table = {}
    for cid, items in PRODUCTS.items():
        cost_rate = dict((c[0], c[3]) for c in CATEGORIES)[cid]
        for i, (name, brand, price, pop) in enumerate(items, 1):
            pid = f"{cid}-{i:02d}"
            prod_table[pid] = dict(product_id=pid, product_name=f"{brand} {name}", category_id=cid, brand=brand,
                                   list_price=price, cost_price=round(price * cost_rate, 2), pop=pop)
    by_cat = {cid: [p for p in prod_table.values() if p["category_id"] == cid] for cid in CAT_IDS}

    traffic, orders, items, daily = [], [], [], {}
    new_user_seq = 0
    for day in DAYS:
        wk = 1.10 if day.weekday() >= 5 else 1.0
        trend = 1 + 0.0008 * (day - START).days
        active = promos_on(day, causes)
        uv_mult = 1.0
        cvr_mult = 1.0
        for p in active:
            uv_mult *= p[6]
            cvr_mult *= p[7]
        cat_w = []
        for cid, _, w, _, _ in CATEGORIES:
            m = 1.0
            for p in active:
                if p[5] == cid:
                    m *= p[8]
            cat_w.append(w * m)
        # 类目活动带来的是新增订单，不挤占其他类目的订单量
        cat_extra = sum(cat_w) / sum(c[2] for c in CATEGORIES)
        oos_active = causes["C2_家电断货"] and OOS_FROM <= day <= OOS_TO
        for ch_id, ch_name, ch_type, base_uv, base_cvr, new_rate, reg_w in CHANNELS:
            r = rand("uv", day, ch_id)
            noise = r.lognormvariate(0, 0.05)
            uv = base_uv * wk * trend * uv_mult * noise
            if causes["C1_信息流限流"] and ch_id == "CH03" and day >= FEED_CUT_FROM:
                uv *= 0.5
            uv = int(round(uv))
            expected_orders = uv * base_cvr * cvr_mult * cat_extra * r.lognormvariate(0, 0.04)
            n_orders = int(round(expected_orders))
            detail_uv = int(round(uv * 0.42 * r.lognormvariate(0, 0.02)))
            cart_uv = int(round(uv * 0.11 * r.lognormvariate(0, 0.03)))
            day_orders = 0
            day_gmv = 0.0
            for k in range(n_orders):
                o = rand("order", day, ch_id, k)
                hour = o.choices(range(24), HOUR_W)[0]
                if causes["C4_支付故障"] and day == PAY_OUTAGE_DAY and hour in PAY_OUTAGE_HOURS and o.random() < 0.8:
                    continue
                cid = o.choices(CAT_IDS, cat_w)[0]
                cand = by_cat[cid]
                first = o.choices(cand, [p["pop"] for p in cand])[0]
                if oos_active and first["product_id"] in OOS_SKUS:
                    if o.random() < 0.5:
                        continue  # 顾客放弃购买
                    cand = [p for p in cand if p["product_id"] not in OOS_SKUS]
                    first = o.choices(cand, [p["pop"] for p in cand])[0]
                n_lines = 1 if cid in ("C01", "C02") else o.choices([1, 2, 3], [0.70, 0.22, 0.08])[0]
                pool = [p for p in cand if p["product_id"] != first["product_id"]]
                lines = [first] + o.sample(pool, min(n_lines - 1, len(pool)))
                line_rows = []
                for p in lines:
                    qty = 1 if cid in ("C01", "C02") else o.choices([1, 2, 3], [0.75, 0.18, 0.07])[0]
                    line_rows.append((p, qty, p["list_price"] * qty))
                amount = sum(x[2] for x in line_rows)
                disc_rate = 0.03 + sum(p[9] for p in active if p[5] in (None, cid))
                discount = round(amount * disc_rate, 2)
                pay = round(amount - discount, 2)
                day_orders += 1
                day_gmv += pay
                if collect:
                    oid = f"O{day:%y%m%d}{ch_id[-1]}{k:05d}"
                    is_new = 1 if o.random() < new_rate else 0
                    if is_new:
                        new_user_seq += 1
                        uid = f"U{2000000 + new_user_seq}"
                    else:
                        uid = f"U{1000000 + o.randrange(60000)}"
                    region = o.choices(REGIONS, reg_w)[0][0]
                    refund_rate = dict((c[0], c[4]) for c in CATEGORIES)[cid]
                    refunded = 1 if o.random() < refund_rate else 0
                    ts = dt.datetime.combine(day, dt.time(hour, o.randrange(60), o.randrange(60)))
                    orders.append((oid, day.isoformat(), ts.isoformat(sep=" "), uid, ch_id, region, is_new,
                                   sum(x[1] for x in line_rows), round(amount, 2), discount, pay, refunded))
                    for p, qty, amt in line_rows:
                        items.append((oid, p["product_id"], qty, p["list_price"], round(amt * (1 - disc_rate), 2)))
            daily[(day, ch_id)] = (uv, day_orders, day_gmv)
            if collect:
                traffic.append((day.isoformat(), ch_id, uv, detail_uv, cart_uv))
    return dict(products=prod_table, traffic=traffic, orders=orders, items=items, new_users=new_user_seq), daily


def month_sum(daily, month):
    uv = sum(v[0] for (d, _), v in daily.items() if d.month == month)
    n = sum(v[1] for (d, _), v in daily.items() if d.month == month)
    g = sum(v[2] for (d, _), v in daily.items() if d.month == month)
    return dict(uv=uv, orders=n, gmv=round(g, 2), cvr=n / uv, aov=g / n)


def build_db(path, data):
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    cur = con.cursor()
    cur.executescript(SCHEMA)
    cur.executemany("INSERT INTO dim_category VALUES (?,?)", [(c[0], c[1]) for c in CATEGORIES])
    cur.executemany("INSERT INTO dim_product VALUES (?,?,?,?,?,?)",
                    [(p["product_id"], p["product_name"], p["category_id"], p["brand"], p["list_price"], p["cost_price"])
                     for p in data["products"].values()])
    cur.executemany("INSERT INTO dim_channel VALUES (?,?,?)", [(c[0], c[1], c[2]) for c in CHANNELS])
    cur.executemany("INSERT INTO dim_region VALUES (?,?)", REGIONS)
    cur.executemany("INSERT INTO dim_promotion VALUES (?,?,?,?,?,?,?)",
                    [(p[0], p[1], p[2].isoformat(), p[3].isoformat(), p[4], p[5],
                      f"额外折扣 {int(p[9] * 100)}%") for p in PROMOS])
    cur.executemany("INSERT INTO fact_traffic_daily VALUES (?,?,?,?,?)", data["traffic"])
    cur.executemany("INSERT INTO fact_orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", data["orders"])
    cur.executemany("INSERT INTO fact_order_items VALUES (?,?,?,?,?)", data["items"])
    # 库存：断货期内三款家电库存为 0
    inv = []
    for day in DAYS:
        for pid, p in data["products"].items():
            r = rand("inv", day, pid)
            oos = 1 if (pid in OOS_SKUS and OOS_FROM <= day <= OOS_TO) else 0
            stock = 0 if oos else int(r.uniform(80, 400) * (3 if p["category_id"] in ("C03", "C05") else 1))
            inv.append((day.isoformat(), pid, stock, oos))
    cur.executemany("INSERT INTO fact_inventory_daily VALUES (?,?,?,?)", inv)
    # 用户表（含个人信息，属于受限表）
    users = []
    for i in range(60000):
        r = rand("user", i)
        users.append((f"U{1000000 + i}", fake_name(r), fake_phone(r),
                      (dt.date(2023, 1, 1) + dt.timedelta(r.randrange(1277))).isoformat(), r.choice(REGIONS)[0]))
    first_order = {}
    for o in data["orders"]:
        if o[6] == 1 and o[3] not in first_order:
            first_order[o[3]] = (o[1], o[5])
    for uid, (d, reg) in first_order.items():
        r = rand("user", uid)
        users.append((uid, fake_name(r), fake_phone(r), d, reg))
    cur.executemany("INSERT INTO dim_user VALUES (?,?,?,?,?)", users)
    cur.executemany("INSERT INTO event_calendar VALUES (?,?,?,?,?,?,?,?)", EVENTS)
    cur.executemany("INSERT INTO meta_metric VALUES (?,?,?,?,?)", METRICS)
    con.commit()
    cur.execute("VACUUM")
    con.close()


SURNAMES = "王李张刘陈杨黄赵吴周徐孙马朱胡郭何林罗高"
GIVEN = "伟芳娜敏静丽强磊洋艳勇军杰娟涛明超秀霞平刚桂英华慧"


def fake_name(r):
    return r.choice(SURNAMES) + "".join(r.choice(GIVEN) for _ in range(r.choice([1, 2])))


def fake_phone(r):
    return "1" + r.choice("3589") + "".join(str(r.randrange(10)) for _ in range(9))


EVENTS = [  # 编码, 开始, 结束, 类型, 标题, 描述, 影响渠道, 影响类目
    ("E01", "2026-07-10", "2026-07-16", "营销活动", "暑期家电节", "家电类目额外 8% 折扣", None, "C01"),
    ("E02", "2026-08-15", "2026-08-18", "营销活动", "818 年中返场大促", "全场额外 12% 折扣，站内外加大投放", None, None),
    ("E03", "2026-08-25", "2026-09-03", "营销活动", "开学季", "手机数码额外 6% 折扣", None, "C02"),
    ("E04", "2026-09-03", "2026-09-03", "产品发布", "App 6.2 版本发布", "商品详情页改版，新增视频讲解模块", None, None),
    ("E05", "2026-09-08", "2026-09-30", "投放调整", "信息流广告账户限流", "信息流广告账户因素材审核问题被平台限流，日投放预算下调约 50%", "CH03", None),
    ("E06", "2026-09-12", "2026-09-18", "竞品动态", "竞品“优鲜集市”生鲜满减", "竞品上线生鲜满 99 减 30 活动（虚构竞品）", None, "C03"),
    ("E07", "2026-09-15", "2026-09-24", "营销活动", "中秋礼盒节", "食品饮料额外 8% 折扣，月饼礼盒主推", None, "C05"),
    ("E08", "2026-09-20", "2026-09-20", "系统故障", "支付通道故障", "21:00 至 23:00 部分支付通道超时，约 2 小时后恢复", None, None),
    ("E09", "2026-09-26", "2026-09-30", "营销活动", "国庆预热", "全场额外 4% 折扣", None, None),
]

METRICS = [  # 编码, 名称, 别名, 计算口径, 拆解关系
    ("gmv", "GMV", "成交额,销售额,交易额,支付金额", "SUM(fact_orders.pay_amount)，含已退款订单", "GMV = 访客数 × 支付转化率 × 客单价"),
    ("net_gmv", "净GMV", "净成交额,扣退GMV", "SUM(pay_amount) 中 is_refunded = 0 的部分", ""),
    ("order_cnt", "订单数", "单量,支付订单数", "COUNT(fact_orders.order_id)", ""),
    ("uv", "访客数", "UV,流量,访问人数", "SUM(fact_traffic_daily.uv)", ""),
    ("cvr", "支付转化率", "转化率,下单转化率", "订单数 ÷ 访客数", ""),
    ("aov", "客单价", "AOV,平均订单金额", "GMV ÷ 订单数", "客单价 = 件单价 × 每单件数"),
    ("asp", "件单价", "平均件单价", "GMV ÷ 销售件数（SUM(fact_orders.item_qty)）", ""),
    ("ipo", "每单件数", "连带率", "销售件数 ÷ 订单数", ""),
    ("gross_profit", "毛利", "商品毛利", "SUM(fact_order_items.pay_amount - dim_product.cost_price × quantity)", ""),
    ("refund_rate", "退款率", "退货率", "已退款订单数 ÷ 订单数", ""),
    ("new_user_share", "新客占比", "新用户占比", "is_new_user = 1 的订单数 ÷ 订单数", ""),
]

SCHEMA = """
CREATE TABLE dim_category (category_id TEXT PRIMARY KEY, category_name TEXT);
CREATE TABLE dim_product (product_id TEXT PRIMARY KEY, product_name TEXT, category_id TEXT, brand TEXT, list_price REAL, cost_price REAL);
CREATE TABLE dim_channel (channel_id TEXT PRIMARY KEY, channel_name TEXT, channel_type TEXT);
CREATE TABLE dim_region (region_id TEXT PRIMARY KEY, region_name TEXT);
CREATE TABLE dim_promotion (promo_id TEXT PRIMARY KEY, promo_name TEXT, start_date TEXT, end_date TEXT, promo_type TEXT, category_id TEXT, discount_desc TEXT);
CREATE TABLE dim_user (user_id TEXT PRIMARY KEY, user_name TEXT, phone TEXT, register_date TEXT, region_id TEXT);
CREATE TABLE fact_traffic_daily (dt TEXT, channel_id TEXT, uv INTEGER, detail_uv INTEGER, cart_uv INTEGER, PRIMARY KEY (dt, channel_id));
CREATE TABLE fact_orders (order_id TEXT PRIMARY KEY, dt TEXT, pay_time TEXT, user_id TEXT, channel_id TEXT, region_id TEXT, is_new_user INTEGER, item_qty INTEGER, order_amount REAL, discount_amount REAL, pay_amount REAL, is_refunded INTEGER);
CREATE TABLE fact_order_items (order_id TEXT, product_id TEXT, quantity INTEGER, unit_price REAL, pay_amount REAL);
CREATE TABLE fact_inventory_daily (dt TEXT, product_id TEXT, stock_qty INTEGER, is_out_of_stock INTEGER, PRIMARY KEY (dt, product_id));
CREATE TABLE event_calendar (event_id TEXT PRIMARY KEY, start_date TEXT, end_date TEXT, event_type TEXT, title TEXT, description TEXT, channel_id TEXT, category_id TEXT);
CREATE TABLE meta_metric (metric_code TEXT PRIMARY KEY, metric_name TEXT, aliases TEXT, definition TEXT, decomposition TEXT);
CREATE INDEX idx_orders_dt ON fact_orders(dt);
CREATE INDEX idx_orders_channel ON fact_orders(channel_id);
CREATE INDEX idx_items_order ON fact_order_items(order_id);
CREATE INDEX idx_items_product ON fact_order_items(product_id);
"""


def main(out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    data, daily_all = generate(CAUSES_ALL, collect=True)
    build_db(out / "ecommerce.db", data)
    aug, sep = month_sum(daily_all, 8), month_sum(daily_all, 9)
    result = dict(aug=aug, sep=sep, causes={})
    for name in CAUSES_ALL:
        c = dict(CAUSES_ALL)
        c[name] = False
        _, d = generate(c, collect=False)
        a2, s2 = month_sum(d, 8), month_sum(d, 9)
        delta_all = sep["gmv"] - aug["gmv"]
        delta_wo = s2["gmv"] - a2["gmv"]
        result["causes"][name] = dict(impact_on_mom_change=round(delta_all - delta_wo, 2),
                                      aug_without=a2, sep_without=s2)
    c0 = {k: False for k in CAUSES_ALL}
    _, d0 = generate(c0, collect=False)
    result["no_causes"] = dict(aug=month_sum(d0, 8), sep=month_sum(d0, 9))
    (out / "_impacts_ecommerce.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "causes"}, ensure_ascii=False, indent=1))
    for k, v in result["causes"].items():
        print(k, v["impact_on_mom_change"])


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/home/claude/wh/out")
