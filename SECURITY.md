# 安全政策

## 当前安全状态

Evolvable User Memory `0.1.x` 是开发预览版，不是生产就绪服务。当前版本：

- 提供进程内存与 PostgreSQL 权威存储、版本化迁移和数据库 Scope/幂等/修订/归因约束，但仍处于本机开发验证阶段；
- PostgreSQL 会为 Observation 摄入、Revision 变更和 Outcome 记录在同一事务追加不含原始证据正文的 outbox 事件，但尚无消费者、发布确认、受控重放或投影；
- 提供 `/livez` 与会检查当前存储的 `/readyz`，前端也会动态展示存储并隔离 Scope 切换后的旧响应；
- 提供本地开发身份与 JWT 身份适配器，并在 application 边界按 action、tenant、subject 和 purpose 默认拒绝授权；开发身份只允许 `development/test`，其他环境配置时拒绝启动；
- 对每次 allow/deny 记录不含正文和 Scope 原值的 HMAC 伪名化授权审计，但尚无独立防篡改审计存储、角色管理 API、撤销/临时授权或数据库 RLS；
- 尚未实现同意、删除证明、保留与抑制执行、生产审计存储和安全运维 SLO；
- 默认只监听本机地址；不要把当前 API 直接暴露到公网，也不要写入真实敏感数据。

支持状态：

| 版本 | 安全报告 | 生产支持 |
| --- | --- | --- |
| `main` / Unreleased | 接受 | 否 |
| `0.1.x` | 尽力处理 | 否，开发预览 |
| 更早版本 | 不再维护 | 否 |

## 私密报告漏洞

请优先使用 GitHub 仓库的
[Private vulnerability reporting](https://github.com/tiammomo/evolvable-user-memory/security/advisories/new)
提交安全报告。报告应尽量包含：

1. 受影响版本、提交或部署方式；
2. 可复现步骤和最小概念验证；
3. 对 Scope 隔离、证据、修订、Trace、Outcome 或删除语义的影响；
4. 已知缓解措施，以及报告者希望采用的署名方式。

如果仓库暂未启用私密报告，请只创建一条不含漏洞细节的普通 Issue，请求维护者提供私密沟通渠道。不要在公开 Issue、讨论、测试夹具或日志中披露利用代码、真实证据、凭据或敏感 Scope 数据。

项目当前由维护者尽力响应，尚不承诺固定响应或修复 SLA。收到报告后会先确认影响范围，再决定修复、缓解、版本说明和披露安排。

## 特别关注的安全不变量

以下问题应按安全漏洞报告，而不是普通功能缺陷处理：

- 跨 tenant 或 subject 读取、写入、召回、Trace 或 Outcome 归因；
- 未经对应 RecallTrace 归因就更新 Utility；
- 覆盖或改写不可变 Observation、EvidenceSpan、MemoryRevision、Trace 或 Outcome；
- 原始证据、凭据或敏感标识进入默认日志；
- 幂等冲突导致静默覆盖或跨 Scope 折叠；
- 删除、保留、抑制或审计规则可被策略演化绕过；
- 生产适配器仅信任客户端请求中的 Scope。
- JWT 校验、grant 解析或授权/审计依赖失败时仍执行记忆操作。

## 安全部署要求

当前容器和 Compose 配置只用于本机评估。现有 PostgreSQL 约束和同事务 outbox 写入不等于完整生产信任边界；正式部署至少还必须提供：

- IdP 成员/角色治理、token 撤销与轮换、增强认证、数据库 RLS 和独立审计存储；
- 数据库最小权限、迁移门禁、并发/故障验证、备份恢复与灾难恢复演练；
- outbox 幂等消费者、发布重放控制、投影隔离与游标恢复；
- TLS、秘密管理、受限网络和最小权限运行；
- 依赖与镜像扫描、持久审计、监控、告警和可验证 SLO；
- 同意、保留、抑制、删除及其投影清理与证明机制。

身份、角色、动作、purpose 和审计边界见[身份与权限设计](docs/authorization.md)。威胁、信任边界和生产安全验收门槛见[威胁模型](docs/threat-model.md)；隐私生命周期的目标工作流和验收标准见[隐私生命周期设计](docs/privacy-lifecycle.md)。进一步的生产缺口和顺序见 [ROADMAP.md](ROADMAP.md) 与[部署指南](docs/deployment.md)。
