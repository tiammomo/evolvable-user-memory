# 前端工作台指南

前端是一个由 Python 标准库静态服务器托管的原生 HTML/CSS/JavaScript 应用。它不需要 Node.js、npm 或单独的构建步骤。

默认地址：<http://127.0.0.1:33009>

## 页面与用途

### 工作台

- 展示前端会话内的写入、召回和反馈计数。
- 展示当前 Scope。
- 用可点击的五步路径引导新人完成“确认 Scope → 写入 → 查看 → 召回 → 反馈”闭环。
- “最近活动”只存在于当前浏览器页面，不是后端审计日志。

## 新手引导

第一次访问会自动打开五屏概念引导，依次说明 Scope、Evidence、Belief、Trace 和 Outcome。页面顶部“新手引导”可以随时重播，也可以访问 `/?tour=1` 强制打开。

浏览器会用 `localStorage` 的 `emf.onboarding.v1` 标记首次引导已经看过。它只控制是否自动弹出，不影响后端数据，也不会阻止手动重播。

首页五步清单与当前前端会话联动：

1. Scope 始终显示当前 `tenant / subject`。
2. 写入成功或当前 Scope 已有记忆后，标记“写入偏好”。
3. 打开“当前记忆”后，标记“查看记忆”。
4. 成功执行召回后，标记“执行召回”。
5. 成功提交 Outcome 后，标记“提交反馈”。

这些进度用于教学，不是后端审计状态；刷新页面后只会从当前记忆恢复“写入偏好”，召回与反馈进度需要在新会话中重新完成。

### 写入记忆

页面右侧会根据 `/health` 返回的 `storage` 动态显示当前存储方式：原生快速开始默认为后端进程内存，后端重启会清空；默认 Compose 使用 PostgreSQL，进程或容器重启后数据仍会保留。

填写：

- 稳定记忆键，例如 `drink.preference`。
- 当前偏好值。
- 用户或来源的原始证据。
- 偏好成立的上下文键值。
- 证据来源与初始置信度。

“载入示例”只填充表单，必须点击“保存为记忆”才会调用 API。

### 当前记忆

调用 `GET /v1/preferences`，展示当前 Scope 内每条偏好的最新修订。

- 列表刷新是只读操作。
- “修订历史”读取完整不可变版本链。
- “修正记忆”会追加版本，不会覆盖旧值。

### 记忆召回

调用 `POST /v1/recall`，展示：

- 排名和综合得分。
- 词法相关性、上下文、信念、效用与时效评分。
- 保存这条偏好的上下文。
- 修订历史和修正入口。
- “有帮助”与“无帮助”Outcome 入口。

每次点击查询都会生成新 Trace；仅打开页面不会产生 Trace。

### 系统架构

以简化视图介绍五个平面和硬不变量。更精确的技术说明以 [架构文档](architecture.md) 为准。

## Scope 切换

当前静态工作台面向 `development` 身份模式，右上角 Scope 可编辑。JWT 生产模式需要由部署方
提供登录/BFF 或安全 token 注入方式；项目不会把 access token 写入 `localStorage`。即使前端
选择了 tenant/subject，后端仍会用可信 grant 检查 action、Scope 和 purpose，UI 不是权限边界。

右上角输入租户和用户后，点击“应用”。前端会：

1. 把 Scope 保存到浏览器 `localStorage`。
2. 取消旧 Scope 尚未完成的列表、写入、召回、修正、历史和 Outcome 请求。
3. 清除旧 Scope 的 Trace、召回结果、活动、会话计数、表单结果、弹窗和引导进度。
4. 重新读取新 Scope 的当前记忆。

每个请求还会记录发起时的 Scope generation；即使浏览器无法及时中止网络请求，旧 generation 的响应也不能覆盖新 Scope 页面。前端不会跨 Scope 合并数据，后端也会在存储读取、Trace 查找、修正和历史查询时再次检查 Scope。

## 请求重试与幂等

写入偏好、提交 Outcome 和追加修订都会按“逻辑操作 + 规范化 payload”保留幂等键：

- 网络超时或临时失败后，保持字段不变再次提交会复用原幂等键。
- 任一业务字段或 Scope 改变后会生成新幂等键。
- 重新打开一次修正弹窗代表新的修正意图，会开始新的幂等操作。

侧边栏健康状态请求最长等待 3.5 秒。失败或超时后可点击“重试”，无需刷新整个页面。

## 页面状态与后端状态

| 状态 | 保存位置 | 刷新页面 | 后端重启 |
| --- | --- | --- | --- |
| 当前 Scope | localStorage | 保留 | 保留 |
| 首次引导已读标记 | localStorage | 保留 | 保留 |
| 工作台会话计数 | JavaScript 内存 | 清空 | 不适用 |
| 最近活动 | JavaScript 内存 | 清空 | 不适用 |
| 记忆、修订、Trace、Outcome | 后端内存或 PostgreSQL | 保留 | 内存模式清空；PostgreSQL 保留 |

“本次会话概览”不是后端全量统计，也不是审计数据。
为避免视觉上混合不同用户，切换 Scope 时也会清空会话计数和最近活动。

## 前后端连接

`index.html` 中的 `api-port` meta 值默认为 `38089`。JavaScript 使用当前页面 hostname 和该端口构造 API 地址，因此：

- `127.0.0.1:33009` 会连接 `127.0.0.1:38089`。
- `localhost:33009` 会连接 `localhost:38089`。

后端默认允许这两个开发来源，也可通过 `EMF_CORS_ORIGINS` 配置。如果修改前端 API 端口，需要同步调整 `api-port` meta 值；修改前端来源时需要同步更新 CORS 配置。

## 修改前端

静态资源位于：

```text
src/evolvable_memory/api/static/
├── index.html
├── styles.css
├── app.js
└── mark.svg
```

静态服务器发送 `Cache-Control: no-store`，保存文件后刷新浏览器即可看到变化，不需要重新构建。修改 JavaScript 后至少运行：

```bash
node --check src/evolvable_memory/api/static/app.js
uv run pytest tests/test_frontend.py tests/test_api.py
```

### 浏览器 E2E

首次运行先安装 Chromium：

```bash
uv sync
uv run playwright install chromium
uv run pytest tests/test_frontend_e2e.py --no-cov
```

E2E fixture 会在随机空闲端口启动真实的内存后端和静态前端，不会占用开发环境的 `33009`/`38089`，也不会读取已有开发数据。测试覆盖：

- `390`、`900`、`1366`、`2048` 四档视口下的新手五步条溢出和文本重叠；
- 新手 Dialog 的可访问名称与键盘流程；
- Scope 切换后的旧结果清理，以及无法取消的迟到响应隔离；
- 跳转链接、导航、Scope 回车应用等基础键盘操作。

本机没有 Chromium 时，浏览器测试会安全跳过。CI 会执行 `playwright install --with-deps chromium` 并设置 `EMF_REQUIRE_BROWSER_E2E=1`，因此浏览器缺失会作为失败处理，不会静默跳过。

不要把证据、偏好值或用户输入直接拼接为 `innerHTML`。动态用户内容必须通过 `textContent` 或文本节点渲染。
