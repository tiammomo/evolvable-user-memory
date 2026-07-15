# 记忆评测指南

本项目提供一个确定性、可离线运行的最小评测入口，用来发现检索质量和核心安全不变量的回归。

> **能力边界**
>
> `builtin:smoke-v1` 是项目自有的 synthetic smoke 数据集，只验证当前记忆垂直切片的契约。它不是 LongMemEval、LoCoMo 或其他论文 benchmark，不衡量完整 Agent 的最终答案质量，也不能证明 SOTA、生产质量或跨数据集泛化能力。

## 1. 三个公开命令

先同步依赖：

```bash
uv sync
```

查看可用数据集：

```bash
uv run evolvable-memory-eval list
```

只校验数据集结构和引用，不执行场景：

```bash
uv run evolvable-memory-eval validate --dataset builtin:smoke-v1
```

执行评测并应用内置硬门禁：

```bash
uv run evolvable-memory-eval run --dataset builtin:smoke-v1
```

`run` 只在所有硬门禁通过时返回成功；指标不足、forbidden 命中、隔离违规或场景执行失败都会导致非零退出，因此可以直接用于本地提交门禁和受控自动化。不要通过 shell 的 `|| true`、忽略退出码或修改数据标签来掩盖失败。

`validate` 和 `run` 会标识数据集名称、版本和规范化 SHA-256 snapshot hash。比较两次结果前先确认 hash 相同；数据快照不同的分数不能直接作为策略优劣结论。

## 2. smoke-v1 验证什么

内置数据完全由项目生成，不包含真实用户数据。它按有序场景覆盖：

| 能力 | 验证重点 |
| --- | --- |
| 写入 | 合成记忆能以预期修订序号创建，重复动作遵守幂等合同 |
| 更新 | 明确修正追加新 Revision，旧版本不会作为当前答案返回 |
| 双时间 | 显式 `valid_at` 能从同一修订链选择当时有效的旧 Revision；时间字段必须带 UTC offset 并参与数据集 snapshot hash |
| 召回 | 相关 Revision 能进入 top-k，排序位置可解释 |
| 拒答 | 没有词法或显式正向上下文相关性时返回空结果 |
| forbidden | 已被替代、错误或属于其他 Scope 的 Revision 不得进入召回结果 |
| Scope 隔离 | 其他 tenant/subject 的记忆不得被当前 Scope 召回 |
| 执行完整性 | 写入、修正、引用解析和召回场景不得异常失败 |

这是 smoke 契约集，不替代单元测试、PostgreSQL 集成测试、浏览器 E2E、安全测试或真实业务的授权评测集。`smoke-v1` 只包含一个最小 `valid_at` 历史选择场景；`known_at` 前后、迟到修正、未来生效 Revision、历史 Outcome Utility、迁移近似和数据库约束仍由 application/API/PostgreSQL 测试覆盖，不能从单个 smoke case 外推完整双时间正确性。

## 3. 指标与硬门禁

| 指标 | 含义 | 如何解读 |
| --- | --- | --- |
| `Recall@k` | top-k 中找回的期望 Revision 比例 | 衡量覆盖，不代表最终答案正确 |
| `MRR@k` | 第一个期望 Revision 的平均倒数排名 | 越接近首位越高 |
| 更新准确率 | 带预期的修正是否得到正确修订序号与幂等结果 | 衡量更新合同，不是自然语言事实评分；写入失败仍计入执行失败 |
| 拒答准确率 | 应拒答场景是否确实返回空结果 | 防止高信念、高效用或新鲜度制造无关召回 |
| forbidden hit count | 禁止出现的 Revision 命中数 | 硬安全指标，必须满足数据集门禁 |
| execution failure count | 场景命令、引用或断言失败数 | 任何执行失败都不能被平均质量分掩盖 |
| 写入/修正 case | 执行数量及逐 case 结果 | 异常或断言失败同时计入 execution failure |

