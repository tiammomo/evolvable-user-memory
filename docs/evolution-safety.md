# 演化安全模型

本项目中的“自演化”是预定义动作空间内、由证据驱动的策略优化，不是允许服务任意改写代码或治理规则。

## 可写动作空间

初始引擎可以提出以下参数的变化：

- 检索分量权重
- 最低召回分数
- 候选数量限制
- 上下文失配惩罚
- 时效半衰期

当前代码实现了 `RetrievalWeights`、`StrategySnapshot`、`StrategyActivation`、`FailureDiagnosis` 和 `PolicyEvolution`。每个权重必须位于 `[0.05, 0.60]`，所有权重之和必须为 1，单次提案最大变化量是 0.03。

引擎只创建不可变子快照，不直接修改活动策略。

## 当前活动策略注册表

进程未显式固定候选策略时，会从权威存储读取当前活动策略。首次启动在事务和 PostgreSQL advisory lock 内创建一个根 `StrategySnapshot`，并追加唯一的 `bootstrap` 激活记录；后续实例和重启复用同一策略，而不是重新生成无法解释的新基线。

`strategy_activations` 是 append-only 历史，当前活动策略由最后一条激活记录确定。每次 Recall 在开始评分时读取并冻结实际活动快照，Trace 继续保存精确 `policy_id` / `policy_version`。显式注册或用候选构造隔离应用只会保存不可变候选，不会改变活动策略。

可信内部 `EvolutionApplication` 已实现幂等提案和阶段推进。每次写入携带显式幂等键与规范化请求指纹；同键同内容返回原结果，同键不同诊断、目标阶段或 Gate Receipt 会失败关闭。

实验当前状态保存在 `evolution_experiments`，创建和每次转换同时追加 `evolution_experiment_transitions`。只有 `CANARY → PROMOTED` 会在同一事务追加 promotion 激活并切换候选；只有 `PROMOTED → ROLLED_BACK` 会原子切回基线。活动策略与预期基线不一致时，实验状态、转换历史和激活记录全部不提交。

阶段推进不再接受调用者提供的裸 `reason` / `evidence_ref`，而必须携带短期有效的 `GateReceipt`。Receipt 用版本化规范 JSON 和 HMAC-SHA256 签名，绑定 receipt/experiment/baseline/candidate ID、唯一的起止阶段边、pass/reject/rollback 决策、硬门禁断言、产物引用及 SHA-256、签发方、key ID、原因和有效期。验证器按 `(issuer, key_id)` 选择受信任密钥，拒绝未知密钥、篡改、未来签发和到期凭证；应用还会把凭证与实验当前状态和目标阶段逐字段匹配。只有 PASS 决策且明确声明硬门禁通过，才能进入正向阶段。

精确相同且已经成功提交的请求可以在 Receipt 到期后幂等重放，因为它只返回已持久化结果，不产生新的阶段变化；换 key、换 Receipt 或换目标都不能借此重放。HMAC 密钥至少 32 bytes，必须来自 secret manager，支持通过新增/移除 `(issuer, key_id)` 做轮换，不得写入数据库、日志或代码仓库。

这些仍是内部编排原语，不是公开控制面：当前没有授权后的 application wrapper 或 HTTP API。系统验证的是 Gate Receipt 的真实性与声明绑定，并未拉取 `artifact_ref` 或重新计算外部产物内容 hash，也没有将完整 Receipt 持久化为独立审计对象。提案创建时的诊断 `evidence_ref` 仍是未签名外部引用；SHADOW/CANARY 阶段也尚未自动路由真实流量。因此 Receipt 证明“受信任签发方作出了这项声明”，不等同于系统已经独立执行评测，阶段名称本身也不能证明真实流量门禁完成。

Receipt 的 `artifact_ref`（提案中的 `evidence_ref`）必须是可审计但不含秘密和用户正文的稳定引用；`artifact_sha256` 保存内容摘要。不得把 HMAC key、access token、Receipt signature、原始 Evidence、评测样本正文或临时签名 URL 写入转换历史。

当前活动策略是部署级全局指针，不带 tenant/subject Scope；所有 Scope 仍各自隔离数据，但使用同一检索策略。按 tenant、cohort 或实验分流属于后续控制面能力，在分配合同和隔离测试完成前不能通过复制活动记录自行模拟。

