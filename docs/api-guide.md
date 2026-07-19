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

`development` 模式使用显式本地身份，因此请求 Scope 适合本机体验。`jwt` 模式把这些字段视为目标资源选择器；只有经过验证的 access token 中同一条 `memory_access` grant 同时覆盖 action、tenant、subject 和 purpose 时才会执行。修改 payload 不能扩大 token 的权限。

### 身份、动作与 purpose

`/v1/*` 端点全部经过 application 权限执行点；`/`、`/health`、`/livez`、`/readyz` 和 OpenAPI 用于发现与探针，当前保持公开且不返回记忆数据。

JWT 模式调用示例：

```bash
curl -H 'Authorization: Bearer ACCESS_TOKEN' \
  'https://api.example/v1/preferences?tenant_id=tenant-a&subject_id=alice&purpose=personalization'
```

token 必须通过类型、签名、issuer、audience、expiry 和 API scope 校验，并包含受信任的
`memory_access`。各端点所需动作、内置角色和 claim 结构见[身份与权限设计](authorization.md)。

请求体与受保护 GET 查询支持 `purpose`，默认值为 `personalization`。purpose 必须被执行该
action 的同一条 grant 允许；不能把只获准个性化的数据改用于 `model-training`。
JWT 授权通过后还必须存在同 Scope、同 purpose、未撤销且未过期的 ProcessingGrant；当前
SuppressionFence 永远优先。缺少 grant 或已抑制返回 `403`，治理依赖不可用返回 `503`，且
不会读取或写入业务数据。治理端点和调用顺序见[治理运行手册](governance.md)。

### 幂等键

写入偏好、修正、Memory Usage 和 Outcome 都需要 `idempotency_key`。

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
- `occurred_at` 表示业务事件何时发生；Revision 和 Outcome 另有由服务端产生的 `recorded_at`，表示系统何时获知并持久化它。
- Recall 的 `valid_at` 表示要查询的业务有效时点，`known_at` 表示系统知识截止时点。两者都省略或只省略其中一个时，缺省轴使用同一次服务端时钟读数，而不是分别读取可能不同的“当前时间”。
- `valid_at` 可以位于未来，用于检查已知的计划生效修订；显式 `known_at` 不能晚于请求执行时点，因为系统不能读取尚未获知的状态。

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

服务端只记录 JSON 结构化访问元数据，包括 request ID、HTTP 方法、路由模板、状态码、耗时和已读取字节数。日志不会记录请求 body、query string、tenant/subject 参数或原始证据。每次到达权限执行点的 allow/deny 还会写入独立的授权日志，其中 principal、client、tenant、subject 和 resource 都是 HMAC 伪名引用，不包含 token 或记忆正文。项目启动入口已关闭 Uvicorn 自带的 request-line access log；使用自定义 Uvicorn/Gunicorn 启动方式时也必须禁用其默认访问日志，避免 query string 被其他日志格式记录。

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

返回版本、存储类型、身份模式、Scope 来源、前端入口、文档入口和生产就绪状态。首次连接服务时建议先调用。

### `GET /health`

返回服务状态、版本、当前存储类型、身份模式和 Scope 来源，便于工作台与人工诊断；不验证外部依赖。它不能替代编排器的就绪探针，也不会暴露角色或 token 内容。

### `GET /livez`

只检查 API 进程是否仍可响应，不访问当前存储。成功时返回 `200` 和 `{"status":"ok"}`。

### `GET /readyz`

检查当前存储、治理表/保护触发器和持久审计是否可用；配置为必需时也检查 Milvus。依赖不可用时返回 `503` 和 `not_ready`，适合作为容器或编排器的就绪探针。PostgreSQL 连接池获取使用独立的 `EMF_DATABASE_READINESS_TIMEOUT_SECONDS` 上限（默认 1 秒），不会把该短超时施加到普通业务事务。

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

上面的普通请求省略双时间字段，服务端会从同一次时钟读数解析 `valid_at` 和 `known_at`。需要查询历史状态时可显式指定任一或两个轴：

```bash
curl -X POST http://127.0.0.1:38089/v1/recall \
  -H 'content-type: application/json' \
  -d '{
    "tenant_id": "demo",
    "subject_id": "alice",
    "query": "当时晚上应该准备什么饮料",
    "context": {"time_of_day": "evening"},
    "limit": 5,
    "valid_at": "2026-06-01T20:00:00+08:00",
    "known_at": "2026-06-02T00:00:00Z"
  }'
```

