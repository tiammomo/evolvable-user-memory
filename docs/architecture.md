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
  ├── MemoryStore port ──> InMemoryMemoryStore
  ├── Clock port ────────> SystemClock
  └── IdGenerator port ──> Uuid4Generator
```

前端和后端是两个独立开发进程。前端不保存权威记忆状态；后端当前使用线程安全进程内存。

## 3. 代码依赖

```text
api / adapters  →  application  →  domain
```

### Domain

拥有领域值、硬不变量和状态转换。必须保持框架无关。

### Application

拥有用例顺序和事务边界，通过类型化端口依赖基础设施。

### Adapters

实现存储、时钟和 ID 端口。基础设施实体不能泄漏到领域层。

### API

校验传输数据、映射命令和响应、生成 OpenAPI，不承载领域状态转换。

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

`MemoryRevision` 表示不可变版本。修正或新证据会追加修订，并通过 `supersedes_revision_id` 连接旧版本。

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
- 返回修订和排名
- 综合得分与评分分量
- 创建时间

`OutcomeEvent` 必须引用同 Scope 的 Trace，并引用该 Trace 中实际出现的 revision。

`UtilityEstimate` 以 `(revision_id, context_fingerprint)` 为键，使用带先验的成功/失败权重更新均值。

规则：读取、列表和召回都不会更新 Belief 或 Utility。

### Projection：投影平面

关键词、embedding、图和摘要索引都是可丢弃投影：

- 可从权威 Observation 和 Revision 重建。
- 不能静默创造新的 Revision。
- 必须有源修订号或游标，才能衡量投影延迟。

当前版本没有独立投影存储，而是扫描 Scope 内活动修订并计算词法分数。这是可替换实现，不是目标架构的权威数据模型。

### Evolution：演化平面

检索权重、阈值和时效参数保存在不可变 `StrategySnapshot` 中。

`PolicyEvolution` 根据失败诊断提出有界子快照，每次只允许很小的权重变化。提案不是自动晋升；离线回放、影子、灰度和回滚由未来适配器编排。

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

列表只读取每条 MemoryRecord 的活动修订，按 key、上下文指纹和 ID 稳定排序。它不生成 Trace，也不学习效用。

### 召回

```text
Scope filter + active revision filter
  ↓
semantic + context + belief + utility + recency
  ↓ weighted score and threshold
sorted RecalledItem list
  ↓
append RecallTrace
```

默认权重：

| 分量 | 权重 |
| --- | ---: |
| semantic | 0.35 |
| context | 0.25 |
| belief | 0.20 |
| utility | 0.15 |
| recency | 0.05 |

默认最低分是 0.20，时效半衰期是 180 天。

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
- 只有活动修订可被召回。
- 列表和召回不改变信念或效用。
- Outcome 不能更新未出现在对应 Trace 中的修订。
- 幂等键只在 Scope 内有意义。
- 策略权重有绝对范围、总和约束和单次变化上限。

## 7. Scope 与安全

当前 API 为开发体验把 `tenant_id` 和 `subject_id` 放在请求中。生产架构必须：

1. 在认证适配器中验证调用身份。
2. 在授权层解析允许访问的 tenant 和 subject。
3. 由服务端把可信 Scope 注入 application command。
4. 在数据库唯一键、索引和查询条件中重复落实 Scope。
5. 保持跨 Scope 的 NotFound 行为一致，避免资源枚举。

不能仅依靠前端隐藏字段或请求体约定实现隔离。

## 8. 持久化路线图

生产适配器建议使用 PostgreSQL 作为 Observation、Revision、Trace 和 Outcome 的权威存储，并在同一事务写 outbox。

向量和图存储只消费投影事件。数据库约束至少需要保证：

- Scope 内观察幂等键唯一。
- Scope 内 Outcome 幂等键唯一。
- record 内 revision sequence 唯一且连续。
- 单活动修订语义。
- Outcome 的 Trace 与 revision 归因完整性。

## 9. 有意未实现

- 认证、授权与生产身份注入
- PostgreSQL 或其他持久化适配器
- 删除证明、保留与抑制策略
- 异步 outbox 与投影消费者
- embedding、图和摘要检索器
- 离线回放数据集、影子路由与灰度控制面
- 生产监控、审计存储和 SLO

这些能力是路线图，不应从当前领域类型的存在推断为已经可用。
