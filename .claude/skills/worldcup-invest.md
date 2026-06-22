---
name: worldcup-invest
description: 世界杯AI投资方案一站式生成 — 同步赔率→预测→投资方案→汇总MD→推送
---

# worldcup-invest

用户通过指定比赛和预算，一键完成从赔率同步到投资方案汇总的完整流程。

## 用户输入格式

```
/worldcup-invest 比赛1,比赛2,... | 预算XXX元 | 方案数N
```

示例：
```
/worldcup-invest 阿根廷vs奥地利, 法国vs伊拉克, 挪威vs塞内加尔, 约旦vs阿尔及利亚 | 预算200元 | 方案数4
```

如果用户未指定方案数，默认生成 4 个方案。如果未指定预算，默认 200 元。

## 执行流程

### 1. 解析用户输入

从用户消息中提取：
- **比赛列表**: 支持中文队名（如"阿根廷vs奥地利"）或直接 match ID
- **预算**: 整数，默认 200
- **方案数**: 整数，默认 4

### 2. 确保后端运行

检查 http://localhost:8000/api/events 是否可达。如果不可达：
```bash
cd E:/worldcup-platform/backend && python run.py &
```
等待服务启动后继续。

### 3. 同步赔率

```bash
curl -s -X POST http://localhost:8000/api/events/world-cup-2026/sync-odds
```

### 4. 查找比赛 ID

如果用户使用中文队名，需要查 match ID。从 GET /api/events/world-cup-2026 返回的 matches 列表中按队名匹配。先用 -o 写入文件，再用 python utf-8 读取，避免 GBK 编码问题。

队名匹配规则：两队在同一个 match 中出现即可（不限主客顺序）。

### 5. 运行 AI 预测

对每场比赛并发启动预测（force=true, sim_count=20）：
```bash
curl -s -X POST "http://localhost:8000/api/events/world-cup-2026/matches/{match_id}/predict?force=true&sim_count=20"
```

然后轮询直到全部完成（status=completed）。轮询间隔 30-60 秒。每场比赛约需 3-5 分钟（20 次 DeepSeek 推演 + meta-summary）。

### 6. 生成投资方案

对每场比赛方案并发生成（每个约 2-3 分钟，3-pass AI pipeline），timeout 600s：
```bash
MATCH_IDS='["id1","id2","id3","id4"]'
curl -s -X POST http://localhost:8000/api/events/world-cup-2026/investments   -H "Content-Type: application/json"   -d "{"matchIds":$MATCH_IDS,"budget":200}"
```

生成的方案数量由用户指定（默认 4 个），并发执行。方案保存为 _invest_plan{N}.json。

### 7. 获取方案详情

对每个生成方案的 plan.id，获取完整详情：
```bash
curl -s "http://localhost:8000/api/events/world-cup-2026/investments/{plan_id}" -o _plan{N}_detail.json
```

### 8. 生成汇总 MD 文档

创建 Python 脚本读取所有方案详情 JSON，输出 MD 到 E:/my_work/投资方案汇总_{日期}.md。

MD 结构：比赛概况表格 → 每个方案（平稳组合表格 + 追梦组合表格 + 预算分配 + 策略分析 + 风控审核）→ 四方案对比分析 → 核心要点。

### 9. 推送到 GitHub

```bash
cd E:/my_work && git add "投资方案汇总_*.md" &&   git commit -m "feat: AI投资方案汇总 — N场比赛×M方案" &&   git push origin master
```

### 10. 清理临时文件

删除所有 _invest_plan*.json、_plan*_detail.json、_all_plans.json、_event_data.json、_generate_md.py。

## 关键约束

- **赛事 slug**: world-cup-2026
- **后端**: http://localhost:8000
- **中文输出**: 先用 -o 写文件，再用 python encoding=utf-8 读取，禁止直接通过管道传中文给 python
- **API timeout**: 预测 30s（启动），投资方案 600s（3-pass pipeline）
- **轮询间隔**: 30-60s
- **并发**: 预测和方案生成均可并发，互不依赖
