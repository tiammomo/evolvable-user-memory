# 文档首页

这套文档按“先成功运行，再理解设计，最后扩展系统”的顺序组织。

如果希望通过一份文档完整了解项目的适用场景、架构、数据库分工、本地运行、网页/API 使用、测试、故障排查和生产边界，请先阅读[完整项目手册](project-handbook.md)。

## 按角色阅读

### 第一次使用

1. [完整项目手册](project-handbook.md)
2. [快速开始](getting-started.md)
3. [前端工作台指南](frontend-guide.md)
4. [部署与运行指南](deployment.md)（使用 Docker 时）
5. [故障排查](troubleshooting.md)

完成后，你应该能够写入一条偏好、召回它、提交真实结果并追加修订。

### API 集成者

1. [HTTP 消费者集成契约](integration-contract.md)
2. [API 使用指南](api-guide.md)
3. [身份与权限设计](authorization.md)
4. [架构说明](architecture.md)
5. 交互式文档：后端启动后打开 <http://127.0.0.1:38089/docs>

重点理解作用域、幂等键、`valid_at` / `known_at` 双时间、`RecallTrace` 归因和错误码。

### 项目开发者

1. [开发指南](development.md)
2. [架构说明](architecture.md)
3. [ADR-0001：按依赖层与记忆平面渐进整理代码](adr/0001-layer-and-plane-boundaries.md)
4. [记忆评测指南](evaluation.md)
5. [演化安全模型](evolution-safety.md)
6. [贡献指南](../CONTRIBUTING.md)
7. [路线图](../ROADMAP.md)

重点保持 `api/adapters → application → domain` 的依赖方向和五个平面的语义边界。

### 评测与策略开发者

1. [记忆评测指南](evaluation.md)
2. [演化安全模型](evolution-safety.md)
3. [架构说明](architecture.md)中的固定 relevance admission
4. [路线图](../ROADMAP.md)中的混合检索与受控演化

先用 `builtin:smoke-v1` 检查 retrieval/invariant 回归，再用 `builtin:temporal-v1` 检查双时间与 Outcome 知识时间回归。两个 synthetic 契约集都不是论文 benchmark 或策略晋升的充分证据。

### 部署与安全维护者

1. [部署与运行指南](deployment.md)
2. [Milvus 投影指南](milvus-projection.md)
3. [安全政策](../SECURITY.md)
4. [身份与权限设计](authorization.md)
5. [威胁模型](threat-model.md)
6. [隐私生命周期设计](privacy-lifecycle.md)
7. [治理运行手册](governance.md)
8. [数据生命周期与安全清理](data-lifecycle.md)
9. [架构说明](architecture.md)中的 Scope 与持久化边界
10. [路线图](../ROADMAP.md)

默认容器仍用于本机开发评估。PostgreSQL 权威存储、同事务 outbox、Milvus 投影、可信 JWT、处理依据、抑制/删除和持久审计已经实现；生产化前仍需部署真实 IdP、审批流程、数据库 RLS、保留/备份治理、故障恢复和可观测性 SLO。

## 当前版本地图

| 能力 | 状态 | 入口 |
| --- | --- | --- |
| 上下文偏好写入 | 已实现 | 前端“写入记忆”或 `POST /v1/preferences` |
| 当前记忆列表 | 已实现 | 前端“当前记忆”或 `GET /v1/preferences` |
| 不可变修订 | 已实现 | 修正弹窗或 corrections API |
| 词法/向量与上下文混合召回 | 已实现；Milvus 可降级 | 前端“记忆召回”或 `POST /v1/recall` |
| 双时间历史状态投影 | 已实现；不是完整历史策略 replay | `POST /v1/recall` 的 `valid_at` / `known_at` |
| 服务端可验证的实际使用凭证 | 已实现 | `POST /v1/usages` |
| Outcome 效用学习 | 已实现 | 召回结果反馈或 `POST /v1/outcomes` |
| 有界策略提案 | 领域模型已实现 | `domain/evolution.py` |
| synthetic retrieval/invariant 与双时间评测 | 已实现（smoke-v1 / temporal-v1） | [记忆评测指南](evaluation.md) |
| PostgreSQL 持久化与迁移 | 已实现（开发预览） | [部署指南](deployment.md) |
| 数据库 Scope/幂等/修订/归因约束 | 已实现（仍需故障与并发加固） | [架构说明](architecture.md) |
| 同事务 outbox 写入 | 已实现 | [部署指南](deployment.md) |
| 前端动态存储提示与 Scope 旧响应隔离 | 已实现 | [前端工作台指南](frontend-guide.md) |
| Milvus embedding 投影 | 已实现（开发预览） | [Milvus 投影指南](milvus-projection.md) |
| Milvus 消费、重试、死信、游标与重建 | 已实现；远程受控重放未实现 | [Milvus 投影指南](milvus-projection.md) |
| JWT 身份与 application 授权执行点 | 已实现（治理基线） | [身份与权限设计](authorization.md) |
| 角色管理、撤销、临时授权和数据库 RLS | 未实现 | [身份与权限设计](authorization.md) |
| ProcessingGrant、抑制、在线删除与证明 | 已实现；保留扫描/备份治理待部署 | [治理运行手册](governance.md) |
| 生产监控、告警与 SLO | 未实现 | [路线图](../ROADMAP.md) |

## 术语速查

| 术语 | 含义 |
| --- | --- |
| Scope | `(tenant_id, subject_id)`，所有有状态操作的隔离边界 |
| Observation | 系统收到的不可变原始输入包络 |
| EvidenceSpan | Observation 中支持或反驳某个解释的最小证据片段 |
| Candidate | 从证据提出、尚未成为持久信念的解释 |
| MemoryRecord | 一条记忆的稳定身份 |
| MemoryRevision | 该记忆在某个时间点的不可变版本 |
| valid time | 由 `valid_from` / `valid_at` 表示的业务有效时间轴 |
| known time | 由 `recorded_at` / `known_at` 表示的系统知识时间轴 |
| RecallTrace | 某次召回的双时间边界、策略版本、候选和评分记录 |
| MemoryUsage | 消费者实际交付的 revision、源投影摘要和交付摘要组成的不可变凭证 |
| OutcomeEvent | 引用 RecallTrace 的真实业务结果；区分业务发生时间与系统记录时间 |
| UtilityEstimate | 某修订在某上下文中的结果效用估计 |
| StrategySnapshot | 不可变、可回滚的检索策略参数快照 |
| StrategyActivation | 某个已注册策略成为活动策略的不可变记录；注册候选本身不产生激活 |
| EvolutionExperiment | 候选从提案到离线、影子、灰度、晋升或回滚的持久化当前状态 |
| ExperimentTransition | 带幂等键、请求指纹和外部证据引用的 append-only 阶段变化记录 |
| historical state projection | 按双时间重建 Revision/Outcome 状态，再用执行时 StrategySnapshot 召回；不等于完整历史策略重放 |
| Hard gate | 不能被平均质量分抵消的通过条件；失败时评测命令返回非零 |
