# API 使用指南

后端默认地址是 `http://127.0.0.1:38089`。启动后可在 `/docs` 使用 Swagger UI 直接调试。

## 契约约定

### 作用域

每个有状态操作都必须明确携带：

```json
{
  "tenant_id": "demo",
  "subject_id": "alice"
}
```

当前 API 为开发合同，因此 Scope 来自请求参数。生产适配器不能信任客户端自行声明的 Scope，必须从经过认证和授权的服务器端上下文中注入。

### 幂等键

写入偏好、修正和 Outcome 都需要 `idempotency_key`。

- 网络重试同一个业务动作时，复用原键。
- 新的事实、新的修正或新的结果使用新键。
- 同一 Scope 内复用键但改变业务内容会得到 `409 Conflict`。
- 不同 Scope 可以安全使用相同键。

建议格式：

```text
<业务事件 ID>:<动作>:<序号>
turn-42:preference:1
task-9:outcome:1
```

### 时间

- 所有领域时间必须带时区，并会规范化为 UTC。
- 请求省略 `occurred_at` 时，API 使用当前 UTC 时间。
- 如果上游知道事实实际发生时间，应显式传入 RFC 3339 时间。

### 错误

领域错误统一返回：

```json
{
  "error": "ConflictError",
  "detail": "idempotency key was reused for a different preference request"
}
```

Pydantic 请求校验错误使用 FastAPI 标准的 `422` 结构。

### 请求关联、大小限制与安全响应

每个 HTTP 响应都会包含 `X-Request-ID`：

- 调用方可以传入由字母、数字、点、下划线、冒号或连字符组成的 1–64 位 ID；
- 请求头缺失或不合法时，服务端生成新的随机 ID；
- 浏览器前端可以通过 CORS 读取该响应头；
- 领域错误和服务端错误的 JSON 也包含同一个 `request_id`，便于排查。

服务端只记录 JSON 结构化访问元数据，包括 request ID、HTTP 方法、路由模板、状态码、耗时和已读取字节数。日志不会记录请求 body、query string、tenant/subject 参数或原始证据。项目启动入口已关闭 Uvicorn 自带的 request-line access log；使用自定义 Uvicorn/Gunicorn 启动方式时也必须禁用其默认访问日志，避免 query string 被其他日志格式记录。

请求体默认最多 `1 MiB`，由 `EMF_MAX_REQUEST_BODY_BYTES` 调整。服务会先检查 `Content-Length`，同时对没有该头或声明不可信的实际请求流累计计数；超过限制统一返回：

```json
{
  "error": "RequestBodyTooLargeError",
  "detail": "Request body exceeds the configured limit of 1048576 bytes.",
  "request_id": "8e68e20353c54f658edc3bf49e0de55c"
}
```

未处理异常只返回通用 `500 InternalServerError`，不会向调用方或应用日志写入异常消息、请求正文或原始证据。响应同时带有 `Cache-Control: no-store`、`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: no-referrer` 和受限 `Permissions-Policy`。这些是应用层基线，不能替代 TLS、可信认证、反向代理限制或浏览器端 CSP。

## 端点

### `GET /`

返回版本、存储类型、前端入口、文档入口和生产就绪状态。首次连接服务时建议先调用。

### `GET /health`

返回服务状态、版本和当前存储类型，便于工作台与人工诊断；不验证外部依赖。它不能替代编排器的就绪探针。

### `GET /livez`

只检查 API 进程是否仍可响应，不访问当前存储。成功时返回 `200` 和 `{"status":"ok"}`。

### `GET /readyz`

检查当前存储是否可用。内存模式在应用正常运行时返回 `200`；PostgreSQL 模式会检查数据库连接。依赖不可用时返回 `503` 和 `not_ready`，适合作为容器或编排器的就绪探针。

### `POST /v1/preferences`

记录一条带上下文的偏好。

```bash
curl -X POST http://127.0.0.1:38089/v1/preferences \
  -H 'content-type: application/json' \
  -d '{
    "tenant_id": "demo",
    "subject_id": "alice",
    "source": "conversation",
    "idempotency_key": "turn-42:preference-1",
    "key": "drink.preference",
    "value": "decaf coffee",
    "context": {"time_of_day": "evening"},
    "evidence_text": "晚上我只喝低因咖啡",
    "confidence": 0.92
  }'
```

关键响应字段：

| 字段 | 含义 |
| --- | --- |
| `observation_id` | 原始输入包络 ID |
| `candidate_id` | 证据解释候选 ID |
| `record_id` | 记忆稳定身份，后续修正和历史查询使用 |
| `revision_id` | 本次不可变版本 ID |
| `sequence` | 修订序号 |
| `idempotent_replay` | 是否为安全重放 |

同一 `key + context` 的新证据会追加修订。值相同会增强 BeliefState；值不同会创建替代版本。

### `GET /v1/preferences`

列出 Scope 内所有当前有效偏好：