双时间筛选按每条 `MemoryRecord` 独立执行：

1. 只考虑 `created_at <= known_at` 的 Record；
2. 只考虑 `recorded_at <= known_at` 且 `valid_from <= valid_at` 的 Revision；
3. 在合格 Revision 中按系统记录时间、修订序号和 ID 稳定选择当时已知的最新版本；
4. Utility 只聚合 `recorded_at <= known_at` 且与请求上下文匹配的 Outcome；
5. Recency 以 `valid_at` 与证据时间的距离计算，而不是以请求执行时间计算。

因此，迟到但追溯生效的修正只有在其 `recorded_at` 到达 `known_at` 后才可见；已经记录的未来生效修订则要等 `valid_at` 到达其 `valid_from` 后才可见。两个字段必须包含 UTC offset；缺失 offset 或格式错误返回 `422`，未来 `known_at` 返回 `400 DomainError`。两轴互相独立，`valid_at` 可以晚于 `known_at`。

每次请求都会产生 RecallTrace，即使没有结果。响应包括：

- `trace_id`：Outcome 归因必须引用。
- `policy_id` / `policy_version`：本次召回使用的策略快照。
- `valid_at` / `known_at`：服务端最终使用并冻结到 Trace 的双时间边界；即使请求省略也会返回解析后的 UTC 时间。
- `created_at`：本次 Trace 的系统创建时间，`known_at` 不会晚于它。
- `items`：排序后的双时间可见修订；每项的 `revision_valid_from` / `revision_recorded_at` 会与值、上下文和分数一起冻结到 Trace item。
- `breakdown`：每条结果的五个评分分量。

| 分量 | 当前含义 |
| --- | --- |
| `semantic` | 词法相似度与可用 Milvus COSINE 分数的较大值；投影不可用时退回词法 |
| `context` | 保存上下文与请求上下文的匹配程度 |
| `belief` | 该双时间快照中修订的信念置信度 |
| `utility` | 当前上下文中、截至 `known_at` 已记录 Outcome 的效用均值 |
| `recency` | 相对 `valid_at`、按策略半衰期计算的证据新鲜度 |

召回只保存 Trace，不会修改 BeliefState 或 UtilityEstimate。

> **能力边界**
>
> `valid_at` / `known_at` 重建的是 Revision 与 Outcome 的历史可见状态。召回评分仍使用请求执行时的不可变 `StrategySnapshot`，而不是自动寻找过去运行时使用过的策略、投影实现或索引版本。因此该接口提供的是 historical state projection，不是完整历史策略 replay。Trace 中的 `policy_id` / `policy_version` 明确记录本次实际使用的执行时策略。

PostgreSQL `0003_bitemporal_recall` 会为旧 Trace 回填边界与 item 修订时间。旧 Outcome 原 Schema 没有保存系统摄入时点，迁移只能用 `min(occurred_at, migration time)` 近似 `recorded_at`；涉及迁移前 Outcome 的历史 Utility 是 best-effort 结果，不能作为精确的历史知识审计。

### `POST /v1/recall-contexts`

从一次不可变 RecallTrace 生成字符预算内的可归因 JSON 上下文：

```bash
curl -X POST http://127.0.0.1:38089/v1/recall-contexts \
  -H 'content-type: application/json' \
  -d '{
    "tenant_id": "demo",
    "subject_id": "alice",
    "trace_id": "TRACE_ID",
    "algorithm": "ranked-extractive-v1",
    "max_characters": 2000
  }'
```

当前算法：

| algorithm | 行为 |
| --- | --- |
| `ranked-extractive-v1` | 按 Trace rank 选择完整的规范 JSON 记忆对象，超预算对象整条省略 |
| `exact-deduplicated-v1` | 先精确去重相同 JSON 对象，再选择；合并片段保留全部源 revision |

响应中的 `content` 是 `{"memories":[...]}` 规范 JSON。`segments` 为每个对象保存源
`record_id`、`revision_id`、rank 和 score；`source_revision_ids` 提供扁平归因清单。
`configuration_sha256`、`source_sha256` 和 `projection_sha256` 分别固定算法配置、完整源 Trace
和最终投影。相同 Trace、算法和预算会得到相同内容与摘要。

该端点是需要 `projection.compress` 权限的纯读取操作：不创建新 Trace，不更新 Belief 或
Utility，也不把压缩正文持久化为第二份权威记忆。字符预算范围为 64–100000；它不是模型
token 预算。`value` 仍是不受信任的数据，下游必须把 JSON 作为数据而不是指令处理。算法、
完整性字段和后续 LLM 摘要扩展边界见[记忆压缩与上下文投影](memory-compression.md)。

