# 身份与权限设计

> **状态：可信授权基线已实现，完整治理控制面仍在建设。**
>
> 当前 API 已支持本地开发身份与生产 JWT 身份两种模式；所有记忆用例在 application
> 权限执行点按 action、tenant、subject 和 purpose 默认拒绝。角色绑定目前来自受信任的
> JWT `memory_access` claim，尚未提供成员/角色管理 API、撤销列表、数据库 RLS、临时授权、
> 双人审批或权限管理 UI，因此项目仍不是完整生产权限平台。

## 1. 安全目标

权限系统必须分别回答：

1. 调用者是谁，身份由谁签发？
2. 调用者可以在什么 tenant 下操作？
3. 可以访问 tenant 内哪些 subject？
4. 可以执行哪个记忆动作？
5. 该动作是否允许用于当前处理目的？
6. 决策由哪个策略版本作出，是否留下不含正文的审计证据？

任何一个条件不满足都必须失败关闭。前端是否隐藏按钮、请求是否来自内网、调用者是否为
tenant 管理员，都不能单独构成读取记忆的依据。

## 2. 信任与执行流程

```text
Bearer access token
        │
        ▼
JWT identity adapter
  typ / signature / issuer / audience / expiry / API scope
        │
        ▼
ActorContext + tenant-local AccessGrant
        │
        ▼
AuthorizedMemoryApplication
  action + tenant + subject + purpose
        │
        ├── deny ──> 403 或隐藏资源的 404 + 审计
        │
        └── allow ─> MemoryApplication ─> Domain ─> Store
                           │
                           └── 授权决策审计
```

`api` 负责验证传输身份，`AuthorizedMemoryApplication` 是 application 层统一权限执行点，
`RolePolicyAuthorizer` 是当前策略适配器。领域层仍保持框架无关，不导入 JWT、FastAPI、角色表
或策略引擎。

请求中的 `tenant_id` / `subject_id` 在 JWT 模式下只是目标资源选择器。它们只有被同一个、
已经验证的 `AccessGrant` 覆盖后才会形成可信 Scope，客户端无法靠修改 payload 扩权。

## 3. 运行模式

### 3.1 `development`

- 默认模式，只允许 `development`/`test` 环境使用；
- 使用显式的 `development-console` 本地身份；
- 该身份拥有通配 Scope 和 `development_admin`，仅用于本机新人闭环；
- 只允许 `development` 或 `test` 环境；其他环境配置该模式时服务拒绝启动。

该模式不是“无权限系统”，而是一个被明确标注、只能在非生产环境使用的适配器。

### 3.2 `jwt`

JWT 模式要求：

- `typ` 为 `at+jwt` 或 `application/at+jwt`；
- 验证签名、`iss`、`aud`、`sub` 和 `exp`；
- 只允许配置非对称签名算法；
- token 的 `scope` 必须包含配置的 API scope；
- token 必须包含至少一条结构正确的 `memory_access`；
- JWT grant 必须显式列出 tenant、subject 和 purpose，不接受通配符；
- token 缺失、过期、签名错误、issuer/audience 错误或 claim 不完整统一返回 `401`。

生产 token 示例：

```json
{
  "iss": "https://identity.example",
  "aud": "evolvable-memory-api",
  "sub": "user-42",
  "exp": 1784102400,
  "scope": "memory",
  "principal_type": "user",
  "client_id": "memory-console",
  "memory_access": [
    {
      "tenant_id": "tenant-a",
      "subject_ids": ["alice"],
      "roles": ["subject_self"],
      "purposes": ["personalization"]
    }
  ]
}
```

角色、subject 和 purpose 必须位于同一条 grant 中。策略不会把一条 grant 的角色与另一条
grant 的 purpose 拼接后放行。

## 4. 动作目录

权限按记忆平面拆分，不能退化为一个笼统的 `memory.read`：

| 平面 | 动作 |
| --- | --- |
| Evidence | `evidence.ingest`、`evidence.read_raw`、`evidence.export` |
| Belief | `belief.read_current`、`belief.read_history`、`belief.correct` |
| Experience | `experience.trace_read`、`experience.outcome_write`、`experience.utility_read` |
| Projection | `projection.recall`、`projection.compress`、`projection.rebuild` |
| Evolution | `evolution.strategy_propose/promote/rollback` |
| Governance | `governance.role_manage`、`policy_manage`、`privacy_suppress`、`erasure_approve`、`audit_read`、`outbox_replay` |

当前 HTTP 用例映射：

| API | 必需动作 |
| --- | --- |
| `POST /v1/preferences` | `evidence.ingest` |
| `GET /v1/preferences` | `belief.read_current` |
| `POST /v1/preferences/{id}/corrections` | `belief.correct` |
| `GET /v1/preferences/{id}/revisions` | `belief.read_history` |
| `POST /v1/recall` | `projection.recall` |
| `POST /v1/recall-contexts` | `projection.compress` |
| `POST /v1/outcomes` | `experience.outcome_write` |

## 5. 内置角色模板

