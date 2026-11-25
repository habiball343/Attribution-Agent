"""归因 Agent：用 LangGraph 编排分析流程和多轮追问。

图结构
  START → route ─┬─ analyze → understand → detect → decompose → drill → hypothesize → verify → events → advise → narrate → END
                 ├─ focus（锁定切片继续下钻）→ END
                 ├─ whatif（按问题里的参数推演）→ END
                 ├─ query（自然语言查数，走 SQL 模型）→ END
                 └─ explain（解释某条结论/假设的依据）→ END
第一轮一定走 analyze；之后的追问由 route 判断走哪条分支，分析上下文 A（意图、分解、下钻、假设与检验结果、结论、焦点切片）
在会话里保存，追问在它上面继续。每个节点通过 emit 把进度和结果流式推给前端。
"""
import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TypedDict

import numpy as np
from langgraph.graph import END, START, StateGraph

from . import conclude as CC
from . import config as settings
from . import drill, llm, sandbox, sqltool, whatif
from .domains import DOMAINS, METRIC_ALIASES, month_period, parse_intent
from .hypotheses import BUILDERS
from .signals import ecommerce_signals, fmcg_signals
from .sql_prompt import ECOMMERCE_SCHEMA, FMCG_SCHEMA

SIGNALS = {"ecommerce": ecommerce_signals, "fmcg": fmcg_signals}
STEPS = [("understand", "意图理解"), ("detect", "识别异动"), ("decompose", "指标拆解"), ("drill", "维度下钻"), ("hypothesize", "假设生成"),
         ("verify", "统计检验"), ("events", "业务归因"), ("advise", "策略建议"), ("narrate", "结论撰写")]


def clean(o):
    """numpy / NaN → JSON 可序列化。"""
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.ndarray):
        return clean(o.tolist())
    return o


class State(TypedDict, total=False):
    question: str
    domain: str
    A: dict
    route: str
    args: dict


def _emit(config):
    return (config.get("configurable") or {}).get("emit") or (lambda *a, **k: None)


class _Step:
    def __init__(self, emit, A, key):
        self.emit, self.A, self.key = emit, A, key
        self.name = dict(STEPS).get(key, key)

    def __enter__(self):
        self.t = time.time()
        self.emit("step", key=self.key, name=self.name, status="running")
        return self

    def done(self, summary):
        self.summary = summary

    def __exit__(self, et, ev, tb):
        ms = int((time.time() - self.t) * 1000)
        status = "error" if et else "done"
        summary = getattr(self, "summary", "") if not et else f"{et.__name__}: {ev}"
        self.A.setdefault("trace", []).append(dict(step=self.key, name=self.name, ms=ms, status=status, summary=summary))
        self.emit("step", key=self.key, name=self.name, status=status, ms=ms, summary=summary)
        return False


# ============================================================ 路由

WHATIF_RE = re.compile(r"如果|假如|要是|模拟|推演|what.?if|会怎样|会怎么样|能挽回|能回到|能恢复多少", re.I)
EXPLAIN_RE = re.compile(r"排除|为什么不是|为什么不算|怎么判断|依据|置信|可信|证据|怎么算的|口径|派生")
QUERY_RE = re.compile(r"查一下|查询|列出|明细|排名|top\s*\d|前\s*\d+|每天|每日|按天|有多少|是多少|各.{1,8}的|分别|看一下.{0,10}数据", re.I)
DRILL_RE = re.compile(r"继续|再往下|深挖|细分|拆开|具体|为什么.*(掉|降|跌|少|低)|原因")


def _route_rules(A, q):
    dom = DOMAINS[A["intent"]["domain"]]
    months = re.findall(r"(\d{1,2})\s*月", q)
    if len(months) >= 2 or (months and re.search(r"比|对比|环比|同比", q)):
        it = parse_intent(q, dom.id if not _other_domain(q, dom.id) else None)
        if it["base"] != A["intent"]["base"] or it["current"] != A["intent"]["current"] or it["metric"] != A["intent"]["metric"] or it["domain"] != dom.id:
            return "analyze", {}
    if _other_domain(q, dom.id):
        return "analyze", {}
    if WHATIF_RE.search(q):
        return "whatif", {}
    if re.search(r"\bH\d+\b", q, re.I) or EXPLAIN_RE.search(q):
        return "explain", {}
    if QUERY_RE.search(q) and "为什么" not in q:
        return "query", {}
    sl = drill.resolve_slice(dom, q)
    if sl and not QUERY_RE.search(q):
        return "focus", dict(slices=sl)
    if QUERY_RE.search(q):
        return "query", {}
    if sl:
        return "focus", dict(slices=sl)
    if DRILL_RE.search(q):
        return "focus", {}
    return "explain", {}


def _other_domain(q, cur):
    words = {"fmcg": ["净利率", "毛利率", "利润率", "快消", "禾悦"], "ecommerce": ["gmv", "成交额", "客单价", "转化率", "青禾"]}
    for d, ws in words.items():
        if d != cur and any(w in q.lower() for w in ws):
            return True
    return False