平均 Recall 或 MRR 不能抵消 forbidden、Scope 隔离、执行失败或拒答护栏。新增策略即使提高排名，只要破坏任一硬不变量，就必须拒绝。

时间化 case 仍使用评测执行时的不可变 `StrategySnapshot`。`valid_at` / `known_at` 只重建 Revision 与 Outcome 的历史状态；评测没有还原历史策略、投影代码或运行环境，因此属于 historical state projection 测试，不是完整历史策略 replay。

`smoke-v1` 的公开命令默认要求 Recall@5、MRR@5、更新准确率和拒答准确率均为 `1.0`，同时要求 forbidden hit 和 execution failure 都为 `0`。显式阈值参数适合受控实验，但不得用降低阈值的方式把安全回归包装成通过。

## 4. 隔离与数据安全

公开 smoke 运行固定使用全新的进程内评测状态：

- 不启动 API 或前端；
- 不读取工作台中的现有记忆；
- 不连接 PostgreSQL，也不使用生产数据库；
- 不应因为环境中存在 `EMF_DATABASE_URL` 而访问该数据库；
- 每次运行从合成数据重新构造状态，不能依赖上一轮结果。

评测报告和终端摘要不得输出 `evidence_text`、Memory `value` 或其他原始正文。报告只保留判断门禁所需的数据集摘要、合成 case 标识、数量、指标、门禁检查和安全失败类别。使用外部私有数据集时也必须保持相同的默认最小披露原则。

需要机器可读结果时：

```bash
uv run evolvable-memory-eval run \
  --dataset builtin:smoke-v1 \
  --format json \
  --report artifacts/memory-evaluation.json
```

该 JSON 同样是最小化报告，不会把数据集中的 evidence/value 复制到 artifact。

## 5. 新人的推荐工作流

### 第一次确认环境

```bash
uv run evolvable-memory-eval list
uv run evolvable-memory-eval validate --dataset builtin:smoke-v1
uv run evolvable-memory-eval run --dataset builtin:smoke-v1
```

三步均成功，说明内置资源可读取、Schema 合法，且当前实现通过最小检索与不变量门禁。

CLI 退出码便于自动化定位：`0` 表示成功，`1` 表示评测执行完成但硬门禁失败，`2` 表示参数/数据集合同错误，`3` 表示运行或报告写入失败。任何非零状态都应先定位原因，而不是忽略。

### 修改检索、上下文或演化逻辑后

```bash
uv run pytest
uv run evolvable-memory-eval run --dataset builtin:smoke-v1
```

如果评测失败：

1. 先看失败的是质量指标、拒答、forbidden/隔离还是执行错误；
2. 运行对应单元测试定位业务规则；
3. 确认没有通过提高 Belief、Utility 或 Recency 权重绕过固定 relevance admission；
4. 若失败涉及时间，确认 RFC 3339 offset、`valid_at`/`known_at` 边界、Outcome `recorded_at` 与 Recency 参考时点，而不是只比较修订序号；
5. 修复实现或合法的数据引用错误，不要为了让当前输出通过而降低安全门禁；
6. 重新运行 `validate` 和 `run`。

## 6. 如何解释结果

可以据此声明：

- 当前提交通过 `builtin:smoke-v1` 的 retrieval/invariant 契约；
- 在该合成数据快照上，Recall@k、MRR、最小 `valid_at` 历史选择、拒答和硬门禁结果可复现。

不能据此声明：

- 系统通过了 LongMemEval、LoCoMo 或任何未实际运行的数据集；
- 系统达到或超过某个论文、模型或供应商的 SOTA；
- synthetic smoke 代表真实用户分布、长对话能力或端到端答案质量；
- 双时间 smoke 代表完整历史策略 replay，或证明迁移前 Outcome 的 system-time 精确；
- 评测通过意味着认证、隐私生命周期、PostgreSQL 故障恢复或生产 SLO 已完成。

后续接入公开或私有 benchmark 前，必须单独确认数据许可、Schema 映射、Scope/隐私处理、答案评分语义和报告最小化，不能直接把第三方数据复制进内置资源。
