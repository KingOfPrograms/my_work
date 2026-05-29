"""
模型 API 压测脚本 (Locust)
==========================
支持 DeepSeek / 阿里百炼 Qwen 的非流式和流式压测。

用法:
    # Web UI 模式（推荐，打开 http://localhost:8089）
    locust -f locustfile.py

    # 无头模式
    locust -f locustfile.py --headless --users 20 --spawn-rate 5 --run-time 60s

环境变量配置:
    LOCUST_PROVIDER=deepseek     # deepseek | qwen | all
    LOCUST_MODE=all              # non_stream | stream | all
"""

import json
import os
import time
from pathlib import Path

import requests
from locust import User, HttpUser, task, between, events

# ---------------------------------------------------------------------------
# .env 加载
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
# 配置
# ---------------------------------------------------------------------------
PROVIDER = os.environ.get("LOCUST_PROVIDER", "all")  # deepseek | qwen | all
MODE = os.environ.get("LOCUST_MODE", "all")            # non_stream | stream | all

# 默认压测问题
PROMPT = os.environ.get("LOCUST_PROMPT", "你好，请用50字介绍人工智能")

# ---------------------------------------------------------------------------
# Provider 配置（与 stream_chat.py 一致）
# ---------------------------------------------------------------------------
PROVIDERS = {
    "deepseek": {
        "url": os.environ.get(
            "DEEPSEEK_BASE_URL",
            "https://api.deepseek.com/anthropic/v1/messages",
        ),
        "headers": {
            "x-api-key": os.environ.get("DEEPSEEK_API_KEY", ""),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        "model": os.environ.get("DEEPSEEK_MODEL", "DeepSeek-V4-Pro"),
        "format": "anthropic",
    },
    "qwen": {
        "url": os.environ.get(
            "DASHSCOPE_BASE_URL",
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        ),
        "headers": {
            "Authorization": f"Bearer {os.environ.get('DASHSCOPE_API_KEY', '')}",
            "content-type": "application/json",
        },
        "model": os.environ.get("DASHSCOPE_MODEL", "qwen-plus"),
        "format": "openai",
    },
}

# 构建 payload
def _make_payload(cfg, stream):
    return {
        "model": cfg["model"],
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": PROMPT}],
        "stream": stream,
    }

# ---------------------------------------------------------------------------
# 非流式用户
# ---------------------------------------------------------------------------

class _BaseNonStreamUser(HttpUser):
    """非流式请求基类"""
    wait_time = between(1, 3)
    provider_name = None

    def on_start(self):
        self.cfg = PROVIDERS[self.provider_name]

    @task
    def call_api(self):
        payload = _make_payload(self.cfg, False)
        with self.client.post(
            self.cfg["url"],
            json=payload,
            headers=self.cfg["headers"],
            catch_response=True,
            name=f"[{self.provider_name}] 非流式",
        ) as resp:
            if resp.status_code >= 400:
                resp.failure(f"HTTP {resp.status_code}: {resp.text[:200]}")
                return
            try:
                data = resp.json()
                # 记录 token
                if self.cfg["format"] == "anthropic":
                    usage = data.get("usage", {})
                    self.environment.events.request.fire(
                        request_type="TOKEN",
                        name=f"[{self.provider_name}] input_tokens",
                        response_time=usage.get("input_tokens", 0),
                        response_length=0,
                    )
                    self.environment.events.request.fire(
                        request_type="TOKEN",
                        name=f"[{self.provider_name}] output_tokens",
                        response_time=usage.get("output_tokens", 0),
                        response_length=0,
                    )
                else:
                    usage = data.get("usage", {})
                    self.environment.events.request.fire(
                        request_type="TOKEN",
                        name=f"[{self.provider_name}] input_tokens",
                        response_time=usage.get("prompt_tokens", 0),
                        response_length=0,
                    )
                    self.environment.events.request.fire(
                        request_type="TOKEN",
                        name=f"[{self.provider_name}] output_tokens",
                        response_time=usage.get("completion_tokens", 0),
                        response_length=0,
                    )
                resp.success()
            except Exception as e:
                resp.failure(f"Parse error: {e}")


class DeepSeekNonStreamUser(_BaseNonStreamUser):
    provider_name = "deepseek"


class QwenNonStreamUser(_BaseNonStreamUser):
    provider_name = "qwen"


# ---------------------------------------------------------------------------
# 流式用户（直接用 requests，保留 SSE 连接）
# ---------------------------------------------------------------------------

class _BaseStreamUser(User):
    """流式请求基类 — 长连接读取 SSE"""
    wait_time = between(2, 5)
    provider_name = None

    def on_start(self):
        self.cfg = PROVIDERS[self.provider_name]

    @task
    def call_api(self):
        payload = _make_payload(self.cfg, True)
        t_start = time.perf_counter()
        ttft = None
        total_output = 0

        try:
            resp = requests.post(
                self.cfg["url"],
                json=payload,
                headers=self.cfg["headers"],
                stream=True,
                timeout=120,
            )

            if resp.status_code >= 400:
                total_ms = round((time.perf_counter() - t_start) * 1000)
                self.environment.events.request.fire(
                    request_type="SSE",
                    name=f"[{self.provider_name}] 流式",
                    response_time=total_ms,
                    response_length=0,
                    exception=Exception(f"HTTP {resp.status_code}: {resp.text[:200]}"),
                )
                return

            t_first_sse = None
            t_first_text = None
            t_last_text = None

            for line in resp.iter_lines():
                if not line:
                    continue
                line = line.decode("utf-8")

                if self.cfg["format"] == "anthropic":
                    if not line.startswith("data: "):
                        continue
                    event = json.loads(line[6:])
                    if t_first_sse is None:
                        t_first_sse = time.perf_counter()
                    etype = event.get("type")
                    if etype == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            now = time.perf_counter()
                            if t_first_text is None:
                                t_first_text = now
                            t_last_text = now
                else:
                    # OpenAI format
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        break
                    try:
                        event = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    if t_first_sse is None:
                        t_first_sse = time.perf_counter()
                    choices = event.get("choices", [])
                    if choices and choices[0] and choices[0].get("delta", {}).get("content"):
                        now = time.perf_counter()
                        if t_first_text is None:
                            t_first_text = now
                        t_last_text = now

            t_end = time.perf_counter()
            total_ms = round((t_end - t_start) * 1000)
            _ttft = round(((t_first_text or t_first_sse or t_start) - t_start) * 1000)
            _stream_dur = round(
                ((t_last_text or t_first_text or t_first_sse) - (t_first_text or t_first_sse or t_start)) * 1000
            )

            # 上报流式总延迟
            self.environment.events.request.fire(
                request_type="SSE",
                name=f"[{self.provider_name}] 流式(总)",
                response_time=total_ms,
                response_length=0,
            )
            # 上报 TTFT
            self.environment.events.request.fire(
                request_type="SSE",
                name=f"[{self.provider_name}] TTFT",
                response_time=_ttft,
                response_length=0,
            )
            # 上报流持续时长
            self.environment.events.request.fire(
                request_type="SSE",
                name=f"[{self.provider_name}] stream_duration",
                response_time=_stream_dur,
                response_length=0,
            )

        except Exception as e:
            total_ms = round((time.perf_counter() - t_start) * 1000)
            self.environment.events.request.fire(
                request_type="SSE",
                name=f"[{self.provider_name}] 流式",
                response_time=total_ms,
                response_length=0,
                exception=e,
            )


class DeepSeekStreamUser(_BaseStreamUser):
    provider_name = "deepseek"


class QwenStreamUser(_BaseStreamUser):
    provider_name = "qwen"


# ---------------------------------------------------------------------------
# 动态注册：根据环境变量决定启动哪些 User 类
# ---------------------------------------------------------------------------

def _should_run(prov, mode, non_stream):
    """判断是否应注册该 User 类"""
    prov_ok = PROVIDER in ("all", prov)
    mode_ok = MODE in ("all", mode)
    return prov_ok and mode_ok


# 导出到模块全局，Locust 通过 __all__ 或模块级变量发现 User 类
EXPORTS = {}

if _should_run("deepseek", "non_stream", True):
    EXPORTS["DeepSeekNonStreamUser"] = DeepSeekNonStreamUser
if _should_run("qwen", "non_stream", True):
    EXPORTS["QwenNonStreamUser"] = QwenNonStreamUser
if _should_run("deepseek", "stream", False):
    EXPORTS["DeepSeekStreamUser"] = DeepSeekStreamUser
if _should_run("qwen", "stream", False):
    EXPORTS["QwenStreamUser"] = QwenStreamUser

# 将导出的类设为模块级变量
for _name, _cls in EXPORTS.items():
    globals()[_name] = _cls

# ---------------------------------------------------------------------------
# 启动时打印配置
# ---------------------------------------------------------------------------

@events.init.add_listener
def on_locust_init(environment, **kwargs):
    enabled = list(EXPORTS.keys())
    if environment.runner and environment.runner.master_host:
        return  # worker 节点不打印
    print(f"\n{'='*60}")
    print(f"  压测配置")
    print(f"{'='*60}")
    print(f"  Provider : {PROVIDER}")
    print(f"  Mode     : {MODE}")
    print(f"  Users    : {enabled}")
    print(f"  Prompt   : {PROMPT[:60]}...")
    for prov_name in ("deepseek", "qwen"):
        if prov_name in PROVIDERS:
            cfg = PROVIDERS[prov_name]
            print(f"  [{prov_name}] model={cfg['model']}")
    print(f"{'='*60}\n")
