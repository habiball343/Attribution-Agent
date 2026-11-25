"""归因结论与行动项。

输入：目标指标的分解结果、全部假设及其沙箱检验结果、事件日历。
输出：
  causes    被支持的原因，按影响排序，分为 主要 / 次要 / 反向因素 / 口径因素 / 轻微
  residual  总变动 − 已解释部分，记为“其他（自然波动与未识别因素）”
  excluded  被排除的假设（伪相关）及排除依据
  derived   派生现象，以及它从属于哪条原因
  chain     因果证据链：事件 → 机制 → 指标因子 → 影响
  actions   行动项：每条绑定一个可干预的原因，写明责任岗位、动作、恢复周期、预期挽回金额
"""
from datetime import date

from . import config

# 假设类型 → (性质, 影响的指标因子, 机制描述)
NATURE = {
    "traffic_drop": ("内部运营（投放）", "访客数", "渠道访客数下降，转化率和客单价不变，订单随之减少"),
    "stockout": ("内部供应链", "支付转化率 / 客单价", "热销商品断货，部分顾客放弃购买、部分改买更便宜的替代品"),
    "spike": ("营销日历（基期/现期效应）", "访客数 / 转化率", "大促集中在某一周期，抬高该周期的基数"),
    "hourly_drop": ("内部系统", "支付转化率", "故障时段无法完成支付，订单流失"),
    "calendar": ("口径因素", "天数", "两个周期天数不同，总量口径下自然产生差异"),
    "promo_category": ("营销日历（类目活动）", "类目 GMV", "类目活动在两个周期的分布不同"),
    "release": ("内部产品", "支付转化率", "版本发布影响转化"),
    "competitor": ("外部竞争", "类目 GMV", "竞品活动分流"),
    "channel_mix": ("渠道结构（经营策略）", "产品成本率 / 佣金率 / 物流费用率", "低利润率渠道营收占比上升，拉低整体利润率"),
    "discount": ("价格（内部促销）", "产品成本率（价格）", "折扣加大使营收减少而成本不变"),
    "base_discount": ("营销日历（基期效应）", "产品成本率（价格）", "基期大促让利偏高，基期利润率本身偏低"),
    "material_price": ("外部成本（原材料）", "产品成本率（单位成本）", "原材料采购价上涨推高单位成本"),
    "commission": ("外部渠道政策", "佣金率", "平台佣金率上调，只影响净利率"),
    "logistics": ("内部履约", "物流费用率", "物流服务商变更"),
    "new_product": ("内部产品", "产品成本率", "新品利润率与结构"),
}
NON_ACTIONABLE = {"spike", "calendar", "base_discount", "promo_category"}

# 派生现象 → 归属的原因类型
DERIVED_PARENT = {"derived_region": "traffic_drop", "derived_newuser": "traffic_drop", "derived_aov": "stockout",
                  "derived_logistics": "channel_mix", "derived_share": "channel_mix"}

# 行动项模板：(责任岗位, 动作, 恢复周期, 可挽回比例)
ACTIONS = {
    "traffic_drop": ("投放运营经理", "排查 {slice} 账户限流原因（素材审核、账户评级、预算消耗），申诉或换户恢复投放；恢复前把预算临时转到搜索广告承接", "1–2 周", 0.8),
    "stockout": ("供应链计划经理", "对断货商品紧急补货，按近 30 天日销设置安全库存与断货预警，断货期间在商品页推荐同价位替代款", "2–4 周", 0.85),
    "hourly_drop": ("支付技术负责人", "复盘 {start} 支付故障，接入备用支付通道并配置订单量分钟级告警（同比跌 40% 即告警）", "1 周", 1.0),
    "channel_mix": ("渠道总监", "重新评估{slice}的合作条款：供货折扣、佣金、配送费三项与平台复议，设定渠道最低贡献利润率红线后再扩量", "1–2 个月", 0.4),
    "discount": ("品类营销经理", "复盘{slice}促销 ROI，把额外让利改为满赠/组合装，设定单品让利上限", "1 个促销周期", 0.5),
    "material_price": ("采购经理", "与白砂糖供应商签季度锁价协议、引入第二供应商比价，评估配方减糖与小幅提价", "1–3 个月", 0.4),
    "commission": ("电商渠道经理", "与平台谈判佣金阶梯（按 GMV 档位返佣），评估在该渠道的定价与主推款结构", "1–2 个月", 0.3),
    "release": ("App 产品经理", "持续监控发版后转化漏斗", "持续", 0.0),
    "competitor": ("品类运营经理", "跟踪竞品活动节奏，必要时定向发券", "按需", 0.0),
}