def node_route(state: State, config):
    A = state.get("A") or {}
    q = state["question"]
    if not A.get("conclusion"):
        return dict(route="analyze", args={})
    route, args = _route_rules(A, q)
    # 规则没有明确信号（落到默认的 explain）时才让大模型判断；规则已识别出切片、情景、查数意图时以规则为准
    weak = route == "explain" and not EXPLAIN_RE.search(q) and not re.search(r"\bH\d+\b", q, re.I)
    if llm.online() and weak:
        try:
            obj = llm.chat_json([{"role": "system", "content": "你是经营分析系统的追问路由器。根据追问内容选择一个分支，只输出 JSON："
                                  '{"route": "focus|whatif|query|explain|analyze"}。focus=针对某个渠道/类目/区域/客群/产品线继续下钻；whatif=假设某个因素调整后指标会怎样；'
                                  "query=查具体数据明细；explain=解释已有结论、假设、证据；analyze=换指标或换时间重新分析。"},
                                 {"role": "user", "content": f"当前分析：{A['intent']['metric_name']}，{A['intent']['current']['label']} 对比 {A['intent']['base']['label']}\n追问：{q}"}],
                                max_tokens=50)
            if obj and obj.get("route") in ("focus", "whatif", "query", "explain", "analyze") and obj["route"] != route:
                if obj["route"] == "focus" and not args.get("slices"):
                    args = dict(slices=drill.resolve_slice(DOMAINS[A["intent"]["domain"]], q))
                route = obj["route"]
        except llm.LLMError:
            pass
    _emit(config)("route", route=route, label={"analyze": "新建归因分析", "focus": "锁定切片继续下钻", "whatif": "What-If 推演",
                                                 "query": "自然语言查数", "explain": "解释结论依据"}[route])
    return dict(route=route, args=args)


# ============================================================ 主流程

def node_understand(state: State, config):
    emit = _emit(config)
    q = state["question"]
    A = dict(question=q, trace=[], custom_events=(state.get("A") or {}).get("custom_events", []), started=time.time(),
             accepted=(state.get("A") or {}).get("accepted", {}))
    prev = (state.get("A") or {}).get("intent")
    dom_hint = state.get("domain") or (prev["domain"] if prev and not _other_domain(q, prev["domain"]) else None)
    with _Step(emit, A, "understand") as st:
        it = parse_intent(q, dom_hint)
        via = "规则"
        if llm.online():
            try:
                dom = DOMAINS[it["domain"]]
                obj = llm.chat_json([{"role": "system", "content": "从经营分析问题中抽取目标指标与对比周期，只输出 JSON："
                                      '{"domain": "ecommerce|fmcg", "metric": "指标编码", "current_month": "YYYY-MM", "base_month": "YYYY-MM", "compare": "环比|同比|指定基期|目标差"}。'
                                      f"ecommerce 指标：{json.dumps({k: v for k, v in DOMAINS['ecommerce'].METRICS.items()}, ensure_ascii=False)}，数据月份 {DOMAINS['ecommerce'].periods_available()}；"
                                      f"fmcg 指标：{json.dumps(DOMAINS['fmcg'].METRICS, ensure_ascii=False)}，数据月份 {DOMAINS['fmcg'].periods_available()}。"
                                      "没说基期时默认环比上月。"},
                                     {"role": "user", "content": q}], max_tokens=120)
                if obj and obj.get("domain") in DOMAINS:
                    d2 = DOMAINS[obj["domain"]]
                    if obj.get("metric") in d2.METRICS and obj.get("current_month") in d2.periods_available() and obj.get("base_month") in d2.periods_available():
                        it = dict(domain=d2.id, metric=obj["metric"], metric_name=d2.METRICS[obj["metric"]], base=month_period(obj["base_month"]),
                                  current=month_period(obj["current_month"]), compare=obj.get("compare") or it["compare"])
                        via = "大模型（规则校验通过）"
            except llm.LLMError as e:
                emit("notice", text=f"大模型不可用，改用规则解析：{e}")
        dom = DOMAINS[it["domain"]]
        if it["metric"] not in ("gmv", "aov", "order_cnt", "net_margin", "gross_margin"):
            emit("notice", text=f"{it['metric_name']}暂无内置拆解树，按{dom.METRICS[dom.main_metric]}分析")
            it["metric"], it["metric_name"] = dom.main_metric, dom.METRICS[dom.main_metric]
        A["intent"] = it
        avail = dom.periods_available()
        if it["base"]["ym"] not in avail or it["current"]["ym"] not in avail or it["base"]["ym"] == it["current"]["ym"]:
            A["error"] = f"{dom.title}的数据范围是 {avail[0]} 至 {avail[-1]}，请在这个范围内选择两个不同的月份对比（解析到：{it['current']['label']} vs {it['base']['label']}）"
            st.done(A["error"])
            emit("answer", text=A["error"], mode="error")
            return dict(A=dict(state.get("A") or {}, error=A["error"]) if prev else A)
        st.done(f"{dom.title}｜{it['metric_name']}｜{it['current']['label']} vs {it['base']['label']}（{it['compare']}，解析：{via}）")
    emit("intent", intent=it, domain_title=dom.title, plan=[n for _, n in STEPS[1:]])
    return dict(A=A, domain=it["domain"])


def node_detect(state: State, config):
    emit, A = _emit(config), state["A"]
    it = A["intent"]
    dom = DOMAINS[it["domain"]]
    with _Step(emit, A, "detect") as st:
        sig, spikes = SIGNALS[dom.id](dom, it["base"], it["current"])
        A["signals"], A["spike_days"] = sig, spikes
        A["events_all"] = dom.event_records(A.get("custom_events", []))
        if dom.id == "ecommerce":
            d = dom.daily(it["base"]["start"], it["current"]["end"])
            A["series"] = dict(x=list(d.dt), y=[float(v) for v in d.gmv], name="日 GMV", unit="元")
        else:
            m = dom.monthly()
            A["series"] = dict(x=list(m.month), y=[float(v) for v in m[it["metric"] if it["metric"] in m else "net_margin"]], name=f"月{it['metric_name']}", unit="%")
        st.done(f"扫描到 {len(sig)} 个异动信号")
    emit("signals", signals=clean(sig), series=clean(A["series"]), events=clean([dict(event_id=e["event_id"], title=e["title"], start=e["start_date"], end=e["end_date"],
                                                                                         event_type=e["event_type"]) for e in A["events_all"]]))
    return dict(A=A)


