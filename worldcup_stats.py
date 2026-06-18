"""
世界杯 AI 模型预测统计 + 球员数据
================================
从 worldcup.lyihub.com 拉取：
  - 比赛胜负/比分预测
  - 球队战术风格 & 球员名单
  - 各模型余额趋势

用法:
    python worldcup_stats.py
"""

import json
import re
import requests
from collections import defaultdict

URL = "https://worldcup.lyihub.com/data/index.json"
BASE = "https://worldcup.lyihub.com/"
OUTPUT = "worldcup_胜率统计.json"
PLAYER_OUTPUT = "worldcup_球员数据.json"

# ============================================================
# 拉取
# ============================================================

resp = requests.get(URL, timeout=30)
resp.raise_for_status()
index_data = resp.json()
matches = index_data["matches"]
print(f"比赛总数: {len(matches)}")
print(f"数据时间: {index_data['generated_at']}")

# 余额趋势
bt = requests.get(BASE + "data/llm_balance_trends.json", timeout=15)
balance_data = bt.json() if bt.status_code == 200 else {}

# ============================================================
# 拉取每场比赛详情（含球员数据）
# ============================================================

all_players = {}        # match_id -> player data
all_team_profiles = {}  # match_id -> team profiles
all_llm_predicts = {}   # match_id -> llm detailed predictions

for i, m in enumerate(matches):
    mid = m["match_id"]
    try:
        r = requests.get(f"{BASE}data/matches/{mid}.json", timeout=15)
        if r.status_code == 200:
            detail = r.json()
            all_players[mid] = detail.get("players", {})
            all_team_profiles[mid] = detail.get("team_profiles", {})
            all_llm_predicts[mid] = detail.get("llm_predict", {})
        if (i + 1) % 20 == 0:
            print(f"  已拉取 {i+1}/{len(matches)} 场比赛详情...")
    except Exception as e:
        print(f"  拉取 {mid} 失败: {e}")

print(f"已拉取 {len(all_players)} 场球员数据")

# ============================================================
# 比分提取
# ============================================================

def extract_scores(comment):
    if not comment or not isinstance(comment, str):
        return []
    scores = []
    for m in re.finditer(r'(\d{1,2})\s*[-:：]\s*(\d{1,2})', comment):
        a, b = int(m.group(1)), int(m.group(2))
        if a <= 20 and b <= 20:
            scores.append((a, b))
    return scores

# ============================================================
# 统计胜负 & 比分
# ============================================================

model_stats = defaultdict(lambda: {
    "correct": 0, "total": 0,
    "score_correct": 0, "score_total": 0,
    "score_details": [],
})

for m in matches:
    actual = m.get("score", {})
    if not actual:
        continue

    a_g, b_g = actual.get("team_a", 0), actual.get("team_b", 0)
    result = "H" if a_g > b_g else ("A" if a_g < b_g else "D")

    bets = m.get("bets", {})
    comments = m.get("comment", {})
    team_a = m.get("team_a", "?")
    team_b = m.get("team_b", "?")

    for bet_type, model_list in bets.items():
        for model in model_list:
            stats = model_stats[model]
            stats["total"] += 1
            if bet_type == result:
                stats["correct"] += 1

            comment = comments.get(model, "")
            predicted_scores = extract_scores(comment)
            for ps in predicted_scores:
                stats["score_total"] += 1
                stats["score_details"].append({
                    "match": f"{team_a} vs {team_b}",
                    "actual": f"{a_g}-{b_g}",
                    "predicted": f"{ps[0]}-{ps[1]}",
                    "correct": ps[0] == a_g and ps[1] == b_g,
                    "comment": comment[:150],
                })
                if ps[0] == a_g and ps[1] == b_g:
                    stats["score_correct"] += 1

# ============================================================
# 输出
# ============================================================

lines = []
ranked = sorted(model_stats.items(), key=lambda x: x[1]["correct"] / x[1]["total"] if x[1]["total"] else 0, reverse=True)

lines.append("=" * 70)
lines.append("一、胜负预测胜率")
lines.append("=" * 70)
lines.append(f"{'模型':<12} {'正确':>6} {'总数':>6} {'胜率':>8}")
lines.append("-" * 35)
for model, stats in ranked:
    rate = stats["correct"] / stats["total"] * 100 if stats["total"] else 0
    lines.append(f"{model:<12} {stats['correct']:>6} {stats['total']:>6} {rate:>7.2f}%")

