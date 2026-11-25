"""候选假设生成与事件关联。

来源有三类，分组展示：
  归因假设    异动扫描发现的信号（渠道流量突变、断货、基期尖峰、小时级骤降、渠道结构、折扣、原料价、佣金）
  事件检验    事件日历中落在分析窗口内、但没有对应数据信号的事件（发版、竞品活动、物流调整、新品等），检验它是否真有影响
  派生现象检验 下钻中最显眼的现象（跌幅最大的区域、新客占比下降、类目客单价下降、物流单件成本上升、渠道占比下降），检验它是不是其他原因的结果
在线模式下，大模型还可以再补充自定义假设并自己写检验代码（见 agent.py）。
"""
from datetime import date

from . import checks as C
from .config import EVENT_WINDOW_DAYS
from .domains import shift_date

# 假设类型 → 可以关联的事件类型
EVENT_TYPES = {
    "traffic_drop": {"投放调整", "竞品动态", "渠道政策"},
    "spike": {"营销活动"},
    "hourly_drop": {"系统故障"},
    "stockout": {"供应链", "物流调整", "采购成本"},
    "channel_mix": {"渠道拓展", "渠道政策"},
    "discount": {"营销活动"},
    "base_discount": {"营销活动"},
    "material_price": {"采购成本"},
    "commission": {"渠道政策"},
}
EXTERNAL_EVENT_TYPES = {"竞品动态", "宏观", "政策变化", "采购成本", "渠道政策"}


def _overlap(a0, a1, b0, b1):
    lo, hi = max(a0, b0), min(a1, b1)
    if lo > hi:
        return 0.0
    from datetime import date
    span = lambda x, y: (date.fromisoformat(y) - date.fromisoformat(x)).days + 1
    return span(lo, hi) / span(min(a0, b0), max(a1, b1))


STEP_TYPES = {"traffic_drop", "commission"}   # 阶跃型异动：事件开始日要落在突变点前后 window 天内


def match_events(h, events, window=EVENT_WINDOW_DAYS):
    """匹配规则：
    1. 时间：阶跃型异动（流量突变、费率调整）要求事件开始日在突变点前后 window 天内；区间型异动要求事件与 [开始 − window, 结束 + window] 重叠
    2. 类型：事件类型与假设类型相容（见 EVENT_TYPES）
    3. 切片：事件标注了渠道/类目/产品线时，必须和假设的切片一致；没标注的视为全局事件
    相关度 = 0.5 × 时间重合度 + 0.5 × 切片重合度（切片一致 1，全局事件 0.5）。"""
    out = []
    if not h.get("start"):
        return out
    s, e = shift_date(h["start"], -window), shift_date(h.get("end") or h["start"], window)
    allowed = EVENT_TYPES.get(h["type"], set())
    for ev in events:
        if ev["event_type"] not in allowed:
            continue
        if h["type"] in STEP_TYPES:
            if not (shift_date(h["start"], -window) <= ev["start_date"] <= shift_date(h["start"], window)):
                continue
        elif ev["start_date"] > e or ev["end_date"] < s:
            continue
        ev_slices = [k for k in ("channel_id", "category_id", "product_line") if ev.get(k)]
        if ev_slices:
            if not any(h.get(k) and h[k] == ev[k] for k in ev_slices):
                continue
            slice_score = 1.0
        else:
            slice_score = 0.5
        time_score = max(_overlap(h["start"], h.get("end") or h["start"], ev["start_date"], ev["end_date"]), 0.2)
        out.append(dict(event_id=ev["event_id"], title=ev["title"], event_type=ev["event_type"], start=ev["start_date"], end=ev["end_date"],
                        description=ev.get("description", ""), relevance=round(0.5 * time_score + 0.5 * slice_score, 2),
                        external=ev["event_type"] in EXTERNAL_EVENT_TYPES))
    return sorted(out, key=lambda x: -x["relevance"])


