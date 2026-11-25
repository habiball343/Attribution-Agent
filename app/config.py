"""配置：环境变量优先，其次项目根目录的 .env。"""
import json
import os
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]


def _load_dotenv():
    f = BASE / ".env"
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

WAREHOUSE_DIR = Path(os.getenv("WAREHOUSE_DIR", BASE / "数据"))
DATA_DIR = Path(os.getenv("APP_DATA_DIR", BASE / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
APP_DB = DATA_DIR / "app.sqlite3"
EXPORT_DIR = DATA_DIR / "exports"
EXPORT_DIR.mkdir(exist_ok=True)

# 分析用的大模型（意图解析、假设生成、结论撰写），OpenAI 兼容接口，默认 DeepSeek
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-flash")
# DeepSeek 的 deepseek-flash 默认开启思考模式：思考过程也占 max_tokens，短输出（改写、路由、JSON）可能被截空，首字也更慢。
# 没有显式配置时，对 DeepSeek 默认关闭思考；需要时在 .env 里写 LLM_EXTRA_BODY={"thinking": {"type": "enabled"}}
_default_extra = '{"thinking": {"type": "disabled"}}' if "deepseek" in LLM_BASE_URL else "{}"
LLM_EXTRA_BODY = json.loads(os.getenv("LLM_EXTRA_BODY", "") or _default_extra)
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "90"))

# 自然语言转 SQL 的模型：默认与分析模型相同；03 的微调模型用 vLLM 部署后，把地址填到这里即可切换
SQL_LLM_BASE_URL = os.getenv("SQL_LLM_BASE_URL", "")
SQL_LLM_API_KEY = os.getenv("SQL_LLM_API_KEY", "EMPTY")
SQL_LLM_MODEL = os.getenv("SQL_LLM_MODEL", "sft")
SQL_LLM_NO_THINKING = os.getenv("SQL_LLM_NO_THINKING", "1") == "1"

# 沙箱配额
SANDBOX_TIMEOUT = float(os.getenv("SANDBOX_TIMEOUT", "20"))
SANDBOX_MEMORY_MB = int(os.getenv("SANDBOX_MEMORY_MB", "2048"))
SANDBOX_MAX_ROWS = int(os.getenv("SANDBOX_MAX_ROWS", "200000"))

# 判定阈值
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.6"))   # 低于它的假设标为“已排除”
MAJOR_SHARE = float(os.getenv("MAJOR_SHARE", "0.2"))                     # 影响占总变动 20% 以上记为主要原因
MINOR_SHARE = float(os.getenv("MINOR_SHARE", "0.02"))                    # 2% 以上记为次要原因
EVENT_WINDOW_DAYS = int(os.getenv("EVENT_WINDOW_DAYS", "3"))             # 事件匹配窗口：异动前后 3 日

SECRET = os.getenv("APP_SECRET", "attr-demo-secret-change-me")
