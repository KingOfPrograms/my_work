\---

name: git-push

description: 代码完成后自动推送到 GitHub — git add、commit、push 一条龙

\---

\# git-push

代码编写完成后，将变更提交并推送到 GitHub 远程仓库。

\## 触发条件

当用户说出以下类似语句时触发：

\- "推送"、"提交"、"push"、"commit"

\- "上传到 GitHub"

\- "保存代码并推送"

\## 执行流程

\### 第 1 步：检查状态

同时执行 git status 和 git diff --stat，向用户展示待提交文件和变更统计。

\### 第 2 步：生成 commit message

根据实际变更内容，用中文生成 commit message（≤50 字），格式为 `<type>: <描述>`。

\### 第 3 步：安全检查

\- 绝不提交：.env、credentials.json、secrets.*、*.pem、*.key

\- 扫描 diff 中是否包含 API key、token、密码

\### 第 4 步：确认并提交

\- 将 commit message 展示给用户确认

\- 用户同意后，git add <逐个文件>（不用 git add -A）

\### 第 5 步：推送

\- git push，若无 upstream 则 git push -u origin <branch>

\### 第 6 步：反馈结果

\- 展示 commit hash、分支、推送状态