def _events_in(events, p0, p1):
    s, e = shift_date(p0["start"], -EVENT_WINDOW_DAYS), shift_date(p1["end"], EVENT_WINDOW_DAYS)
    return [ev for ev in events if ev["start_date"] <= e and ev["end_date"] >= s]


def build_ecommerce(dom, intent, signals, spike_days, decomp, drills, events):
    p0, p1 = intent["base"], intent["current"]
    total = decomp["change"]
    H = []
    shift_channels = [s for s in signals if s["type"] == "traffic_shift"]
    excl = sorted(set(spike_days))
    for s in signals:
        if s["type"] == "traffic_shift":
            H.append(dict(group="归因假设", type="traffic_drop", title=f"{s['channel_name']}访客数自 {s['start'][5:]} 起骤降", channel_id=s["channel_id"],
                          slice=f"渠道={s['channel_name']}；{s['start']} 起", start=s["start"], end=s["end"], signal=s["text"],
                          code=C.fill(C.TRAFFIC_DROP, total=total, ch=s["channel_id"], cname=s["channel_name"], tau=s["start"], end=s["end"], exclude=excl)))
        elif s["type"] == "stockout":
            H.append(dict(group="归因假设", type="stockout", title=f"{s['category_name']}热销商品断货", category_id=s["category_id"],
                          slice=f"类目={s['category_name']}；{len(s['products'])} 个商品；{s['start']} 至 {s['end']}", start=s["start"], end=s["end"], signal=s["text"],
                          code=C.fill(C.STOCKOUT, total=total, skus=s["products"], cat=s["category_id"], catname=s["category_name"], n=len(s["products"]),
                                      s=s["start"], e=s["end"], exclude=excl)))
        elif s["type"] == "hourly_drop":
            H.append(dict(group="归因假设", type="hourly_drop", title=f"{s['date'][5:]} {'、'.join(map(str, s['hours']))} 点订单骤降", slice=f"时间={s['date']} {s['hours']} 点",
                          start=s["date"], end=s["date"], signal=s["text"],
                          code=C.fill(C.HOURLY_DROP, total=total, date=s["date"], hours=s["hours"], p_start=p1["start"], p_end=p1["end"])))
        elif s["type"] == "spike":
            # 尖峰日与营销活动对上时，用活动的完整起止日期
            ev = [e for e in events if e["event_type"] == "营销活动" and e["start_date"] <= s["end"] and e["end_date"] >= s["start"] and not e.get("category_id")]
            st, en = (max(ev[0]["start_date"], (p0 if s["period"] == "base" else p1)["start"]), min(ev[0]["end_date"], (p0 if s["period"] == "base" else p1)["end"])) if ev else (s["start"], s["end"])
            title = ev[0]["title"] if ev else "日 GMV 尖峰"
            if s["period"] == "base":
                H.append(dict(group="归因假设", type="spike", title=f"基期“{title}”抬高了基期", slice=f"基期 {st} 至 {en}", start=st, end=en, signal=s["text"],
                              code=C.fill(C.BASE_SPIKE, total=total, s=st, e=en, p_start=p0["start"], p_end=p0["end"], title=title)))
            elif ev:  # 现期活动带来的增量：反向因素，和基期尖峰用同一套检验，符号相反
                code = C.fill(C.BASE_SPIKE, total=total, s=st, e=en, p_start=p1["start"], p_end=p1["end"], title=title)
                code = code.replace("effect = -excess", "effect = excess").replace("基期因此多出", "现期因此多出").replace("抬高了基期，使环比降幅看起来更大", "带来现期增量，部分抵消了下滑")
                H.append(dict(group="归因假设", type="spike", title=f"现期“{title}”带来增量", slice=f"现期 {st} 至 {en}", start=st, end=en, signal=s["text"], code=code))
    from .domains import days_of
    d0, d1 = days_of(p0), days_of(p1)
    if d0 != d1:
        H.append(dict(group="归因假设", type="calendar", kind="口径", title=f"现期比基期{'少' if d1 < d0 else '多'} {abs(d1 - d0)} 天（口径因素）", slice="日历天数",
                      start=None, end=None, signal=f"基期 {d0} 天，现期 {d1} 天",
                      code=C.fill(C.CALENDAR, total=total, p0s=p0["start"], p0e=p0["end"], d0=d0, d1=d1, exclude=sorted(set(spike_days)))))
    used_events = set()
    for h in H:
        h["events"] = match_events(h, events)
        used_events |= {e["event_id"] for e in h["events"]}
    # 事件检验：窗口内、没被信号认领的事件
    excl_ch = [s["channel_id"] for s in shift_channels]
    promo_days = sorted(set(spike_days) | {shift_date(e["start_date"], i) for e in events if e["event_type"] == "营销活动"
                                            for i in range((date.fromisoformat(e["end_date"]) - date.fromisoformat(e["start_date"])).days + 1)})
    excl_names = "、".join(s["channel_name"] for s in shift_channels) or "无"
    cats = dict(dom.q("SELECT category_id, category_name FROM dim_category").values)
    for ev in _events_in(events, p0, p1):
        if ev["event_id"] in used_events:
            continue
        if ev["event_type"] == "产品发布":
            H.append(dict(group="事件检验", type="release", title=f"“{ev['title']}”影响转化率", slice=f"{ev['start_date']} 前后 7 天", start=ev["start_date"], end=ev["end_date"],
                          signal=f"事件日历：{ev['title']}（{ev['description']}）", events=[dict(event_id=ev["event_id"], title=ev["title"], event_type=ev["event_type"], start=ev["start_date"],
                          end=ev["end_date"], description=ev["description"], relevance=1.0, external=False)],
                          code=C.fill(C.RELEASE_EFFECT, total=total, date=ev["start_date"], title=ev["title"], excl=excl_ch, excl_names=excl_names, promo_days=promo_days)))
        elif ev["event_type"] == "营销活动" and ev.get("category_id"):
            H.append(dict(group="事件检验", type="promo_category", title=f"类目活动“{ev['title']}”的增量", category_id=ev["category_id"],
                          slice=f"类目={cats.get(ev['category_id'], '')}；{ev['start_date']} 至 {ev['end_date']}", start=ev["start_date"], end=ev["end_date"],
                          signal=f"事件日历：{ev['title']}（{ev['description']}）", events=[dict(event_id=ev["event_id"], title=ev["title"], event_type=ev["event_type"],
                          start=ev["start_date"], end=ev["end_date"], description=ev["description"], relevance=1.0, external=False)],
                          code=C.fill(C.PROMO_CATEGORY, total=total, cat=ev["category_id"], catname=cats.get(ev["category_id"], ""), s=ev["start_date"], e=ev["end_date"],
                                      title=ev["title"], p0s=p0["start"], p0e=p0["end"], p1s=p1["start"], p1e=p1["end"], exclude=excl)))
        elif ev["event_type"] == "竞品动态" and ev.get("category_id"):
            H.append(dict(group="事件检验", type="competitor", title=f"“{ev['title']}”分流{cats.get(ev['category_id'], '')}", category_id=ev["category_id"],
                          slice=f"类目={cats.get(ev['category_id'], '')}；{ev['start_date']} 至 {ev['end_date']}", start=ev["start_date"], end=ev["end_date"],
                          signal=f"事件日历：{ev['title']}（{ev['description']}）", events=[dict(event_id=ev["event_id"], title=ev["title"], event_type=ev["event_type"],
                          start=ev["start_date"], end=ev["end_date"], description=ev["description"], relevance=1.0, external=True)],
                          code=C.fill(C.COMPETITOR, total=total, cat=ev["category_id"], catname=cats.get(ev["category_id"], ""), s=ev["start_date"], e=ev["end_date"], title=ev["title"])))
    # 派生现象检验
    region = next((d for d in drills if d["dim"] == "region"), None)
    if region and shift_channels:
        worst = min(region["rows"], key=lambda r: r["change_rate"] or 0)
        H.append(dict(group="派生现象检验", type="derived_region", title=f"{worst['key']}跌幅最大是否独立原因", slice=f"区域={worst['key']}", signal=f"{worst['key']} GMV {worst['change_rate']:+.1%}，跌幅最大",
                      code=C.fill(C.REGION_DERIVED, total=total, region=worst["key"], cname=shift_channels[0]["channel_name"], ch=shift_channels[0]["channel_id"],
                                  p0s=p0["start"], p0e=p0["end"], p1s=p1["start"], p1e=p1["end"])))
    if shift_channels and decomp["m1"]["new_user_share"] < decomp["m0"]["new_user_share"] - 0.005:
        H.append(dict(group="派生现象检验", type="derived_newuser", title="新客占比下降是否独立原因", slice="新老客",
                      signal=f"新客占比 {decomp['m0']['new_user_share']:.1%} → {decomp['m1']['new_user_share']:.1%}",
                      code=C.fill(C.NEWUSER_DERIVED, total=total, cname=shift_channels[0]["channel_name"], ch=shift_channels[0]["channel_id"],
                                  p0s=p0["start"], p0e=p0["end"], p1s=p1["start"], p1e=p1["end"])))
    for s in signals:
        if s["type"] == "stockout":
            H.append(dict(group="派生现象检验", type="derived_aov", title=f"{s['category_name']}客单价下降是否独立原因", slice=f"类目={s['category_name']}",
                          signal=f"{s['category_name']}客单价在断货期下降",
                          code=C.fill(C.CAT_AOV_DERIVED, total=total, skus=s["products"], cat=s["category_id"], catname=s["category_name"], s=s["start"], e=s["end"])))
    return _finish(H)


