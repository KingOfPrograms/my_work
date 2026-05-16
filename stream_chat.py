"""
DeepSeek API 多轮对话客户端
=============================
通过 Anthropic 兼容接口调用 DeepSeek-V4-Pro，支持流式（SSE）和非流式两种模式。
自动记录首轮和末轮的 tokens 耗用与请求时延。

用法:
    from stream_chat import chat, ChatError

    conversation = [{"role": "user", "content": "你好"}]

    # 非流式
    r = chat(conversation, stream=False)

    # 流式（返回首字延迟 + 流持续时长）
    r = chat(conversation, stream=True)
"""

import requests
import json
import time
import os

# ---------------------------------------------------------------------------
# .env 文件加载（可选，文件不存在时静默跳过）
# ---------------------------------------------------------------------------

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
# 配置区 — 优先从环境变量读取，未设置时用默认值
# ---------------------------------------------------------------------------

API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
"""DeepSeek API 密钥。通过环境变量或 .env 文件设置，不硬编码到仓库中。"""

URL = os.environ.get(
    "DEEPSEEK_BASE_URL",
    "https://api.deepseek.com/anthropic/v1/messages",
)
"""DeepSeek 的 Anthropic 兼容端点。"""

DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "DeepSeek-V4-Pro")
"""默认模型。"""

HEADERS = {
    "x-api-key": API_KEY,
    "anthropic-version": "2023-06-01",
    "content-type": "application/json",
}

# ---------------------------------------------------------------------------
# 异常定义
# ---------------------------------------------------------------------------


class ChatError(Exception):
    """API 请求失败时抛出的异常。

    Attributes:
        status_code: HTTP 状态码（如 429、500）
        response_body: 响应体内容（dict 或截断后的字符串）
    """

    def __init__(self, status_code, message, response_body=None):
        self.status_code = status_code
        self.response_body = response_body
        super().__init__(message)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _check_response(resp):
    """校验 HTTP 响应状态码。

    对于 2xx 响应直接放行；对于 4xx/5xx 响应，尝试解析 JSON 错误体，
    解析失败则截取前 1000 字符的文本，统一包装为 ChatError 抛出。
    这样调用方可以用 try/except ChatError 统一处理所有 API 错误。
    """
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
# 主函数
# ---------------------------------------------------------------------------


