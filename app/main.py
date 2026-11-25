"""HTTP 接口。启动：python scripts/start.py（默认端口 8001），浏览器打开 http://127.0.0.1:8001"""
import json
import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agent, config, dashboard, db, drill, llm, report, sandbox, sqltool, whatif
from .auth import PERMISSIONS, ROLES, check_password, make_token, parse_token, seed_users, user_ctx
from .domains import DOMAINS

app = FastAPI(title="经营归因分析系统", version="1.0")
WEB = Path(__file__).resolve().parents[1] / "web"
CACHE = {}           # 会话 id → 分析上下文 A（内存缓存，持久化在 sessions.state）
RUNNING = {}         # 会话 id → 正在执行的任务（断点保持：前端断开后任务继续跑完并落库）
LOCK = threading.Lock()


@app.on_event("startup")
def _startup():
    db.conn()
    seed_users()
    seed_metrics()
    mode = db.get_setting("sql_mode")
    if mode in sqltool.MODES:
        sqltool.set_mode(mode)
    th = db.get_setting("confidence_threshold")
    if th:
        config.CONFIDENCE_THRESHOLD = float(th)
    for k in ("SANDBOX_TIMEOUT", "SANDBOX_MEMORY_MB"):
        v = db.get_setting(k.lower())
        if v:
            setattr(config, k, type(getattr(config, k))(v))


# ---------------------------------------------------------------- 鉴权