### `POST /v1/usages`

在投影真正进入消费者上下文时记录不可变使用凭证：

```bash
curl -X POST http://127.0.0.1:38089/v1/usages \
  -H 'content-type: application/json' \
  -d '{
    "tenant_id": "demo",
    "subject_id": "alice",
    "trace_id": "TRACE_ID",
    "algorithm": "exact-deduplicated-v1",
    "max_characters": 2000,
    "source_projection_sha256": "PROJECTION_SHA256",
    "delivered_context_sha256": "DELIVERED_SHA256",
    "revision_ids": ["REVISION_ID_ACTUALLY_DELIVERED"],
    "idempotency_key": "task-9:memory-usage"
  }'
```

服务端会用同一个 Trace、算法和预算重建投影，核对 `source_projection_sha256`，并确认所有
`revision_ids` 都属于该投影。`delivered_context_sha256` 固定消费者二次过滤后真正交付的内容，
Memory 不需要保存这份正文。摘要篡改、Trace 外 revision 或 Scope 不匹配都会失败；安全重试
返回同一个 `usage_id` 和 `idempotent_replay=true`。

Usage 只是“实际进入上下文”的事实，不会更新 Belief 或 Utility，也不能被当作成功样本。

### `POST /v1/outcomes`

把真实结果归因到一次召回中的某条修订：

```bash
curl -X POST http://127.0.0.1:38089/v1/outcomes \
  -H 'content-type: application/json' \
  -d '{
    "tenant_id": "demo",
    "subject_id": "alice",
    "trace_id": "TRACE_ID",
    "usage_id": "USAGE_ID",
    "revision_id": "REVISION_ID_FROM_THAT_TRACE",
    "kind": "helpful",
    "idempotency_key": "task-9:outcome-1",
    "weight": 1.0,
    "note": "the recommendation was accepted"
  }'
```

提供 `usage_id` 时，`trace_id` 必须与 Usage 相同，且 `revision_id` 必须存在于该次实际使用中；
否则返回 `404` 或 `422 AttributionError`。为兼容旧消费者，省略 `usage_id` 时仍按 Trace 项校验，
但新集成应始终使用 Usage 级归因。

请求中的 `occurred_at` 表示结果在业务上发生的时间；服务端另行记录不可由调用方指定的 `recorded_at`。双时间召回使用 `recorded_at` 判断系统当时是否已经知道这条 Outcome，避免把迟到上报的旧业务事件泄漏到更早的知识快照。

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
| `201` | 写入、修正、Usage 或 Outcome 成功；幂等重放仍返回 201 |
| `401` | JWT 模式缺少 token，或 token 类型/签名/issuer/audience/expiry/claim 无效 |
| `403` | 身份可信，但 action 或 purpose 未授权 |
| `400` | 一般领域规则失败，例如 Recall 的 `known_at` 晚于服务端当前时间 |
| `404` | 资源在当前 Scope 中不存在，或 token 不覆盖目标 tenant/subject |
| `409` | 幂等内容冲突或并发状态冲突 |
| `413` | 请求体超过 `EMF_MAX_REQUEST_BODY_BYTES` |
| `422` | 请求结构校验失败，或 Outcome 无法归因 |
| `500` | 未处理服务端错误；响应只暴露安全通用信息和 request ID |
| `503` | `/readyz` 检测到当前存储不可用 |

## 集成建议

1. 在业务入口生成并持久化幂等键，不要每次 HTTP 重试都生成新键。
2. 保存 `trace_id`，实际交付时创建 `usage_id` 并固定最终使用的 revision；任务完成后再提交 Outcome。
3. 不要把召回次数当成成功指标。
4. 生产调用必须使用 JWT 模式；把 tenant/subject 当作目标选择器，不要把它们当作授权证明。
5. 对 `409` 区分安全重试和业务内容冲突，不要盲目换键重试。
6. 对 `404` 保持 Scope 无关的错误文案，避免跨租户枚举。
7. 需要可复现的历史状态时同时保存请求与响应中的 `valid_at`、`known_at`、`policy_id` 和 `policy_version`；不要把 historical state projection 表述为完整历史策略 replay。
8. 需要给 Agent 控制上下文体积时，先保存 Recall 的 `trace_id`，再调用 `/v1/recall-contexts`；仍应使用目标模型 tokenizer 做最终预算复核。
