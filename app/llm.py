"""大模型调用：OpenAI 兼容的 /chat/completions（默认 DeepSeek）。

没有配置 LLM_API_KEY 时为离线模式：意图解析、假设、结论全部走规则引擎，整条分析链路照样能跑通，
只是少了大模型补充的自定义假设和自然语言润色。
"""
import json
import re
import time

import httpx

from . import config


class LLMError(Exception):
    pass


def online():
    return bool(config.LLM_API_KEY)


def estimate_tokens(text):
    cn = len(re.findall(r"[一-鿿]", text))
    return int(cn / 1.5 + (len(text) - cn) / 4) + 1


USAGE = dict(calls=0, prompt_tokens=0, completion_tokens=0, errors=0)


def _post(base_url, api_key, body, timeout):
    url = base_url.rstrip("/") + "/chat/completions"
    last = None
    for attempt in range(3):
        try:
            r = httpx.post(url, headers={"Authorization": f"Bearer {api_key}"}, json=body, timeout=httpx.Timeout(timeout, connect=10))
            if r.status_code == 200:
                return r.json()
            last = f"模型接口返回 {r.status_code}：{r.text[:300]}"
            if r.status_code in (400, 401, 403, 404):
                break
        except httpx.HTTPError as e:
            last = f"模型接口连接失败：{e}"
        time.sleep(1.5 * (attempt + 1))
    USAGE["errors"] += 1
    raise LLMError(last)


def chat(messages, temperature=0.2, max_tokens=1500, json_mode=False, base_url=None, api_key=None, model=None, extra=None):
    body = dict(model=model or config.LLM_MODEL, messages=messages, temperature=temperature, max_tokens=max_tokens)
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    body.update(config.LLM_EXTRA_BODY if model is None else {})
    if extra:
        body.update(extra)
    data = _post(base_url or config.LLM_BASE_URL, api_key or config.LLM_API_KEY, body, config.LLM_TIMEOUT)
    text = data["choices"][0]["message"].get("content") or ""
    u = data.get("usage") or {}
    USAGE["calls"] += 1
    USAGE["prompt_tokens"] += u.get("prompt_tokens") or sum(estimate_tokens(m["content"]) for m in messages)
    USAGE["completion_tokens"] += u.get("completion_tokens") or estimate_tokens(text)
    return text


def stream(messages, temperature=0.3, max_tokens=1500):
    """逐段产出文本。"""
    body = dict(model=config.LLM_MODEL, messages=messages, temperature=temperature, max_tokens=max_tokens, stream=True, **config.LLM_EXTRA_BODY)
    out = []
    with httpx.stream("POST", config.LLM_BASE_URL.rstrip("/") + "/chat/completions", headers={"Authorization": f"Bearer {config.LLM_API_KEY}"},
                      json=body, timeout=httpx.Timeout(config.LLM_TIMEOUT, connect=10)) as r:
        if r.status_code != 200:
            r.read()
            USAGE["errors"] += 1
            raise LLMError(f"模型接口返回 {r.status_code}：{r.text[:300]}")
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            for ch in obj.get("choices") or []:
                piece = (ch.get("delta") or {}).get("content")
                if piece:
                    out.append(piece)
                    yield piece
    USAGE["calls"] += 1
    USAGE["prompt_tokens"] += sum(estimate_tokens(m["content"]) for m in messages)
    USAGE["completion_tokens"] += estimate_tokens("".join(out))


def parse_json(text):
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    try:
        return json.loads(t)
    except Exception:
        m = re.search(r"\{.*\}|\[.*\]", t, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def chat_json(messages, **kw):
    return parse_json(chat(messages, json_mode=True, temperature=0.0, **kw))