def _days(s, e):
    return (date.fromisoformat(e) - date.fromisoformat(s)).days + 1


def _fmt_effect(dom, v):
    if dom.id == "ecommerce":
        return f"{v / 1e4:+,.1f} 万元"
    return f"{v * 100:+.2f} 个百分点"


def conclude(dom, intent, decomp, hyps):
    total = decomp["change"]
    sign = 1 if total >= 0 else -1
    causes, excluded, derived, failed = [], [], [], []
    for h in hyps:
        r = h.get("result")
        if not r:
            failed.append(h)
            continue
        if r["verdict"] == "派生":
            derived.append(h)
        elif r["verdict"] == "支持" and r["confidence"] >= config.CONFIDENCE_THRESHOLD:
            causes.append(h)
        else:
            excluded.append(h)
    explained = 0.0
    out = []
    for h in sorted(causes, key=lambda h: -abs(h["result"]["effect"])):
        eff = h["result"]["effect"]
        share = eff / total if total else 0
        if h["type"] == "calendar":
            rank = "口径因素"
        elif eff * sign < 0:
            rank = "反向因素"
        elif abs(share) >= config.MAJOR_SHARE:
            rank = "主要"
        elif abs(share) >= config.MINOR_SHARE:
            rank = "次要"
        else:
            rank = "轻微"
        nature, factor, mech = NATURE.get(h["type"], ("其他", "—", h.get("signal", "")))
        evs = h.get("events") or []
        external = any(e.get("external") for e in evs) or nature.startswith("外部")
        calendar_like = h["type"] in NON_ACTIONABLE
        explained += eff
        out.append(dict(id=h["id"], title=h["title"], type=h["type"], rank=rank, effect=eff, share=share, effect_text=_fmt_effect(dom, eff),
                        confidence=h["result"]["confidence"], p_value=h["result"].get("p_value"), nature=nature,
                        scope="日历与口径" if calendar_like else "外部诱因" if external else "内部原因", factor=factor, mechanism=mech, finding=h["result"]["finding"],
                        events=evs, slice=h.get("slice", ""), start=h.get("start"), end=h.get("end"), source=h.get("source", "检查库")))
    residual = total - explained
    rank_order = {"主要": 0, "次要": 1, "轻微": 2, "反向因素": 3, "口径因素": 4}
    out.sort(key=lambda c: (rank_order[c["rank"]], -abs(c["effect"])))
    by_type = {}
    for c in out:
        by_type.setdefault(c["type"], c)
    der = []
    for h in derived:
        parent = by_type.get(DERIVED_PARENT.get(h["type"], ""))
        phen = h["title"]
        for suf in ("是否独立原因", "是否物流涨价", "是否该渠道变差"):
            phen = phen.replace(suf, "")
        der.append(dict(id=h["id"], title=h["title"], phenomenon=phen, finding=h["result"]["finding"], parent=parent["id"] if parent else None,
                        parent_title=parent["title"] if parent else None))
    exc = [dict(id=h["id"], title=h["title"], group=h["group"], finding=h["result"]["finding"], confidence=h["result"]["confidence"],
                p_value=h["result"].get("p_value"), events=h.get("events") or []) for h in excluded]
    chain = []
    for c in out:
        if c["rank"] in ("轻微",):
            continue
        ev = c["events"][0] if c["events"] else None
        chain.append(dict(cause=c["id"], title=c["title"], rank=c["rank"],
                          event=f"{ev['event_id']} {ev['title']}（{ev['start']}{'' if ev['end'] == ev['start'] else ' 至 ' + ev['end']}，{ev['event_type']}）" if ev else "事件日历无记录（数据扫描发现）",
                          external=c["scope"] == "外部诱因", mechanism=c["mechanism"], factor=c["factor"], effect=c["effect_text"],
                          confidence=c["confidence"], relevance=ev["relevance"] if ev else None))
    major = [c for c in out if c["rank"] == "主要"]
    minor = [c for c in out if c["rank"] == "次要"]
    rev = [c for c in out if c["rank"] in ("反向因素",)]
    unit = (lambda v: f"{v / 1e4:,.1f} 万元") if dom.id == "ecommerce" else (lambda v: f"{v * 100:.2f} 个百分点")
    change_txt = (f"{decomp['name']}从 {decomp['v0'] / 1e4:,.1f} 万元变为 {decomp['v1'] / 1e4:,.1f} 万元，变动 {total / 1e4:+,.1f} 万元（{decomp['change_rate']:+.1%}）"
                  if dom.id == "ecommerce" else
                  f"{decomp['name']}从 {decomp['v0']:.2%} 变为 {decomp['v1']:.2%}，变动 {total * 100:+.2f} 个百分点")
    headline = change_txt + "。" + (
        "首要原因：" + "；".join(f"{c['title']}（{c['effect_text']}）" for c in major) + "。" if major else "") + (
        "次要原因：" + "；".join(f"{c['title']}（{c['effect_text']}）" for c in minor) + "。" if minor else "") + (
        "反向因素：" + "；".join(f"{c['title']}（{c['effect_text']}）" for c in rev) + "。" if rev else "")
    return dict(total=total, total_text=_fmt_effect(dom, total), causes=out, residual=residual, residual_text=_fmt_effect(dom, residual),
                explained_share=explained / total if total else 0, excluded=exc, derived=der, chain=chain, headline=headline,
                failed=[dict(id=h["id"], title=h["title"], error=h.get("error")) for h in failed],
                internal=[c["id"] for c in out if c["scope"] == "内部原因" and c["rank"] in ("主要", "次要")],
                external=[c["id"] for c in out if c["scope"] == "外部诱因" and c["rank"] in ("主要", "次要")])


