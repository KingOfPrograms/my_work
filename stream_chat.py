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
    """发送非流式请求（Anthropic 格式）。

    Returns:
        {"text": str, "thinking": str|None, "tool_calls": list, "metrics": dict}
    """
    t_start = time.perf_counter()
    resp = requests.post(url, headers=headers, json=payload)
    _check_response(resp)
    t_end = time.perf_counter()

    data = resp.json()
    # Anthropic content 有三种块类型:
    #   text:     {"type":"text", "text":"..."}
    #   thinking: {"type":"thinking","thinking":"..."}
    #   tool_use: {"type":"tool_use","name":"...","input":{...},"id":"..."}
    text_parts = []
    thinking_parts = []
    tool_calls = []

    for block in data["content"]:
        btype = block.get("type", "")
        if btype == "text":
            text_parts.append(block["text"])
        elif btype == "thinking":
            thinking_parts.append(block["thinking"])
        elif btype == "tool_use":
            tool_calls.append({
                "id": block.get("id"),
                "name": block.get("name"),
                "input": block.get("input"),
            })

    usage = data["usage"]
    return {
        "text": "".join(text_parts),
        "thinking": "\n".join(thinking_parts) if thinking_parts else None,
        "tool_calls": tool_calls,
        "metrics": {
            "latency_ms": round((t_end - t_start) * 1000),
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
        },
    }


# ---------------------------------------------------------------------------
# 非流式请求 — OpenAI 格式 (阿里百炼)
# ---------------------------------------------------------------------------


def _request_openai(url, headers, payload):
    """发送非流式请求（OpenAI 格式）。

    Returns:
        {"text": str, "thinking": None, "tool_calls": list, "metrics": dict}
    """
    t_start = time.perf_counter()
    resp = requests.post(url, headers=headers, json=payload)
    _check_response(resp)
    t_end = time.perf_counter()

    data = resp.json()
    # OpenAI 格式: {"choices":[{"message":{"role":"assistant","content":"...或null","tool_calls":[...]}}]}
    msg = data["choices"][0]["message"]
    usage = data.get("usage", {})

    tool_calls = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        tool_calls.append({
            "id": tc.get("id"),
            "name": fn.get("name"),
            "input": json.loads(fn.get("arguments", "{}")),
        })

    return {
        "text": msg.get("content") or "",
        "thinking": None,  # 标准 OpenAI 格式无 thinking 字段
        "tool_calls": tool_calls,
        "metrics": {
            "latency_ms": round((t_end - t_start) * 1000),
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
        },
    }


# ---------------------------------------------------------------------------
# 流式请求 — Anthropic SSE 格式 (DeepSeek)
# ---------------------------------------------------------------------------