def node_decompose(state: State, config):
    emit, A = _emit(config), state["A"]
    it = A["intent"]
    dom = DOMAINS[it["domain"]]
    with _Step(emit, A, "decompose") as st:
        dec = dom.decompose(it["metric"], it["base"], it["current"])
        A["decomp"] = clean(dec)
        A["tree"] = dom.tree()
        chk = dec.get("check") or {}
        st.done(f"{dec['method']}：{len(dec['factors'])} 个因子，贡献之和 − 总变动 = {chk.get('diff', 0):.2e}（{'守恒' if chk.get('ok') else '不守恒'}）")
    emit("decomp", decomp=A["decomp"], tree=A["tree"])
    return dict(A=A)


def node_drill(state: State, config):
    emit, A = _emit(config), state["A"]
    it = A["intent"]
    dom = DOMAINS[it["domain"]]
    with _Step(emit, A, "drill") as st:
        drills = [dom.drilldown(it["metric"], d, it["base"], it["current"]) for d in dom.DIMS]
        A["drills"] = clean(drills)
        A["cross"] = clean(dom.cross_drill(it["base"], it["current"])) if dom.id == "ecommerce" else None
        A["sankey"] = clean(drill.sankey(dom, it["current"]))
        tops = []
        for d in drills:
            r = d["rows"][0]
            tops.append(f"{d['dim_name']}：{r['key']}（{(r['change'] / 1e4) if dom.id == 'ecommerce' else r['change'] * 100:+,.1f}{' 万元' if dom.id == 'ecommerce' else ' pp'}）")
        st.done("变动最集中 — " + "；".join(tops))
    emit("drill", drills=A["drills"], cross=A["cross"], sankey=A["sankey"])
    return dict(A=A)


LLM_HYP_SYS = """你是资深经营分析师。规则检查库已经生成了一批候选归因假设，请补充最多 2 条它没有覆盖、但数据里值得验证的假设，并为每条写 Python 检验代码。
代码运行在只读沙箱里，可用：
- q(sql, params=()) → pandas.DataFrame，SQLite 只读，只允许 SELECT/WITH，禁止访问 dim_user
- pd、np、stats（scipy.stats）、math；变量 TOTAL（目标指标的总变动，单位同目标指标；比率指标是小数，如 -0.049 表示 -4.9 个百分点）
- conf(p, effect)：按 p 值和影响大小算置信度
不允许 import 其他模块、读写文件、联网、使用 eval/exec/open 和双下划线属性。
代码最后必须赋值：result = dict(effect=影响量（负数表示拉低目标指标）, confidence=conf(p, effect), verdict="支持" 或 "排除", p_value=p,
finding="一句话结论，带关键数字", evidence=["证据1", "证据2"])
只输出 JSON：{"hypotheses": [{"title": "假设标题", "slice": "涉及的维度切片", "rationale": "为什么值得验证", "code": "..."}]}；没有值得补充的就输出 {"hypotheses": []}"""


def node_hypothesize(state: State, config):
    emit, A = _emit(config), state["A"]
    it = A["intent"]
    dom = DOMAINS[it["domain"]]
    with _Step(emit, A, "hypothesize") as st:
        H = BUILDERS[dom.id](dom, it, A["signals"], A["spike_days"], A["decomp"], A["drills"], A["events_all"])
        n_llm = 0
        if llm.online():
            try:
                ctx = dict(question=A["question"], metric=it["metric_name"], base=it["base"], current=it["current"],
                           total_change=A["decomp"]["change"], factors=[(f["name"], f["contrib"]) for f in A["decomp"]["factors"]],
                           drill_top={d["dim_name"]: [(r["key"], r["change"]) for r in d["rows"][:3]] for d in A["drills"]},
                           existing=[h["title"] for h in H])
                obj = llm.chat_json([{"role": "system", "content": LLM_HYP_SYS},
                                     {"role": "user", "content": (ECOMMERCE_SCHEMA if dom.id == "ecommerce" else FMCG_SCHEMA) + "\n\n分析上下文：" + json.dumps(clean(ctx), ensure_ascii=False)}],
                                    max_tokens=2500)
                for x in (obj or {}).get("hypotheses", [])[:2]:
                    if x.get("title") and x.get("code"):
                        H.append(dict(group="大模型补充假设", type="llm", title=x["title"], slice=x.get("slice", ""), signal=x.get("rationale", ""),
                                      code=f"TOTAL = {A['decomp']['change']!r}\n" + sandbox_prelude() + x["code"], source="大模型", events=[]))
                        n_llm += 1
            except llm.LLMError as e:
                emit("notice", text=f"大模型补充假设失败，仅使用检查库：{e}")
        for i, h in enumerate(H, 1):
            h["id"] = f"H{i}"
            h.setdefault("events", [])
            h.setdefault("source", "检查库")
        A["hypotheses"] = H
        groups = {}
        for h in H:
            groups[h["group"]] = groups.get(h["group"], 0) + 1
        st.done(f"{len(H)} 条候选假设（" + "，".join(f"{k} {v}" for k, v in groups.items()) + "）" + (f"，其中大模型补充 {n_llm} 条" if n_llm else ""))
    emit("hypotheses", hypotheses=[_hyp_card(h) for h in H])
    return dict(A=A)


def sandbox_prelude():
    from .checks import PRELUDE
    return PRELUDE.replace("TOTAL = __TOTAL__", "")


def _hyp_card(h):
    return clean(dict(id=h["id"], group=h["group"], type=h["type"], title=h["title"], slice=h.get("slice", ""), signal=h.get("signal", ""),
                      events=h.get("events", []), source=h.get("source"), code=h["code"], result=h.get("result"), error=h.get("error"),
                      elapsed_ms=h.get("elapsed_ms"), accepted=h.get("accepted"), repaired=h.get("repaired"), first_error=h.get("first_error"), overlap=h.get("overlap")))


REPAIR_SYS = "你写的沙箱检验代码执行出错。请只修正代码，保持假设不变，只输出 JSON：{\"code\": \"...\"}。沙箱规则同前：只能用 q()/pd/np/stats/math/conf，最后赋值 result。"


