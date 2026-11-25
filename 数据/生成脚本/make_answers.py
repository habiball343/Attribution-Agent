"""根据数仓和反事实结果，写出两个场景的标准答案（JSON 供评测程序使用，Markdown 供人阅读）。"""
import json
import math
import sqlite3
import sys
from pathlib import Path

out = Path(sys.argv[1] if len(sys.argv) > 1 else "/home/claude/wh/out")
imp_e = json.loads((out / "_impacts_ecommerce.json").read_text(encoding="utf-8"))
imp_f = json.loads((out / "_impacts_fmcg.json").read_text(encoding="utf-8"))


def L(a, b):
    return a if a == b else (a - b) / (math.log(a) - math.log(b))


# ---------- 场景一：参考拆解（直接从数仓计算） ----------
c = sqlite3.connect(out / "ecommerce.db")
q = lambda s: c.execute(s).fetchall()
tot = {}
for m in ("2026-08", "2026-09"):
    uv = q(f"SELECT SUM(uv) FROM fact_traffic_daily WHERE substr(dt,1,7)='{m}'")[0][0]
    n, g = q(f"SELECT COUNT(*), SUM(pay_amount) FROM fact_orders WHERE substr(dt,1,7)='{m}'")[0]
    days = q(f"SELECT COUNT(DISTINCT dt) FROM fact_traffic_daily WHERE substr(dt,1,7)='{m}'")[0][0]
    tot[m] = dict(uv=uv, orders=n, gmv=round(g, 2), cvr=n / uv, aov=g / n, days=days)
a, s = tot["2026-08"], tot["2026-09"]
lw = L(s["gmv"], a["gmv"])
lmdi = {k: round(lw * math.log(s[k] / a[k]), 2) for k in ("uv", "cvr", "aov")}
by_channel = q("""SELECT ch.channel_name,
  ROUND(SUM(CASE WHEN substr(o.dt,1,7)='2026-08' THEN pay_amount END),2),
  ROUND(SUM(CASE WHEN substr(o.dt,1,7)='2026-09' THEN pay_amount END),2)
  FROM fact_orders o JOIN dim_channel ch USING(channel_id) WHERE o.dt >= '2026-08-01' GROUP BY 1""")
by_cat = q("""SELECT cat.category_name,
  ROUND(SUM(CASE WHEN substr(o.dt,1,7)='2026-08' THEN i.pay_amount END),2),
  ROUND(SUM(CASE WHEN substr(o.dt,1,7)='2026-09' THEN i.pay_amount END),2)
  FROM fact_order_items i JOIN fact_orders o USING(order_id) JOIN dim_product p USING(product_id)
  JOIN dim_category cat USING(category_id) WHERE o.dt >= '2026-08-01' GROUP BY 1""")
by_region = q("""SELECT r.region_name,
  ROUND(SUM(CASE WHEN substr(o.dt,1,7)='2026-08' THEN pay_amount END),2),
  ROUND(SUM(CASE WHEN substr(o.dt,1,7)='2026-09' THEN pay_amount END),2)
  FROM fact_orders o JOIN dim_region r USING(region_id) WHERE o.dt >= '2026-08-01' GROUP BY 1""")
delta = round(s["gmv"] - a["gmv"], 2)
ce = imp_e["causes"]
cause_sum = sum(v["impact_on_mom_change"] for v in ce.values())

