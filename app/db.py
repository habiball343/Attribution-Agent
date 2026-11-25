"""应用库（SQLite）：用户、会话与消息、分析快照、补录事件、指标字典、报告与批注、订阅、配置、审计与运行日志。
数仓（ecommerce.db / fmcg.db）只读，应用自己的数据都在这里。"""
import json
import sqlite3
import threading
from datetime import datetime

from .config import APP_DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT UNIQUE, name TEXT, password_hash TEXT, role TEXT, status TEXT DEFAULT 'active', created_at TEXT);
CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, user_id INTEGER, title TEXT, domain TEXT, metric TEXT, headline TEXT,
  state TEXT, turns INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, events TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_msg_s ON messages(session_id);
CREATE TABLE IF NOT EXISTS snapshots (id INTEGER PRIMARY KEY, session_id TEXT, turn INTEGER, route TEXT, question TEXT, state TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS custom_events (id INTEGER PRIMARY KEY, domain TEXT, event_id TEXT, start_date TEXT, end_date TEXT, event_type TEXT, title TEXT,
  description TEXT, channel_id TEXT, category_id TEXT, product_line TEXT, created_by INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS metric_defs (id INTEGER PRIMARY KEY, domain TEXT, code TEXT, name TEXT, kind TEXT, aliases TEXT, formula TEXT,
  decomposition TEXT, parent TEXT, model TEXT, description TEXT, updated_by INTEGER, updated_at TEXT, UNIQUE(domain, code));
CREATE TABLE IF NOT EXISTS reports (id INTEGER PRIMARY KEY, session_id TEXT, domain TEXT, title TEXT, status TEXT DEFAULT '草稿', content TEXT,
  created_by INTEGER, created_at TEXT, updated_at TEXT, published_at TEXT);
CREATE TABLE IF NOT EXISTS annotations (id INTEGER PRIMARY KEY, report_id INTEGER, section TEXT, text TEXT, user_id INTEGER, author TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS subscriptions (id INTEGER PRIMARY KEY, user_id INTEGER, domain TEXT, frequency TEXT, created_at TEXT, UNIQUE(user_id, domain));
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS audit_logs (id INTEGER PRIMARY KEY, user_id INTEGER, action TEXT, target TEXT, detail TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS run_logs (id INTEGER PRIMARY KEY, session_id TEXT, user_id INTEGER, question TEXT, route TEXT, status TEXT, ms INTEGER,
  steps TEXT, llm_calls INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER, error TEXT, created_at TEXT);
"""

_lock = threading.RLock()
_conn = None


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def conn():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(APP_DB, check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA)
    return _conn


def q(sql, params=()):
    with _lock:
        return [dict(r) for r in conn().execute(sql, params).fetchall()]


def q1(sql, params=()):
    r = q(sql, params)
    return r[0] if r else None


def x(sql, params=()):
    with _lock:
        return conn().execute(sql, params).lastrowid


def dumps(v):
    return json.dumps(v, ensure_ascii=False)


def loads(s, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


def audit(user_id, action, target="", detail=""):
    x("INSERT INTO audit_logs (user_id, action, target, detail, created_at) VALUES (?,?,?,?,?)", (user_id, action, str(target), str(detail)[:2000], now()))


def get_setting(key, default=None):
    r = q1("SELECT value FROM settings WHERE key=?", (key,))
    return loads(r["value"], default) if r else default


def set_setting(key, value):
    x("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, dumps(value)))