def node_verify(state: State, config):
    emit, A = _emit(config), state["A"]
    dom = DOMAINS[A["intent"]["domain"]]
    with _Step(emit, A, "verify") as st:
        H = A["hypotheses"]

        def run(h):
            r = sandbox.run(h["code"], dom.db)
            if not r["ok"] and h.get("source") == "大模型" and llm.online():
                try:
                    obj = llm.chat_json([{"role": "system", "content": REPAIR_SYS},
                                         {"role": "user", "content": f"代码：\n{h['code']}\n\n报错：{r['error']}"}], max_tokens=2000)
                    if obj and obj.get("code"):
                        h["first_error"] = r["error"]
                        h["code"] = obj["code"] if "def conf" in obj["code"] else f"TOTAL = {A['decomp']['change']!r}\n" + sandbox_prelude() + obj["code"]
                        h["repaired"] = True
                        r = sandbox.run(h["code"], dom.db)
                except llm.LLMError:
                    pass
            return h, r
        ok = fail = 0
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = [ex.submit(run, h) for h in H]
            for f in as_completed(futs):
                h, r = f.result()
                h["elapsed_ms"] = r["elapsed_ms"]
                if r["ok"] and isinstance(r["result"], dict) and {"effect", "verdict", "finding"} <= set(r["result"]):
                    res = r["result"]
                    res.setdefault("confidence", 0.0)
                    res.setdefault("evidence", [])
                    res["confidence"] = float(res["confidence"] or 0)
                    total = A["decomp"]["change"]
                    if h.get("source") == "大模型" and total and abs(float(res["effect"] or 0)) > 2 * abs(total):
                        res["verdict"] = "排除"
                        res["confidence"] = 0.0
                        res["finding"] += f"（影响量超过总变动的 2 倍，疑似单位或口径错误，未采信，请分析师复核检验代码）"
                    if res["verdict"] == "支持" and res["confidence"] < settings.CONFIDENCE_THRESHOLD:
                        res["verdict"] = "排除"
                        res["finding"] += f"（置信度 {res['confidence']:.2f} 低于阈值 {settings.CONFIDENCE_THRESHOLD}，自动排除）"
                    h["result"] = clean(res)
                    h["stdout"] = r.get("stdout", "")
                    ok += 1
                else:
                    h["result"] = None
                    h["error"] = r.get("error") or "检验代码没有产出规范的 result"
                    fail += 1
                emit("hyp_result", hypothesis=_hyp_card(h))
        generic = {"类目", "渠道", "下降", "上升", "拉低", "拉高", "影响", "导致", "现期", "基期", "原因", "变化", "占比", "利润", "润率", "利率", "成本",
                   "毛利", "净利", "GM", "MV", "折扣", "增量", "月G", "9月", "8月", "6月", "而非", "是否", "独立", "比上", "升拉", "降拉"}

        def bigrams(t):
            t = re.sub(r"[^一-鿿A-Za-z0-9]", "", t or "")
            return {t[i:i + 2] for i in range(len(t) - 1)} - generic
        rule_sup = [x for x in H if x.get("source") == "检查库" and x.get("result") and x["result"]["verdict"] == "支持"]
        for h in H:  # 大模型补充的假设被支持、又和检查库已支持的原因说的是同一件事时标出来，避免分析师重复采纳
            if h.get("source") == "大模型" and h.get("result") and h["result"]["verdict"] == "支持" and rule_sup:
                best = max(rule_sup, key=lambda x: len(bigrams(h["title"]) & bigrams(x["title"] + x.get("slice", ""))))
                if len(bigrams(h["title"]) & bigrams(best["title"] + best.get("slice", ""))) >= 2:
                    h["overlap"] = f"{best['id']} {best['title']}"
        for h in H:  # 分析师在上一轮做过的审核（采纳/驳回）沿用
            if h["title"] in A.get("accepted", {}):
                h["accepted"] = A["accepted"][h["title"]]
        sup = sum(1 for h in H if h.get("result") and h["result"]["verdict"] == "支持")
        exc = sum(1 for h in H if h.get("result") and h["result"]["verdict"] == "排除")
        der = sum(1 for h in H if h.get("result") and h["result"]["verdict"] == "派生")
        st.done(f"沙箱执行 {len(H)} 段检验代码：支持 {sup}、排除 {exc}、派生 {der}" + (f"、执行失败 {fail}" if fail else ""))
    return dict(A=A)


def _effective(A):
    """分析师审核后的假设集合：驳回的不计入；大模型补充的默认不计入贡献，采纳后才计入。"""
    out = []
    for h in A["hypotheses"]:
        h2 = dict(h)
        acc = h.get("accepted")
        if acc is None:
            acc = h.get("source") != "大模型"
        if not acc and h2.get("result") and h2["result"]["verdict"] == "支持":
            h2["result"] = dict(h2["result"], verdict="排除", finding=h2["result"]["finding"] + ("（大模型补充假设，待分析师采纳后计入）" if h.get("source") == "大模型" and h.get("accepted") is None else "（分析师已驳回）"))
        out.append(h2)
    return out


def recompute(A):
    dom = DOMAINS[A["intent"]["domain"]]
    A["conclusion"] = clean(CC.conclude(dom, A["intent"], A["decomp"], _effective(A)))
    A["actions"] = clean(CC.actions(dom, A["intent"], A["decomp"], A["conclusion"]))
    A["whatif"] = clean(whatif.build(dom, A["intent"], A))
    A["whatif_result"] = clean(whatif.simulate(A["whatif"], {}))
    return A


