import requests
import json
import time

API_KEY = "your-api-key"
URL = "https://api.anthropic.com/v1/messages"
HEADERS = {
    "x-api-key": API_KEY,
    "anthropic-version": "2023-06-01",
    "content-type": "application/json",
}


def chat(messages, model="claude-sonnet-4-6", max_tokens=1024, stream=False):
    """
    多轮对话，自动记录首轮和末轮的 tokens 与时延。

    参数:
        messages: [{"role": "user", "content": "..."}, ...]
        stream: True 则使用 SSE 流式，False 则非流式

    返回:
        {
            "turn_count": N,
            "conversation": [...],      # 完整的 messages 列表
            "first_turn":  { 首轮指标 },
            "last_turn":   { 末轮指标 },
        }
    """
    if len(messages) % 2 == 0:
        raise ValueError("messages 必须以 user 开头，且轮次为奇数（最后一条是 assistant 回复）")

    turns = (len(messages) + 1) // 2  # 总轮次数（user+assistant 为一轮）
    result = {"turn_count": turns, "conversation": messages.copy()}
    responses = []

    for turn_idx in range(turns):
        user_idx = turn_idx * 2
        turn_payload = messages[: user_idx + 1]  # 当前对话上下文

        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": turn_payload,
            "stream": stream,
        }

        if stream:
            t_request = time.perf_counter()
            resp = requests.post(URL, headers=HEADERS, json=payload, stream=True)
            t_first_sse = None
            t_last_sse = None
            assistant_text = ""
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
                if etype == "message_start":
                    input_tokens = event["message"]["usage"]["input_tokens"]
                elif etype == "content_block_delta":
                    text = event["delta"].get("text", "")
                    assistant_text += text
                elif etype == "message_delta":
                    output_tokens = event["usage"]["output_tokens"]

                t_last_sse = time.perf_counter()

            t_end = time.perf_counter()
            turn_metrics = {
                "ttft_ms": round((t_first_sse - t_request) * 1000),
                "stream_duration_ms": round((t_last_sse - t_first_sse) * 1000),
                "total_ms": round((t_end - t_request) * 1000),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }
        else:
            t_start = time.perf_counter()
            resp = requests.post(URL, headers=HEADERS, json=payload)
            t_end = time.perf_counter()
            data = resp.json()
            assistant_text = data["content"][0]["text"]
            usage = data["usage"]
            turn_metrics = {
                "latency_ms": round((t_end - t_start) * 1000),
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
            }

        messages.append({"role": "assistant", "content": assistant_text})
        responses.append(assistant_text)

        if turn_idx == 0:
            result["first_turn"] = turn_metrics
        if turn_idx == turns - 1:
            result["last_turn"] = turn_metrics

    return result


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