E = dict(
    scenario="电商 GMV 下滑归因",
    database="ecommerce.db",
    question="青禾优选 2026 年 9 月 GMV 比 8 月下降了多少？为什么？",
    metric="GMV（SUM(fact_orders.pay_amount)，含已退款订单）",
    base_period="2026-08-01 至 2026-08-31（31 天）",
    current_period="2026-09-01 至 2026-09-30（30 天）",
    actual=dict(base=a, current=s, change=delta, change_rate=round(delta / a["gmv"], 4),
                daily_avg_change_rate=round((s["gmv"] / s["days"]) / (a["gmv"] / a["days"]) - 1, 4)),
    reference_decomposition=dict(
        method="LMDI 乘法分解：GMV = 访客数 × 支付转化率 × 客单价，三项贡献之和等于总变动",
        uv=lmdi["uv"], cvr=lmdi["cvr"], aov=lmdi["aov"], sum=round(sum(lmdi.values()), 2), total_change=delta,
        by_channel={r[0]: dict(base=r[1], current=r[2], change=round(r[2] - r[1], 2)) for r in by_channel},
        by_category={r[0]: dict(base=r[1], current=r[2], change=round(r[2] - r[1], 2)) for r in by_cat},
        by_region={r[0]: dict(base=r[1], current=r[2], change=round(r[2] - r[1], 2)) for r in by_region},
    ),
    root_causes=[
        dict(id="C1", name="信息流广告账户限流", type="内部运营（投放）", rank="主要",
             mechanism="9 月 8 日起信息流广告渠道访客数减半，转化率不变，订单和 GMV 随之减少",
             slice=dict(channel="信息流广告（CH03）", start="2026-09-08", end="2026-09-30"),
             evidence=["fact_traffic_daily 中 CH03 的 uv 自 9/8 起约为此前一半", "event_calendar 中 E05"],
             true_impact=ce["C1_信息流限流"]["impact_on_mom_change"]),
        dict(id="C2", name="三款热销家电断货", type="内部供应链", rank="主要",
             mechanism="9 月 10 日至 25 日三款热销家电（C01-01、C01-02、C01-03）库存为 0，想买的顾客一半放弃、一半改买其他较便宜的家电，家电订单减少、客单价下降",
             slice=dict(category="家电（C01）", products=["C01-01", "C01-02", "C01-03"], start="2026-09-10", end="2026-09-25"),
             evidence=["fact_inventory_daily 中三款商品 is_out_of_stock = 1", "家电 GMV 与客单价在断货期明显下降", "事件日历中没有这条，只能从库存数据发现"],
             true_impact=ce["C2_家电断货"]["impact_on_mom_change"]),
        dict(id="C3", name="8 月 818 大促抬高基期", type="外部业务事件（营销日历）", rank="主要",
             mechanism="8 月 15 日至 18 日的 818 大促使 8 月访客和转化率偏高，9 月没有同等规模的大促，环比自然回落",
             slice=dict(period="2026-08-15 至 2026-08-18"),
             evidence=["event_calendar 中 E02、dim_promotion 中 P02", "8/15-8/18 访客和订单明显高于前后"],
             true_impact=ce["C3_818大促基数"]["impact_on_mom_change"]),
        dict(id="C4", name="9 月 20 日支付通道故障", type="系统故障", rank="次要",
             mechanism="9 月 20 日 21:00 至 23:00 支付通道超时，约八成订单未能完成",
             slice=dict(date="2026-09-20", hours="21:00-23:00"),
             evidence=["fact_orders.pay_time 中 9/20 21 点和 22 点订单骤降", "event_calendar 中 E08"],
             true_impact=ce["C4_支付故障"]["impact_on_mom_change"]),
    ],
    other_effects=dict(
        description="9 月比 8 月少 1 天、开学季活动大部分落在 8 月、中秋礼盒节与国庆预热带来的增量、自然增长和随机波动",
        impact=round(delta - cause_sum, 2)),
    red_herrings=[
        dict(id="R1", name="App 6.2 版本发布（9/3）", why="发布前后各渠道转化率基本不变，与 GMV 下滑无关",
             check="对比 9/3 前后非信息流渠道的转化率"),
        dict(id="R2", name="竞品“优鲜集市”生鲜满减（9/12-9/18）", why="活动期间生鲜在全站 GMV 中的占比没有下降，生鲜下滑幅度与全站一致",
             check="对比活动前后生鲜 GMV 占比，而不是只看生鲜 GMV 绝对值"),
    ],
    derived_phenomena=[
        dict(name="华北区域跌幅最大", explanation="信息流广告的访客中华北占比高（约 35%），华北跌幅大是信息流限流的结果，不是独立原因"),
        dict(name="新客占比下降", explanation="信息流广告带来的新客比例高，限流后新客减少，属于 C1 的派生现象"),
        dict(name="家电客单价下降", explanation="断货期间顾客改买较便宜的家电，属于 C2 的派生现象"),
    ],
)