def node_events(state: State, config):
    emit, A = _emit(config), state["A"]
    dom = DOMAINS[A["intent"]["domain"]]
    with _Step(emit, A, "events") as st:
        A["conclusion"] = clean(CC.conclude(dom, A["intent"], A["decomp"], _effective(A)))
        c = A["conclusion"]
        linked = {e["event_id"] for h in A["hypotheses"] for e in h.get("events", [])}
        p0, p1 = A["intent"]["base"], A["intent"]["current"]
        window = [e for e in A["events_all"] if e["start_date"] <= p1["end"] and e["end_date"] >= p0["start"]]
        A["event_table"] = []
        for e in window:
            hs = [h for h in A["hypotheses"] if any(x["event_id"] == e["event_id"] for x in h.get("events", []))]
            if hs:
                v = hs[0].get("result") or {}
                status = {"支持": "已关联为原因", "排除": "已检验：无显著影响", "派生": "派生"}.get(v.get("verdict"), "检验失败")
                rel = max(x["relevance"] for x in hs[0]["events"] if x["event_id"] == e["event_id"])
                A["event_table"].append(dict(event_id=e["event_id"], title=e["title"], event_type=e["event_type"], start=e["start_date"], end=e["end_date"],
                                             status=status, hypothesis=hs[0]["id"], relevance=rel, source=e.get("source", "数仓")))
            else:
                A["event_table"].append(dict(event_id=e["event_id"], title=e["title"], event_type=e["event_type"], start=e["start_date"], end=e["end_date"],
                                             status="窗口内未检出关联信号", hypothesis=None, relevance=None, source=e.get("source", "数仓")))
        st.done(f"分析窗口内 {len(window)} 个事件，{len(linked & {e['event_id'] for e in window})} 个进入因果链；"
                f"内部原因 {len(c['internal'])} 条、外部诱因 {len(c['external'])} 条；已解释 {c['explained_share']:.0%}")
    emit("conclusion", conclusion=A["conclusion"], event_table=clean(A["event_table"]))
    return dict(A=A)


def node_advise(state: State, config):
    emit, A = _emit(config), state["A"]
    dom = DOMAINS[A["intent"]["domain"]]
    with _Step(emit, A, "advise") as st:
        A["actions"] = clean(CC.actions(dom, A["intent"], A["decomp"], A["conclusion"]))
        A["whatif"] = clean(whatif.build(dom, A["intent"], A))
        A["whatif_result"] = clean(whatif.simulate(A["whatif"], {}))
        A["whatif_compare"] = clean(whatif.compare(A["whatif"], A["whatif"]["presets"]))
        tot = sum(a["amount"] for a in A["actions"])
        st.done(f"{len(A['actions'])} 条行动项，预期合计挽回 {tot / 1e4:,.1f} 万元/月；What-If 杠杆 {len(A['whatif']['levers'])} 个")
    emit("actions", actions=A["actions"], whatif=A["whatif"], whatif_result=A["whatif_result"], whatif_compare=A["whatif_compare"])
    return dict(A=A)


def _facts(A):
    c = A["conclusion"]
    return clean(dict(问题=A["question"], 指标=A["intent"]["metric_name"], 基期=A["intent"]["base"]["label"], 现期=A["intent"]["current"]["label"],
                      总变动=c["total_text"], 拆解=[(f["name"], f["contrib"]) for f in A["decomp"]["factors"]],
                      原因=[dict(编号=x["id"], 原因=x["title"], 等级=x["rank"], 影响=x["effect_text"], 占比=f"{x['share']:.0%}", 性质=x["nature"], 内外部=x["scope"],
                                事件=[e["title"] for e in x["events"]], 发现=x["finding"]) for x in c["causes"]],
                      其他未解释=c["residual_text"], 排除=[dict(假设=x["title"], 依据=x["finding"]) for x in c["excluded"]],
                      派生=[dict(现象=x["phenomenon"], 归属=x["parent_title"], 依据=x["finding"]) for x in c["derived"]],
                      行动=[dict(负责人=a["owner"], 动作=a["action"], 周期=a["period"], 预期挽回=a["amount_text"]) for a in A.get("actions", [])]))


NARRATE_SYS = """你是经营分析师，根据给定的归因结果写结论。要求：
1. 先一句话回答“变了多少”，再按主要、次要、反向因素依次说明原因，每条带影响数字和支撑证据（事件、检验结果）
2. 单独一段说明被排除的干扰项和派生现象，指出“看起来像原因但不是”的地方
3. 最后一段给出优先级最高的 2–3 条行动
4. 只能使用给定的数字，不能编造；不要用表格；500 字以内"""


def _template_narrative(A):
    c = A["conclusion"]
    lines = [c["headline"]]
    if c["excluded"]:
        lines.append("排除的干扰项：" + "；".join(f"{x['title']}——{x['finding']}" for x in c["excluded"]))
    if c["derived"]:
        lines.append("派生现象：" + "；".join(f"{x['phenomenon']}，是「{x['parent_title']}」的结果，不是独立原因" for x in c["derived"] if x["parent_title"]))
    lines.append(f"已解释 {c['explained_share']:.0%}，其余 {c['residual_text']} 归入自然波动与未识别因素。")
    if A.get("actions"):
        lines.append("优先行动：" + "；".join(f"{a['owner']}：{a['action']}（{a['period']}，预期挽回 {a['amount_text']}）" for a in A["actions"][:3]))
    return "\n\n".join(lines)


def _stream_text(emit, messages, fallback):
    out = []
    if llm.online():
        try:
            for piece in llm.stream(messages):
                out.append(piece)
                emit("delta", text=piece)
            return "".join(out), "大模型"
        except llm.LLMError as e:
            emit("notice", text=f"大模型撰写失败，改用模板：{e}")
    for i in range(0, len(fallback), 24):
        emit("delta", text=fallback[i:i + 24])
    return fallback, "模板"


def node_narrate(state: State, config):
    emit, A = _emit(config), state["A"]
    with _Step(emit, A, "narrate") as st:
        text, via = _stream_text(emit, [{"role": "system", "content": NARRATE_SYS},
                                        {"role": "user", "content": json.dumps(_facts(A), ensure_ascii=False)}], _template_narrative(A))
        A["narrative"] = text
        A["elapsed_s"] = round(time.time() - A.get("started", time.time()), 1)
        st.done(f"{via}撰写，{len(text)} 字；全流程 {A['elapsed_s']} 秒")
    emit("answer", text=text, mode="analysis")
    return dict(A=A)


