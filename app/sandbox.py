"""假设验证沙箱：在独立子进程里执行大模型或检查库生成的 Python 代码。

安全措施（多层）：
1. 静态检查：解析语法树，只允许导入 pandas / numpy / scipy.stats / math / statistics / json / datetime / collections；
   禁止 open、exec、eval、__import__、getattr 等内置函数，禁止访问双下划线属性，禁止 pandas/numpy 的文件读写接口（read_*、to_csv 等）。
2. 只读数据：代码只能通过注入的 q(sql, params) 读数，q 走只读连接，只允许单条 SELECT，拦截受限表（dim_user）。
3. 资源配额：独立进程、超时强制结束；Linux / macOS 上再限制内存（RLIMIT_AS）和 CPU 时间。
4. 进程隔离：以 -I（隔离模式）启动解释器，不读用户环境变量和当前目录下的模块。
代码必须给变量 result 赋值一个 dict：effect（对目标指标变动的贡献）、confidence（0 到 1）、verdict、finding 等。
"""
import ast
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import config
from .warehouse import RESTRICTED, db_path

RUNNER = Path(__file__).with_name("sandbox_runner.py")
ALLOWED_IMPORTS = {"pandas", "numpy", "math", "statistics", "json", "datetime", "collections", "itertools", "scipy", "scipy.stats"}
FORBIDDEN_CALLS = {"open", "exec", "eval", "compile", "__import__", "input", "breakpoint", "globals", "locals", "vars",
                   "getattr", "setattr", "delattr", "memoryview", "exit", "quit", "help", "dir"}


# 文件与进程相关的属性：pandas / numpy 的读写文件接口、系统调用
SAFE_TO = {"to_dict", "to_list", "to_numpy", "to_frame", "to_records", "to_string", "to_datetime", "to_numeric", "to_timedelta", "to_period", "to_timestamp"}
FORBIDDEN_ATTRS = {"eval", "system", "popen", "spawn", "load", "save", "savez", "tofile", "fromfile", "loadtxt", "savetxt", "genfromtxt", "memmap",
                   "ctypeslib", "DataSource", "lib", "testing", "f2py", "distutils", "connect", "execute", "remove", "unlink", "rmdir", "chmod"}


class SandboxRejected(Exception):
    pass


def static_check(code):
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise SandboxRejected(f"语法错误：第 {e.lineno} 行 {e.msg}")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name not in ALLOWED_IMPORTS:
                    raise SandboxRejected(f"不允许导入 {a.name}")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "") not in ALLOWED_IMPORTS:
                raise SandboxRejected(f"不允许导入 {node.module}")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_CALLS:
            raise SandboxRejected(f"不允许使用 {node.id}")
        elif isinstance(node, ast.Attribute):
            a = node.attr
            if a.startswith("__"):
                raise SandboxRejected(f"不允许访问 {a}")
            if a.startswith("read_") or (a.startswith("to_") and a not in SAFE_TO) or a in FORBIDDEN_ATTRS:
                raise SandboxRejected(f"不允许调用 {a}（文件或系统操作）")
    return True


def run(code, db, timeout=None, memory_mb=None):
    t0 = time.time()
    try:
        static_check(code)
    except SandboxRejected as e:
        return dict(ok=False, error=f"沙箱拒绝执行：{e}", elapsed_ms=0, stdout="")
    payload = dict(code=code, db_path=str(db_path(db)), restricted=sorted(RESTRICTED.get(db, ())),
                   max_rows=config.SANDBOX_MAX_ROWS, memory_mb=memory_mb or config.SANDBOX_MEMORY_MB,
                   cpu_s=int((timeout or config.SANDBOX_TIMEOUT) + 5))
    try:
        # 数值库线程数固定为 1：多核机器上 OpenBLAS 会按核数预分配线程缓冲区，在内存配额（RLIMIT_AS）下卡死
        # （128 核的云主机上实测：不限线程时导入 numpy 就超时）；单线程对这里的小规模统计也更快
        env = dict(os.environ, OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", NUMEXPR_NUM_THREADS="1")
        p = subprocess.run([sys.executable, "-I", str(RUNNER)], input=json.dumps(payload), capture_output=True,
                           text=True, encoding="utf-8", timeout=timeout or config.SANDBOX_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        return dict(ok=False, error=f"执行超过 {timeout or config.SANDBOX_TIMEOUT:.0f} 秒，已强制结束", elapsed_ms=int((time.time() - t0) * 1000), stdout="")
    elapsed = int((time.time() - t0) * 1000)
    out = p.stdout.strip().splitlines()
    marker = next((i for i in range(len(out) - 1, -1, -1) if out[i].startswith("__RESULT__")), None)
    if marker is None:
        err = (p.stderr or "").strip().splitlines()
        return dict(ok=False, error=(err[-1] if err else f"进程退出码 {p.returncode}")[:500], elapsed_ms=elapsed, stdout="\n".join(out)[-2000:])
    res = json.loads(out[marker][len("__RESULT__"):])
    stdout = "\n".join(out[:marker])[-2000:]
    if res.get("error"):
        return dict(ok=False, error=res["error"][:800], elapsed_ms=elapsed, stdout=stdout)
    return dict(ok=True, result=res["result"], elapsed_ms=elapsed, stdout=stdout)
