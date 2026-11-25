"""沙箱子进程入口（由 sandbox.py 以 python -I 启动，从标准输入读任务，最后一行输出 __RESULT__{json}）。"""
import json
import re
import sqlite3
import sys
import traceback


def main():
    task = json.loads(sys.stdin.read())
    try:  # 内存和 CPU 配额（仅类 Unix 系统支持）
        import resource
        mem = task["memory_mb"] * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_CPU, (task["cpu_s"], task["cpu_s"]))
    except Exception:
        pass
    import numpy as np
    import pandas as pd

    write_re = re.compile(r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|vacuum|reindex|truncate)\b|\breplace\b(?!\s*\()", re.I)
    con = sqlite3.connect(f"file:{task['db_path']}?mode=ro", uri=True)
    queries = []

    def q(sql, params=()):
        s = sql.strip().rstrip(";")
        if ";" in s or not re.match(r"^\s*(select|with)\b", s, re.I) or write_re.search(re.sub(r"'[^']*'", "''", s)):
            raise PermissionError("沙箱只允许单条只读 SELECT 查询")
        for t in task["restricted"]:
            if re.search(rf"\b{t}\b", s.lower()):
                raise PermissionError(f"禁止访问受限表 {t}")
        cur = con.execute(s, params)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(task["max_rows"])
        queries.append(re.sub(r"\s+", " ", s)[:300])
        return pd.DataFrame(rows, columns=cols)

    env = {"q": q, "pd": pd, "np": np, "__builtins__": __builtins__}
    try:
        from scipy import stats
        env["stats"] = stats
    except Exception:
        pass

    def default(o):
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.floating,)):
            return None if np.isnan(o) else float(o)
        if isinstance(o, (np.bool_,)):
            return bool(o)
        if isinstance(o, pd.DataFrame):
            return o.to_dict("records")
        if isinstance(o, pd.Series):
            return o.to_dict()
        return str(o)

    try:
        exec(compile(task["code"], "<hypothesis>", "exec"), env)
        result = env.get("result")
        if not isinstance(result, dict):
            raise ValueError("代码没有给 result 赋值一个 dict")
        result["queries"] = queries
        out = {"result": result}
    except Exception as e:
        tb = traceback.format_exc().strip().splitlines()
        line = next((l for l in reversed(tb) if "<hypothesis>" in l), "")
        out = {"error": f"{type(e).__name__}: {e} {line.strip()}"}
    print("__RESULT__" + json.dumps(out, ensure_ascii=False, default=default))


if __name__ == "__main__":
    main()
