# 威胁模型

> **状态：设计与安全验收基线，不是生产安全声明。**
>
> 当前版本是开发预览：已经具备 JWT 身份、类型化授权、伪名化授权审计基线和 Milvus 独立投影，但没有完整权限治理控制面、隐私生命周期执行、受控重放/删除屏障或生产 SLO。本文记录现有控制、已知缺口和未来上线门槛；未被测试和运行证据覆盖的建议不能视为已交付。

## 1. 范围与假设

本模型覆盖浏览器工作台、FastAPI 边界、application/domain、内存与 PostgreSQL 适配器、迁移、同事务 outbox、Compose 运行环境，以及未来的消费者、投影、备份和运维控制面。

当前安全假设仅适用于本机评估：

- 前后端默认绑定 `127.0.0.1`，PostgreSQL 不发布到宿主；
- 调用方传入的 `tenant_id` / `subject_id` 始终是不可信目标选择器；开发模式由本地身份放行，JWT 模式必须由可信 grant 覆盖；
- 开发用数据库凭据和 Compose 配置不是生产秘密管理方案；
- 使用真实敏感数据或把 API 暴露到公网超出当前支持范围。

## 2. 需要保护的资产

- 原始 Observation、EvidenceSpan 和来源元数据；
- 当前及历史 MemoryRevision、上下文和置信度；
- RecallTrace 中的查询、候选、排名与策略版本；
- Outcome、自由文本 note 和 UtilityEstimate；
- tenant/subject Scope、幂等键与内部对象 ID；
- PostgreSQL 凭据、迁移权限、备份和连接；
- outbox 事件、未来投影游标和重放控制；
- StrategySnapshot、append-only StrategyActivation、演化实验和治理配置；
- 日志、指标、追踪、审计证明和构建产物。

首要安全目标是机密性、Scope 隔离、权威状态完整性、可归因性、可用性，以及隐私生命周期不会被旁路。

## 3. 参与者

- 合法用户与集成服务；
- 伪造 Scope 或枚举 ID 的恶意客户端；
- 被劫持的浏览器、前端依赖或 API 凭据；
- 越权 tenant/subject 与恶意内部用户；
- 拥有数据库、备份、日志或重放权限的操作人员；
- 受损的 outbox 消费者、投影服务或模型供应商；
- 供应链攻击者和拒绝服务攻击者。

## 4. 信任边界

```text
不可信浏览器 / API client
        │  payload Scope 不可信；Bearer token 也必须完整验证
        ▼
FastAPI 传输边界
        │  JWT identity adapter + schema 映射
        ▼
Application authorization PEP
        │  action + tenant + subject + purpose；默认拒绝并审计
        ▼
Application + Domain
        │  typed ports；Scope 与归因规则
        ├──────────────> In-memory store（临时、进程内）
        ▼
PostgreSQL authority + outbox
        │  当前到此为止
        ▼
未来消费者 ──> 可丢弃投影 / 缓存 / 外部服务

运维人员 ──> 秘密、迁移、日志、备份、重放与治理控制面
```

边界要求：

1. 浏览器输入、请求 Scope、自由文本、幂等键和对象 ID 都不可信。
2. Application 端口是业务边界，不代表调用方已获授权；可信 Scope 必须在进入 command 前确定。
3. PostgreSQL 是 PostgreSQL 模式的权威状态；向量、图、摘要和缓存只能是可重建投影。
4. outbox 行与权威变更同事务写入并不表示事件已发布、下游已处理或投影正确。
5. 运维与重放权限是高风险控制面，必须与普通 API 权限分离。

## 5. 当前已有控制

这些控制已经存在，但不足以构成生产安全：