# ---------- 场景二 ----------
f = sqlite3.connect(out / "fmcg.db")
fq = lambda s: f.execute(s).fetchall()
comp = {}
for m in ("2026-06", "2026-09"):
    rev, = fq(f"SELECT SUM(net_revenue) FROM fact_sales_daily WHERE substr(dt,1,7)='{m}'")[0]
    cogs, = fq(f"""SELECT SUM(s.sales_qty * c.unit_cogs) FROM fact_sales_daily s JOIN fact_sku_cost_monthly c
                 ON c.sku_id = s.sku_id AND c.month = substr(s.dt,1,7) WHERE substr(s.dt,1,7)='{m}'""")[0]
    logi, = fq(f"SELECT SUM(logistics_cost) FROM fact_logistics_daily WHERE substr(dt,1,7)='{m}'")[0]
    comm, = fq(f"SELECT SUM(commission_amount) FROM fact_commission_monthly WHERE month='{m}'")[0]
    mkt, = fq(f"SELECT SUM(amount) FROM fact_marketing_monthly WHERE month='{m}'")[0]
    comp[m] = dict(revenue=round(rev, 2), cogs_rate=cogs / rev, logistics_rate=logi / rev, commission_rate=comm / rev,
                   marketing_rate=mkt / rev, gross_margin=(rev - cogs) / rev, net_margin=(rev - cogs - logi - comm - mkt) / rev)
j, sp = comp["2026-06"], comp["2026-09"]
pp = lambda x: round(x * 100, 3)
decomp = dict(
    method="加法分解：净利率 = 1 − 产品成本率 − 物流费用率 − 佣金率 − 营销费用率，各项费用率变化之和等于净利率变化（单位：百分点）",
    cogs_rate=pp(-(sp["cogs_rate"] - j["cogs_rate"])), logistics_rate=pp(-(sp["logistics_rate"] - j["logistics_rate"])),
    commission_rate=pp(-(sp["commission_rate"] - j["commission_rate"])), marketing_rate=pp(-(sp["marketing_rate"] - j["marketing_rate"])),
    total=pp(sp["net_margin"] - j["net_margin"]))
