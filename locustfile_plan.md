# 模型 API 压测方案

## 目标

对 DeepSeek / 阿里百炼 Qwen 两个模型的 API 进行非流式和流式压测，获取 P50/P75/P90/P95/P99 延迟分位数及 QPS。

## 技术选型：Locust

| 对比项 | Locust | k6 | wrk |
|--------|--------|----|-----|
| Python 栈一致性 | 是 | 否（JS/Go） | 否（C/Lua） |
| 流式 SSE 支持 | 自定义，requests 直接读 | 有限 | 不支持 |
| Web UI 实时监控 | 内置 | 需 Grafana | 无 |
| 可编程性 | 高（任意 Python 逻辑） | 中 | 低 |

**结论：Locust**，与现有 `stream_chat.py` 共用同一套请求格式。

## 压测指标

| 指标 | 非流式 | 流式 | 说明 |
|------|--------|------|------|
| 总延迟 (total_ms) | 有 | 有 | 请求发起到最后字节收到 |
| 首 Token 延迟 (TTFT) | 无 | 有 | 第一个文本 token 到达时间 |
| 流式持续 (stream_duration) | 无 | 有 | 首 token 到末 token |
| Token 吞吐 | 有 | 有 | input_tokens, output_tokens |
| QPS | 有 | 无意义 | 长连接流式不适合 QPS |
| 错误率 | 有 | 有 | HTTP 非 2xx 或解析失败 |

## 架构设计

```
locustfile.py
├── DeepSeekNonStreamUser   (HttpUser, weight=1)
├── DeepSeekStreamUser      (User, weight=1)
├── QwenNonStreamUser       (HttpUser, weight=1)
└── QwenStreamUser          (User, weight=1)

可配置参数（环境变量）:
  LOCUST_PROVIDER=deepseek    # deepseek | qwen | all
  LOCUST_MODE=non_stream      # non_stream | stream | all
  LOCUST_USERS=10             # 并发用户数
  LOCUST_SPAWN_RATE=5         # 每秒启动用户数
  LOCUST_RUN_TIME=60s         # 运行时长
```

## 请求体

与 `stream_chat.py` 的 `chat()` 函数格式一致：

```json
{
  "model": "DeepSeek-V4-Pro",
  "max_tokens": 1024,
  "messages": [
    {"role": "user", "content": "你好，请用50字介绍人工智能"}
  ],
  "stream": false
}
```

## 非流式压测流程

1. `HttpUser` 直接 `self.client.post(url, json=payload, headers=headers)`
2. Locust 自动记录 `response_time`、`failure/success`
3. 额外上报 token 耗用

## 流式压测流程

1. 使用原始 `requests.post(stream=True)` 绕过 Locust 的短连接限制
2. 手动计时：`ttft`（首 token）、`total_ms`（总耗时）
3. 通过 `locust.events.request.fire()` 上报自定义事件，Locust Web UI 可展示
4. 每个 `User` 持有一个长连接，持续读取 SSE 直到 `message_stop`

## 运行方式

```bash
# Web UI 模式（推荐）
locust -f locustfile.py --host=https://api.deepseek.com

# 无头模式（CI/脚本）
locust -f locustfile.py --host=https://api.deepseek.com \
  --users 20 --spawn-rate 5 --run-time 60s --headless \
  --csv=results/load_test
```

## 输出

- Locust Web UI 实时图表（http://localhost:8089）
- CSV 导出：`results/load_test_stats.csv`（聚合）、`results/load_test_stats_history.csv`（时序）
- 控制台打印分位数摘要

## 文件变更

| 文件 | 操作 | 说明 |
|------|------|------|
| `locustfile.py` | 新建 | 压测主脚本 |
| `requirements.txt` | 修改 | 新增 locust 依赖 |