```bash
curl 'http://127.0.0.1:38089/v1/preferences?tenant_id=demo&subject_id=alice'
```

该端点只读取当前头版本，不生成 RecallTrace，也不修改信念或效用。

### `POST /v1/preferences/{record_id}/corrections`

追加明确的用户修正：

```bash
curl -X POST http://127.0.0.1:38089/v1/preferences/RECORD_ID/corrections \
  -H 'content-type: application/json' \
  -d '{
    "tenant_id": "demo",
    "subject_id": "alice",
    "source": "explicit-feedback",
    "idempotency_key": "turn-43:correction-1",
    "value": "herbal tea",
    "evidence_text": "其实晚上我改喝花草茶",
    "reason": "user corrected an outdated preference",
    "expected_revision_id": "CURRENT_REVISION_ID"
  }'
```

修正不会覆盖旧修订，而是通过 `supersedes_revision_id` 形成版本链。
`expected_revision_id` 可选；新客户端应传入页面读取到的当前修订 ID。若其已被其他写入替代，服务返回 `409`，避免旧页面静默覆盖新修订。为了兼容旧客户端，省略该字段时仍沿用原有行为。

### `GET /v1/preferences/{record_id}/revisions`

```bash
curl 'http://127.0.0.1:38089/v1/preferences/RECORD_ID/revisions?tenant_id=demo&subject_id=alice'
```

如果记录不存在于该 Scope，会返回 `404`，不会泄露其他 Scope 是否存在同一 ID。

### `POST /v1/recall`

```bash
curl -X POST http://127.0.0.1:38089/v1/recall \
  -H 'content-type: application/json' \
  -d '{
    "tenant_id": "demo",
    "subject_id": "alice",
    "query": "晚上应该准备什么饮料",
    "context": {"time_of_day": "evening"},
    "limit": 5
  }'
```

每次请求都会产生 RecallTrace，即使没有结果。响应包括：

- `trace_id`：Outcome 归因必须引用。
- `policy_id` / `policy_version`：本次召回使用的策略快照。
- `items`：排序后的当前有效修订。
- `breakdown`：每条结果的五个评分分量。

| 分量 | 当前含义 |
| --- | --- |
| `semantic` | 查询与 `key + value` 的词法相似度 |
| `context` | 保存上下文与请求上下文的匹配程度 |
| `belief` | 当前修订的信念置信度 |
| `utility` | 当前上下文中的 Outcome 效用均值 |
| `recency` | 按策略半衰期计算的证据新鲜度 |

召回只保存 Trace，不会修改 BeliefState 或 UtilityEstimate。

### `POST /v1/outcomes`

把真实结果归因到一次召回中的某条修订：

```bash
curl -X POST http://127.0.0.1:38089/v1/outcomes \
  -H 'content-type: application/json' \
  -d '{
    "tenant_id": "demo",
    "subject_id": "alice",
    "trace_id": "TRACE_ID",
    "revision_id": "REVISION_ID_FROM_THAT_TRACE",
    "kind": "helpful",
    "idempotency_key": "task-9:outcome-1",
    "weight": 1.0,
    "note": "the recommendation was accepted"
  }'
```

`revision_id` 不在对应 Trace 中时返回 `422 AttributionError`。

Outcome 种类：

| kind | 作为成功样本 | 典型用途 |
| --- | --- | --- |
| `helpful` | 是 | 记忆帮助完成任务 |
| `accepted` | 是 | 用户或业务接受了建议 |
| `harmful` | 否 | 结果造成明显负面影响 |
| `rejected` | 否 | 建议被拒绝或无帮助 |
| `corrected` | 否 | 召回内容随后被明确修正 |

`weight` 范围是 `(0, 10]`。不要把重复点击、曝光或单纯读取伪装成 Outcome。

## 状态码速查

| 状态码 | 含义 |
| --- | --- |
| `200` | 查询、列表或召回成功 |
| `201` | 写入、修正或 Outcome 成功；幂等重放仍返回 201 |
| `400` | 一般领域规则失败 |
| `404` | 资源在当前 Scope 中不存在 |
| `409` | 幂等内容冲突或并发状态冲突 |
| `413` | 请求体超过 `EMF_MAX_REQUEST_BODY_BYTES` |
| `422` | 请求结构校验失败，或 Outcome 无法归因 |
| `500` | 未处理服务端错误；响应只暴露安全通用信息和 request ID |
| `503` | `/readyz` 检测到当前存储不可用 |

## 集成建议

1. 在业务入口生成并持久化幂等键，不要每次 HTTP 重试都生成新键。
2. 保存 `trace_id` 与最终使用的 `revision_id`，任务完成后再提交 Outcome。
3. 不要把召回次数当成成功指标。
4. 在生产适配器中从认证上下文注入 Scope。
5. 对 `409` 区分安全重试和业务内容冲突，不要盲目换键重试。
6. 对 `404` 保持 Scope 无关的错误文案，避免跨租户枚举。
