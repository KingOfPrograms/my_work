"""
多模型 API 对话客户端
======================
支持 DeepSeek (Anthropic 格式) 和阿里百炼 Qwen (OpenAI 格式)。
自动记录首轮和末轮的 tokens 耗用与请求时延。

用法:
    from stream_chat import chat, ChatError

    conversation = [{"role": "user", "content": "你好"}]

    # DeepSeek
    r = chat(conversation, provider="deepseek", stream=False)

    # 阿里 Qwen
    r = chat(conversation, provider="qwen", stream=True)
"""

import requests
import json
import time
import os

# ---------------------------------------------------------------------------
# .env 文件加载（可选，文件不存在时静默跳过）
# ---------------------------------------------------------------------------

# 加载项目 .env 到 os.environ。用 setdefault 保证已存在的环境变量优先级更高
# （命令行 export 覆盖 .env），且只 split 第一个 =，允许值本身包含 =
_ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
try:
    with open(_ENV_PATH, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())
except FileNotFoundError:
    pass

# ---------------------------------------------------------------------------
# 异常定义
# ---------------------------------------------------------------------------


class ChatError(Exception):
    """API 请求失败时抛出的异常。"""

    def __init__(self, status_code, message, response_body=None):
        self.status_code = status_code
        self.response_body = response_body
        super().__init__(message)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _check_response(resp):
    """校验 HTTP 状态码，非 2xx 时抛出 ChatError。"""
    if resp.status_code < 200 or resp.status_code >= 300:
        try:
            body = resp.json()
        except Exception:
            body = resp.text[:1000]
        raise ChatError(
            resp.status_code,
            f"HTTP {resp.status_code}: {resp.reason}",
            response_body=body,
        )


# ---------------------------------------------------------------------------
# 非流式请求 — Anthropic 格式 (DeepSeek)
# ---------------------------------------------------------------------------


def _request_anthropic(url, headers, payload):
    """发送非流式请求，返回 (assistant_text, turn_metrics)。"""
    t_start = time.perf_counter()
    resp = requests.post(url, headers=headers, json=payload)
    _check_response(resp)
    t_end = time.perf_counter()

    data = resp.json()
    # DeepSeek V4 的 content 数组包含 thinking（推理过程）和 text 两类块，
    # thinking 块结构为 {"type":"thinking","thinking":"..."}，没有 "text" 键。
    # 只取 type=="text" 的块，跳过推理过程
    assistant_text = "".join(
        block["text"] for block in data["content"] if block["type"] == "text"
    )
    usage = data["usage"]
    return assistant_text, {
        "latency_ms": round((t_end - t_start) * 1000),
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
    }


# ---------------------------------------------------------------------------
# 非流式请求 — OpenAI 格式 (阿里百炼)
# ---------------------------------------------------------------------------


def _request_openai(url, headers, payload):
    """发送非流式请求（OpenAI 格式），返回 (assistant_text, turn_metrics)。"""
    t_start = time.perf_counter()
    resp = requests.post(url, headers=headers, json=payload)
    _check_response(resp)
    t_end = time.perf_counter()

    data = resp.json()
    # OpenAI 格式: {"choices":[{"message":{"role":"assistant","content":"..."}}], "usage":{...}}
    choice = data["choices"][0]
    assistant_text = choice["message"]["content"]
    usage = data.get("usage", {})  # 部分 API 版本可能不返回 usage
    return assistant_text, {
        "latency_ms": round((t_end - t_start) * 1000),
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
    }


# ---------------------------------------------------------------------------
# 流式请求 — Anthropic SSE 格式 (DeepSeek)
# ---------------------------------------------------------------------------