## 永不可写的约束

演化引擎不能修改：

- 身份认证和授权
- tenant / subject 隔离
- 同意、保留、抑制和删除规则
- 审计完整性
- 证据归因要求
- Outcome 幂等规则
- 固定 relevance admission 与评测硬门禁
- 晋升与回滚门禁

这些约束位于演化动作空间之外。平均召回质量提升不能抵消任何隐私、授权或隔离回归。

## 晋升流程

```text
诊断失败样本
    ↓
生成有界候选快照
    ↓
离线回放 ──失败──> 拒绝
    ↓ 通过
影子评估 ──失败──> 回滚
    ↓ 通过
小流量灰度 ─失败─> 回滚
    ↓ 通过
晋升不可变策略快照
```

当前版本已经提供 `builtin:smoke-v1` retrieval/invariant 与 `builtin:temporal-v1` 双时间/Outcome synthetic 评测，但它们只是一组提交前契约门禁。候选/基线对比、获授权的代表性离线数据集、影子路由、灰度编排和控制面仍未实现。

在讨论候选策略前至少运行：

```bash
uv run evolvable-memory-eval validate --dataset builtin:smoke-v1
uv run evolvable-memory-eval run --dataset builtin:smoke-v1
uv run evolvable-memory-eval validate --dataset builtin:temporal-v1
uv run evolvable-memory-eval run --dataset builtin:temporal-v1
```

任何 forbidden/Scope 隔离命中、错误拒答或执行失败都必须阻止后续阶段；平均 Recall@k 或 MRR 提升不能抵消硬门禁失败。smoke 通过也不能跳过后续代表性离线回放、影子、灰度和回滚验证。详细指标与声明边界见[记忆评测指南](evaluation.md)。

## 必须同时观察的指标

质量指标：

- 可归因成功率
- 修正率
- harmful / rejected Outcome 比率
- 上下文匹配质量
- 无结果率

硬护栏：

- Scope 隔离违规数，目标必须为零
- 未归因 Outcome 接受数，目标必须为零
- 删除或抑制规则违规数，目标必须为零
- 审计缺口数，目标必须为零

系统指标：

- 召回延迟
- 投影延迟
- 每次召回成本
- 策略回滚时间

## 失败诊断不是授权

`FailureDiagnosis` 只选择需要轻微增加权重的检索分量。例如上下文失配最多时，可以提高 context 权重并从当前最大 donor 权重中等量扣减。

诊断不能：

- 改变 Scope 过滤条件。
- 绕过固定 relevance admission；候选必须有词法命中，或两侧都有显式且正向匹配的上下文。
- 跳过最低分阈值的绝对边界。
- 放宽 Outcome Trace 归因。
- 改写历史或证据。

## 生产实现检查清单

- 候选和基线都使用不可变 ID 与版本。
- 并发启动只能产生一个 bootstrap，重启必须复用权威活动策略；注册候选不得改变活动指针。
- 提案和阶段推进必须安全幂等；竞争晋升失败不能写入阶段、历史或激活的任何子集。
- 每次新阶段推进必须验证未过期的 Gate Receipt，并逐字段绑定实验身份、策略身份、阶段边、决策与硬门禁结果；不得恢复裸证据引用旁路。
- promotion/rollback 必须与活动策略切换同事务提交，并能从持久化实验和转换历史重建原因及外部证据引用。
- `builtin:smoke-v1` 与 `builtin:temporal-v1` 的 Recall@k、MRR、更新、双时间/Utility、拒答、forbidden/隔离和执行硬门禁全部通过。
- smoke 结果只标记为 synthetic retrieval/invariant，不宣传成 LongMemEval、LoCoMo 或 SOTA 结果。
- 每个实验阶段转换都持久化并可审计。
- 离线数据集包含 tenant 隔离和危险召回反例。
- 影子结果不会影响真实用户或 Utility。
- 灰度有明确流量上限、停止条件和自动回滚。
- 晋升是外部受控决策，不由提案函数自动完成。
- 回滚恢复完整策略快照，而不是局部手工改值。
