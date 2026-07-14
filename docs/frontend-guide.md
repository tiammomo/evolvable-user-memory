# 前端工作台指南

前端是一个由 Python 标准库静态服务器托管的原生 HTML/CSS/JavaScript 应用。它不需要 Node.js、npm 或单独的构建步骤。

默认地址：<http://127.0.0.1:33009>

## 页面与用途

### 工作台

- 展示前端会话内的写入、召回和反馈计数。
- 展示当前 Scope。
- 用四步路径引导新人完成首条闭环。
- “最近活动”只存在于当前浏览器页面，不是后端审计日志。

### 写入记忆

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
- 语义、上下文、信念、效用与时效评分。
- 保存这条偏好的上下文。
- 修订历史和修正入口。
- “有帮助”与“无帮助”Outcome 入口。

每次点击查询都会生成新 Trace；仅打开页面不会产生 Trace。

### 系统架构

以简化视图介绍五个平面和硬不变量。更精确的技术说明以 [架构文档](architecture.md) 为准。

## Scope 切换

右上角输入租户和用户后，点击“应用”。前端会：

1. 把 Scope 保存到浏览器 `localStorage`。
2. 重置当前页面的召回和反馈引导状态。
3. 重新读取新 Scope 的当前记忆。

前端不会跨 Scope 合并数据。后端也会在存储读取、Trace 查找、修正和历史查询时再次检查 Scope。

## 页面状态与后端状态

| 状态 | 保存位置 | 刷新页面 | 后端重启 |
| --- | --- | --- | --- |
| 当前 Scope | localStorage | 保留 | 保留 |
| 工作台会话计数 | JavaScript 内存 | 清空 | 不适用 |
| 最近活动 | JavaScript 内存 | 清空 | 不适用 |
| 记忆、修订、Trace、Outcome | 后端进程内存 | 保留 | 清空 |

“本次会话概览”不是后端全量统计，也不是审计数据。

## 前后端连接

`index.html` 中的 `api-port` meta 值默认为 `38089`。JavaScript 使用当前页面 hostname 和该端口构造 API 地址，因此：

- `127.0.0.1:33009` 会连接 `127.0.0.1:38089`。
- `localhost:33009` 会连接 `localhost:38089`。

后端 CORS 只允许这两个开发来源。如果修改端口，需要同步调整前端 meta 值和后端 CORS 配置。

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

不要把证据、偏好值或用户输入直接拼接为 `innerHTML`。动态用户内容必须通过 `textContent` 或文本节点渲染。