# ============================================================ 追问分支

def _default_slice(A):
    for c in A["conclusion"]["causes"]:
        h = next((h for h in A["hypotheses"] if h["id"] == c["id"]), {})
        for dim, k in (("channel", "channel_id"), ("category", "category_id"), ("product_line", "product_line")):
            if h.get(k):
                dom = DOMAINS[A["intent"]["domain"]]
                for d, key, name in drill.resolve_slice(dom, h.get("slice", "") + h["title"]):
                    if d == dim:
                        return [(d, key, name)]
    return []


def node_focus(state: State, config):
    emit, A = _emit(config), state["A"]
    dom = DOMAINS[A["intent"]["domain"]]
    q = state["question"]
    slices = (state.get("args") or {}).get("slices") or []
    if not slices and A.get("drill_history") and re.search(r"继续|再往下|往下|深挖|细分|拆开|具体", q):
        # “继续往下”：在上一层切片里找变动最大的子维度取值，作为下一层
        last = A["drill_history"][-1]
        visited = {f[0] for f in A.get("focus", [])}
        seen = {f[2] for f in A.get("focus", [])}
        for b in last["breakdowns"]:
            if b["by"] in visited or b["by"] in ("product", "sku"):
                continue
            for row in b["rows"]:
                if row["k"] in seen:
                    continue
                cand = [x for x in drill.resolve_slice(dom, str(row["k"])) if x[0] == b["by"]]
                if cand:
                    slices = [cand[0]]
                    break
            if slices:
                break
    if not slices:
        slices = [tuple(A["focus"][-1][:3])] if A.get("focus") and re.search(r"它|这个", q) else _default_slice(A)
    t = time.time()
    emit("step", key="focus", name="切片下钻", status="running")
    if not slices:
        text = "没有在问题里识别到具体的渠道、类目、区域、客群或产品线，可以这样问：“信息流广告为什么掉这么多？”"
        emit("step", key="focus", name="切片下钻", status="done", ms=0, summary="未识别到切片")
        emit("answer", text=text, mode="focus")
        return dict(A=A)
    dim, key, name = slices[0]
    prev = A.get("focus") or []
    parent = prev[-1] if prev and prev[-1][0] != dim else None
    res = clean(drill.drill_slice(dom, A["intent"], A, dim, key, name))
    if len(slices) > 1:  # 两个切片：第二个当作在第一个里面看的维度
        d2, k2, n2 = slices[1]
        for b in res["breakdowns"]:
            if b["by"] == d2:
                row = next((r for r in b["rows"] if r["k"] == n2), None)
                if row:
                    res["points"].insert(0, f"{name}中{n2}：{row.get('change', 0) / 1e4:+,.1f} 万元（占该切片变动 {row.get('contrib_share', 0):.0%}）" if dom.id == "ecommerce"
                                         else f"{name}中{n2}：毛利率 {row.get('gm_change_pp', 0) * 100:+.2f} 个百分点")
    res["parent"] = parent
    if parent and len(slices) == 1:  # 上一层切片在这一层里的表现，把两层下钻串起来
        for b in res["breakdowns"]:
            if b["by"] == parent[0]:
                row = next((r for r in b["rows"] if r["k"] == parent[2]), None)
                if row:
                    res["points"].insert(0, (f"承接上一层（{parent[2]}）：{name}中来自{parent[2]}的变动 {row.get('change', 0) / 1e4:+,.1f} 万元，占{name}变动的 {row.get('contrib_share', 0):.0%}"
                                             if dom.id == "ecommerce" else f"承接上一层（{parent[2]}）：{name}在{parent[2]}的毛利率变动 {row.get('gm_change_pp', 0) * 100:+.2f} 个百分点"))
    A.setdefault("focus", []).append([dim, key, name])
    A.setdefault("drill_history", []).append(res)
    ms = int((time.time() - t) * 1000)
    emit("step", key="focus", name="切片下钻", status="done", ms=ms, summary=f"锁定 {res['dim_name']}={name}，拆 {len(res['breakdowns'])} 个子维度，关联假设 {len(res['related'])} 条、事件 {len(res['events'])} 个")
    emit("drill_focus", focus=res, path=A["focus"])
    unit = "万元" if dom.id == "ecommerce" else "个百分点"
    mtxt = "；".join(f"{m['name']} {_fmt(m['v0'], m)} → {_fmt(m['v1'], m)}" for m in res["metrics"])
    fallback = (f"锁定切片 {res['dim_name']}={name}" + (f"（上一层：{parent[2]}）" if parent else "") + f"。\n\n{mtxt}。\n\n" + "\n".join("· " + p for p in res["points"]) +
                ("\n\n相关结论：" + "；".join(f"{r['id']} {r['title']}（{r['verdict']}）：{r['finding']}" for r in res["related"]) if res["related"] else "") +
                ("\n\n相关事件：" + "；".join(f"{e['event_id']} {e['title']}（{e['start']}）" for e in res["events"]) if res["events"] else ""))
    text, _ = _stream_text(emit, [{"role": "system", "content": "你是经营分析师。用户在追问某个切片，根据下钻数据回答。数字已经带好单位，原样引用，不要换算、不要编造；基期在前、现期在后。300 字以内，最后指出下一步可以继续看什么。"},
                                  {"role": "user", "content": f"追问：{q}\n下钻数据：{json.dumps(_slim_focus(res, dom.id == "ecommerce"), ensure_ascii=False)}\n此前结论：{A['conclusion']['headline']}"}], fallback)
    emit("answer", text=text, mode="focus")
    return dict(A=A)


