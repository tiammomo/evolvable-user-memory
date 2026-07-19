# HTTP 消费者集成契约

Evolvable User Memory 与业务应用保持进程、代码和存储解耦。消费者只依赖版本化 HTTP
契约，不应导入本项目 Python 包、连接本项目 PostgreSQL/Milvus，或复制内部持久化实体。

## 能力协商

消费者首次连接时读取公开的 `GET /`。响应中的 `api_contract` 独立于应用发布版本；当前值为：

```text
evolvable-memory-http/v1
```

`capabilities` 列出当前实例支持的稳定业务能力。消费者必须按能力名称启用可选功能，不能用
应用版本号猜测接口行为。健康探针使用 `/livez`，依赖就绪检查使用 `/readyz`。

`production_ready` 与 `production_blockers` 是部署门禁，不是普通存活状态。生产消费者必须
默认拒绝 `production_ready=false` 的实例。阻塞项清零表示服务自有在线存储已启用
ProcessingGrant、抑制/删除、持久审计和可信 JWT；它不替代部署方的 IdP、保留、备份和
法务治理。开发环境可以显式允许 blocker 存在。

`configuration.*` 表示当前实例仍在使用开发适配器；只有已选择 PostgreSQL/JWT 生产适配器
但对应表、保护触发器或依赖不可用时，才报告 `privacy.*` / `audit.*` 功能运行阻塞。

## 依赖边界

```text
业务应用 -> HTTPS / JSON -> Evolvable User Memory API
```

- 业务应用拥有自己的用户、会话、任务和授权入口。
- Memory 服务拥有 Evidence、Belief、RecallTrace、MemoryUsage、Outcome 和检索策略状态。
- `tenant_id` / `subject_id` 是目标 Scope；生产授权必须来自可信 JWT grant，而不是请求体。
- 业务应用保存自己的业务事件 ID，以及后续归因所需的 `usage_id` / `trace_id` / `revision_id`。
- 网络重试复用原有 `idempotency_key`；不能通过更换键掩盖 `409` 内容冲突。
- Memory 不可用时是否降级由消费者业务决定；禁止把个性化可用性与授权或安全策略绑定。

## Consumer 与 workspace 隔离

Memory 的硬安全 Scope 是 `tenant_id + subject_id`。接入多个产品时，每个独立消费应用必须
使用自己的稳定 tenant、自己的 IdP client/workload token 和自己的 ProcessingGrant；禁止让
QuantPilot、另一个 Agent 产品或测试流量共用一个生产 tenant。JWT grant 必须只覆盖目标
tenant/subject/purpose，因此修改 payload 不能跨 Consumer 访问。

消费应用内部的 workspace/project 不是 Memory 授权 Scope。若偏好只适用于一个工作区，
Consumer 可写入服务端拥有的 `context.project_id`，召回时携带同一 facet，并在把 projection
交给模型前再次丢弃其他 project 的 segment。全局个人偏好不带 `project_id`，可在该 Consumer
的多个 workspace 间复用。该 facet 是选择器而非安全边界，不能用它替代独立 tenant；互不
信任的产品或法务隔离域必须拆 tenant，必要时拆实例。

推荐层级：

```text
tenant_id  = consumer application / legal boundary
subject_id = authenticated end user inside that consumer
context.product + context.project_id = consumer-owned personalization selector
```

模型和浏览器不能直接提供这三个值。业务路由应先完成项目成员授权，再由服务端从数据库
项目 ID 构造 context；Memory token broker 只接受服务端认证会话映射出的 tenant/subject。

## 推荐调用顺序

1. 在业务入口完成可信用户鉴权，并映射固定的 tenant/subject Scope。
2. 由治理控制面预先签发与业务 purpose 一致且未过期的 ProcessingGrant。
3. 调用 `/v1/recall`，保存 Trace 的策略和双时间边界。
4. 调用 `/v1/recall-contexts`，把有界 JSON 作为不可信数据交给下游。
5. 在内容真正进入下游上下文前调用 `/v1/usages`，提交相同算法、字符预算、源投影摘要、实际交付摘要和采用的 revision。服务端从不可变 Trace 重建投影并签发 `usage_id`。
6. 真实结果发生后，使用 `usage_id`、原 Trace 和 revision 调用 `/v1/outcomes`；服务端会拒绝不在该次实际使用中的 revision。
7. 新事实、明确修正、实际使用和结果反馈分别使用独立且稳定的业务幂等键。

消费者可以在自己的任务审计里组合 Memory `usage_id` 与知识平台 receipt，但不能把联合清单
变成 Memory 权威状态、复制记忆正文或让 Memory 直接依赖另一个平台。QuantPilot 的参考组合
流程见其 `docs/context-composition.md`。

Quant 等消费者不得签发自己的 Memory grant 或直连治理表。它只携带由可信 IdP 签发的短时
JWT；ProcessingGrant 的创建/撤销和删除由独立治理工作流调用。详见[治理运行手册](governance.md)。

完整字段、错误码与授权要求见 [API 使用指南](api-guide.md)和[身份与权限设计](authorization.md)。
