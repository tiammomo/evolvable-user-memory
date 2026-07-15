# 文档首页

这套文档按“先成功运行，再理解设计，最后扩展系统”的顺序组织。

## 按角色阅读

### 第一次使用

1. [快速开始](getting-started.md)
2. [前端工作台指南](frontend-guide.md)
3. [部署与运行指南](deployment.md)（使用 Docker 时）
4. [故障排查](troubleshooting.md)

完成后，你应该能够写入一条偏好、召回它、提交真实结果并追加修订。

### API 集成者

1. [API 使用指南](api-guide.md)
2. [身份与权限设计](authorization.md)
3. [架构说明](architecture.md)
4. 交互式文档：后端启动后打开 <http://127.0.0.1:38089/docs>

重点理解作用域、幂等键、`RecallTrace` 归因和错误码。

### 项目开发者

1. [开发指南](development.md)
2. [架构说明](architecture.md)
3. [演化安全模型](evolution-safety.md)
4. [贡献指南](../CONTRIBUTING.md)
5. [路线图](../ROADMAP.md)

重点保持 `api/adapters → application → domain` 的依赖方向和五个平面的语义边界。

### 部署与安全维护者

1. [部署与运行指南](deployment.md)
2. [安全政策](../SECURITY.md)
3. [身份与权限设计](authorization.md)
4. [威胁模型](threat-model.md)
5. [隐私生命周期设计](privacy-lifecycle.md)
6. [架构说明](architecture.md)中的 Scope 与持久化边界
7. [路线图](../ROADMAP.md)

当前容器仅用于本机开发评估。PostgreSQL 权威存储、迁移、数据库约束、同事务 outbox 和 JWT 授权基线已经实现；生产化前仍必须补齐权限治理控制面、数据库 RLS、隐私生命周期执行、outbox 消费/投影、故障恢复和可观测性 SLO。

## 当前版本地图

| 能力 | 状态 | 入口 |
| --- | --- | --- |
| 上下文偏好写入 | 已实现 | 前端“写入记忆”或 `POST /v1/preferences` |
| 当前记忆列表 | 已实现 | 前端“当前记忆”或 `GET /v1/preferences` |
| 不可变修订 | 已实现 | 修正弹窗或 corrections API |
| 词法与上下文召回 | 已实现 | 前端“记忆召回”或 `POST /v1/recall` |
| Outcome 效用学习 | 已实现 | 召回结果反馈或 `POST /v1/outcomes` |
| 有界策略提案 | 领域模型已实现 | `domain/evolution.py` |
| PostgreSQL 持久化与迁移 | 已实现（开发预览） | [部署指南](deployment.md) |
| 数据库 Scope/幂等/修订/归因约束 | 已实现（仍需故障与并发加固） | [架构说明](architecture.md) |
| 同事务 outbox 写入 | 已实现；消费者未实现 | [部署指南](deployment.md) |
| 前端动态存储提示与 Scope 旧响应隔离 | 已实现 | [前端工作台指南](frontend-guide.md) |
| embedding / graph 投影 | 未实现 | [路线图](../ROADMAP.md) |
| outbox 消费、重放与投影游标 | 未实现 | [路线图](../ROADMAP.md) |
| JWT 身份与 application 授权执行点 | 已实现（治理基线） | [身份与权限设计](authorization.md) |
| 角色管理、撤销、临时授权和数据库 RLS | 未实现 | [身份与权限设计](authorization.md) |
| 同意、保留、抑制、删除与证明 | 仅有设计，未实现 | [隐私生命周期设计](privacy-lifecycle.md) |
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
| RecallTrace | 某次召回的查询、策略版本、候选和评分记录 |
| OutcomeEvent | 引用 RecallTrace 的真实业务结果 |
| UtilityEstimate | 某修订在某上下文中的结果效用估计 |
| StrategySnapshot | 不可变、可回滚的检索策略参数快照 |