def _slim_focus(res, eco):
    """给大模型的下钻数据：数值先格式化成带单位的文字（万元、百分比），避免模型把元当成万元或弄反基期现期。"""
    def money(v):
        return f"{v / 1e4:,.1f} 万元"
    out = dict(切片=f"{res['dim_name']}={res['name']}", 上一层=res["parent"][2] if res.get("parent") else None,
               指标=[f"{m['name']}：基期 {_fmt(m['v0'], m)} → 现期 {_fmt(m['v1'], m)}" for m in res["metrics"]],
               要点=res["points"], 相关结论=[f"{r['id']} {r['title']}（{r['verdict']}）：{r['finding']}" for r in res["related"]],
               相关事件=[f"{e['event_id']} {e['title']}（{e['start']}）" for e in res["events"]], 子维度拆解={})
    for b in res["breakdowns"]:
        rows = []
        for r in b["rows"][:6]:
            if eco:
                rows.append(f"{r['k']}：{money(r['v0'])} → {money(r['v1'])}，变动 {r['change'] / 1e4:+,.1f} 万元（占切片变动 {r['contrib_share']:.0%}）")
            else:
                rows.append(f"{r['k']}：营收 {money(r['rev0'])} → {money(r['rev1'])}，毛利率变动 {r['gm_change_pp'] * 100:+.2f} 个百分点，折扣率变动 {r['disc_change_pp'] * 100:+.2f} 个百分点")
        out["子维度拆解"][f"按{b['by_name']}"] = rows
    return out


def _fmt(v, m):
    if v is None:
        return "—"
    if m.get("ratio") or m["key"] in ("cvr", "share", "margin", "gross_margin", "disc_rate", "cogs_rate", "logi_rate", "comm_rate", "mkt_rate"):
        return f"{v:.2%}"
    if m["key"] in ("gmv", "rev"):
        return f"{v / 1e4:,.1f} 万元"
    if m["key"] in ("aov", "asp"):
        return f"{v:,.0f} 元"
    return f"{v:,.0f}"


def _parse_levers(m, q):
    """从问题里读杠杆取值：杠杆名里的关键词 + 最近的数字。"""
    vals = {}
    nums = [(mm.start(), float(mm.group(1))) for mm in re.finditer(r"(-?\d+(?:\.\d+)?)\s*%?", q)]
    for l in m["levers"]:
        words = [w for w in re.split(r"[（）()、 ]", l["name"]) if w]
        keys = set()
        for w in words:
            for kw in ("信息流", "搜索", "断货", "补货", "预算", "转化", "价格", "弹性", "社区团购", "折扣", "让利", "促销", "白砂糖", "糖", "佣金", "营销"):
                if kw in w:
                    keys.add(kw)
        if l["key"] == "gb_share":
            keys = {"占比"} if "社区团购" in q and "占比" in q else set()
        if l["key"] == "gb_disc":
            keys = {"折扣"} if "社区团购" in q and "折扣" in q else set()
        pos = [q.find(k) for k in keys if k in q]
        if not pos or not nums:
            continue
        p = min(pos)
        after = [n for n in nums if n[0] > p]
        if not after:
            continue
        v = after[0][1]
        if re.search(r"(降|减|下调|少)", q[p:after[0][0]]) and l["key"] in ("price", "paid_budget", "cvr", "sugar") and v > 0:
            v = -v
        vals[l["key"]] = max(l["min"], min(l["max"], v))
    if re.search(r"全部恢复|完全恢复|恢复正常", q):
        for l in m["levers"]:
            if l["key"].startswith("rec_"):
                vals[l["key"]] = 100
    return vals


def node_whatif(state: State, config):
    emit, A = _emit(config), state["A"]
    q = state["question"]
    m = A["whatif"]
    t = time.time()
    emit("step", key="whatif", name="What-If 推演", status="running")
    vals = _parse_levers(m, q)
    if llm.online() and not vals:
        try:
            obj = llm.chat_json([{"role": "system", "content": "把用户的假设情景映射到杠杆取值，只输出 JSON {\"values\": {\"杠杆key\": 数值}}，没提到的杠杆不要输出。"},
                                 {"role": "user", "content": f"杠杆：{json.dumps([dict(key=l['key'], name=l['name'], min=l['min'], max=l['max'], unit=l['unit'], current=l['default']) for l in m['levers']], ensure_ascii=False)}\n情景：{q}"}],
                                max_tokens=200)
            for k, v in ((obj or {}).get("values") or {}).items():
                l = next((x for x in m["levers"] if x["key"] == k), None)
                if l is not None and isinstance(v, (int, float)):
                    vals[k] = max(l["min"], min(l["max"], float(v)))
        except llm.LLMError:
            pass
    res = clean(whatif.simulate(m, {**A.get("whatif_values", {}), **vals}))
    A["whatif_values"] = {**A.get("whatif_values", {}), **vals}
    A["whatif_result"] = res
    emit("step", key="whatif", name="What-If 推演", status="done", ms=int((time.time() - t) * 1000),
         summary="识别到参数：" + ("，".join(f"{next(l['name'] for l in m['levers'] if l['key'] == k)}={v:g}" for k, v in vals.items()) or "无（沿用当前参数）"))
    emit("whatif_result", whatif_result=res, values=A["whatif_values"])
    f = (lambda v: f"{v / 1e4:,.1f} 万元") if m["domain"] == "ecommerce" else (lambda v: f"{v:.2%}")
    fd = (lambda v: f"{v / 1e4:+,.1f} 万元") if m["domain"] == "ecommerce" else (lambda v: f"{v * 100:+.2f} 个百分点")
    text = (f"{m['note']}。推演结果：{m['metric_name']}预计 {f(res['predicted'])}（{fd(res['delta'])}），95% 区间 {f(res['band'][0])} ~ {f(res['band'][1])}。\n\n"
            + "\n".join(f"· {c['name']}：{fd(c['delta'])}" for c in res["contrib"]) +
            ("\n\n没有从问题里识别到参数，可以直接说“如果信息流恢复 80%、补货到位 100%”，或在右侧模拟器拖动滑块。" if not vals else ""))
    for i in range(0, len(text), 24):
        emit("delta", text=text[i:i + 24])
    emit("answer", text=text, mode="whatif")
    return dict(A=A)