| 角色 | 用途与边界 |
| --- | --- |
| `subject_self` | 当前信念、历史、修正、召回/压缩、摄入和 Outcome；不读取原始 Evidence |
| `memory_reader` | 当前信念、历史、召回和可归因上下文压缩 |
| `memory_operator` | 当前 API 的完整记忆闭环与上下文压缩；不管理治理策略 |
| `service_agent` | 摄入、当前信念、召回/压缩和 Outcome；不读取历史或原始 Evidence |
| `privacy_officer` | 原始证据导出、抑制和删除审批 |
| `auditor` | 只读取授权审计 |
| `tenant_admin` | 管理角色和策略；不会自动获得记忆读取权 |
| `strategy_operator` | 提案、晋级和回滚检索策略 |
| `platform_operator` | 重建投影和受控 outbox 重放；不读取业务记忆 |
| `development_admin` | 仅本地开发适配器使用的全部动作 |

租户管理员如果同时需要记忆读取权，必须获得独立的 `memory_reader` grant。这样可以避免把
“管理成员”静默升级为“查看用户数据”。

## 6. Purpose 限制

所有受保护请求都带有受限长度的 `purpose`，默认开发示例为 `personalization`。策略要求：

```text
同一 AccessGrant
  覆盖 tenant/subject
  并包含所需 role/action
  并允许请求 purpose
```

例如只授权 `personalization` 的服务 token 不能把同一份记忆用于 `model-training`。purpose
不是自由授权开关；它必须由受信任的 grant 预先允许。未来 `ProcessingGrant` 上线后，还需
同时满足 subject 的处理依据、有效期和生命周期状态。

## 7. 授权审计

每次到达 application 权限执行点的允许或拒绝都会记录：

- decision ID、request ID、action、plane 和 purpose；
- allow/deny、稳定 reason code 和 policy version；
- principal kind、认证方式；
- principal、client、tenant、subject 和 resource 的 HMAC 截断引用。

审计日志不会包含 token、tenant/subject 原值、请求正文、Evidence、Memory value、query 或
Outcome note。JWT 模式要求独立的至少 32 字符 HMAC 密钥。该日志目前仍输出到运行时日志，
完整生产环境还需要不可篡改的独立审计存储、访问控制、保留策略、告警和密钥轮换。

## 8. 错误语义

| 状态码 | 权限语义 |
| --- | --- |
| `401` | 未提供有效 Bearer token，或 token 校验/claim 校验失败 |
| `403` | 身份可信，但 action 或 purpose 未授权 |
| `404` | token 不覆盖目标 tenant/subject；隐藏资源是否存在 |

响应不返回内部 policy reason。完整 reason 只进入受控授权审计。

## 9. 生产配置

```bash
export EMF_ENVIRONMENT=production
export EMF_AUTH_MODE=jwt
export EMF_AUTH_JWT_ISSUER='https://identity.example'
export EMF_AUTH_JWT_AUDIENCE='evolvable-memory-api'
export EMF_AUTH_JWT_JWKS_URL='https://identity.example/.well-known/jwks.json'
export EMF_AUTH_JWT_ALGORITHMS='RS256'
export EMF_AUTH_REQUIRED_SCOPE='memory'
export EMF_AUTH_AUDIT_HMAC_KEY='replace-with-a-secret-from-your-secret-manager'
```

即使这些配置有效，当前版本仍不能直接宣称生产就绪。部署方还必须提供：

- IdP 中角色/成员变更、撤销、离职和 token 生命周期管理；
- JWKS 网络故障、轮换和缓存策略；
- 短时 token、增强认证和高风险操作双人审批；
- 独立审计存储、告警、导出和保留策略；
- 数据库非 owner 应用角色与 RLS；
- `ProcessingGrant`、抑制、删除和删除证明；
- 权限管理 API/UI、有效权限预览和策略模拟。

## 10. 数据库 RLS 后续设计

现有 PostgreSQL 复合 Scope 约束仍是必要基础。下一阶段应增加：

1. migration owner、应用角色、隐私处理器和备份角色分离；
2. scoped 事务使用 `SET LOCAL` 写入可信 tenant/subject 上下文；
3. 权威表启用并强制 RLS，应用角色不是表 owner 且无 `BYPASSRLS`；
4. 连接池复用、事务回滚和异常路径验证上下文不会残留；
5. 后台扫描、删除和重放使用独立、限时且可审计的能力。

RLS 只作为纵深防御，不能替代 application action、purpose、字段级和生命周期授权。

## 11. 变更与测试门禁

新增端点时必须先定义 action 和 plane，再接入统一权限执行点。至少覆盖：

- 允许角色的成功路径；
- 无 action、错误 purpose、跨 subject 和跨 tenant；
- token 缺失、过期、错误 typ/issuer/audience/signature；
- 未知 resource 与跨 Scope 的一致外部行为；
- 拒绝请求没有领域写入或副作用；
- allow/deny 都有不含正文的审计事件；
- 策略或审计依赖异常时失败关闭；
- 连接池和未来 RLS 不泄漏上一个请求的 Scope。

Evolution 只能调整受限检索策略，永远不能修改 action、角色、Scope、ProcessingGrant、
保留、删除、抑制或审计规则。
