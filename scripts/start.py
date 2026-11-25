"""一键启动：检查数仓文件，启动服务并打开浏览器。首次启动自动创建三个演示账号和指标字典。

用法：python scripts/start.py [--port 8001] [--no-browser]
"""
import argparse
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8001)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    from app import config
    for f in ("ecommerce.db", "fmcg.db"):
        if not (config.WAREHOUSE_DIR / f).exists():
            print(f"找不到数仓文件 {config.WAREHOUSE_DIR / f}。请确认“数据”目录完整，或在 .env 里设置 WAREHOUSE_DIR。")
            sys.exit(1)
    llm = f"在线（{config.LLM_MODEL}）" if config.LLM_API_KEY else "离线规则模式（未配置 LLM_API_KEY：意图解析、假设、结论由规则引擎完成）"
    sql = config.SQL_LLM_BASE_URL or "未配置（自然语言查数使用分析大模型）"
    url = f"http://{args.host}:{args.port}"
    print(f"\n分析模型：{llm}\n微调 SQL 模型：{sql}\n地址：{url}\n"
          "演示账号：analyst / analyst123（经营分析师）  manager / manager123（业务负责人）  admin / admin123（数据管理员）\n")
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    import uvicorn
    uvicorn.run("app.main:app", host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