def current(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录或登录已过期")
    uid = parse_token(authorization[7:])
    ctx = user_ctx(uid) if uid else None
    if not ctx:
        raise HTTPException(401, "未登录或登录已过期")
    return ctx


def need(*perms):
    def dep(ctx=Depends(current)):
        if not any(p in ctx["perms"] for p in perms):
            raise HTTPException(403, f"没有操作权限（需要：{'或'.join(PERMISSIONS.get(p, p) for p in perms)}）")
        return ctx
    return dep


class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(body: LoginIn):
    u = db.q1("SELECT id, password_hash FROM users WHERE username=?", (body.username.strip(),))
    if not u or not check_password(body.password, u["password_hash"]):
        raise HTTPException(400, "用户名或密码错误")
    db.audit(u["id"], "登录")
    return dict(token=make_token(u["id"]), user=user_ctx(u["id"]))


@app.get("/api/me")
def me(ctx=Depends(current)):
    return ctx


@app.get("/api/system")
def system(ctx=Depends(current)):
    return dict(llm_online=llm.online(), llm_model=config.LLM_MODEL if llm.online() else "离线模式（未配置 LLM_API_KEY，规则引擎运行）",
                llm_base_url=config.LLM_BASE_URL, sql_mode=sqltool.STATE["mode"], sql_modes=[dict(key=k, name=v, available=k in sqltool.available_modes()) for k, v in sqltool.MODES.items()],
                sql_llm_base_url=config.SQL_LLM_BASE_URL, confidence_threshold=config.CONFIDENCE_THRESHOLD, major_share=config.MAJOR_SHARE,
                minor_share=config.MINOR_SHARE, event_window_days=config.EVENT_WINDOW_DAYS, sandbox_timeout=config.SANDBOX_TIMEOUT,
                sandbox_memory_mb=config.SANDBOX_MEMORY_MB, warehouse=str(config.WAREHOUSE_DIR),
                domains=[dict(id=d.id, title=d.title, metrics=d.METRICS, dims=d.DIMS, periods=d.periods_available(), default_question=d.default_question) for d in DOMAINS.values()],
                roles={k: dict(name=v[0], perms=v[1]) for k, v in ROLES.items()}, permissions=PERMISSIONS, graph=agent.mermaid(), llm_usage=llm.USAGE)


# ---------------------------------------------------------------- 大盘

@app.get("/api/dashboard")
def get_dashboard(domain: str = "ecommerce", ctx=Depends(need("dash:view"))):
    if domain not in DOMAINS:
        raise HTTPException(400, "未知指标域")
    return agent.clean(dashboard.build(domain))


# ---------------------------------------------------------------- 会话

def _own(sid, ctx, write=False):
    s = db.q1("SELECT * FROM sessions WHERE id=?", (sid,))
    if not s:
        raise HTTPException(404, "会话不存在")
    if write and s["user_id"] != ctx["id"]:
        raise HTTPException(403, "只能操作自己的会话")
    if not write and s["user_id"] != ctx["id"] and "analysis:view" not in ctx["perms"]:
        raise HTTPException(403, "没有权限查看该会话")
    return s


def _state(sid):
    if sid in CACHE:
        return CACHE[sid]
    s = db.q1("SELECT state FROM sessions WHERE id=?", (sid,))
    A = db.loads(s["state"], {}) if s else {}
    CACHE[sid] = A
    return A


def _save(sid, A, route=None, question=None):
    CACHE[sid] = A
    st = db.dumps(agent.clean(A))
    it = A.get("intent") or {}
    turns = (db.q1("SELECT turns FROM sessions WHERE id=?", (sid,)) or {}).get("turns", 0) + (1 if question else 0)
    db.x("UPDATE sessions SET state=?, domain=?, metric=?, headline=?, turns=?, updated_at=? WHERE id=?",
         (st, it.get("domain"), it.get("metric_name"), (A.get("conclusion") or {}).get("headline"), turns, db.now(), sid))
    if question:
        db.x("INSERT INTO snapshots (session_id, turn, route, question, state, created_at) VALUES (?,?,?,?,?,?)", (sid, turns, route, question, st, db.now()))


class SessionIn(BaseModel):
    title: Optional[str] = None
    domain: Optional[str] = None


@app.get("/api/sessions")
def list_sessions(ctx=Depends(need("analysis:run", "analysis:view"))):
    if "analysis:run" in ctx["perms"]:
        rows = db.q("SELECT id, title, domain, metric, headline, turns, created_at, updated_at, user_id FROM sessions WHERE user_id=? ORDER BY updated_at DESC", (ctx["id"],))
    else:
        rows = db.q("SELECT id, title, domain, metric, headline, turns, created_at, updated_at, user_id FROM sessions ORDER BY updated_at DESC LIMIT 100")
    for r in rows:
        r["running"] = r["id"] in RUNNING
    return rows


@app.post("/api/sessions")
def create_session(body: SessionIn, ctx=Depends(need("analysis:run"))):
    sid = uuid.uuid4().hex[:12]
    db.x("INSERT INTO sessions (id, user_id, title, domain, state, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
         (sid, ctx["id"], body.title or "新的归因分析", body.domain, "{}", db.now(), db.now()))
    db.audit(ctx["id"], "新建会话", sid)
    return dict(id=sid)


def view_model(A):
    """工作台需要的全部数据（不含检验代码以外的大字段）。"""
    if not A.get("intent"):
        return {}
    keys = ["intent", "decomp", "tree", "drills", "cross", "sankey", "series", "signals", "conclusion", "actions", "whatif", "whatif_result", "whatif_compare",
            "event_table", "narrative", "trace", "focus", "drill_history", "queries", "whatif_values", "saved_scenarios", "elapsed_s"]
    vm = {k: A.get(k) for k in keys}
    vm["hypotheses"] = [agent._hyp_card(h) for h in A.get("hypotheses", [])]
    vm["events"] = [dict(event_id=e["event_id"], title=e["title"], start=e["start_date"], end=e["end_date"], event_type=e["event_type"], source=e.get("source"))
                    for e in A.get("events_all", [])]
    vm["domain_title"] = DOMAINS[A["intent"]["domain"]].title
    return agent.clean(vm)


@app.get("/api/sessions/{sid}")
def get_session(sid: str, ctx=Depends(need("analysis:run", "analysis:view"))):
    s = _own(sid, ctx)
    msgs = db.q("SELECT id, role, content, events, created_at FROM messages WHERE session_id=? ORDER BY id", (sid,))
    for m in msgs:
        m["events"] = db.loads(m["events"], [])
    snaps = db.q("SELECT id, turn, route, question, created_at FROM snapshots WHERE session_id=? ORDER BY id", (sid,))
    return dict(session={k: s[k] for k in ("id", "title", "domain", "metric", "headline", "turns", "created_at", "updated_at")},
                messages=msgs, snapshots=snaps, state=view_model(_state(sid)), running=sid in RUNNING)


class RenameIn(BaseModel):
    title: str


@app.put("/api/sessions/{sid}")
def rename_session(sid: str, body: RenameIn, ctx=Depends(need("analysis:run"))):
    _own(sid, ctx, write=True)
    db.x("UPDATE sessions SET title=? WHERE id=?", (body.title.strip()[:60], sid))
    return dict(ok=True)


@app.delete("/api/sessions/{sid}")
def delete_session(sid: str, ctx=Depends(need("analysis:run"))):
    _own(sid, ctx, write=True)
    for t in ("messages", "snapshots"):
        db.x(f"DELETE FROM {t} WHERE session_id=?", (sid,))
    db.x("DELETE FROM sessions WHERE id=?", (sid,))
    CACHE.pop(sid, None)
    db.audit(ctx["id"], "删除会话", sid)
    return dict(ok=True)


@app.get("/api/sessions/{sid}/snapshots/{snap_id}")
def get_snapshot(sid: str, snap_id: int, ctx=Depends(need("analysis:run", "analysis:view"))):
    _own(sid, ctx)
    r = db.q1("SELECT * FROM snapshots WHERE id=? AND session_id=?", (snap_id, sid))
    if not r:
        raise HTTPException(404, "快照不存在")
    return dict(turn=r["turn"], question=r["question"], route=r["route"], state=view_model(db.loads(r["state"], {})))


class AskIn(BaseModel):
    question: str
    domain: Optional[str] = None


@app.post("/api/sessions/{sid}/ask")
def ask(sid: str, body: AskIn, ctx=Depends(need("analysis:run"))):
    s = _own(sid, ctx, write=True)
    q = body.question.strip()
    if not q:
        raise HTTPException(400, "问题不能为空")
    if sid in RUNNING:
        raise HTTPException(409, "该会话还有分析在执行，请稍候")
    A = _state(sid)
    A["custom_events"] = dashboard.custom_events()
    db.x("INSERT INTO messages (session_id, role, content, events, created_at) VALUES (?,?,?,?,?)", (sid, "user", q, "[]", db.now()))
    if s["title"] in ("新的归因分析", None, ""):
        db.x("UPDATE sessions SET title=? WHERE id=?", (q[:40], sid))
    qu = queue.Queue()
    events, answer = [], []
    info = dict(route="analyze")
    usage0 = dict(llm.USAGE)

    def emit(kind, data):
        ev = dict(event=kind, **agent.clean(data))
        qu.put(ev)
        if kind == "delta":
            return
        if kind == "answer":
            answer.append(data.get("text", ""))
        if kind == "route":
            info["route"] = data["route"]
        if kind in ("step", "route", "notice", "intent", "answer"):
            events.append(ev)
        else:
            events.append(dict(event=kind))  # 大块数据只记事件名，详细数据在会话状态里

    def work():
        t0 = time.time()
        err = None
        try:
            newA = agent.run(q, A=A, domain=body.domain or s["domain"] if not A.get("intent") else None, emit=emit)
            if newA.get("conclusion"):
                newA.pop("error", None)
            _save(sid, newA, info["route"], q)
            qu.put(dict(event="state", state=view_model(newA)))
        except Exception as e:  # noqa: BLE001
            import traceback
            err = f"{type(e).__name__}: {e}"
            traceback.print_exc()
            qu.put(dict(event="error", text=f"分析出错：{err}"))
            answer.append(f"分析出错：{err}")
        finally:
            db.x("INSERT INTO messages (session_id, role, content, events, created_at) VALUES (?,?,?,?,?)",
                 (sid, "assistant", "\n".join(answer), db.dumps(events), db.now()))
            steps = [e for e in events if e["event"] == "step" and e.get("status") in ("done", "error")]
            db.x("""INSERT INTO run_logs (session_id, user_id, question, route, status, ms, steps, llm_calls, prompt_tokens, completion_tokens, error, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (sid, ctx["id"], q, info["route"], "失败" if err else "成功", int((time.time() - t0) * 1000), db.dumps(steps),
                  llm.USAGE["calls"] - usage0["calls"], llm.USAGE["prompt_tokens"] - usage0["prompt_tokens"],
                  llm.USAGE["completion_tokens"] - usage0["completion_tokens"], err, db.now()))
            db.audit(ctx["id"], "归因分析" if info["route"] == "analyze" else "追问", sid, q)
            RUNNING.pop(sid, None)
            qu.put(None)

    th = threading.Thread(target=work, daemon=True)
    RUNNING[sid] = th
    th.start()

    def gen():
        while True:
            try:
                ev = qu.get(timeout=15)
            except queue.Empty:
                yield ": keep-alive\n\n"
                continue
            if ev is None:
                yield f"data: {json.dumps(dict(event='done'), ensure_ascii=False)}\n\n"
                break
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class ReviewIn(BaseModel):
    accepted: Optional[bool] = None


@app.patch("/api/sessions/{sid}/hypotheses/{hid}")
def review_hypothesis(sid: str, hid: str, body: ReviewIn, ctx=Depends(need("analysis:run"))):
    """分析师审核证据链：采纳 / 驳回 / 恢复默认，结论、行动项和 What-If 随之重算。"""
    _own(sid, ctx, write=True)
    A = _state(sid)
    h = next((h for h in A.get("hypotheses", []) if h["id"] == hid), None)
    if not h:
        raise HTTPException(404, "假设不存在")
    h["accepted"] = body.accepted
    A.setdefault("accepted", {})
    if body.accepted is None:
        A["accepted"].pop(h["title"], None)
    else:
        A["accepted"][h["title"]] = body.accepted
    agent.recompute(A)
    _save(sid, A)
    db.audit(ctx["id"], "审核假设", f"{sid}/{hid}", {True: "采纳", False: "驳回", None: "恢复默认"}[body.accepted])
    return view_model(A)


class WhatIfIn(BaseModel):
    values: dict = {}
    name: Optional[str] = None


@app.post("/api/sessions/{sid}/whatif")
def run_whatif(sid: str, body: WhatIfIn, ctx=Depends(need("analysis:run"))):
    _own(sid, ctx, write=True)
    A = _state(sid)
    if not A.get("whatif"):
        raise HTTPException(400, "先完成一次归因分析")
    res = agent.clean(whatif.simulate(A["whatif"], body.values))
    A["whatif_values"] = body.values
    A["whatif_result"] = res
    if body.name:
        A.setdefault("saved_scenarios", [])
        A["saved_scenarios"] = [s for s in A["saved_scenarios"] if s["name"] != body.name] + [dict(name=body.name, values=res["values"], predicted=res["predicted"],
                                                                                                    delta=res["delta"], band=res["band"])]
        db.audit(ctx["id"], "保存情景", sid, body.name)
    CACHE[sid] = A
    if body.name:
        _save(sid, A)
    return dict(result=res, saved=A.get("saved_scenarios", []), presets=A.get("whatif_compare", []))


@app.delete("/api/sessions/{sid}/scenarios/{name}")
def delete_scenario(sid: str, name: str, ctx=Depends(need("analysis:run"))):
    _own(sid, ctx, write=True)
    A = _state(sid)
    A["saved_scenarios"] = [s for s in A.get("saved_scenarios", []) if s["name"] != name]
    _save(sid, A)
    return dict(saved=A["saved_scenarios"])


@app.get("/api/sessions/{sid}/pivot")
def get_pivot(sid: str, d1: str, d2: Optional[str] = None, ctx=Depends(need("analysis:run", "analysis:view"))):
    _own(sid, ctx)
    A = _state(sid)
    dom = DOMAINS[A["intent"]["domain"]]
    ok = set(dom.DIMS) | ({"product"} if dom.id == "ecommerce" else {"sku"})
    if d1 not in ok or (d2 and d2 not in ok) or d1 == d2:
        raise HTTPException(400, "维度不合法")
    return agent.clean(drill.pivot(dom, A["intent"], d1, d2 or None))


@app.get("/api/sessions/{sid}/decompose")
def sub_decompose(sid: str, metric: str, ctx=Depends(need("analysis:run", "analysis:view"))):
    """点击拆解树节点：对子指标再做一次拆解（如客单价 = 件单价 × 每单件数）。"""
    _own(sid, ctx)
    A = _state(sid)
    dom = DOMAINS[A["intent"]["domain"]]
    if metric not in dom.METRICS:
        raise HTTPException(400, "未知指标")
    return agent.clean(dom.decompose(metric, A["intent"]["base"], A["intent"]["current"]))


class CodeIn(BaseModel):
    code: str


@app.post("/api/sessions/{sid}/hypotheses/{hid}/rerun")
def rerun_hypothesis(sid: str, hid: str, body: CodeIn, ctx=Depends(need("analysis:run"))):
    """分析师修改检验代码后重新在沙箱执行（同样受静态检查和配额限制）。"""
    _own(sid, ctx, write=True)
    A = _state(sid)
    h = next((h for h in A.get("hypotheses", []) if h["id"] == hid), None)
    if not h:
        raise HTTPException(404, "假设不存在")
    dom = DOMAINS[A["intent"]["domain"]]
    r = sandbox.run(body.code, dom.db)
    if not r["ok"]:
        return dict(ok=False, error=r["error"], stdout=r.get("stdout"))
    res = r["result"]
    if not isinstance(res, dict) or not {"effect", "verdict", "finding"} <= set(res):
        return dict(ok=False, error="代码需要赋值 result = dict(effect=..., verdict=..., finding=...)")
    h["code"], h["result"], h["source"] = body.code, agent.clean(res), "分析师修改"
    h.pop("error", None)
    agent.recompute(A)
    _save(sid, A)
    db.audit(ctx["id"], "修改检验代码", f"{sid}/{hid}")
    return dict(ok=True, state=view_model(A))


# ---------------------------------------------------------------- 查数

class SqlIn(BaseModel):
    question: str
    domain: str = "ecommerce"
    mode: Optional[str] = None


@app.post("/api/sql")
def nl2sql(body: SqlIn, ctx=Depends(need("analysis:run"))):
    if body.domain not in DOMAINS:
        raise HTTPException(400, "未知指标域")
    r = sqltool.ask(DOMAINS[body.domain].db, body.question, mode=body.mode)
    db.audit(ctx["id"], "自然语言查数", body.domain, f"{body.question} → {r.get('sql', '')}")
    return agent.clean(r)


# ---------------------------------------------------------------- 报告

class ReportIn(BaseModel):
    title: Optional[str] = None


def _report_meta(rid):
    notes = db.q("SELECT section, text, author, created_at AS created FROM annotations WHERE report_id=? ORDER BY id", (rid,))
    return notes


@app.post("/api/sessions/{sid}/report")
def make_report(sid: str, body: ReportIn, ctx=Depends(need("report:edit"))):
    _own(sid, ctx, write=True)
    A = _state(sid)
    if not A.get("conclusion"):
        raise HTTPException(400, "会话还没有完成归因分析")
    old = db.q1("SELECT id, title FROM reports WHERE session_id=? ORDER BY id DESC LIMIT 1", (sid,))
    title = body.title or (old or {}).get("title")
    R = report.build(A, dict(title=title, author=ctx["name"]))
    if old:
        db.x("UPDATE reports SET title=?, content=?, updated_at=? WHERE id=?", (R["title"], db.dumps(R), db.now(), old["id"]))
        rid = old["id"]
    else:
        rid = db.x("INSERT INTO reports (session_id, domain, title, content, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                   (sid, A["intent"]["domain"], R["title"], db.dumps(R), ctx["id"], db.now(), db.now()))
    db.audit(ctx["id"], "生成报告", rid)
    return dict(id=rid)


@app.get("/api/reports")
def list_reports(ctx=Depends(need("report:view", "report:edit"))):
    if "report:edit" in ctx["perms"]:
        rows = db.q("SELECT r.id, r.title, r.domain, r.status, r.session_id, r.created_at, r.updated_at, r.published_at, u.name AS author FROM reports r LEFT JOIN users u ON u.id = r.created_by ORDER BY r.updated_at DESC")
    else:
        rows = db.q("SELECT r.id, r.title, r.domain, r.status, r.session_id, r.created_at, r.updated_at, r.published_at, u.name AS author FROM reports r LEFT JOIN users u ON u.id = r.created_by WHERE r.status='已发布' ORDER BY r.published_at DESC")
    return rows


def _get_report(rid, ctx):
    r = db.q1("SELECT * FROM reports WHERE id=?", (rid,))
    if not r:
        raise HTTPException(404, "报告不存在")
    if r["status"] != "已发布" and "report:edit" not in ctx["perms"]:
        raise HTTPException(403, "报告未发布")
    R = db.loads(r["content"], {})
    R["annotations"] = _report_meta(rid)
    R["id"], R["status"], R["session_id"], R["published_at"] = r["id"], r["status"], r["session_id"], r["published_at"]
    return R


@app.get("/api/reports/{rid}")
def get_report(rid: int, ctx=Depends(need("report:view", "report:edit"))):
    return _get_report(rid, ctx)


class ReportEdit(BaseModel):
    title: Optional[str] = None
    summary: Optional[str] = None
    section_texts: Optional[dict] = None
    status: Optional[str] = None


@app.put("/api/reports/{rid}")
def edit_report(rid: int, body: ReportEdit, ctx=Depends(need("report:edit"))):
    r = db.q1("SELECT * FROM reports WHERE id=?", (rid,))
    if not r:
        raise HTTPException(404, "报告不存在")
    R = db.loads(r["content"], {})
    if body.title:
        R["title"] = body.title.strip()
    if body.summary is not None:
        R["summary"] = body.summary
    for k, v in (body.section_texts or {}).items():
        for s in R["sections"]:
            if s["key"] == k:
                s["text"] = v
    status, pub = r["status"], r["published_at"]
    if body.status in ("草稿", "已发布"):
        status = body.status
        pub = db.now() if status == "已发布" else None
    db.x("UPDATE reports SET title=?, content=?, status=?, published_at=?, updated_at=? WHERE id=?", (R["title"], db.dumps(R), status, pub, db.now(), rid))
    db.audit(ctx["id"], "发布报告" if body.status == "已发布" else "编辑报告", rid)
    return _get_report(rid, ctx)


class NoteIn(BaseModel):
    section: str
    text: str


@app.post("/api/reports/{rid}/annotations")
def add_note(rid: int, body: NoteIn, ctx=Depends(need("report:edit", "report:view"))):
    if not db.q1("SELECT 1 FROM reports WHERE id=?", (rid,)):
        raise HTTPException(404, "报告不存在")
    db.x("INSERT INTO annotations (report_id, section, text, user_id, author, created_at) VALUES (?,?,?,?,?,?)",
         (rid, body.section, body.text.strip()[:1000], ctx["id"], ctx["name"], db.now()))
    db.audit(ctx["id"], "添加批注", rid, body.text[:100])
    return dict(annotations=_report_meta(rid))


@app.get("/api/reports/{rid}/export")
def export_report(rid: int, fmt: str = "md", ctx=Depends(need("report:export"))):
    R = _get_report(rid, ctx)
    name = R["title"]
    db.audit(ctx["id"], "导出报告", rid, fmt)
    from urllib.parse import quote
    if fmt == "md":
        data, mt, ext = report.to_markdown(R).encode("utf-8"), "text/markdown; charset=utf-8", "md"
    elif fmt == "xlsx":
        data, mt, ext = report.to_excel(R), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"
    elif fmt == "pdf":
        data, mt, ext = report.to_pdf(R), "application/pdf", "pdf"
    else:
        raise HTTPException(400, "只支持 md / xlsx / pdf")
    return Response(data, media_type=mt, headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}.{ext}"})


# ---------------------------------------------------------------- 简报订阅

class SubIn(BaseModel):
    domain: str
    frequency: str = "每周"


@app.get("/api/subscriptions")
def list_subs(ctx=Depends(need("brief:subscribe"))):
    return db.q("SELECT * FROM subscriptions WHERE user_id=?", (ctx["id"],))


@app.post("/api/subscriptions")
def subscribe(body: SubIn, ctx=Depends(need("brief:subscribe"))):
    db.x("INSERT INTO subscriptions (user_id, domain, frequency, created_at) VALUES (?,?,?,?) ON CONFLICT(user_id, domain) DO UPDATE SET frequency=excluded.frequency",
         (ctx["id"], body.domain, body.frequency, db.now()))
    db.audit(ctx["id"], "订阅简报", body.domain, body.frequency)
    return list_subs(ctx)


@app.delete("/api/subscriptions/{domain}")
def unsubscribe(domain: str, ctx=Depends(need("brief:subscribe"))):
    db.x("DELETE FROM subscriptions WHERE user_id=? AND domain=?", (ctx["id"], domain))
    return list_subs(ctx)


@app.get("/api/briefs")
def briefs(ctx=Depends(need("brief:subscribe", "report:edit"))):
    """周期性归因简报：每个订阅的指标域取大盘异动 + 最近一份已发布报告的结论与行动项。"""
    subs = db.q("SELECT domain, frequency FROM subscriptions WHERE user_id=?", (ctx["id"],)) or [dict(domain=d, frequency="—") for d in DOMAINS]
    out = []
    for s in subs:
        dash = dashboard.build(s["domain"])
        r = db.q1("SELECT id, title, content, published_at FROM reports WHERE domain=? AND status='已发布' ORDER BY published_at DESC LIMIT 1", (s["domain"],))
        R = db.loads(r["content"], {}) if r else None
        actions = next((sec["tables"][0]["rows"] for sec in (R or {}).get("sections", []) if sec["key"] == "actions"), [])
        out.append(agent.clean(dict(domain=s["domain"], title=dash["title"], frequency=s["frequency"], cards=[dict(key=c["key"], name=c["name"], value=c["value"], mom=c["mom"], flag=c["flag"], unit=c["unit"], ratio=c.get("ratio"))
                                                                                                       for c in dash["cards"]],
                                    anomalies=dash["anomalies"][:5], report=dict(id=r["id"], title=r["title"], published_at=r["published_at"], summary=R["summary"]) if r else None,
                                    actions=actions[:5])))
    return out


# ---------------------------------------------------------------- 事件日历

class EventIn(BaseModel):
    domain: str
    start_date: str
    end_date: str
    event_type: str
    title: str
    description: str = ""
    channel_id: Optional[str] = None
    category_id: Optional[str] = None
    product_line: Optional[str] = None


EVENT_TYPES = ["营销活动", "投放调整", "系统故障", "产品发布", "竞品动态", "供应链", "采购成本", "渠道拓展", "渠道政策", "物流调整", "新品上市", "政策变化", "宏观"]


@app.get("/api/events")
def list_events(domain: str = "ecommerce", ctx=Depends(need("events:view"))):
    dom = DOMAINS[domain]
    custom = dashboard.custom_events()
    ids = {c["event_id"]: c["id"] for c in custom}
    out = []
    for e in dom.event_records(custom):
        e = dict(e)
        e["editable"] = e.get("source") == "补录"
        e["id"] = ids.get(e["event_id"])
        out.append(e)
    dims = {}
    if domain == "ecommerce":
        dims["channels"] = dom.q("SELECT channel_id AS value, channel_name AS label FROM dim_channel").to_dict("records")
        dims["categories"] = dom.q("SELECT category_id AS value, category_name AS label FROM dim_category").to_dict("records")
    else:
        dims["channels"] = dom.q("SELECT channel_id AS value, channel_name AS label FROM dim_channel").to_dict("records")
        dims["product_lines"] = [dict(value=x, label=x) for (x,) in dom.q("SELECT DISTINCT product_line FROM dim_sku").values]
    return agent.clean(dict(events=out, types=EVENT_TYPES, dims=dims))


def _check_event(body):
    if body.domain not in DOMAINS:
        raise HTTPException(400, "未知指标域")
    if body.end_date < body.start_date:
        raise HTTPException(400, "结束日期不能早于开始日期")
    if body.event_type not in EVENT_TYPES:
        raise HTTPException(400, "事件类型不合法")
    if not body.title.strip():
        raise HTTPException(400, "标题不能为空")


@app.post("/api/events")
def add_event(body: EventIn, ctx=Depends(need("events:edit"))):
    _check_event(body)
    eid = db.x("""INSERT INTO custom_events (domain, start_date, end_date, event_type, title, description, channel_id, category_id, product_line, created_by, created_at)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (body.domain, body.start_date, body.end_date, body.event_type, body.title.strip(), body.description,
                                                      body.channel_id or None, body.category_id or None, body.product_line or None, ctx["id"], db.now()))
    db.x("UPDATE custom_events SET event_id=? WHERE id=?", (f"U{eid}", eid))
    db.audit(ctx["id"], "新增事件", f"U{eid}", body.title)
    return dict(id=eid)


@app.put("/api/events/{eid}")
def edit_event(eid: int, body: EventIn, ctx=Depends(need("events:edit"))):
    _check_event(body)
    if not db.q1("SELECT 1 FROM custom_events WHERE id=?", (eid,)):
        raise HTTPException(404, "只能编辑补录的事件（数仓事件只读）")
    db.x("""UPDATE custom_events SET domain=?, start_date=?, end_date=?, event_type=?, title=?, description=?, channel_id=?, category_id=?, product_line=? WHERE id=?""",
         (body.domain, body.start_date, body.end_date, body.event_type, body.title.strip(), body.description, body.channel_id or None, body.category_id or None,
          body.product_line or None, eid))
    db.audit(ctx["id"], "编辑事件", f"U{eid}", body.title)
    return dict(ok=True)


@app.delete("/api/events/{eid}")
def delete_event(eid: int, ctx=Depends(need("events:edit"))):
    db.x("DELETE FROM custom_events WHERE id=?", (eid,))
    db.audit(ctx["id"], "删除事件", f"U{eid}")
    return dict(ok=True)


# ---------------------------------------------------------------- 指标字典

KIND = {"gmv": "复合", "net_gmv": "派生", "order_cnt": "原子", "uv": "原子", "cvr": "派生", "aov": "复合", "asp": "派生", "ipo": "派生", "refund_rate": "派生",
        "new_user_share": "派生", "revenue": "原子", "cogs": "原子", "gross_profit": "派生", "gross_margin": "派生", "net_margin": "复合", "discount_rate": "派生",
        "logistics_rate": "派生", "commission_rate": "派生", "marketing_rate": "派生", "cogs_rate": "派生", "net_profit": "派生"}
PARENT = {"uv": "gmv", "cvr": "gmv", "aov": "gmv", "asp": "aov", "ipo": "aov", "cogs_rate": "net_margin", "logistics_rate": "net_margin",
          "commission_rate": "net_margin", "marketing_rate": "net_margin", "gross_margin": "net_margin"}
MODEL = {"gmv": "乘法（LMDI）", "aov": "乘法（LMDI）", "order_cnt": "乘法（LMDI）", "net_margin": "加法（差额）", "gross_margin": "加法（差额）"}


def seed_metrics():
    if db.q1("SELECT 1 FROM metric_defs"):
        return
    for d in DOMAINS.values():
        try:
            df = d.metric_dict()
        except Exception:
            continue
        for r in df.to_dict("records"):
            db.x("""INSERT OR IGNORE INTO metric_defs (domain, code, name, kind, aliases, formula, decomposition, parent, model, description, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (d.id, r["metric_code"], r["metric_name"], KIND.get(r["metric_code"], "派生"), r.get("aliases") or "",
                                                         r.get("definition") or "", r.get("decomposition") or "", PARENT.get(r["metric_code"]),
                                                         MODEL.get(r["metric_code"], ""), "", db.now()))


@app.get("/api/metrics")
def list_metrics(domain: str = "ecommerce", ctx=Depends(need("metrics:view"))):
    rows = db.q("SELECT * FROM metric_defs WHERE domain=? ORDER BY CASE kind WHEN '复合' THEN 0 WHEN '原子' THEN 1 ELSE 2 END, id", (domain,))
    return dict(metrics=rows, tree=DOMAINS[domain].tree())


class MetricIn(BaseModel):
    domain: str
    code: str
    name: str
    kind: str = "派生"
    aliases: str = ""
    formula: str = ""
    decomposition: str = ""
    parent: Optional[str] = None
    model: str = ""
    description: str = ""


@app.post("/api/metrics")
def add_metric(body: MetricIn, ctx=Depends(need("metrics:edit"))):
    if body.kind not in ("原子", "派生", "复合"):
        raise HTTPException(400, "类型只能是 原子 / 派生 / 复合")
    try:
        db.x("""INSERT INTO metric_defs (domain, code, name, kind, aliases, formula, decomposition, parent, model, description, updated_by, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (body.domain, body.code.strip(), body.name.strip(), body.kind, body.aliases, body.formula, body.decomposition,
                                                     body.parent or None, body.model, body.description, ctx["id"], db.now()))
    except Exception:
        raise HTTPException(400, "指标编码已存在")
    db.audit(ctx["id"], "新增指标", f"{body.domain}/{body.code}")
    return dict(ok=True)


@app.put("/api/metrics/{mid}")
def edit_metric(mid: int, body: MetricIn, ctx=Depends(need("metrics:edit"))):
    if body.kind not in ("原子", "派生", "复合"):
        raise HTTPException(400, "类型只能是 原子 / 派生 / 复合")
    db.x("""UPDATE metric_defs SET name=?, kind=?, aliases=?, formula=?, decomposition=?, parent=?, model=?, description=?, updated_by=?, updated_at=? WHERE id=?""",
         (body.name, body.kind, body.aliases, body.formula, body.decomposition, body.parent or None, body.model, body.description, ctx["id"], db.now(), mid))
    db.audit(ctx["id"], "编辑指标", mid)
    return dict(ok=True)


# ---------------------------------------------------------------- 配置、审计

class SettingsIn(BaseModel):
    sql_mode: Optional[str] = None
    confidence_threshold: Optional[float] = None
    sandbox_timeout: Optional[float] = None
    sandbox_memory_mb: Optional[int] = None
    dash_month_threshold: Optional[float] = None
    dash_day_threshold: Optional[float] = None


@app.put("/api/settings")
def put_settings(body: SettingsIn, ctx=Depends(need("settings:edit", "analysis:run"))):
    admin_only = [k for k in ("confidence_threshold", "sandbox_timeout", "sandbox_memory_mb", "dash_month_threshold", "dash_day_threshold") if getattr(body, k) is not None]
    if admin_only and "settings:edit" not in ctx["perms"]:
        raise HTTPException(403, "只有管理员可以修改阈值和沙箱配额")
    if body.sql_mode:
        if body.sql_mode not in sqltool.MODES:
            raise HTTPException(400, "未知模型")
        if body.sql_mode not in sqltool.available_modes():
            raise HTTPException(400, f"{sqltool.MODES[body.sql_mode]} 未配置（{'LLM_API_KEY' if body.sql_mode == 'api' else 'SQL_LLM_BASE_URL'}）")
        sqltool.set_mode(body.sql_mode)
        db.set_setting("sql_mode", body.sql_mode)
    if body.confidence_threshold is not None:
        if not 0 < body.confidence_threshold < 1:
            raise HTTPException(400, "置信度阈值应在 0 到 1 之间")
        config.CONFIDENCE_THRESHOLD = body.confidence_threshold
        db.set_setting("confidence_threshold", body.confidence_threshold)
    if body.sandbox_timeout is not None:
        config.SANDBOX_TIMEOUT = max(2.0, min(120.0, body.sandbox_timeout))
        db.set_setting("sandbox_timeout", config.SANDBOX_TIMEOUT)
    if body.sandbox_memory_mb is not None:
        config.SANDBOX_MEMORY_MB = max(256, min(8192, body.sandbox_memory_mb))
        db.set_setting("sandbox_memory_mb", config.SANDBOX_MEMORY_MB)
    for k in ("dash_month_threshold", "dash_day_threshold"):
        if getattr(body, k) is not None:
            db.set_setting(k, getattr(body, k))
    db.audit(ctx["id"], "修改配置", "", body.model_dump(exclude_none=True))
    return system(ctx)


@app.get("/api/audit")
def audit_logs(limit: int = 200, ctx=Depends(need("audit:view"))):
    return db.q("SELECT a.*, u.name AS user_name FROM audit_logs a LEFT JOIN users u ON u.id = a.user_id ORDER BY a.id DESC LIMIT ?", (min(limit, 1000),))


@app.get("/api/runlogs")
def run_logs(limit: int = 100, ctx=Depends(need("audit:view"))):
    rows = db.q("SELECT r.*, u.name AS user_name FROM run_logs r LEFT JOIN users u ON u.id = r.user_id ORDER BY r.id DESC LIMIT ?", (min(limit, 500),))
    for r in rows:
        r["steps"] = db.loads(r["steps"], [])
    return rows


# ---------------------------------------------------------------- 静态页面

if (WEB / "vendor").exists():
    app.mount("/vendor", StaticFiles(directory=WEB / "vendor"), name="vendor")


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/{name}.{ext}")
def static_file(name: str, ext: str):
    p = WEB / f"{name}.{ext}"
    if ext not in ("js", "css", "html", "svg", "png", "ico") or not p.exists():
        raise HTTPException(404)
    return FileResponse(p)
