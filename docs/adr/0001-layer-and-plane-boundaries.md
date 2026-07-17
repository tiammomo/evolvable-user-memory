# ADR-0001：按依赖层与记忆平面渐进整理代码

- 状态：已接受
- 日期：2026-07-16

## 背景

项目已经形成完整的偏好写入、修订、双时间召回、Outcome 归因和有界策略演化闭环，但 API、应用服务、存储端口和静态前端文件正在变大。直接按文件大小拆分容易把技术分层与业务平面混在一起，也可能在移动代码时破坏 Scope、幂等、不可变修订或审计边界。

本决策回答两个问题：哪些依赖永远不能反向，以及后续应沿什么接缝逐步拆分。

## 决策

### 1. 依赖方向是硬边界

允许的项目内依赖如下：

| 层 | 可以依赖 |
| --- | --- |
| `domain` | `domain` 与 Python 标准库 |
| `application` | `application`、`domain` 与 Python 标准库 |
| `adapters` | `adapters`、`application`、`domain` |
| `api` | 传输模型、应用用例；只在组合根接触具体适配器 |
| `bootstrap` | 配置、API 和所有具体适配器 |

`domain` 和 `application` 不导入 FastAPI、Pydantic、数据库驱动、HTTP 客户端或供应商 SDK。自动化架构测试扫描这两个内层的 Python import，发现反向依赖或第三方依赖就失败。

### 2. 五个平面是业务边界

技术层内继续明确区分五种状态：

| 平面 | 权威状态 | 允许的变化 |
| --- | --- | --- |
| Evidence | Observation、EvidenceSpan、Candidate | 原始事实只追加 |
| Belief | MemoryRecord、不可变 MemoryRevision | 通过证据追加修订 |
| Experience | RecallTrace、OutcomeEvent、UtilityEstimate | 只由可归因结果学习效用 |
| Projection | 召回候选、索引、读模型 | 可丢弃并从权威状态重建 |
| Evolution | StrategySnapshot、Experiment、Gate Receipt | 在固定安全门内提案、验证、晋升或回滚 |

读取本身不能改变 Belief 或 Utility；Evolution 不能修改授权、租户隔离、删除、保留或审计规则。这些规则优先于目录便利性。

### 3. 使用兼容外观渐进迁移

不做一次性目录重写。`bootstrap` 组合根已经建立，其余目标结构用于指导新代码和后续小步迁移：

```text
src/evolvable_memory/
├── bootstrap/                 # 组合根、资源生命周期
├── api/
│   ├── routers/               # 按用例拆分路由
│   ├── schemas/               # 按合同拆分传输模型
│   └── errors.py
├── application/
│   ├── evidence/
│   ├── belief/
│   ├── experience/
│   ├── projection/
│   └── evolution/
├── domain/                    # 同一五平面的纯领域模型
├── adapters/
│   ├── persistence/
│   ├── authorization/
│   └── observability/
└── frontend/                  # 独立服务与模块化静态资源
```

迁移时保留旧 import 和启动入口的薄兼容层；每次只移动一个接缝，并在同一变更中验证行为、Scope 隔离、幂等、错误路径与只读召回不变量。数据库迁移历史不因 Python 目录整理而重写。

建议顺序：

1. 固化内层 import 护栏和 API 合同。
2. 把资源创建与生命周期移到 `bootstrap` 组合根。
3. 按业务用例拆分 API router/schema，但保持 URL 与 OpenAPI 兼容。
4. 从 `MemoryStore` 提取窄端口，同时由现有存储实现兼容外观。
5. 独立迁移前端静态资源，再按状态、API、导航、引导和视图拆成原生模块。

## 后果

- 优点：结构整理有明确方向，内层保持可测试、无框架且不会被基础设施反向污染。
- 优点：五平面语义不会因技术拆包而被合并，安全与演化边界可持续审查。
- 代价：迁移期会短暂存在兼容外观和更多小文件。
- 代价：拆分必须分批完成，不能仅以“目录更整齐”为完成标准。

## 验证

- `tests/test_architecture.py` 自动执行内层依赖检查。
- 常规测试继续验证业务不变量、Scope 隔离、幂等与错误合同。
- 每个结构迁移批次必须通过 lint、format、strict mypy、全量 pytest 与内置评测门禁。