- 所有有状态 application 操作显式携带 tenant/subject Scope；查询与错误行为避免直接暴露其他 Scope 资源。
- JWT 模式验证 access token 的类型、签名、issuer、audience、expiry 和 API scope；生产/预发布配置本地身份时拒绝启动。
- 受保护用例统一通过 application 权限执行点，按 action、tenant、subject 与 purpose 默认拒绝；tenant 管理角色不自动获得记忆读取权。
- allow/deny 授权决策记录 policy version、request ID 和 HMAC 伪名化主体/Scope/资源引用，不记录 token 或记忆正文。
- PostgreSQL 使用复合外键/唯一约束落实 Scope、幂等键、修订身份和 Outcome 对 Trace item 的归因。
- 内存与 PostgreSQL 适配器对同一 Scope 内“相同键、不同内容”返回冲突，不静默覆盖。
- Revision 通过当前应用路径只追加，列表/召回不会更新 Belief 或 Utility。
- PostgreSQL 为 Observation 摄入、Revision 变更和 Outcome 记录在同一权威事务内追加 outbox，payload 不包含原始证据正文。
- `/livez` 检查进程，`/readyz` 检查当前存储连接；前端从 `/health` 动态显示存储方式。
- 前端切换 Scope 时取消旧请求并用 generation 隔离迟到响应；一次逻辑写入的网络重试复用幂等键。
- 默认 Compose 仅绑定本机，数据库只在内部网络；应用容器非 root、只读根文件系统、移除 capabilities 并启用 `no-new-privileges`。
- CI 执行 lint、格式、严格类型、覆盖率、PostgreSQL 集成、内存/PostgreSQL 双存储 Chromium E2E、axe-core 无障碍审计、发行包和容器构建检查。

## 6. 威胁场景与要求

| ID | 场景与影响 | 当前控制 | 生产前仍需的控制与验收 |
| --- | --- | --- | --- |
| T1 | 客户端伪造 tenant/subject，读取或写入他人记忆 | JWT grant 与 action/Scope/purpose 检查；开发身份仅限非生产；查询过滤 | IdP 成员生命周期、撤销/临时授权、权限管理控制面与数据库 RLS |
| T2 | 猜测 record、revision、trace ID 进行 IDOR 或枚举 | Scope 条件、授权 conceal、复合外键、统一 NotFound 和审计 | 数据库 RLS、批量/时序侧信道测试与异常枚举告警 |
| T3 | 重放写入或复用幂等键静默改变业务内容 | Scope 内唯一键与内容冲突 | 限流、重放窗口/业务事件归属；并发和故障恢复测试保证只发生一次副作用 |
| T4 | Scope 切换后旧响应覆盖新用户页面 | 取消请求、generation 检查、清理状态 | 真实浏览器并发回归；服务端授权仍是最终边界，不能依赖 UI 隔离 |
| T5 | 原始证据通过日志、outbox、错误或监控泄漏 | 应用默认不记录正文；outbox 不含正文 | 全链路日志/追踪脱敏测试、字段级访问、静态/动态泄漏扫描和事件响应流程 |
| T6 | SQL/Schema/自由文本注入或超大输入耗尽资源 | Pydantic 边界与参数化数据库调用 | 速率/配额、请求/查询超时、连接池隔离、模糊测试和容量上限 |
| T7 | 伪造 Trace 或 revision 提交 Outcome，污染 Utility | JWT 身份、`experience.outcome_write`、应用归因检查与数据库 Trace-item 复合外键 | actor/decision 持久溯源、委托链、异常率监控和跨 Scope 并发测试 |
| T8 | outbox 丢失、重复、乱序或恶意重放 | Milvus job receipt、租约、重试/死信、幂等 upsert、游标 | 通用发布确认、受权重放、删除屏障和积压告警 |
| T9 | 投影跨 Scope 泄漏、陈旧结果或删除后复活 | 独立投影尚未实现 | Scope 键、源 revision/游标、删除屏障、确定性重建与投影延迟降级测试 |
| T10 | 在线数据已删，但 Trace、Outcome、缓存、投影或备份仍可还原 | 未实现隐私生命周期 | 按[隐私生命周期设计](privacy-lifecycle.md)实现清单、抑制、编排、恢复屏障与证明 |
| T11 | 演化策略放宽治理规则、注册候选时偷换活动策略，或伪造阶段/证据后晋升 | 领域动作空间排除治理规则；候选注册与激活分离；幂等实验、合法阶段数据库守卫、append-only 转换历史、HMAC Gate Receipt 身份/阶段/决策/有效期绑定及原子晋升/回滚 | 授权控制面、外部产物摘要复核、非对称证明与独立 Receipt 审计存储、真实离线/影子/灰度编排和双人审批 |
| T12 | 数据库凭据、备份、迁移或运维控制面被滥用 | 本机网络与非 root 容器 | 秘密管理、TLS、最小数据库角色、短期凭据、双人审批、不可篡改审计与恢复演练 |
| T13 | 浏览器认证引入 token 泄漏、CSRF、错误 CORS 或会话固定 | API Bearer 校验、精确开发 CORS、安全响应头 | 生产 BFF/会话模型、PKCE、CSRF、防泄漏、登出/撤销和增强认证测试 |
| T14 | 依赖、镜像或构建产物被投毒 | 锁文件与 CI 构建检查 | 依赖/镜像扫描、SBOM、来源证明、签名发布、保护分支和漏洞响应门禁 |
| T15 | 数据库或消费者故障长期不可见，服务仍接流量 | PostgreSQL `/readyz` 独立短超时；池连接终止及完整网络中断/恢复回归 | 分层 SLI/SLO、告警、熔断/背压和消费者故障注入；就绪不等于端到端正确性 |

