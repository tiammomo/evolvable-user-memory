# 架构说明

## 1. 系统目标

系统必须分别回答三个问题：

1. 系统实际观察到了什么？
2. 系统当前相信什么，置信度与有效时间是什么？
3. 在特定上下文中使用这条信念是否真的有用？

把三者混为一谈会形成自我强化错误：一段内容因为被频繁召回而得分更高，又因为得分更高而被更频繁召回。本项目通过独立数据结构和更新规则切断这个循环。

## 2. 运行时结构

```text
Browser
  │  http://127.0.0.1:33009
  ▼
Static frontend (HTML / CSS / JavaScript)
  │  JSON over HTTP + CORS
  ▼
FastAPI backend :38089
  │
  ▼
MemoryApplication
  │
  ├── MemoryStore port ──> InMemoryMemoryStore / PostgresMemoryStore
  ├── Clock port ────────> SystemClock
  └── IdGenerator port ──> Uuid4Generator
```

前端和后端是两个独立进程。前端不保存权威记忆状态；原生快速开始默认使用线程安全进程内存，默认 Compose 使用 PostgreSQL 权威存储。

## 3. 代码依赖

```text
bootstrap  →  api / adapters  →  application  →  domain
```

### Domain

拥有领域值、硬不变量和状态转换。必须保持框架无关。

### Application

拥有用例顺序和事务边界，通过类型化端口依赖基础设施。

### Adapters

实现存储、时钟和 ID 端口。基础设施实体不能泄漏到领域层。

### API

校验传输数据、映射命令和响应、生成 OpenAPI，不承载领域状态转换。

### Bootstrap

`bootstrap/api.py` 是显式进程组合根。CLI 读取并验证配置后在这里创建 API 应用；单独导入 `create_app` 或历史 ASGI `app` 不会连接 PostgreSQL，兼容 `app` 只在第一次 ASGI 使用时惰性组装。

依赖护栏、五平面与后续拆包顺序已经固化在 [ADR-0001](adr/0001-layer-and-plane-boundaries.md)；结构调整采用兼容外观渐进迁移，不做一次性重写。

## 4. 五个平面

### Evidence：证据平面

`Observation` 是不可变输入包络，保存 Scope、来源、原始内容、业务发生时间、摄入时间和幂等键。

`EvidenceSpan` 指向 Observation 中最小有用证据，可支持或反驳某个解释。

`Candidate` 是从证据提出的解释，不等于持久信念。当前偏好垂直切片会立即接受合法候选；未来可以插入审核、策略确认或隔离流程。

规则：

- 重复投递通过 Scope 内幂等键折叠。
- 证据只追加，不会为了适配后续结论而改写。
- 原始证据默认不进入日志。

### Belief：信念平面

`MemoryRecord` 提供稳定身份，身份由 Scope、记忆键和上下文共同确定。

`MemoryRevision` 表示不可变版本。修正或新证据会追加修订，并通过 `supersedes_revision_id` 连接旧版本。每个修订同时保存两个时间轴：

- `valid_from`：该信念在业务世界中从何时开始成立；
- `recorded_at`：系统从何时开始知道并持久化该修订。

`active_revision_id` 仍是写入并发控制和最新事务头指针，但历史状态查询不能只读取它。双时间投影必须从不可变修订链按业务有效时间与系统知识时间重新选择。

`BeliefState` 跟踪：

- confidence
- support_count
- contradiction_count
- source_diversity
- last_evidence_at

这些字段描述系统相信结论的程度，不描述它是否有用。

### Experience：经验平面

每次召回都会保存 `RecallTrace`：

- Scope、查询和上下文
- 策略 ID 与版本
- 已解析的 `valid_at` 与 `known_at`
- 返回修订和排名
- 每个命中修订的 `revision_valid_from` 与 `revision_recorded_at`
- 综合得分与评分分量
- 创建时间

`OutcomeEvent` 必须引用同 Scope 的 Trace，并引用该 Trace 中实际出现的 revision。它区分调用方提供的业务 `occurred_at` 与服务端生成的系统 `recorded_at`；历史 Utility 只使用在 `known_at` 之前已经记录的 Outcome。