def chat(messages, model=DEFAULT_MODEL, max_tokens=1024, stream=False):
    """执行多轮对话，自动记录首轮和末轮的性能指标。

    每次调用处理 messages 中所有 pending 的 user 轮次（即末尾尚未被
    assistant 回复的 user 消息），逐轮请求并将 assistant 回复追加到
    内部列表，最终返回完整对话和指标。

    Args:
        messages: 对话历史，奇数条（以 user 开头，末尾可以缺少 assistant 回复）。
                  格式: [{"role": "user", "content": "..."}, ...]
        model: 模型名称，默认 "DeepSeek-V4-Pro"。
        max_tokens: 单次回复的最大 token 数。
        stream: True 使用 SSE 流式请求，False 使用普通请求。

    Returns:
        dict:
            turn_count   (int)  — 本轮处理的对话轮数
            conversation (list) — 完整的对话历史（含新生成的 assistant 回复）
            first_turn   (dict) — 第一轮的性能指标
            last_turn    (dict) — 最后一轮的性能指标

        非流式指标 (first_turn / last_turn):
            latency_ms    (int) — 请求-响应总时延（毫秒）
            input_tokens  (int) — 输入 token 数
            output_tokens (int) — 输出 token 数

        流式指标额外包含:
            ttft_ms            (int) — 首字延迟，从请求发出到首个 SSE 事件的毫秒数
            stream_duration_ms (int) — 流持续时长，首个到末个 SSE 事件的毫秒数
            total_ms           (int) — 从请求发出到连接关闭的总时延

    Raises:
        ValueError: messages 长度校验失败（偶数条意味着以 assistant 结尾）。
        ChatError:  API 返回非 2xx 状态码。
    """
    # ---- 输入校验 ----
    if len(messages) % 2 == 0:
        raise ValueError(
            "messages 必须以 user 开头，且轮次为奇数"
            "（最后一条是 assistant 回复）"
        )

    # 浅拷贝每一条消息，避免修改调用方传入的列表
    messages = [m.copy() for m in messages]

    # 待处理的轮数 = (当前消息数 + 1) // 2
    # 例：1 条 user 消息 → 1 轮；3 条 user+asst+user → 2 轮
    turns = (len(messages) + 1) // 2

    result = {
        "turn_count": turns,
        "conversation": messages.copy(),
    }
    responses = []

    # ---- 逐轮对话 ----
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

        # ============================================================
        # 流式路径
        # ============================================================
        if stream:
            # 记录请求发起时间
            t_request = time.perf_counter()

            resp = requests.post(
                URL, headers=HEADERS, json=payload, stream=True
            )
            _check_response(resp)

            # t_first_sse 在收到第一个 SSE 事件时赋值
            t_first_sse = None
            # t_last_sse 在每个事件后更新，最终为末个事件的时间
            t_last_sse = None

            assistant_text = ""
            input_tokens = output_tokens = None

            # 逐行读取 SSE 事件流
            for line in resp.iter_lines():
                # 跳过 SSE 空行（协议用空行分隔事件）
                if not line:
                    continue

                line = line.decode("utf-8")

                # SSE 协议：事件以 "data: " 开头
                if not line.startswith("data: "):
                    continue

                # 解析 JSON 事件体（跳过 "data: " 前缀的 6 个字符）
                event = json.loads(line[6:])

                # 首字时延 = 第一个 SSE 到达 - 请求发出
                if t_first_sse is None:
                    t_first_sse = time.perf_counter()

                etype = event.get("type")

                # --- 事件分发 ---
                if etype == "message_start":
                    # message_start 携带输入 token 信息
                    input_tokens = (
                        event.get("message", {})
                        .get("usage", {})
                        .get("input_tokens")
                    )

                elif etype == "content_block_delta":
                    # 增量文本；DeepSeek 还有 thinking_delta 类型
                    # 这里用 text_delta 过滤，跳过推理过程
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        assistant_text += delta.get("text", "")

                elif etype == "message_delta":
                    # message_delta 携带输出 token 信息
                    output_tokens = (
                        event.get("usage", {}).get("output_tokens")
                    )

                # 更新末个事件时间（每个事件都更新）
                t_last_sse = time.perf_counter()

            t_end = time.perf_counter()

            # 组装本轮指标
            turn_metrics = {
                "ttft_ms": round((t_first_sse - t_request) * 1000),
                "stream_duration_ms": round(
                    (t_last_sse - t_first_sse) * 1000
                ),
                "total_ms": round((t_end - t_request) * 1000),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }

        # ============================================================
        # 非流式路径
        # ============================================================
        else:
            t_start = time.perf_counter()
            resp = requests.post(URL, headers=HEADERS, json=payload)
            _check_response(resp)
            t_end = time.perf_counter()

            data = resp.json()

            # DeepSeek 响应中 content 数组可能包含 thinking 和 text 两类块
            # 只拼接 type == "text" 的块，跳过推理过程
            assistant_text = "".join(
                block["text"]
                for block in data["content"]
                if block["type"] == "text"
            )

            usage = data["usage"]
            turn_metrics = {
                "latency_ms": round((t_end - t_start) * 1000),
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
            }

        # ---- 追加 assistant 回复到对话历史 ----
        messages.append({"role": "assistant", "content": assistant_text})
        responses.append(assistant_text)

        # ---- 记录首轮和末轮指标 ----
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

    print("=== 非流式 ===\n")
    r = chat(conversation, stream=False)
    print(f"轮次: {r['turn_count']}")
    print(f"首轮: {r['first_turn']}")
    print(f"末轮: {r['last_turn']}")

    print("\n=== 流式 ===\n")
    r2 = chat(conversation, stream=True)
    print(f"轮次: {r2['turn_count']}")
    print(f"首轮: {r2['first_turn']}")
    print(f"末轮: {r2['last_turn']}")
