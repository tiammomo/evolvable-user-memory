# 开发指南

## 设计目标

系统优先保证记忆语义可解释、可追溯和可隔离，再逐步增加持久化与检索性能。不要为了快速接入数据库或模型供应商而破坏领域边界。

## 依赖方向

```text
api / adapters  →  application  →  domain
```

### Domain

- 只包含纯 Python 领域值、规则和状态转换。
- 使用不可变 `dataclass(frozen=True, slots=True)`。
- 不导入 FastAPI、Pydantic、数据库驱动、HTTP 客户端或供应商 SDK。
- 所有时间必须带时区，并规范化为 UTC。

### Application

- 使用按业务意图命名的命令和结果。
- 编排事务、端口调用和领域对象。
- 通过 `Protocol` 端口依赖存储、时钟和 ID 生成器。
- 不依赖具体数据库或 Web 框架。

### Adapters

- 实现 application 端口。
- 不把数据库实体泄露给 domain。
- 生产存储必须在数据库约束层重复落实 Scope、幂等、单活动修订和 Trace 归因规则。

### API

- Pydantic 只负责传输边界校验和 OpenAPI 描述。
- 把请求映射为 application command，把 result 映射为响应。
- 不在路由中实现业务状态转换。

## 五个平面不能混合

- Evidence 是输入事实，不能被后续信念改写。
- Belief 是从证据得出的当前结论，不代表有用性。
- Experience 来自可归因 Outcome，不来自读取次数。
- Projection 可丢弃并重建，不是事实来源。
- Evolution 只修改有界策略快照，不修改治理规则。

## 添加一个新用例

以新增一种记忆写入命令为例：

1. 在 `domain/` 定义必要的不可变值与规则。
2. 在 `application/commands.py` 定义业务命令和结果。
3. 如果跨边界需要新能力，在 `application/ports.py` 增加最小端口方法。
4. 在 `application/service.py` 编排事务和状态转换。
5. 在 `adapters/in_memory.py` 实现端口并落实一致性检查。
6. 在 `api/schemas.py` 定义请求、响应、字段说明和示例。
7. 在 `api/app.py` 增加薄路由。
8. 在 `application/security.py` 为端点定义精确 action，并通过 `AuthorizedMemoryApplication` 接入统一权限执行点。
9. 根据需要在前端增加入口，并更新 API、权限与使用文档。

## 测试要求

每个行为变化至少考虑：

- 业务规则的正常路径。
- tenant / subject 隔离。
- 允许角色、拒绝角色、错误 purpose 与授权审计。
- 幂等重放与幂等内容冲突。
- 缺失资源、非法状态或非法归因的错误路径。
- 修订历史是否保留。
- 召回或列表读取是否保持信念和效用不变。

测试分层：

| 位置 | 关注点 |
| --- | --- |
| `tests/test_memory_application.py` | 用例语义、隔离、幂等、归因 |
| `tests/test_api.py` | HTTP 状态、Schema、错误映射、CORS、OpenAPI |
| `tests/test_authorization.py` | JWT claim、角色/action、Scope、purpose、失败关闭与伪名审计 |
| `tests/test_evolution.py` | 策略边界和实验状态机 |
| `tests/test_frontend.py` | 静态服务、入口和安全响应头 |
| `tests/test_frontend_e2e.py` | 真实浏览器主流程、响应式布局、键盘和 Scope 迟到响应 |
| `tests/test_config.py` | 默认值、环境覆盖和非法配置 |
| `tests/test_postgres_integration.py` | PostgreSQL 持久化、重启恢复、约束语义和 outbox 写入 |

## 质量门禁

提交前运行：

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv build
```

覆盖率门禁当前为 85%，但不要用无意义测试追求数字；优先覆盖不变量和边界。

## 持久化现状与扩展要求

当前 PostgreSQL 适配器已经作为默认 Compose 的权威事件和修订存储，并具备版本化迁移、Scope/幂等/修订/Trace 归因数据库约束。Observation 摄入、Revision 变更和 Outcome 记录会与对应 outbox 事件在同一事务写入。内存适配器仍用于快速开发和确定性测试。

新增持久化能力时继续遵守：

1. 以 `(tenant_id, subject_id)` 作为查询、唯一键与索引的 Scope 前缀。
2. 同时在应用层和数据库层拒绝跨 Scope、幂等冲突、非法修订与错误 Trace 归因。
3. 权威状态与 outbox 必须在同一事务提交；outbox payload 不得包含原始证据正文。
4. 当前尚无 outbox 消费者。未来消费者必须支持租约/重试、幂等发布、可观测失败和受控重放。
5. 向量、图或摘要消费者只能更新可丢弃投影，不能反向创建权威 Revision。
6. 用源修订号与投影游标衡量延迟，并验证从权威状态确定性重建。

不要让向量数据库成为 MemoryRevision 的唯一存储。

## 检索扩展建议

当前词法检索器直接扫描活动修订。新增 embedding 或 graph 检索时：

- 保留当前 Scope 过滤。
- 返回可解释的候选和评分分量。
- 继续把最终结果固化到 RecallTrace。
- 投影缺失或滞后时不能静默发明信念。
- 召回仍然必须是信念和效用的只读操作。

## 安全边界

- 不默认记录原始证据日志。
- 不从不可信请求体决定生产 Scope；JWT 目标必须被同一条 grant 覆盖。
- tenant 管理员不自动继承记忆读取权限；原始 Evidence 使用独立高敏动作。
- 不允许演化引擎修改访问控制、删除、保留、抑制或审计规则。
- 添加删除能力时，需要同时处理权威状态、投影、缓存、Trace 保留与删除证明。

身份和动作变更必须对照[身份与权限设计](authorization.md)；隐私相关改动必须先对照[隐私生命周期设计](privacy-lifecycle.md)的失败关闭和验收标准；身份、隔离、outbox 或投影相关改动必须同时复核[威胁模型](threat-model.md)。