def _stream_anthropic(url, headers, payload):
    """发送 SSE 流式请求（Anthropic 格式）。

    Anthropic SSE 事件序列:
      message_start ─→ content_block_start ─→ content_block_delta×N ─→ content_block_stop ─→ ... ─→ message_delta

    content_block 有三种类型:
      text:     content_block_start(type="text")     + text_delta      ×N
      thinking: content_block_start(type="thinking") + thinking_delta  ×N + signature_delta
      tool_use: content_block_start(type="tool_use") + input_json_delta×N

    Returns:
        {"text": str, "thinking": str|None, "tool_calls": list, "metrics": dict}
    """
    t_request = time.perf_counter()
    resp = requests.post(url, headers=headers, json=payload, stream=True)
    _check_response(resp)

    t_first_sse = None
    t_first_text = None
    t_last_text = None
    text_parts = []
    thinking_parts = []
    tool_calls = []
    # 跟踪当前正在构建的 content block
    current_block_type = None   # "text" | "thinking" | "tool_use"
    current_tool = None         # 当前构建的 tool_use 对象
    current_tool_json = []      # 累积的 input JSON 片段
    input_tokens = output_tokens = None

    for line in resp.iter_lines():
        if not line:
            continue
        line = line.decode("utf-8")
        if not line.startswith("data: "):
            continue
        event = json.loads(line[6:])

        if t_first_sse is None:
            t_first_sse = time.perf_counter()

        etype = event.get("type")

        # --- content_block_start: 标记一个新 block 的开始 ---
        if etype == "content_block_start":
            block = event.get("content_block", {})
            current_block_type = block.get("type")
            if current_block_type == "tool_use":
                current_tool = {
                    "id": block.get("id"),
                    "name": block.get("name"),
                }
                current_tool_json = []
            else:
                current_tool = None
                current_tool_json = []

        # --- content_block_delta: 增量内容 ---
        elif etype == "content_block_delta":
            delta = event.get("delta", {})
            dtype = delta.get("type")

            if dtype == "text_delta":
                txt = delta.get("text", "")
                if txt:
                    now = time.perf_counter()
                    if t_first_text is None:
                        t_first_text = now
                    t_last_text = now
                text_parts.append(txt)

            elif dtype == "thinking_delta":
                thinking_parts.append(delta.get("thinking", ""))

            elif dtype == "input_json_delta":
                current_tool_json.append(delta.get("partial_json", ""))

        # --- content_block_stop: 当前 block 结束 ---
        elif etype == "content_block_stop":
            if current_block_type == "tool_use" and current_tool:
                try:
                    current_tool["input"] = json.loads("".join(current_tool_json))
                except json.JSONDecodeError:
                    current_tool["input"] = "".join(current_tool_json)
                tool_calls.append(current_tool)
            current_block_type = None
            current_tool = None
            current_tool_json = []

        # --- message_start / message_delta: token 统计 ---
        elif etype == "message_start":
            input_tokens = (
                event.get("message", {}).get("usage", {}).get("input_tokens")
            )
        elif etype == "message_delta":
            output_tokens = event.get("usage", {}).get("output_tokens")

    t_end = time.perf_counter()

    _t_first = t_first_text or t_first_sse
    _t_last = t_last_text or t_first_text or t_first_sse

    return {
        "text": "".join(text_parts),
        "thinking": "".join(thinking_parts) if thinking_parts else None,
        "tool_calls": tool_calls,
        "metrics": {
            "ttft_ms": round((_t_first - t_request) * 1000 if _t_first else 0),
            "ttfb_ms": round((t_first_sse - t_request) * 1000 if t_first_sse else 0),
            "stream_duration_ms": round(
                (_t_last - _t_first) * 1000 if (_t_last and _t_first) else 0
            ),
            "total_ms": round((t_end - t_request) * 1000),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }


# ---------------------------------------------------------------------------
# 流式请求 — OpenAI SSE 格式 (阿里百炼)
# ---------------------------------------------------------------------------


def _stream_openai(url, headers, payload):
    """发送 SSE 流式请求（OpenAI 格式）。

    OpenAI SSE:
        data: {"choices":[{"delta":{"content":"你好"}}]}
        data: {"choices":[{"delta":{"tool_calls":[{"function":{"name":"...","arguments":"..."}}]}}]}
        data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{...}}
        data: [DONE]

    Returns:
        {"text": str, "thinking": None, "tool_calls": list, "metrics": dict}
    """
    t_request = time.perf_counter()
    resp = requests.post(url, headers=headers, json=payload, stream=True)
    _check_response(resp)

    t_first_sse = None
    t_first_text = None
    t_last_text = None
    text_parts = []
    usage = {}
    # OpenAI 流式 tool_calls 按 index 累积
    tool_call_buffers = {}  # index → {id, name, arguments_parts}

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

        choices = event.get("choices", [])
        if choices and choices[0] is not None:
            delta = choices[0].get("delta")
            if delta is not None and isinstance(delta, dict):
                # --- 文本增量 ---
                txt = delta.get("content", "")
                if txt:
                    now = time.perf_counter()
                    if t_first_text is None:
                        t_first_text = now
                    t_last_text = now
                    text_parts.append(txt)

                # --- 工具调用增量 ---
                for tc_delta in delta.get("tool_calls") or []:
                    idx = tc_delta.get("index", 0)
                    buf = tool_call_buffers.setdefault(idx, {
                        "id": None,
                        "name": "",
                        "arguments_parts": [],
                    })
                    if "id" in tc_delta and tc_delta["id"]:
                        buf["id"] = tc_delta["id"]
                    fn = tc_delta.get("function") or {}
                    if fn.get("name"):
                        buf["name"] += fn["name"]
                    if fn.get("arguments"):
                        buf["arguments_parts"].append(fn["arguments"])

        if "usage" in event and event["usage"] is not None:
            usage = event["usage"]

    t_end = time.perf_counter()

    # 组装 tool_calls
    tool_calls = []
    for idx in sorted(tool_call_buffers):
        buf = tool_call_buffers[idx]
        args_str = "".join(buf["arguments_parts"])
        try:
            args = json.loads(args_str) if args_str.strip() else {}
        except json.JSONDecodeError:
            args = args_str
        tool_calls.append({"id": buf["id"], "name": buf["name"], "input": args})

    _t_first = t_first_text or t_first_sse
    _t_last = t_last_text or t_first_text or t_first_sse

    return {
        "text": "".join(text_parts),
        "thinking": None,  # 标准 OpenAI 格式无 thinking 字段
        "tool_calls": tool_calls,
        "metrics": {
            "ttft_ms": round((_t_first - t_request) * 1000 if _t_first else 0),
            "ttfb_ms": round((t_first_sse - t_request) * 1000 if t_first_sse else 0),
            "stream_duration_ms": round(
                (_t_last - _t_first) * 1000 if (_t_last and _t_first) else 0
            ),
            "total_ms": round((t_end - t_request) * 1000),
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
        },
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


def chat(messages, provider="deepseek", model=None, max_tokens=1024, stream=False, tools=None):
    """执行多轮对话，自动记录首轮和末轮的性能指标。

    Args:
        messages: 对话历史列表 [{"role": "user", "content": "..."}, ...]
        provider: "deepseek" | "qwen"
        model: 模型名，默认使用 provider 对应的默认模型
        max_tokens: 单次回复最大 token 数
        stream: True 流式, False 非流式
        tools: 工具定义列表，Anthropic/OpenAI 格式的工具 JSON Schema

    Returns:
        dict: {
            "turn_count":   int  — 处理的对话轮数
            "conversation": list — 完整对话历史（assistant 含 content/thinking/tool_calls）
            "first_turn":   dict — 首轮: {text, thinking, tool_calls, metrics}
            "last_turn":    dict — 末轮: 同上
        }

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

    # 第一条必须是 user，最后一条可以是 user 或 tool（tool 角色用于 OpenAI 工具结果）
    if not messages or messages[0].get("role") != "user":
        raise ValueError("messages 必须以 user 消息开头")
    last_role = messages[-1].get("role")
    if last_role not in ("user", "tool"):
        raise ValueError(
            f"messages 最后一条必须是 user 或 tool 角色，当前为 {last_role!r}"
        )

    model = model or cfg["model"]
    url = cfg["url"]
    headers = cfg["headers"]
    request_fn = cfg["request"]   # 非流式处理函数
    stream_fn = cfg["stream"]     # 流式处理函数

    # 浅拷贝每条消息，避免修改调用方传入的列表
    messages = [m.copy() for m in messages]

    # 找出所有 user 消息的索引，每个 user 消息对应一轮请求
    user_indices = [
        i for i, m in enumerate(messages) if m.get("role") == "user"
    ]
    if not user_indices:
        raise ValueError("messages 中未找到 user 消息")

    result = {"turn_count": 0, "conversation": messages.copy()}
    responses = []

    fn = stream_fn if stream else request_fn

    for turn_idx, user_idx in enumerate(user_indices):
        # 中间轮次: 取到当前 user 消息为止
        # 最后一轮: 取全部消息，包含 user 消息之后的 tool 结果（Qwen 用 role=tool）
        if turn_idx == len(user_indices) - 1:
            turn_payload = messages[:]
        else:
            turn_payload = messages[: user_idx + 1]

        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": turn_payload,
            "stream": stream,
        }
        if tools:
            payload["tools"] = tools

        out = fn(url, headers, payload)  # → {text, thinking, tool_calls, metrics}

        # 将 assistant 回复追加到对话历史
        assistant_msg = {"role": "assistant", "content": out["text"]}
        if out["thinking"]:
            assistant_msg["thinking"] = out["thinking"]
        if out["tool_calls"]:
            assistant_msg["tool_calls"] = out["tool_calls"]
        messages.append(assistant_msg)
        responses.append(assistant_msg)

        result["turn_count"] += 1

        if turn_idx == 0:
            result["first_turn"] = {
                "text": out["text"],
                "thinking": out["thinking"],
                "tool_calls": out["tool_calls"],
                "metrics": out["metrics"],
            }
        if turn_idx == len(user_indices) - 1:
            result["last_turn"] = {
                "text": out["text"],
                "thinking": out["thinking"],
                "tool_calls": out["tool_calls"],
                "metrics": out["metrics"],
            }

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
            t = r["first_turn"]
            print(f"  轮次: {r['turn_count']}")
            print(f"  text: {t['text'][:100]}{'...' if len(t['text'])>100 else ''}")
            print(f"  thinking: {t['thinking'][:100] if t['thinking'] else None}{'...' if t['thinking'] and len(t['thinking'])>100 else ''}")
            print(f"  tool_calls: {t['tool_calls']}")
            print(f"  metrics: {t['metrics']}")

            print("\n--- 流式 ---")
            r2 = chat(conversation, provider=prov, stream=True)
            t2 = r2["first_turn"]
            print(f"  轮次: {r2['turn_count']}")
            print(f"  text: {t2['text'][:100]}{'...' if len(t2['text'])>100 else ''}")
            print(f"  thinking: {t2['thinking'][:100] if t2['thinking'] else None}{'...' if t2['thinking'] and len(t2['thinking'])>100 else ''}")
            print(f"  tool_calls: {t2['tool_calls']}")
            print(f"  metrics: {t2['metrics']}")
        except ChatError as e:
            print(f"  [{prov}] API 错误 [{e.status_code}]: {json.dumps(e.response_body, ensure_ascii=False)[:500]}")
        except Exception as e:
            print(f"  [{prov}] 异常: {e}")

    # ---- 工具调用多轮测试 (两个模型 × 两种模式) ----
    # Anthropic 格式工具定义 (DeepSeek)
    WEATHER_TOOL_ANTHROPIC = {
        "name": "get_weather",
        "description": "获取指定城市当前天气",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "城市名"}},
            "required": ["city"],
        },
    }
    # OpenAI 格式工具定义 (Qwen)
    WEATHER_TOOL_OPENAI = {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "获取指定城市当前天气",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string", "description": "城市名"}},
                "required": ["city"],
            },
        },
    }

    FAKE_WEATHER = {"北京": "晴 25°C", "上海": "多云 28°C"}

    for prov, tool_def in [("deepseek", WEATHER_TOOL_ANTHROPIC), ("qwen", WEATHER_TOOL_OPENAI)]:
        for use_stream in (False, True):
            mode = "流式" if use_stream else "非流式"
            print(f"\n{'='*60}")
            print(f"  工具调用多轮测试 — {prov} ({mode})")
            print(f"{'='*60}")

            turn1 = [{"role": "user", "content": "帮我查一下北京和上海的天气"}]
            print(f"\n--- 第1轮: 用户问天气 [{prov}/{mode}] ---")
            r1 = chat(turn1, provider=prov, tools=[tool_def], stream=use_stream)
            t1 = r1["first_turn"]
            print(f"  text: {t1['text'][:80]}")
            if t1["thinking"]:
                print(f"  thinking: {t1['thinking'][:120]}")
            print(f"  tool_calls ({len(t1['tool_calls'])}):")
            for tc in t1["tool_calls"]:
                print(f"    - {tc['name']}({tc['input']})  [id={tc['id']}]")
            print(f"  metrics: {t1['metrics']}")

            if not t1["tool_calls"]:
                print("  ⚠ 模型未返回工具调用，跳过第2轮")
                continue

            if prov == "deepseek":
                # Anthropic 格式: thinking 块必须回传
                assistant_content = []
                if t1["thinking"]:
                    assistant_content.append({"type": "thinking", "thinking": t1["thinking"]})
                assistant_content += [
                    {"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["input"]}
                    for tc in t1["tool_calls"]
                ]
                tool_msg_user = {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": tc["id"],
                         "content": f"{tc['input']['city']}: {FAKE_WEATHER.get(tc['input']['city'], '未知')}"}
                        for tc in t1["tool_calls"]
                    ],
                }
                turn2 = turn1 + [
                    {"role": "assistant", "content": assistant_content},
                    tool_msg_user,
                ]
            else:
                # OpenAI 格式: tool_calls 在 assistant message，结果用 role=tool
                assistant_msg = {
                    "role": "assistant",
                    "content": t1["text"] or None,
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(tc["input"], ensure_ascii=False),
                            },
                        }
                        for tc in t1["tool_calls"]
                    ],
                }
                tool_msgs = [
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": f"{tc['input']['city']}: {FAKE_WEATHER.get(tc['input']['city'], '未知')}",
                    }
                    for tc in t1["tool_calls"]
                ]
                turn2 = turn1 + [assistant_msg] + tool_msgs

            print(f"\n--- 第2轮: 传入工具结果 [{prov}/{mode}] ---")
            r2 = chat(turn2, provider=prov, tools=[tool_def], stream=use_stream)
            t2 = r2["last_turn"]
            print(f"  text: {t2['text'][:200]}")
            print(f"  tool_calls ({len(t2['tool_calls'])}):")
            for tc in t2["tool_calls"]:
                print(f"    - {tc['name']}({tc['input']})")
            print(f"  metrics: {t2['metrics']}")
