"""自然语言查数：问题 → SQL → 只读执行 → 结果表。

模型可切换：
  api  通用大模型（默认 DeepSeek，和分析用的是同一个）
  sft  配套项目 Text2SQL-Qwen-SFT 微调的 Qwen3.5-9B（vLLM 部署的 OpenAI 兼容接口，地址填 SQL_LLM_BASE_URL）
两者用同一份提示词（sql_prompt.py，和训练数据一致）。执行报错时把报错带回去让模型修一次。
所有 SQL 都过 warehouse.check_sql：只读、单条、受限表（dim_user）拦截。
"""
import re
import time

from . import config, llm, warehouse
from .sql_prompt import SYSTEM_PROMPT, user_prompt

MODES = {"api": "通用大模型（DeepSeek）", "sft": "微调 SQL 模型（Qwen3.5-9B QLoRA）"}
STATE = {"mode": "api"}


def available_modes():
    out = []
    if llm.online():
        out.append("api")
    if config.SQL_LLM_BASE_URL:
        out.append("sft")
    return out


def set_mode(mode):
    if mode not in MODES:
        raise ValueError(f"未知模型：{mode}")
    STATE["mode"] = mode


def _call(mode, messages):
    if mode == "sft":
        extra = {"chat_template_kwargs": {"enable_thinking": False}} if config.SQL_LLM_NO_THINKING else None
        return llm.chat(messages, temperature=0, max_tokens=800, base_url=config.SQL_LLM_BASE_URL, api_key=config.SQL_LLM_API_KEY,
                        model=config.SQL_LLM_MODEL, extra=extra)
    return llm.chat(messages, temperature=0, max_tokens=800, json_mode=True)


def _fallback(db, question):
    """离线兜底：几类常见问法直接套模板，保证没有模型时“按某维度看某指标”也能查。"""
    q = question
    months = re.findall(r"(\d{1,2})\s*月", q)
    if not months:
        return None
    m = f"2026-{int(months[0]):02d}"
    if db == "ecommerce":
        dims = [("渠道", "ch.channel_name", "JOIN dim_channel ch ON ch.channel_id = o.channel_id"),
                ("区域", "r.region_name", "JOIN dim_region r ON r.region_id = o.region_id"),
                ("新老客", "CASE WHEN o.is_new_user = 1 THEN '新客' ELSE '老客' END", "")]
        for name, col, join in dims:
            if name in q:
                return (f"SELECT {col} AS {name}, SUM(o.pay_amount) AS gmv, COUNT(*) AS orders, SUM(o.pay_amount) * 1.0 / COUNT(*) AS aov "
                        f"FROM fact_orders o {join} WHERE substr(o.dt, 1, 7) = '{m}' GROUP BY 1 ORDER BY gmv DESC")
        if "每天" in q or "每日" in q or "按天" in q:
            return f"SELECT dt, SUM(pay_amount) AS gmv, COUNT(*) AS orders FROM fact_orders WHERE substr(dt, 1, 7) = '{m}' GROUP BY dt ORDER BY dt"
    else:
        if "渠道" in q:
            return (f"SELECT c.channel_name AS 渠道, SUM(s.net_revenue) AS revenue, SUM(s.discount_amount) * 1.0 / SUM(s.gross_amount) AS discount_rate "
                    f"FROM fact_sales_daily s JOIN dim_channel c ON c.channel_id = s.channel_id WHERE substr(s.dt, 1, 7) = '{m}' GROUP BY 1 ORDER BY revenue DESC")
        if "产品线" in q:
            return (f"SELECT d.product_line AS 产品线, SUM(s.net_revenue) AS revenue, 1 - SUM(s.sales_qty * c.unit_cogs) / SUM(s.net_revenue) AS gross_margin "
                    f"FROM fact_sales_daily s JOIN dim_sku d ON d.sku_id = s.sku_id JOIN fact_sku_cost_monthly c ON c.sku_id = s.sku_id AND c.month = substr(s.dt, 1, 7) "
                    f"WHERE substr(s.dt, 1, 7) = '{m}' GROUP BY 1 ORDER BY revenue DESC")
    return None


def ask(db, question, mode=None, max_rows=200):
    mode = mode or STATE["mode"]
    t0 = time.time()
    trace = []
    if mode not in available_modes():
        sql = _fallback(db, question)
        if not sql:
            return dict(type="error", question=question, mode="rule", error="没有可用的查数模型（未配置 LLM_API_KEY / SQL_LLM_BASE_URL），离线只支持“按渠道/区域/新老客/产品线看某月指标”这类问法", trace=trace)
        obj = dict(type="sql", sql=sql)
        mode = "rule"
    else:
        msgs = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_prompt(db, question)}]
        try:
            obj = llm.parse_json(_call(mode, msgs))
        except llm.LLMError as e:
            return dict(type="error", question=question, mode=mode, error=str(e), trace=trace)
    for attempt in range(2):
        if not obj or obj.get("type") not in ("sql", "clarify", "refuse"):
            return dict(type="error", question=question, mode=mode, error="模型输出无法解析", trace=trace)
        if obj["type"] != "sql":
            return dict(type=obj["type"], question=question, mode=mode, message=obj.get("question") or obj.get("reason"), trace=trace,
                        elapsed_ms=int((time.time() - t0) * 1000))
        sql = obj["sql"].strip()
        err = warehouse.check_sql(sql, db)
        if err:
            trace.append(dict(sql=sql, error=f"安全检查拦截：{err}"))
            return dict(type="refuse", question=question, mode=mode, sql=sql, message=f"安全检查拦截：{err}", trace=trace)
        try:
            df = warehouse.query(db, sql, timeout_s=config.SANDBOX_TIMEOUT, max_rows=5000)
            trace.append(dict(sql=sql, error=None))
            return dict(type="sql", question=question, mode=mode, sql=sql, columns=list(df.columns), rows=df.head(max_rows).values.tolist(),
                        total_rows=len(df), trace=trace, elapsed_ms=int((time.time() - t0) * 1000))
        except warehouse.QueryError as e:
            trace.append(dict(sql=sql, error=str(e)))
            if attempt == 1 or mode == "rule":
                return dict(type="error", question=question, mode=mode, sql=sql, error=str(e), trace=trace)
            msgs = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_prompt(db, question, prev_sql=sql, error=str(e))}]
            try:
                obj = llm.parse_json(_call(mode, msgs))
            except llm.LLMError as e2:
                return dict(type="error", question=question, mode=mode, error=str(e2), trace=trace)