`UtilityEstimate` 以 `(revision_id, context_fingerprint)` 为键，使用带先验的成功/失败权重更新均值。

规则：读取、列表和召回都不会更新 Belief 或 Utility。

### Projection：投影平面

关键词、embedding、图和摘要索引都是可丢弃投影：

- 可从权威 Observation 和 Revision 重建。
- 不能静默创造新的 Revision。
- 必须有源修订号或游标，才能衡量投影延迟。

当前版本没有独立投影存储，而是从 Scope 内权威修订链重建双时间可见快照，再计算词法分数。这是可替换实现，不是目标架构的权威数据模型。

### Evolution：演化平面

检索权重、阈值和时效参数保存在不可变 `StrategySnapshot` 中。根策略和子策略形成只增不改的谱系；父策略必须已存在，子版本必须连续。

`PolicyEvolution` 根据失败诊断提出有界子快照，每次只允许很小的权重变化。提案不是自动晋升；离线回放、影子、灰度和回滚由未来适配器编排。

默认运行通过 append-only `StrategyActivation` 历史解析权威活动策略。首次启动只允许原子 `bootstrap`；并发实例和进程重启复用同一快照。显式注入候选用于隔离评测时只注册并固定该候选，不改变活动策略。

可信内部 `EvolutionApplication` 以幂等请求指纹创建候选和 `EvolutionExperiment`，并追加不可变 `ExperimentTransition`。阶段推进必须提供 HMAC-SHA256 `GateReceipt`；它把受信任 issuer/key、实验与策略身份、唯一阶段边、门禁决策、产物引用/摘要和有效期绑定到签名，应用拒绝篡改、过期、错绑及硬门禁未通过的正向推进。阶段状态使用 compare-and-set 更新；`CANARY → PROMOTED` 与候选激活、`PROMOTED → ROLLED_BACK` 与基线恢复分别在同一存储事务完成。

当前只验证 Receipt 声明，没有抓取外部产物并重新计算 `artifact_sha256`，完整 Receipt 也尚未作为独立审计对象持久化；提案诊断引用仍未签名。真实 shadow/canary 路由和授权后的 HTTP 控制面同样未实现。

当前活动策略是部署级全局配置，不属于某个 tenant 或 subject。未来若增加 cohort/tenant 策略分配，必须显式建模分配 Scope、授权、冲突和 Trace 归因，不能把数据 Scope 偷渡进全局策略表。

演化永远不能修改身份、授权、Scope 隔离、删除、保留、抑制或审计规则。

## 5. 当前偏好闭环

### 写入

```text
PreferenceWriteRequest
  ↓ API mapping
RememberPreference
  ↓ transaction
Observation + EvidenceSpan + Candidate
  ↓ accept
MemoryRecord + MemoryRevision #1 + CREATED transition
```

如果相同 `key + context` 已存在：

- 值相同：追加修订并组合置信度与证据。
- 值不同：追加替代修订，重置信念证据计数。

### 修正

```text
explicit user correction
  ↓
USER_FEEDBACK Observation
  ↓
new MemoryRevision #N+1
  ↓
old revision remains in history
```

### 当前列表

列表从同一次服务端时钟读数解析 `valid_at = known_at = now`，只展示此刻有效且此刻已知的 Revision，并按 key、上下文指纹和 ID 稳定排序。因此已记录但未来才生效的修订不会提前出现。列表不生成 Trace，也不学习效用。

### 双时间状态选择

Recall 的两个可选轴含义不同：

- `valid_at` 回答“事实在什么业务时点有效”；
- `known_at` 回答“系统截至什么时点已经知道这些 Revision 与 Outcome”。

application 只读取一次时钟；任一省略轴都使用该同一时点。显式 `known_at` 晚于执行时点会失败，因为系统不能重建尚未发生的知识状态；`valid_at` 可以位于未来，以查询已经记录的计划生效信念。两轴不要求前后顺序。

每条 Record 的选择流程为：

```text
record.created_at <= known_at
  ↓
revision.recorded_at <= known_at
  AND revision.valid_from <= valid_at
  ↓
latest (recorded_at, sequence, id)
```

按系统记录时间而不是只按 `valid_from` 选择，能保证一条迟到记录、但追溯到更早业务时间的修正，在系统知道它之后仍可替代旧信念。若没有合格 Revision，该 Record 在该历史状态中不可见。

