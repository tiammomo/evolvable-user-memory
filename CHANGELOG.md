# Changelog

本文件记录项目中对使用者和集成者可见的重要变化。格式参考
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/)，版本遵循
[Semantic Versioning](https://semver.org/spec/v2.0.0.html)。

## [Unreleased]

### Added

- GitHub Actions 质量检查、Python 3.12–3.14/PostgreSQL 测试矩阵、Chromium 前端 E2E、发行包和容器构建。
- 以非 root 用户运行的本机评估镜像，以及按 PostgreSQL、迁移、后端、前端顺序启动的 Compose 配置。
- PostgreSQL 权威存储适配器、版本化 Schema 迁移、数据库 Scope/幂等/修订/Trace 归因约束与显式内存模式 Compose。
- Observation 摄入、Revision 变更和 Outcome 记录与不含原始证据正文的 outbox 事件同事务写入。
- `/livez` 进程探针、检查当前存储的 `/readyz` 就绪探针，以及前端动态存储方式提示。
- 请求 ID、实际请求流大小限制、仅元数据访问日志，以及 API/前端安全响应头。
- 部署指南、安全政策、分阶段路线图、隐私生命周期设计与威胁模型。
- 首次使用概念引导和可点击五步闭环。
- 本地开发身份与 RFC 9068 风格 JWT 身份适配器，包含非对称算法、issuer、audience、expiry 和 API scope 校验。
- 按 action、tenant、subject 和 purpose 默认拒绝的 application 权限执行点、内置最小权限角色，以及 HMAC 伪名化 allow/deny 审计。
- 身份与权限设计文档、生产配置门禁和 JWT/角色/Scope/purpose 负向测试。
- `evolvable-memory-eval` 评测入口与内置 synthetic `smoke-v1`，覆盖 Recall@k、MRR、更新、拒答、forbidden/Scope 隔离和执行失败硬门禁；CI 保存最小化报告，并从构建后的 wheel 验证资源，报告不输出 evidence/value。
- `POST /v1/recall` 的可选 `valid_at` / `known_at` 双时间历史状态投影；Revision 与 Outcome 按系统知识时间过滤，时效评分以业务有效时间为参照，Trace item 冻结命中修订的有效/记录时间。
- PostgreSQL `0003_bitemporal_recall` 迁移、双时间索引与约束，以及内存/PostgreSQL 一致的历史状态与历史效用重建。

### Changed

- 改善前端字体、色彩、响应式布局、焦点状态和新人操作反馈。
- 扩充 README、快速开始和前端指南，使页面流程与实际状态边界一致。
- 修复 Scope 切换时的旧请求/旧响应污染，并让一次逻辑写入在网络重试时复用幂等键。
- 加固应用与 API 的 Scope、幂等冲突和错误边界，保持内存与 PostgreSQL 适配器语义一致。
- 强化候选、修订链、Trace item 与策略版本的数据库跨表归因约束，并覆盖并发写入与迁移回滚。
- 增加不可演化的固定 relevance admission，防止 Belief、Utility 或 Recency 单独制造无关召回。
- 当前记忆列表按同一次服务端时点解析业务有效时间与系统知识时间，未来生效的修订不会提前成为当前结果。
- Outcome 分离业务 `occurred_at` 与系统 `recorded_at`；旧 Outcome 因原 Schema 未保存摄入时点，只能在迁移时以 `min(occurred_at, migration time)` 做 best-effort 近似回填。

## [0.1.0] - 2026-07-14

### Added

- Evidence、Belief、Experience、Projection 和 Evolution 五平面领域模型。
- 带 Scope、上下文、证据、置信度和幂等键的偏好记忆闭环。
- 不可变记忆修订、当前列表、修订历史与显式修正。
- 可解释召回、RecallTrace、可归因 Outcome 与上下文 Utility。
- 有界检索策略提案和演化实验状态机。
- FastAPI/OpenAPI 开发 API、进程内存适配器和原生 Web 工作台。
- 应用、API、隔离、幂等、演化、配置与静态前端测试。
- 快速开始、API、架构、开发、演化安全与故障排查文档。