cf = imp_f["causes"]
F = dict(
    scenario="快消品利润率异动归因",
    database="fmcg.db",
    question="禾悦食品 2026 年 9 月净利率比 6 月下降了多少个百分点？为什么？",
    metric="净利率 =（营收 − 产品成本 − 物流费用 − 渠道佣金 − 营销费用）÷ 营收，贡献利润口径，未分摊管理费用和税费",
    base_period="2026-06", current_period="2026-09",
    actual=dict(jun={k: (round(v, 6) if isinstance(v, float) else v) for k, v in j.items()},
                sep={k: (round(v, 6) if isinstance(v, float) else v) for k, v in sp.items()},
                net_margin_change_pp=pp(sp["net_margin"] - j["net_margin"]),
                gross_margin_change_pp=pp(sp["gross_margin"] - j["gross_margin"])),
    reference_decomposition=decomp,
    root_causes=[
        dict(id="C3", name="社区团购渠道扩张", type="渠道结构", rank="主要",
             mechanism="7 月 15 日起社区团购销量快速增长，营收占比从约 6% 升到约 14%；该渠道供货折扣 35%、佣金 12%、每件配送成本 0.6 元，利润率远低于其他渠道，结构变化拉低整体利润率",
             evidence=["fact_sales_daily 按渠道的营收占比变化", "dim_channel.base_discount_rate、dim_channel_commission、fact_logistics_daily", "event_calendar 中 FE03"],
             true_impact_net_pp=cf["C3_社区团购扩张"]["net_margin_pp"], true_impact_gross_pp=cf["C3_社区团购扩张"]["gross_margin_pp"]),
        dict(id="C4", name="中秋促销让利", type="价格", rank="主要",
             mechanism="9 月 10 日至 25 日坚果零食线（批发除外）额外让利 12%，礼盒主推，折扣率上升、毛利率下降",
             evidence=["fact_sales_daily 中坚果零食线 9 月折扣率明显上升", "event_calendar 中 FE07"],
             true_impact_net_pp=cf["C4_中秋促销让利"]["net_margin_pp"], true_impact_gross_pp=cf["C4_中秋促销让利"]["gross_margin_pp"]),
        dict(id="C1", name="白砂糖采购价上涨 50%", type="成本", rank="主要",
             mechanism="7 月起白砂糖单价由约 6 元/kg 涨到约 9 元/kg，含糖饮料和部分零食的单位成本上升",
             evidence=["fact_material_price_monthly 中 M01", "bom 中各 SKU 的白砂糖用量", "fact_sku_cost_monthly 的单位成本变化", "event_calendar 中 FE02"],
             true_impact_net_pp=cf["C1_白砂糖涨价"]["net_margin_pp"], true_impact_gross_pp=cf["C1_白砂糖涨价"]["gross_margin_pp"]),
        dict(id="C2", name="内容电商平台佣金上调", type="渠道费用", rank="次要",
             mechanism="8 月 1 日起内容电商平台佣金率由 5% 调到 10%，只影响净利率，不影响毛利率",
             evidence=["dim_channel_commission 中 F02 的费率变化", "fact_commission_monthly", "event_calendar 中 FE05"],
             true_impact_net_pp=cf["C2_内容电商佣金上调"]["net_margin_pp"], true_impact_gross_pp=0.0),
        dict(id="C5", name="6 月 618 大促压低基期利润率", type="基期效应（营销日历）", rank="反向因素",
             mechanism="6 月 1 日至 18 日电商渠道额外让利 8%，6 月利润率本身偏低；它让“9 月对比 6 月”的降幅看起来比实际小",
             evidence=["event_calendar 中 FE01", "6 月上半月电商渠道折扣率偏高"],
             true_impact_net_pp=cf["C5_618大促基数"]["net_margin_pp"], true_impact_gross_pp=cf["C5_618大促基数"]["gross_margin_pp"]),
    ],
    other_effects=dict(description="饮料季节性导致的产品线结构变化、自然增长和随机波动",
                       impact_net_pp=round(pp(sp["net_margin"] - j["net_margin"]) - sum(v["net_margin_pp"] for v in cf.values()), 3)),
    red_herrings=[
        dict(id="R1", name="华南物流服务商更换（8/1）", why="合同单价不变；物流费用率 9 月反而低于 6 月。物流总额上升是销量增长所致",
             check="看物流费用率（物流费用 ÷ 营收）或分渠道每件物流成本，而不是物流费用总额"),
        dict(id="R2", name="轻甜气泡水新品上市（9/5）", why="新品利润率与同线产品接近，营收占比很小，对整体利润率影响可以忽略",
             check="计算新品营收占比和单品利润率"),
    ],
    derived_phenomena=[
        dict(name="平均每件物流成本上升", explanation="社区团购每件配送成本最高，其占比上升拉高了平均每件物流成本，属于 C3 的派生现象，不是物流涨价"),
        dict(name="电商渠道营收占比下降", explanation="社区团购增长后其他渠道占比被动下降，并非电商渠道本身变差"),
    ],
)

