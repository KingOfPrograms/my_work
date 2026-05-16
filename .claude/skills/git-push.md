---
name: git-push
description: 代码完成后自动推送到 GitHub — 含安全自动修复
---
# git-push
## 执行流程
1. git status + diff 检查变更
2. 安全检查：扫描 API key/token/密码 → 发现后自动提取到 .env，修改源码用 os.environ
3. 生成中文 commit message
4. 确认提交
5. 推送
6. 反馈结果