def node_query(state: State, config):
    emit, A = _emit(config), state["A"]
    dom = DOMAINS[A["intent"]["domain"]]
    q = state["question"]
    t = time.time()
    emit("step", key="query", name="自然语言查数", status="running")
    r = clean(sqltool.ask(dom.db, q))
    emit("step", key="query", name="自然语言查数", status="done" if r["type"] == "sql" else "error", ms=int((time.time() - t) * 1000),
         summary=f"模型：{sqltool.MODES.get(r.get('mode'), r.get('mode'))}；" + (f"返回 {r['total_rows']} 行" if r["type"] == "sql" else r.get("message") or r.get("error") or r["type"]))
    emit("query_result", result=r)
    A.setdefault("queries", []).append(r)
    if r["type"] == "sql":
        text = f"已生成并执行 SQL（{sqltool.MODES.get(r['mode'], r['mode'])}），返回 {r['total_rows']} 行，结果见右侧数据表。"
        if r["rows"] and len(r["columns"]) <= 6:
            text += "\n\n" + "\n".join("· " + "，".join(f"{c}={_cell(v)}" for c, v in zip(r["columns"], row)) for row in r["rows"][:5])
    elif r["type"] == "clarify":
        text = "需要先确认：" + (r.get("message") or "")
    elif r["type"] == "refuse":
        text = "这个请求不能执行：" + (r.get("message") or "")
    else:
        text = "查数失败：" + (r.get("error") or "")
    for i in range(0, len(text), 24):
        emit("delta", text=text[i:i + 24])
    emit("answer", text=text, mode="query")
    return dict(A=A)


def _cell(v):
    if isinstance(v, float):
        return f"{v:.4f}" if abs(v) < 1 else f"{v:,.2f}"
    return str(v)


def node_explain(state: State, config):
    emit, A = _emit(config), state["A"]
    q = state["question"]
    t = time.time()
    emit("step", key="explain", name="解释依据", status="running")
    hits = []
    for mm in re.finditer(r"\bH(\d+)\b", q, re.I):
        hits += [h for h in A["hypotheses"] if h["id"] == f"H{mm.group(1)}"]
    if not hits:  # 按标题和关联事件标题的二字词重合度找
        def bigrams(t):
            t = re.sub(r"[^一-鿿A-Za-z0-9]", "", t)
            return {t[i:i + 2] for i in range(len(t) - 1)}
        qb = bigrams(q)
        scored = []
        for h in A["hypotheses"]:
            hb = bigrams(h["title"] + "".join(e["title"] for e in h.get("events", [])))
            n = len(qb & hb)
            if n >= 2:
                scored.append((n, h))
        scored.sort(key=lambda x: -x[0])
        hits = [h for n, h in scored if n >= scored[0][0] - 1] if scored else []
    hits = hits[:3]
    emit("step", key="explain", name="解释依据", status="done", ms=int((time.time() - t) * 1000), summary=f"定位到 {len(hits)} 条相关假设" if hits else "按整体结论回答")
    if hits:
        emit("evidence", hypotheses=[_hyp_card(h) for h in hits])
    fallback = ("\n\n".join(f"{h['id']} {h['title']}：{(h.get('result') or {}).get('verdict', '未得出结果')}。{(h.get('result') or {}).get('finding', h.get('error', ''))}\n证据："
                            + "；".join((h.get("result") or {}).get("evidence", [])) + (f"\n关联事件：" + "、".join(e["title"] for e in h["events"]) if h.get("events") else "")
                            for h in hits) or A.get("narrative") or A["conclusion"]["headline"])
    ctx = dict(结论=_facts(A), 相关假设=[dict(编号=h["id"], 标题=h["title"], 检验代码=h["code"][-1500:], 结果=h.get("result")) for h in hits])
    text, _ = _stream_text(emit, [{"role": "system", "content": "你是经营分析师。根据给定的归因结果和检验代码回答用户的追问，说明判断依据（用了什么数据、什么方法、统计显著性），只用给定数字，300 字以内。"},
                                  {"role": "user", "content": f"追问：{q}\n{json.dumps(clean(ctx), ensure_ascii=False)}"}], fallback)
    emit("answer", text=text, mode="explain")
    return dict(A=A)


# ============================================================ 组图

def _build():
    g = StateGraph(State)
    g.add_node("route", node_route)
    for name, fn in [("understand", node_understand), ("detect", node_detect), ("decompose", node_decompose), ("drill", node_drill),
                     ("hypothesize", node_hypothesize), ("verify", node_verify), ("events", node_events), ("advise", node_advise), ("narrate", node_narrate),
                     ("focus", node_focus), ("whatif", node_whatif), ("query", node_query), ("explain", node_explain)]:
        g.add_node(name, fn)
    g.add_edge(START, "route")
    g.add_conditional_edges("route", lambda s: s["route"], {"analyze": "understand", "focus": "focus", "whatif": "whatif", "query": "query", "explain": "explain"})
    chain = ["detect", "decompose", "drill", "hypothesize", "verify", "events", "advise", "narrate"]
    g.add_conditional_edges("understand", lambda s: "end" if s["A"].get("error") else "detect", {"end": END, "detect": "detect"})
    for a, b in zip(chain, chain[1:]):
        g.add_edge(a, b)
    for n in ("narrate", "focus", "whatif", "query", "explain"):
        g.add_edge(n, END)
    return g.compile()


GRAPH = _build()


def run(question, A=None, domain=None, emit=None):
    """跑一轮（首轮分析或追问），返回更新后的分析上下文 A。"""
    emit = emit or (lambda *a, **k: None)
    out = GRAPH.invoke(dict(question=question, A=A or {}, domain=domain or ""), config={"configurable": {"emit": lambda kind, **kw: emit(kind, kw)}})
    return out["A"]


def mermaid():
    return GRAPH.get_graph().draw_mermaid()