def build_fmcg(dom, intent, signals, spike_days, decomp, drills, events):
    p0, p1 = intent["base"], intent["current"]
    total = decomp["change"]
    kw = dict(total=total, p0s=p0["start"], p0e=p0["end"], p1s=p1["start"], p1e=p1["end"], metric=intent["metric"], metricname=intent["metric_name"])
    # 常态月份：剔除基期、现期，以及有 7 天以上营销活动的月份
    def promo_heavy(m):
        days = 0
        for e in events:
            if e["event_type"] == "营销活动":
                lo, hi = max(e["start_date"], m + "-01"), min(e["end_date"], m + "-31")
                if lo <= hi:
                    days += int(hi[8:10]) - int(lo[8:10]) + 1
        return days >= 7
    normal = [m for m in dom.periods_available() if m not in (p0["start"][:7], p1["start"][:7]) and not promo_heavy(m)]
    H = []
    mix_up = [s for s in signals if s["type"] == "channel_mix" and s["share1"] > s["share0"] and s["margin1"] < decomp["v1"]]
    for s in signals:
        if s in mix_up:
            H.append(dict(group="归因假设", type="channel_mix", title=f"{s['channel_name']}占比上升拉低利润率", channel_id=s["channel_id"],
                          slice=f"渠道={s['channel_name']}", start=p0["start"], end=p1["end"], signal=s["text"],
                          code=C.fill(C.CHANNEL_MIX, ch=s["channel_id"], cname=s["channel_name"], **kw)))
        elif s["type"] == "discount":
            H.append(dict(group="归因假设", type="discount", title=f"{s['product_line']}加大让利", product_line=s["product_line"],
                          slice=f"产品线={s['product_line']}", start=p1["start"], end=p1["end"], signal=s["text"],
                          code=C.fill(C.DISCOUNT_LINE, line=s["product_line"], ref=normal or [p0["start"][:7]], **kw)))
        elif s["type"] == "base_discount":
            ev = [e for e in events if e["event_type"] == "营销活动" and e["start_date"][:7] == s["month"]]
            H.append(dict(group="归因假设", type="base_discount", title=f"基期“{ev[0]['title'] if ev else '大促'}”压低了基期利润率", slice=f"基期 {s['month']}",
                          start=p0["start"], end=p0["end"], signal=s["text"],
                          code=C.fill(C.BASE_DISCOUNT, month=s["month"], neighbors=s["neighbors"], title=ev[0]["title"] if ev else "大促", **kw)))
        elif s["type"] == "material_price":
            H.append(dict(group="归因假设", type="material_price", title=f"{s['material_name']}采购价上涨", slice=f"原材料={s['material_name']}",
                          start=p1["start"], end=p1["end"], signal=s["text"],
                          code=C.fill(C.MATERIAL_PRICE, mid=s["material_id"], mname=s["material_name"], **kw)))
        elif s["type"] == "commission":
            H.append(dict(group="归因假设", type="commission", title=f"{s['channel_name']}佣金率上调", channel_id=s["channel_id"], slice=f"渠道={s['channel_name']}；{s['start']} 起",
                          start=s["start"], end=p1["end"], signal=s["text"],
                          code=C.fill(C.COMMISSION, ch=s["channel_id"], cname=s["channel_name"], start=s["start"], r0=s["rate0"], r1=s["rate1"], **kw)))
    used = set()
    for h in H:
        h["events"] = match_events(h, events)
        if h["type"] == "material_price":  # 采购成本事件按标题里的原料名匹配
            h["events"] = [e for e in h["events"] if any(w in e["title"] for w in [h["slice"].split("=")[1][:2]])] or h["events"]
        used |= {e["event_id"] for e in h["events"]}
    skus = dom.q("SELECT sku_id, sku_name, product_line, launch_date FROM dim_sku")
    for ev in _events_in(events, p0, p1):
        if ev["event_id"] in used:
            continue
        evref = [dict(event_id=ev["event_id"], title=ev["title"], event_type=ev["event_type"], start=ev["start_date"], end=ev["end_date"],
                      description=ev["description"], relevance=1.0, external=ev["event_type"] in EXTERNAL_EVENT_TYPES)]
        if ev["event_type"] == "物流调整":
            H.append(dict(group="事件检验", type="logistics", title=f"“{ev['title']}”抬高物流成本", slice=f"{ev['start_date']} 前后", start=ev["start_date"], end=ev["end_date"],
                          signal=f"事件日历：{ev['title']}（{ev['description']}）", events=evref,
                          code=C.fill(C.LOGISTICS_VENDOR, title=ev["title"], date=ev["start_date"], **kw)))
        elif ev["event_type"] == "新品上市":
            new = skus[(skus.launch_date == ev["start_date"])]
            if len(new):
                r = new.iloc[0]
                H.append(dict(group="事件检验", type="new_product", title=f"新品“{r.sku_name}”拉低利润率", slice=f"SKU={r.sku_name}", start=ev["start_date"], end=ev["end_date"],
                              signal=f"事件日历：{ev['title']}", events=evref,
                              code=C.fill(C.NEW_PRODUCT, sku=r.sku_id, skuname=r.sku_name, date=ev["start_date"], **kw)))
    for s in mix_up:
        H.append(dict(group="派生现象检验", type="derived_logistics", title="平均每件物流成本上升是否物流涨价", slice="物流",
                      signal="平均每件物流成本上升", code=C.fill(C.LOGI_UNIT_DERIVED, ch=s["channel_id"], cname=s["channel_name"], **kw)))
        break
    for s in signals:
        if s["type"] == "channel_mix" and s["share1"] < s["share0"]:
            H.append(dict(group="派生现象检验", type="derived_share", title=f"{s['channel_name']}占比下降是否该渠道变差", slice=f"渠道={s['channel_name']}",
                          signal=s["text"], code=C.fill(C.SHARE_DERIVED, ch=s["channel_id"], cname=s["channel_name"], normal=normal, **kw)))
    return _finish(H)


def _finish(H):
    for i, h in enumerate(H, 1):
        h["id"] = f"H{i}"
        h.setdefault("events", [])
        h.setdefault("source", "检查库")
    return H


BUILDERS = {"ecommerce": build_ecommerce, "fmcg": build_fmcg}