def _stream_anthropic(url, headers, payload):
    """发送 SSE 流式请求（Anthropic 格式），返回 (assistant_text, turn_metrics)。

    计时点分布:
        t_request ───── t_first_sse ── t_first_text ────────── t_last_text ── t_end
           │ 建连+等待  │ 首个 SSE     │ 首个文字 token  │          文字结束   │ 连接关闭
    """
    t_request = time.perf_counter()
    resp = requests.post(url, headers=headers, json=payload, stream=True)
    _check_response(resp)

    t_first_sse = None       # 首个 SSE 事件（含元数据）
    t_first_text = None      # 首个文字 token 到达
    t_last_text = None       # 末个文字 token 到达
    assistant_text = ""
    input_tokens = output_tokens = None

    # Anthropic SSE 事件序列:
    #   message_start → content_block_start → content_block_delta × N → content_block_stop → message_delta
    # DeepSeek V4 额外有 thinking 类型的 content block（推理过程），通过 type=="text_delta" 过滤
    for line in resp.iter_lines():
        if not line:
            continue
        line = line.decode("utf-8")
        if not line.startswith("data: "):
            continue
        event = json.loads(line[6:])

        # 首个 SSE 事件（通常是 message_start）— 衡量网络建连+TLS+首包延迟
        if t_first_sse is None:
            t_first_sse = time.perf_counter()

        etype = event.get("type")
        if etype == "message_start":
            # 携带 input_tokens，结构: {"message": {"usage": {"input_tokens": N}}}
            input_tokens = (
                event.get("message", {}).get("usage", {}).get("input_tokens")
            )
        elif etype == "content_block_delta":
            # 增量文本块，可能是 text_delta（文字）或 thinking_delta（推理，跳过）
            delta = event.get("delta", {})
            if delta.get("type") == "text_delta":
                now = time.perf_counter()
                text = delta.get("text", "")
                # 只有非空文字才算"首个 token"，与前端渲染体感对齐
                if text and t_first_text is None:
                    t_first_text = now
                if text:
                    t_last_text = now
                assistant_text += text

        elif etype == "message_delta":
            # 携带 output_tokens，结构: {"usage": {"output_tokens": N}}
            output_tokens = event.get("usage", {}).get("output_tokens")

    t_end = time.perf_counter()

    # 如果没有收到文字 token（异常情况），用首个 SSE 时间兜底
    _t_first = t_first_text or t_first_sse
    _t_last = t_last_text or t_first_text or t_first_sse

    return assistant_text, {
        "ttft_ms": round(
            (_t_first - t_request) * 1000 if _t_first else 0
        ),
        "ttfb_ms": round(
            (t_first_sse - t_request) * 1000 if t_first_sse else 0
        ),
        "stream_duration_ms": round(
            (_t_last - _t_first) * 1000 if (_t_last and _t_first) else 0
        ),
        "total_ms": round((t_end - t_request) * 1000),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


# ---------------------------------------------------------------------------
# 流式请求 — OpenAI SSE 格式 (阿里百炼)
# ---------------------------------------------------------------------------


def _stream_openai(url, headers, payload):
    """发送 SSE 流式请求（OpenAI 格式），返回 (assistant_text, turn_metrics)。

    OpenAI SSE 格式:
        data: {"choices":[{"delta":{"content":"你好"},"index":0}]}
        data: {"choices":[{"delta":{},"finish_reason":"stop","index":0}],"usage":{...}}
        data: [DONE]

    计时点:
        t_request ──── t_first_sse ── t_first_text ────── t_last_text ── t_end
           │ 建连+等待 │ 首个 SSE     │ 首个文字 token   │  文字结束    │ 连接关闭
    """
    t_request = time.perf_counter()
    resp = requests.post(url, headers=headers, json=payload, stream=True)
    _check_response(resp)

    t_first_sse = None       # 首个 SSE 事件（可能不含文字）
    t_first_text = None      # 首个包含文字内容的 delta
    t_last_text = None       # 末个包含文字内容的 delta
    assistant_text = ""
    usage = {}

    # OpenAI SSE: 每行 "data: {json}"，以 "data: [DONE]" 结束
    # 阿里百炼的 delta 可能为 null（finish_reason 事件），需要多层 None 守卫
    for line in resp.iter_lines():
        if not line:
            continue
        raw = line.decode("utf-8")
        if not raw.startswith("data: "):
            continue

        data_str = raw[6:]

        if data_str.strip() == "[DONE]":
            break

        try:
            event = json.loads(data_str)
        except json.JSONDecodeError:
            continue

        if t_first_sse is None:
            t_first_sse = time.perf_counter()

        # choices[0].delta.content 可能为 null/None:
        #   - 正常: {"delta": {"content": "你好"}}
        #   - 结束: {"delta": null, "finish_reason": "stop"}  ← choices[0] 存在但 delta 是 None
        choices = event.get("choices", [])
        if choices and choices[0] is not None:
            delta = choices[0].get("delta")
            if delta is not None and isinstance(delta, dict):
                text = delta.get("content", "")
                if text:
                    now = time.perf_counter()
                    if t_first_text is None:
                        t_first_text = now
                    t_last_text = now
                    assistant_text += text

        # usage 可能为 null（阿里百炼 SSE 某些事件中返回 "usage": null）
        if "usage" in event and event["usage"] is not None:
            usage = event["usage"]

    t_end = time.perf_counter()

    _t_first = t_first_text or t_first_sse
    _t_last = t_last_text or t_first_text or t_first_sse

    return assistant_text, {
        "ttft_ms": round(
            (_t_first - t_request) * 1000 if _t_first else 0
        ),
        "ttfb_ms": round(
            (t_first_sse - t_request) * 1000 if t_first_sse else 0
        ),
        "stream_duration_ms": round(
            (_t_last - _t_first) * 1000 if (_t_last and _t_first) else 0
        ),
        "total_ms": round((t_end - t_request) * 1000),
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
    }


# ---------------------------------------------------------------------------
# Provider 注册表
# ---------------------------------------------------------------------------

# Provider 配置表 — 策略模式：每个 provider 绑定自己的端点、鉴权、模型和请求/流式处理函数。
# 新增模型只需在此添加一个条目，无需修改 chat() 主逻辑。
PROVIDERS = {
    "deepseek": {
        "url": os.environ.get(
            "DEEPSEEK_BASE_URL",
            "https://api.deepseek.com/anthropic/v1/messages",
        ),
        "headers": {
            # Anthropic 兼容端点用 x-api-key 鉴权
            "x-api-key": os.environ.get("DEEPSEEK_API_KEY", ""),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        "model": os.environ.get("DEEPSEEK_MODEL", "DeepSeek-V4-Pro"),
        "request": _request_anthropic,   # 函数引用，非调用
        "stream": _stream_anthropic,
    },
    "qwen": {
        "url": os.environ.get(
            "DASHSCOPE_BASE_URL",
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        ),
        "headers": {
            # OpenAI 兼容端点用 Bearer Token 鉴权
            "Authorization": f"Bearer {os.environ.get('DASHSCOPE_API_KEY', '')}",
            "content-type": "application/json",
        },
        "model": os.environ.get("DASHSCOPE_MODEL", "qwen-plus"),
        "request": _request_openai,
        "stream": _stream_openai,
    },
}

# 保持向后兼容的全局变量
API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
URL = PROVIDERS["deepseek"]["url"]
HEADERS = PROVIDERS["deepseek"]["headers"]
DEFAULT_MODEL = PROVIDERS["deepseek"]["model"]

# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------


def chat(messages, provider="deepseek", model=None, max_tokens=1024, stream=False):
    """执行多轮对话，自动记录首轮和末轮的性能指标。

    Args:
        messages: 对话历史列表 [{"role": "user", "content": "..."}, ...]
        provider: "deepseek" | "qwen"
        model: 模型名，默认使用 provider 对应的默认模型
        max_tokens: 单次回复最大 token 数
        stream: True 流式, False 非流式

    Returns:
        dict: {
            "turn_count":        int  — 处理的对话轮数
            "conversation":      list — 完整对话历史（含新生成的 assistant 回复）
            "first_turn":        dict — 首轮性能指标
            "last_turn":         dict — 末轮性能指标
        }
        非流式指标: latency_ms, input_tokens, output_tokens
        流式指标:   ttft_ms (首字), ttfb_ms (首包), stream_duration_ms (生成耗时),
                    total_ms (总量), input_tokens, output_tokens

    Raises:
        ValueError: provider 不存在 或 messages 格式错误
        ChatError:  API 返回非 2xx
    """
    # 查表取 provider 配置
    cfg = PROVIDERS.get(provider)
    if cfg is None:
        raise ValueError(
            f"未知 provider: {provider!r}，可选: {list(PROVIDERS)}"
        )

    # messages 必须奇数条（最后一条是 user，等待 assistant 回复）
    if len(messages) % 2 == 0:
        raise ValueError(
            "messages 必须以 user 开头，且轮次为奇数（最后一条是 assistant 回复）"
        )

    model = model or cfg["model"]
    url = cfg["url"]
    headers = cfg["headers"]
    request_fn = cfg["request"]   # 非流式处理函数
    stream_fn = cfg["stream"]     # 流式处理函数

    # 浅拷贝每条消息，避免修改调用方传入的列表
    messages = [m.copy() for m in messages]
    turns = (len(messages) + 1) // 2  # 例: 1条user=1轮, 3条=2轮
    result = {"turn_count": turns, "conversation": messages.copy()}
    responses = []

    # 策略模式：根据 stream 参数选择对应的处理函数
    fn = stream_fn if stream else request_fn

    # 逐轮请求
    for turn_idx in range(turns):
        user_idx = turn_idx * 2
        # 取从对话开头到当前 user 消息为止的上下文
        turn_payload = messages[: user_idx + 1]

        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": turn_payload,
            "stream": stream,
        }

        # fn → _request_anthropic | _stream_anthropic | _request_openai | _stream_openai
        assistant_text, turn_metrics = fn(url, headers, payload)

        # 将 assistant 回复追加到对话历史，下一轮自动携带
        messages.append({"role": "assistant", "content": assistant_text})
        responses.append(assistant_text)

        # 首轮和末轮的指标分别记录
        if turn_idx == 0:
            result["first_turn"] = turn_metrics
        if turn_idx == turns - 1:
            result["last_turn"] = turn_metrics

    return result


# ---------------------------------------------------------------------------
# 自测
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    conversation = [
        {"role": "user", "content": "你好，1+1等于几？"},
    ]

    for prov in ["deepseek", "qwen"]:
        print(f"\n{'='*60}")
        print(f"  Provider: {prov}")
        print(f"{'='*60}")

        try:
            print("\n--- 非流式 ---")
            r = chat(conversation, provider=prov, stream=False)
            print(f"  轮次: {r['turn_count']}")
            print(f"  首轮: {r['first_turn']}")

            print("\n--- 流式 ---")
            r2 = chat(conversation, provider=prov, stream=True)
            print(f"  轮次: {r2['turn_count']}")
            print(f"  首轮: {r2['first_turn']}")
        except ChatError as e:
            print(f"  [{prov}] API 错误 [{e.status_code}]: {e.response_body}")
        except Exception as e:
            print(f"  [{prov}] 异常: {e}")