### 召回

```text
Scope filter + bitemporal Revision selection
  ↓
Outcome.recorded_at <= known_at utility aggregation
  ↓
fixed relevance admission
  ↓
semantic + context + belief + utility + recency
  ↓ weighted score and threshold
sorted RecalledItem list
  ↓
append RecallTrace
```

固定 relevance admission 先于加权评分：候选必须有词法命中，或者保存与请求两侧都提供了显式上下文且上下文为正向匹配。信念、效用和时效分量本身不能把无关候选变成相关结果。这条安全底线不属于 `StrategySnapshot` 的可演化权重，策略调优不能降低或绕过它。

默认权重：

| 分量 | 权重 |
| --- | ---: |
| semantic | 0.35 |
| context | 0.25 |
| belief | 0.20 |
| utility | 0.15 |
| recency | 0.05 |

默认最低分是 0.20，时效半衰期是 180 天。

Recency 的参考时点是 `valid_at`，而不是请求执行时刻；Utility 只聚合 `recorded_at <= known_at` 且 Trace 上下文匹配的 Outcome。Trace 冻结最终双时间边界和每个 item 的修订时间，因此后续修正不会改写这次召回实际返回的内容。

本次评分使用请求执行时配置的不可变 `StrategySnapshot`，其 `policy_id` / `policy_version` 被写入 Trace。当前没有按 `known_at` 还原旧策略、投影代码、索引版本或运行环境，因此这里实现的是 historical state projection，不是完整历史策略 replay。

### Outcome

```text
trace exists in Scope?
  ↓ yes
revision present in trace?
  ↓ yes
outcome idempotency valid?
  ↓ yes
append OutcomeEvent → update contextual UtilityEstimate
```

## 6. 核心不变量

- 每个有状态键都包含 tenant 和 subject Scope。
- 所有时间都带时区并规范化为 UTC。
- Observation、EvidenceSpan、Revision、Trace 和 Outcome 只追加。
- 一条 MemoryRecord 最多只有一个活动修订。
- 修正保留旧修订并显式记录替代关系。
- 只有同时满足 Scope、`valid_from <= valid_at` 与 `recorded_at <= known_at` 的 Revision 才可进入对应历史状态；每条 Record 最多选择一个版本。
- 历史 Utility 不得使用 `recorded_at > known_at` 的 Outcome，Recency 必须相对 `valid_at` 计算。
- Trace 的 `known_at` 不得晚于 `created_at`，Trace item 的修订时间必须满足本次双时间边界。
- 列表和召回不改变信念或效用。
- Outcome 不能更新未出现在对应 Trace 中的修订。
- 幂等键只在 Scope 内有意义。
- 策略权重有绝对范围、总和约束和单次变化上限。

## 7. Scope 与安全

API 保留 `tenant_id` 和 `subject_id` 作为目标资源选择器。`development` 模式使用显式本地身份；`jwt` 模式先验证 access token，再要求同一条可信 `AccessGrant` 同时覆盖 action、tenant、subject 和 purpose。当前执行链为：

```text
JWT identity adapter
  → ActorContext
  → AuthorizedMemoryApplication（统一 PEP）
  → AuthorizationPort（默认拒绝 PDP）
  → MemoryApplication
  → Domain / MemoryStore
```

权限实现遵循：

1. 在认证适配器中验证调用身份。
2. 在授权层解析允许访问的 tenant 和 subject。
3. 只有授权 grant 覆盖的目标才能形成 application 调用的可信 Scope。
4. 在数据库唯一键、索引和查询条件中重复落实 Scope。
5. 保持跨 Scope 的 NotFound 行为一致，避免资源枚举。
6. action 按 Evidence、Belief、Experience、Projection、Evolution 与外部 Governance 平面拆分。
7. 所有 allow/deny 记录 policy version 和伪名化审计引用。

不能仅依靠前端隐藏字段或请求体约定实现隔离。

当前角色绑定来自 JWT `memory_access` claim，尚未提供角色管理 API、撤销/临时授权、数据库 RLS 或独立审计存储。完整动作、角色与 token 合同见[身份与权限设计](authorization.md)。

