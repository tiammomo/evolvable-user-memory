# 治理运行手册

本手册描述 Memory 服务已经实现的四条生产治理链路：可信 JWT、ProcessingGrant、
抑制/删除和 PostgreSQL 持久审计。它约束服务自有的在线存储，不替代部署方的法律判断、
IdP 治理、备份到期、导出副本清理或安全审批。

## 不可绕过的执行顺序

所有偏好写入/修正/列表/历史、Recall、RecallContext、Memory Usage 和 Outcome 都执行：

```text
JWT typ/signature/issuer/audience/expiry/API scope
  -> memory_access 的 action + tenant + subject + purpose
  -> 持久化授权审计（失败则停止）
  -> 当前未撤销且未过期的 ProcessingGrant
  -> 当前 SuppressionFence（抑制永远优先）
  -> 权威业务事务 / 双时间查询
```

请求里的 `tenant_id` / `subject_id` 只是目标选择器；JWT 中同一条 grant 必须覆盖目标、角色
和请求 purpose。ProcessingGrant 由独立治理 API 管理，普通业务角色、模型输出、Outcome 和
Evolution 都不能创建它。

## 生产配置

生产/预发布启动时会强制要求以下组合；缺少任一项会在配置校验阶段拒绝启动：

```bash
export EMF_ENVIRONMENT=production
export EMF_STORE=postgres
export EMF_DATABASE_URL='postgresql://memory_app:...@postgres:5432/memory'

export EMF_AUTH_MODE=jwt
export EMF_AUTH_JWT_ISSUER='https://identity.example'
export EMF_AUTH_JWT_AUDIENCE='evolvable-memory-api'
export EMF_AUTH_JWT_JWKS_URL='https://identity.example/.well-known/jwks.json'
export EMF_AUTH_JWT_ALGORITHMS='RS256'
export EMF_AUTH_REQUIRED_SCOPE='memory'

export EMF_AUTH_AUDIT_SINK=postgres
export EMF_AUTH_AUDIT_HMAC_KEY='load-at-least-32-characters-from-secret-manager'
export EMF_GOVERNANCE_MODE=postgres
export EMF_GOVERNANCE_HMAC_KEY='use-a-separate-32-character-secret'
export EMF_GOVERNANCE_PSEUDONYM_KEY_ID='governance-2026-01'
export EMF_PRIVACY_POLICY_VERSION='privacy-2026-01'
```

先运行 `uv run evolvable-memory-migrate`。`GET /` 只有在 PostgreSQL、治理表/保护触发器、
持久审计表/保护触发器和 JWT 模式都符合配置时，才会清除这四项 blocker。`/readyz` 还会
实际检查这些依赖。HMAC key 必须由 secret manager 注入；轮换时使用新的 key ID，并制定旧
治理记录的迁移/并行查询方案，不能直接替换密钥后丢失旧 Scope 的定位能力。

## JWT 职责分离

推荐至少签发两类短时 access token：

- `tenant_admin` + purpose `privacy-governance`：签发或撤销 ProcessingGrant；
- `privacy_officer` + purpose `privacy-governance`：建立抑制、批准删除和读取删除证明；
- `subject_self` / `memory_operator` / `service_agent` + 业务 purpose：执行普通记忆操作。

治理 token 的 `memory_access` 仍必须显式列出 tenant 和 subject，不接受 `*`。一个 token
同时拥有多角色时也不会绕过 purpose 或 ProcessingGrant。

## ProcessingGrant

签发请求必须显式提供用途、依据代码、确定的 `valid_from`、可选 `valid_until` 和幂等键：

```http
POST /v1/governance/processing-grants
Authorization: Bearer <tenant-admin-token>
Content-Type: application/json

{
  "tenant_id": "tenant-a",
  "subject_id": "alice",
  "purposes": ["personalization"],
  "lawful_basis": "explicit-consent",
  "idempotency_key": "consent-event-42",
  "valid_from": "2026-07-19T08:00:00Z",
  "valid_until": "2027-07-19T08:00:00Z",
  "purpose": "privacy-governance"
}
```

相同 Scope 和幂等键的相同请求返回同一 grant；内容变化返回 `409`。撤销使用：

```http
POST /v1/governance/processing-grants/{grant_id}/revocation
```

撤销幂等。grant 缺失、过期、用途不匹配或治理数据库不可用时，普通处理在接触业务数据前
失败关闭。治理表只保存 tenant/subject/principal 的 HMAC 引用，不保存原始 Scope。

## 抑制与删除

抑制是立即停止使用，且当前没有普通“解除抑制”端点：

```http
POST /v1/governance/suppressions

{
  "tenant_id": "tenant-a",
  "subject_id": "alice",
  "reason_code": "subject-request",
  "idempotency_key": "privacy-case-100:suppress",
  "purpose": "privacy-governance"
}
```

屏障建立后，新写入、列表、历史、Recall、上下文压缩、Usage、Outcome 和 projector upsert 全部失败
关闭；`valid_at` / `known_at` 位于抑制之前也不能恢复可见性。Scope 级锁保证已在途的业务操作
先完成，抑制再落库；抑制返回后不会有已通过门禁的旧操作继续写入。

删除端点会自动先建立同一屏障：

```http
POST /v1/governance/erasures

{
  "tenant_id": "tenant-a",
  "subject_id": "alice",
  "reason_code": "subject-request",
  "idempotency_key": "privacy-case-100:erase",
  "purpose": "privacy-governance"
}
```

处理器覆盖 Observation、EvidenceSpan、Candidate、MemoryRecord/Revision/Transition、
RecallTrace/item、MemoryUsage/item、Outcome、UtilityEstimate、个人 outbox/projection job 和 Milvus Scope 文档；
全局 StrategySnapshot 不会因单个 subject 被删除。Milvus 先清理，权威表随后在一个
PostgreSQL 事务中清理；任何处理器失败时请求保持 `pending`、抑制保持有效，使用同一幂等键
重试。

删除证明可通过以下端点读取：

```http
GET /v1/governance/erasures/{request_id}?tenant_id=tenant-a&subject_id=alice&purpose=privacy-governance
```

证明只包含 request ID、HMAC Scope digest、策略版本、时间、数据类别计数和处理器状态，不含
原始 Scope、Evidence、Memory value、query 或 Outcome note。完成后的证明由数据库触发器
保护，不能更新或删除。

## 持久审计

`EMF_AUTH_AUDIT_SINK=postgres` 将每次 application 授权 allow/deny 写入
`authorization_audit_events`。记录只含决策元数据和 HMAC 引用；数据库触发器拒绝 UPDATE 和
DELETE。审计写入发生在领域操作之前，因此审计数据库失败不会形成“操作成功但无审计”的
窗口。

应用 API 目前不提供批量审计导出。生产部署应使用单独只读审计角色、受控查询/导出任务，
并为积压、失败、保留期和异常治理操作配置指标与告警。

## 仍由部署方完成

- 决定合法处理依据、支持用途、审批人、最长有效期和撤回流程；
- IdP 成员变更、token 撤销、增强认证和高风险操作双人审批；
- 自动保留扫描、法定保留例外、导出/分析副本和备份介质的最长存续期；
- HMAC 密钥轮换、审计访问控制、告警、恢复演练和删除抽样核验；
- 多区域部署、数据库最小权限/RLS、TLS、网络策略和生产 SLO。