## 7. 生产安全门禁

在以下证据全部可复现前，不能宣称生产或多租户就绪。

### 身份与隔离

- [x] 当前记忆 API 动作有认证主体、授权动作、资源范围、purpose 和可信 grant 覆盖。
- [ ] IdP 成员/角色管理、撤销、临时授权、增强认证和策略变更流程形成运行证据。
- [ ] 同租户跨 subject、跨租户、未知 ID、批量枚举和时序侧信道测试通过。
- [ ] 数据库应用角色无迁移、超级用户、备份或任意跨 Scope 调试权限。

### 数据与隐私

- [ ] 数据分类、最小化、加密、日志脱敏、访问审计和密钥轮换形成测试与运行证据。
- [ ] [隐私生命周期设计](privacy-lifecycle.md)中的摄入失败关闭、抑制、删除、备份恢复和证明标准全部通过。
- [ ] 任何新投影、缓存、导出或模型供应商都进入数据清单并完成删除/隔离测试。

### 持久化与消息

- [ ] 迁移升级/回滚策略、并发约束、备份恢复、数据库中断和池耗尽经过故障注入。
- [ ] Milvus 消费者已覆盖至少一次投递、租约、幂等和死信；仍需证明乱序边界、重放授权、删除屏障和积压告警。
- [ ] 投影可从权威状态确定性重建；投影滞后/不可用不会静默发明或跨 Scope 返回信念。

### 运行时与供应链

- [ ] TLS、秘密管理、网络策略、最小权限、CORS/CSRF、限流、超时和资源配额有环境级验证。
- [ ] 依赖与镜像扫描、SBOM、构建来源证明、签名和紧急修复流程进入发布门禁。
- [ ] 管理、迁移、备份、删除和重放操作采用独立权限并产生防篡改审计记录。

### 观测与响应

- [ ] 为可用性、写入正确性、outbox 积压、投影延迟、跨 Scope 拒绝、删除积压定义 SLI/SLO 和告警。
- [ ] 生产日志、指标与追踪不包含原始证据正文或凭据，且有保留与访问策略。
- [ ] 完成越权、数据泄漏、数据库故障、投影污染和删除失败的演练与恢复记录。

当前尚未定义或验证上述生产 SLO。

## 8. 变更时如何维护本模型

下列变化必须在合并前更新本文件、测试和相应运行手册：

- 新增认证方式、API 端点、数据类别或外部供应商；
- 新增 outbox 消费者、投影、缓存、导出或后台任务；
- 改变 Scope、幂等、Trace 归因、删除、保留或演化规则；
- 改变数据库角色、网络入口、备份、日志或部署拓扑。

评审必须记录受影响资产、跨越的信任边界、滥用路径、失败关闭行为和可执行验收测试。只描述“已加密”“已隔离”或“已审计”而没有可复现证据，不足以关闭威胁。
