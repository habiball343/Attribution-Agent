"""指标异动大盘：核心指标卡片（月环比、日环比、是否超阈值）、日趋势、异动清单，以及一键发起归因的问题模板。"""
import numpy as np

from . import db
from .domains import DOMAINS, month_period
from .signals import ecommerce_signals, fmcg_signals


def _q(name, m_cur, m_base, down):
    return f"{int(m_cur[5:])} 月{name}比 {int(m_base[5:])} 月{'下降' if down else '上升'}了多少？为什么？"


def build(domain):
    dom = DOMAINS[domain]
    th_month = db.get_setting("dash_month_threshold", 0.05)
    th_day = db.get_setting("dash_day_threshold", 0.2)
    months = dom.periods_available()
    cur, prev = months[-1], months[-2]
    p1, p0 = month_period(cur), month_period(prev)
    cards = []
    if domain == "ecommerce":
        m1, m0 = dom.measures(p1), dom.measures(p0)
        d = dom.kpi_daily()
        d["cvr"] = d.orders / d.uv
        d["aov"] = d.gmv / d.orders
        last, before = d.iloc[-1], d.iloc[-2]
        for key, name, unit in dom.kpis():
            col = "orders" if key == "orders" else key
            v1, v0 = m1[key], m0[key]
            mom = v1 / v0 - 1 if v0 else 0
            if key in ("gmv", "uv", "orders"):  # 总量指标按日均比，剔除天数差异
                mom = (v1 / m1["days"]) / (v0 / m0["days"]) - 1
            dod = last[col] / before[col] - 1 if before[col] else 0
            # 日环比异常：最近 30 天内单日较前一日的最大跌幅
            tail = d.tail(30)
            chg = tail[col].pct_change()
            worst = chg.idxmin()
            flag = mom <= -th_month or chg.min() <= -th_day
            cards.append(dict(key=key, name=name, unit=unit, value=float(v1), base=float(v0), mom=float(mom), mom_basis="日均" if key in ("gmv", "uv", "orders") else "",
                              last_day=d.dt.iloc[-1], dod=float(dod), worst_day=d.dt[worst], worst_dod=float(chg.min()), flag=bool(flag),
                              question=_q(name, cur, prev, mom < 0) if key != "gmv" else dom.default_question,
                              spark=dict(x=list(d.dt), y=[float(v) for v in d[col]])))
        sig, _ = ecommerce_signals(dom, p0, p1)
    else:
        mm = dom.monthly()
        d = dom.kpi_daily()
        for key, name, unit in [("net_margin", "净利率", "%"), ("gross_margin", "毛利率", "%"), ("revenue", "营收", "元"), ("discount_rate", "折扣率", "%")]:
            v1 = float(mm[mm.month == cur][key].iloc[0])
            v0 = float(mm[mm.month == prev][key].iloc[0])
            ratio = unit == "%"
            mom = (v1 - v0) if ratio else (v1 / v0 - 1)
            down_bad = key != "discount_rate"
            flag = (mom <= -0.01 if down_bad else mom >= 0.01) if ratio else mom <= -th_month
            peak = mm[key].max() if down_bad else mm[key].min()
            vs_best = v1 - peak if ratio else v1 / peak - 1
            q = dom.default_question if key == "net_margin" else _q(name, cur, prev, mom < 0)
            if key == "gross_margin":
                q = "9 月毛利率比 6 月下降了多少？为什么？"
            cards.append(dict(key=key, name=name, unit=unit, value=v1, base=v0, mom=mom, mom_basis="百分点" if ratio else "", ratio=ratio,
                              vs_best=float(vs_best), flag=bool(flag or (ratio and down_bad and vs_best <= -0.02)), question=q,
                              spark=dict(x=list(mm.month), y=[float(v) for v in mm[key]])))
        sig, _ = fmcg_signals(dom, month_period("2026-06"), p1)
    anomalies = [dict(type=s["type"], text=s["text"], start=s.get("start") or s.get("date") or s.get("month"), level="高" if s["type"] in ("traffic_shift", "stockout", "channel_mix", "spike") else "中")
                 for s in sig]
    if domain == "ecommerce":
        d = dom.kpi_daily()
        series = dict(x=list(d.dt), gmv=[float(v) for v in d.gmv], uv=[float(v) for v in d.uv], orders=[float(v) for v in d.orders])
    else:
        d = dom.kpi_daily()
        series = dict(x=list(d.dt), revenue=[float(v) for v in d.revenue], gross_margin=[float(v) for v in d.gross_margin], discount_rate=[float(v) for v in d.discount_rate])
    events = [dict(event_id=e["event_id"], title=e["title"], start=e["start_date"], end=e["end_date"], event_type=e["event_type"]) for e in dom.event_records(custom_events())]
    return dict(domain=domain, title=dom.title, current=cur, base=prev, cards=cards, anomalies=anomalies, series=series, events=events,
                thresholds=dict(month=th_month, day=th_day), default_question=dom.default_question)


def custom_events():
    return [dict(r, event_id=r["event_id"] or f"U{r['id']}") for r in db.q("SELECT * FROM custom_events ORDER BY start_date")]
