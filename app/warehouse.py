"""数仓只读访问。所有查询都走这里：只读连接、只允许 SELECT/WITH、受限表拦截、超时中断。"""
import re
import sqlite3
import time
from pathlib import Path

import pandas as pd

from . import config

WRITE_RE = re.compile(r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|vacuum|reindex|truncate)\b|\breplace\b(?!\s*\()", re.I)
RESTRICTED = {"ecommerce": {"dim_user"}, "fmcg": set()}
DB_FILES = {"ecommerce": "ecommerce.db", "fmcg": "fmcg.db"}


class QueryError(Exception):
    pass


def db_path(db):
    p = Path(config.WAREHOUSE_DIR) / DB_FILES[db]
    if not p.exists():
        raise QueryError(f"找不到数仓文件：{p}")
    return p


def check_sql(sql, db):
    s = sql.strip().rstrip(";")
    if ";" in s:
        return "只允许单条语句"
    if not re.match(r"^\s*(select|with)\b", s, re.I):
        return "只允许 SELECT 查询"
    if WRITE_RE.search(re.sub(r"'[^']*'", "''", s)):
        return "包含写操作关键字"
    low = s.lower()
    for t in RESTRICTED.get(db, ()):
        if re.search(rf"\b{t}\b", low):
            return f"访问了受限表 {t}"
    return None


def connect(db):
    con = sqlite3.connect(f"file:{db_path(db)}?mode=ro", uri=True, check_same_thread=False)
    return con


def query(db, sql, params=(), timeout_s=15.0, max_rows=200000):
    err = check_sql(sql, db)
    if err:
        raise QueryError(err)
    con = connect(db)
    deadline = time.time() + timeout_s
    con.set_progress_handler(lambda: 1 if time.time() > deadline else 0, 20000)
    try:
        cur = con.execute(sql, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(max_rows + 1)
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e):
            raise QueryError(f"查询超过 {timeout_s:.0f} 秒被中断")
        raise QueryError(str(e))
    finally:
        con.close()
    if len(rows) > max_rows:
        rows = rows[:max_rows]
    return pd.DataFrame(rows, columns=cols)


def scalar(db, sql, params=()):
    df = query(db, sql, params)
    return None if df.empty else df.iloc[0, 0]
