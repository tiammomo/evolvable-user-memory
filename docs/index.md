# 文档首页

这套文档按“先成功运行，再理解设计，最后扩展系统”的顺序组织。

## 按角色阅读

### 第一次使用

1. [快速开始](getting-started.md)
2. [前端工作台指南](frontend-guide.md)
3. [故障排查](troubleshooting.md)

完成后，你应该能够写入一条偏好、召回它、提交真实结果并追加修订。

### API 集成者

1. [API 使用指南](api-guide.md)
2. [架构说明](architecture.md)
3. 交互式文档：后端启动后打开 <http://127.0.0.1:38089/docs>

重点理解作用域、幂等键、`RecallTrace` 归因和错误码。

### 项目开发者

1. [开发指南](development.md)
2. [架构说明](architecture.md)
3. [演化安全模型](evolution-safety.md)
4. [贡献指南](../CONTRIBUTING.md)

重点保持 `api/adapters → application → domain` 的依赖方向和五个平面的语义边界。

## 当前版本地图

| 能力 | 状态 | 入口 |
| --- | --- | --- |
| 上下文偏好写入 | 已实现 | 前端“写入记忆”或 `POST /v1/preferences` |
| 当前记忆列表 | 已实现 | 前端“当前记忆”或 `GET /v1/preferences` |
| 不可变修订 | 已实现 | 修正弹窗或 corrections API |
| 词法与上下文召回 | 已实现 | 前端“记忆召回”或 `POST /v1/recall` |
| Outcome 效用学习 | 已实现 | 召回结果反馈或 `POST /v1/outcomes` |
| 有界策略提案 | 领域模型已实现 | `domain/evolution.py` |
| PostgreSQL 持久化 | 未实现 | 路线图 |
| embedding / graph 投影 | 未实现 | 路线图 |
| 认证、授权和生产隔离 | 未实现 | 必须由生产适配器提供 |
| 删除证明与保留策略 | 未实现 | 路线图 |

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