def actions(dom, intent, decomp, concl):
    """每条行动绑定一个可干预的原因。预期挽回金额按“影响 ÷ 影响天数 × 30 天 × 可挽回比例”折成月度金额。"""
    p1 = intent["current"]
    out = []
    revenue = decomp["m1"].get("revenue") if dom.id == "fmcg" else None
    for c in concl["causes"]:
        if c["type"] in NON_ACTIONABLE or c["rank"] in ("反向因素", "口径因素") or c["type"] not in ACTIONS:
            continue
        if ACTIONS[c["type"]][3] <= 0:
            continue
        role, act, period, ratio = ACTIONS[c["type"]]
        slice_name = c["slice"].split("；")[0].split("=")[-1]
        act = act.format(slice=slice_name, start=c.get("start") or "")
        if dom.id == "ecommerce":
            s, e = c.get("start") or p1["start"], c.get("end") or p1["end"]
            s, e = max(s, p1["start"]), min(e, p1["end"])
            days = max(_days(s, e), 1)
            if c["type"] == "hourly_drop":
                monthly = abs(c["effect"])
                basis = "一次性损失，整改后避免再次发生"
            else:
                monthly = abs(c["effect"]) / days * 30
                basis = f"影响 {days} 天共 {abs(c['effect']) / 1e4:,.1f} 万元，折合每月 {monthly / 1e4:,.1f} 万元"
            amount = monthly * ratio
            amount_text = f"{amount / 1e4:,.1f} 万元/月"
        else:
            amount = abs(c["effect"]) * revenue * ratio
            monthly = abs(c["effect"]) * revenue
            basis = f"影响 {abs(c['effect']) * 100:.2f} 个百分点 × 现期营收 {revenue / 1e4:,.1f} 万元 = {monthly / 1e4:,.1f} 万元/月"
            amount_text = f"{amount / 1e4:,.1f} 万元/月（{decomp['name']}约 +{abs(c['effect']) * ratio * 100:.2f} 个百分点）"
        out.append(dict(cause=c["id"], cause_title=c["title"], priority="P0" if c["rank"] == "主要" else "P1", owner=role, action=act,
                        period=period, recover_ratio=ratio, amount=amount, amount_text=amount_text, basis=basis + f"，预计可挽回 {ratio:.0%}",
                        driver=c["factor"]))
    out.sort(key=lambda a: (a["priority"], -a["amount"]))
    return out