# 比分
score_ranked = sorted(model_stats.items(),
    key=lambda x: (x[1]["score_correct"] / x[1]["score_total"] if x[1]["score_total"] else 0, x[1]["score_correct"]),
    reverse=True)

lines.append("")
lines.append("=" * 70)
lines.append("二、比分预测（从评论提取）")
lines.append("=" * 70)
lines.append(f"{'模型':<12} {'提比分次数':>10} {'命中':>6} {'准确率':>8}")
lines.append("-" * 45)
for model, stats in score_ranked:
    st, sc = stats["score_total"], stats["score_correct"]
    rate = sc / st * 100 if st else 0
    lines.append(f"{model:<12} {st:>10} {sc:>6} {rate:>7.2f}%")

lines.append("")
lines.append("=" * 70)
lines.append("三、比分详情")
lines.append("=" * 70)
for model, stats in score_ranked:
    if stats["score_details"]:
        lines.append(f"\n--- {model} ---")
        for d in stats["score_details"]:
            mark = "[MATCH]" if d["correct"] else "[MISS]"
            lines.append(f"  {mark} {d['match']}: 预测 {d['predicted']}  实际 {d['actual']}")

# 余额
if balance_data:
    lines.append("")
    lines.append("=" * 70)
    lines.append("四、各模型账户余额趋势（最新）")
    lines.append("=" * 70)
    lines.append(f"{'模型':<12} {'最新余额':>10}")
    lines.append("-" * 25)
    balances = {}
    for llm in balance_data.get("llms", []):
        b_list = balance_data.get("balances", {}).get(llm, [])
        latest = b_list[-1] if b_list else 0
        balances[llm] = latest
    for llm, bal in sorted(balances.items(), key=lambda x: x[1], reverse=True):
        lines.append(f"{llm:<12} {bal:>+10.0f}")

# 球队 & 球员统计
lines.append("")
lines.append("=" * 70)
lines.append("五、球队 & 球员数据概览")
lines.append("=" * 70)

# 收集所有球队
all_teams = {}
for mid, profiles in all_team_profiles.items():
    for side, profile in profiles.items():
        if profile and isinstance(profile, dict):
            # Try to find team name
            name = profile.get("球队风格", "") or profile.get("球队风格", "")
            all_teams[mid] = all_teams.get(mid, {})
            # Skip garbled Chinese, collect raw data

player_count = sum(
    sum(len(players.get(side, [])) for side in players)
    for players in all_players.values()
)
teams_with_profiles = sum(1 for p in all_team_profiles.values() if p)
matches_with_players = sum(1 for p in all_players.values() if p)

lines.append(f"  有战术数据的比赛: {teams_with_profiles} 场")
lines.append(f"  有球员数据的比赛: {matches_with_players} 场")
lines.append(f"  球员记录总数: {player_count} 条")

result_text = "\n".join(lines)
print(result_text)
with open("_worldcup_result.txt", "w", encoding="utf-8") as f:
    f.write(result_text)

# ============================================================
# 输出 JSON
# ============================================================

output = {
    "generated_at": index_data["generated_at"],
    "total_matches": len(matches),
    "models": {}
}
for model, stats in ranked:
    t, c = stats["total"], stats["correct"]
    st, sc = stats["score_total"], stats["score_correct"]
    output["models"][model] = {
        "win_loss_correct": c, "win_loss_total": t,
        "win_loss_rate": round(c / t * 100, 2) if t else 0,
        "score_correct": sc, "score_total": st,
        "score_rate": round(sc / st * 100, 2) if st else 0,
    }
if balance_data:
    output["balances"] = balance_data

with open(OUTPUT, "w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

# 球员数据单独输出（可能很大）
with open(PLAYER_OUTPUT, "w", encoding="utf-8") as f:
    json.dump({
        "generated_at": index_data["generated_at"],
        "team_profiles": all_team_profiles,
        "players": all_players,
    }, f, ensure_ascii=False, indent=2)

print(f"\n已生成: {OUTPUT}")
print(f"已生成: {PLAYER_OUTPUT}")