for name, obj in (("标准答案_电商GMV.json", E), ("标准答案_快消利润率.json", F)):
    (out / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")

# ---------- Markdown ----------
w = lambda x: f"{x / 1e4:,.1f} 万元"
md = ["# 归因分析标准答案", "",
      "> 仅供评测使用。归因 Agent 运行时不得读取本文件和同名 JSON。",
      "> 数据为模拟生成，埋入的原因可逐个关闭重新生成，“真实影响”是关闭该原因前后结果之差。", "",
      "## 场景一：电商 GMV 下滑归因", "",
      f"问题：{E['question']}", "",
      f"- 8 月 GMV {w(a['gmv'])}，9 月 {w(s['gmv'])}，变化 {w(delta)}（{E['actual']['change_rate']:.1%}）；日均 GMV 变化 {E['actual']['daily_avg_change_rate']:.1%}。",
      f"- 参考 LMDI 拆解：访客数 {w(lmdi['uv'])}，支付转化率 {w(lmdi['cvr'])}，客单价 {w(lmdi['aov'])}，合计 {w(sum(lmdi.values()))}。", "",
      "### 埋入的原因", "", "| 原因 | 级别 | 真实影响 | 如何发现 |", "| --- | --- | --- | --- |"]
for r in E["root_causes"]:
    md.append(f"| {r['name']} | {r['rank']} | {w(r['true_impact'])} | {'；'.join(r['evidence'])} |")
md += [f"| 其他因素合计 | — | {w(E['other_effects']['impact'])} | {E['other_effects']['description']} |", "",
       "### 应当排除的干扰项", ""]
md += [f"- {r['name']}：{r['why']}。检验方法：{r['check']}。" for r in E["red_herrings"]]
md += ["", "### 派生现象（不是独立原因）", ""] + [f"- {r['name']}：{r['explanation']}。" for r in E["derived_phenomena"]]
md += ["", "## 场景二：快消品利润率异动归因", "", f"问题：{F['question']}", "",
       f"- 6 月净利率 {j['net_margin']:.2%}，9 月 {sp['net_margin']:.2%}，变化 {F['actual']['net_margin_change_pp']:.2f} 个百分点；毛利率由 {j['gross_margin']:.2%} 变为 {sp['gross_margin']:.2%}。",
       f"- 参考加法拆解（百分点）：产品成本率 {decomp['cogs_rate']}，物流费用率 {decomp['logistics_rate']}，佣金率 {decomp['commission_rate']}，营销费用率 {decomp['marketing_rate']}，合计 {decomp['total']}。", "",
       "### 埋入的原因", "", "| 原因 | 级别 | 对净利率的真实影响 | 对毛利率的真实影响 | 如何发现 |", "| --- | --- | --- | --- | --- |"]
for r in F["root_causes"]:
    md.append(f"| {r['name']} | {r['rank']} | {r['true_impact_net_pp']:+.2f} 个百分点 | {r['true_impact_gross_pp']:+.2f} 个百分点 | {'；'.join(r['evidence'])} |")
md += [f"| 其他因素合计 | — | {F['other_effects']['impact_net_pp']:+.2f} 个百分点 | — | {F['other_effects']['description']} |", "",
       "### 应当排除的干扰项", ""]
md += [f"- {r['name']}：{r['why']}。检验方法：{r['check']}。" for r in F["red_herrings"]]
md += ["", "### 派生现象（不是独立原因）", ""] + [f"- {r['name']}：{r['explanation']}。" for r in F["derived_phenomena"]]
md += ["", "## 评测口径", "",
       "1. 埋入原因命中率：Agent 结论中列出的原因，与上表“主要”“次要”原因方向一致、切片（渠道、类目、时间）正确的，算命中。",
       "2. 伪相关排除率：干扰项被 Agent 检验后标为“已排除”的比例；把派生现象当成独立根因的，记为错误。",
       "3. 量化偏差：Agent 给出的各原因影响与真实影响的相对误差，作为参考指标。"]
(out / "标准答案说明.md").write_text("\n".join(md), encoding="utf-8")
print("\n".join(md[:30]))