## 8. 持久化实现与后续加固

当前 PostgreSQL 适配器把 Observation、Revision、Trace 和 Outcome 作为权威状态。Observation 摄入、Revision 创建/追加和 Outcome 记录会在各自权威事务内写入不包含原始证据正文的 outbox 事件。版本化迁移由 `evolvable-memory-migrate` 执行。

`0003_bitemporal_recall` 为 Trace 增加双时间边界、为 Trace item 增加命中修订时间快照，并为 Outcome 增加系统 `recorded_at`。迁移会从权威 Revision 回填旧 item 时间，并尽量保持旧 Trace 已冻结结果仍满足新边界。旧 Outcome 在原 Schema 中没有真实摄入时间，因此只能以 `min(occurred_at, migration time)` best-effort 回填 `recorded_at`；迁移前 Outcome 的历史 Utility 不能被解释为精确的系统知识审计。

`0004_active_strategy_registry` 增加 append-only `strategy_activations`。当前活动策略按数据库生成的单调 sequence 选择最后一条记录；初始化使用事务级 advisory lock，确保并发启动只产生一个根策略和一次 bootstrap。迁移不会猜测旧快照中哪一个曾是活动策略，升级后的第一次应用启动会创建新的、明确可归因的默认根策略。

`0005_evolution_experiments` 增加实验当前状态和 append-only 转换证据。数据库触发器拒绝非法阶段、删除实验以及修改/删除转换历史；应用适配器进一步验证候选谱系、活动基线、转换请求指纹和激活证据，并把阶段更新、历史追加及策略切换放在同一事务。

Milvus projector 消费 Revision outbox，使用独立 `projection_jobs` receipt 实现租约、重试、死信、幂等 upsert、游标与全量重建。outbox 的全局 `published_at` 仍不表示所有下游已经处理；通用发布确认、授权后的受控重放与删除屏障尚未实现。

Milvus 和未来图存储只能消费投影事件，不能反向写入权威 Revision。Milvus 候选必须由 PostgreSQL 按真实 Scope 和双时间最终复核。当前数据库约束与适配器事务共同保证：

`0002_scope_integrity` 把 Candidate、修订链、Trace item 和策略版本的跨表引用绑定到一致的 record、Scope 或不可变策略身份；`0003_bitemporal_recall` 进一步把 Trace item 冻结的修订时间绑定到同一权威 Revision，并约束 `known_at <= created_at`。

- Scope 内观察幂等键唯一。
- Scope 内 Outcome 幂等键唯一。
- record 内 revision sequence 唯一；追加事务锁定 record 并只接受下一个连续序号。
- 活动 revision 外键必须指向同一 record 和 Scope，因此每条 record 只有一个活动指针。
- Candidate、修订链与 Trace item 的 revision 必须同时匹配 record 和 Scope。
- RecallTrace 的策略 ID 与版本必须引用同一个不可变 StrategySnapshot。
- 活动策略记录必须引用已注册快照；bootstrap 不得伪造前一策略或实验，promotion/rollback 必须携带前一策略和实验 ID。
- RecallTrace 的双时间边界和 item 修订时间通过数据库非空、检查、唯一键与复合外键保持一致。
- Outcome 的 Trace 与 revision 归因完整性。

隐私生命周期的目标状态与删除证明验收标准见[隐私生命周期设计](privacy-lifecycle.md)，生产信任边界与攻击场景见[威胁模型](threat-model.md)。二者都是设计基线，不代表当前已有对应执行能力。

## 9. 有意未实现

- 成员/角色治理控制面、撤销/临时授权与数据库 RLS
- 删除证明、保留与抑制策略
- 通用 outbox 发布、授权后的重放控制、删除屏障与积压 SLO
- 图和摘要检索器，以及生产级 embedding 质量门禁
- 代表性离线回放数据集、影子路由与灰度控制面
- 外部评测产物获取与内容摘要复核、非对称签名/独立 Receipt 审计存储、真实 shadow/canary 路由，以及受授权的策略控制面/API
- 按历史 StrategySnapshot、投影版本与运行环境执行的完整历史策略 replay
- 生产监控、审计存储和 SLO

这些能力是路线图，不应从当前领域类型的存在推断为已经可用。
