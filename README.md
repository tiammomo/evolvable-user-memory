# Evolvable User Memory

一套面向 AI 应用的、证据驱动且结果感知的用户记忆服务。

它不把“记忆”简化成向量库中的文本片段，而是分别管理输入证据、当前信念、召回轨迹、真实结果和受控策略演化，从而避免“因为读得多，所以越来越相信”的自我强化循环。

> **项目状态：开发预览版 `0.1.0`**
>
> 已提供完整可运行的偏好记忆闭环、Web 工作台、OpenAPI、进程内存与 PostgreSQL 适配器。原生快速开始默认使用内存，后端重启后数据会清空；默认 Compose 使用 PostgreSQL。API 已具备本地开发身份与 JWT 授权基线，但完整权限治理、删除证明和生产运维仍未完成，请勿直接暴露到公网或写入真实敏感数据。

[快速开始](#快速开始) · [容器运行](docs/deployment.md) · [首次体验](#完成第一条记忆闭环) · [评测门禁](#运行内置评测门禁) · [API](#api-一览) · [架构](docs/architecture.md) · [路线图](ROADMAP.md) · [完整文档](docs/index.md)

## 为什么需要这样的记忆系统

一个可信的 AI 记忆系统需要分别回答：

1. **系统实际观察到了什么？**
2. **系统当前相信什么，依据和置信度是什么？**
3. **在特定上下文中使用这条信念，结果是否真的有帮助？**

本项目通过五个概念平面保持这些问题的边界：

| 平面 | 回答的问题 | 当前核心对象 |
| --- | --- | --- |
| Evidence | 实际观察到了什么？ | `Observation`、`EvidenceSpan`、`Candidate` |
| Belief | 当前相信什么？ | `MemoryRecord`、`MemoryRevision`、`BeliefState` |
| Experience | 使用后结果如何？ | `RecallTrace`、`OutcomeEvent`、`UtilityEstimate` |
| Projection | 如何高效检索？ | 词法召回 + 可选 Milvus 向量投影；PostgreSQL 最终复核 |
| Evolution | 如何安全优化策略？ | `StrategySnapshot`、有界提案、实验状态机 |

最重要的不变量是：**列表和召回都是只读操作，不会强化信念或效用。只有引用某次 `RecallTrace` 的真实 Outcome 才能学习上下文效用。**

## 当前能力

- 写入带原始证据、置信度和上下文的用户偏好。
- 使用 Scope 内幂等键安全重试写入、修正和结果反馈。
- 在应用与存储边界重复检查 tenant/subject Scope，并拒绝同键不同内容的幂等冲突。
- 在 application 边界按 action、tenant、subject 与 purpose 默认拒绝授权，并为允许/拒绝生成不含正文的伪名化审计记录。
- 支持本地开发身份与 RFC 9068 风格 JWT access token；生产环境禁止使用开发身份启动。
- 为相同记忆追加不可变修订，不原地覆盖历史。
- 列出指定租户与用户当前有效的偏好记忆。
- 通过可选 `valid_at` / `known_at` 在业务有效时间与系统知识时间上重建 Scope 内历史状态；省略时两轴使用同一次服务端时钟读数。
- 按词法/向量相关性、上下文、信念、截至 `known_at` 已记录的 Outcome 效用，以及相对 `valid_at` 的时效进行可解释召回。
- 为每次召回冻结双时间边界、策略版本、命中修订的有效/记录时间、评分拆解和 `trace_id`。
- 只接受能归因到对应 Trace 的 Outcome，并更新上下文效用。
- 生成有界策略提案，持久化带幂等键、证据引用和 append-only 转换历史的演化实验；只有完整经过阶段顺序的内部编排才能原子晋升候选或回滚基线，普通候选注册不会激活。
- 提供隔离运行的 `smoke-v1` 与显式时间线 `temporal-v1` synthetic 评测，对 Recall@k、MRR、更新、迟到/未来修订、历史 Outcome Utility、预期领域拒绝、forbidden/Scope 隔离和执行失败应用硬门禁。
- 提供 PostgreSQL 权威存储、版本化迁移、数据库约束和显式内存模式。
- PostgreSQL 会为 Observation 摄入、Revision 变更和 Outcome 记录在同一事务追加不含原始证据正文的 outbox 事件。
- 默认 Compose 运行 Milvus 2.6、独立投影 worker 和确定性离线 embedding；任务支持租约抢占、指数退避、死信、游标和全量重建。
- Milvus 只保存哈希化 Scope、修订标识、时间元数据和向量，不保存原始 Evidence、key 或 value；召回候选必须再次通过 PostgreSQL Scope 与双时间可见性校验。
- 前端根据后端状态动态展示当前存储，并在 Scope 切换时取消旧请求、隔离旧响应。
- 前端与后端共同读取 `EMF_PUBLIC_API_URL`；前端服务在运行时下发 API 地址和对应 CSP，部署时无需修改静态 HTML。
- 浏览器按 Scope 保存五步新手闭环进度；进度不包含证据、记忆正文、Trace 或 Outcome 数据。
- CI 在内存与 PostgreSQL 两种权威存储下运行同一套 Chromium E2E，并验证 PostgreSQL 池连接被终止后 `/readyz` 与权威数据可恢复。
- 浏览器门禁使用锁定版本的 axe-core 审计首页、引导、写入、列表、召回与修正弹窗的可见状态。
- 提供独立前端工作台、Swagger UI 和可执行客户端示例。

## 记忆闭环

```text
原始输入
  │
  ▼
Observation + EvidenceSpan       事实：系统实际看到了什么
  │
  ▼
Candidate → MemoryRevision       信念：系统当前相信什么
  │
  ▼
RecallResult + RecallTrace       使用：这次召回了什么、为什么
  │
  ▼
OutcomeEvent → UtilityEstimate   经验：真实结果是否有帮助
```

修正不会覆盖旧版本，而是追加新的 `MemoryRevision` 并保留完整版本链。

双时间召回回答的是“在业务时点 `valid_at`，并且只使用系统截至 `known_at` 已经记录的信息，哪些记忆可见”。它使用请求执行时的不可变 `StrategySnapshot` 评分，因此属于 **historical state projection（历史状态投影）**，不是对过去策略、投影实现和运行环境的完整历史重放。

## 快速开始

### 1. 环境要求

- Python 3.12 或更高版本
- [uv](https://docs.astral.sh/uv/) 包与环境管理器
- 本地空闲端口：前端 `33009`、后端 `38089`；默认 Compose 另使用 Milvus `19530` 和健康端口 `19091`

### 2. 获取项目并安装依赖

```bash
git clone https://github.com/tiammomo/evolvable-user-memory.git
cd evolvable-user-memory
uv sync
```

### 3. 启动后端

在终端 A 运行：

```bash
uv run evolvable-memory
```

### 4. 启动前端

在终端 B 运行：

```bash
uv run evolvable-memory-frontend
```

### 5. 打开入口

| 入口 | 地址 | 用途 |
| --- | --- | --- |
| 记忆工作台 | <http://127.0.0.1:33009> | 推荐的首次体验入口 |
| OpenAPI 文档 | <http://127.0.0.1:38089/docs> | 查看和调试全部 API |
| 服务发现 | <http://127.0.0.1:38089/> | 查看版本、存储类型和运行边界 |
| 服务状态 | <http://127.0.0.1:38089/health> | 查看版本和当前存储方式 |
| 依赖就绪 | <http://127.0.0.1:38089/readyz> | 检查当前存储是否可用 |

快速验证后端：

```bash
curl http://127.0.0.1:38089/health
```

预期响应：

```json
{"status":"ok","version":"0.1.0","storage":"memory","auth_mode":"development","scope_source":"request","projection":"disabled"}
```

### 使用 Docker Compose（可选）

如果希望隔离本机 Python 环境，可以使用同一镜像启动前后端：

```bash
docker compose up --build -d
```

随后仍访问上表中的默认地址。默认 Compose 会启动 PostgreSQL、执行 Schema 迁移，再启动后端与前端；数据库保存在命名 volume 中。它明确使用本地开发身份，只用于本机评估，不具备完整生产权限与隐私治理。停止服务但保留数据：

```bash
docker compose down
```

需要临时内存模式时使用 `docker compose -f compose.memory.yaml up --build -d`。镜像安全设置、数据清理、日志、健康检查和生产缺口见 [部署与运行指南](docs/deployment.md)。

## 完成第一条记忆闭环

首次打开工作台会显示五屏概念引导；关闭后可通过顶部“新手引导”随时重播。随后按首页可点击的五步清单完成实际操作：

1. 保持默认开发 Scope `demo / alice`。
2. 进入“写入记忆”，点击“载入示例”并保存晚间饮品偏好。
3. 进入“当前记忆”，查看当前修订、置信度和证据数量。
4. 进入“记忆召回”，查询“晚上应该准备什么饮料？”。
5. 对召回结果提交“有帮助”或“无帮助”，观察上下文效用变化。

完成主闭环后，可以点击“修正记忆”追加新版本，再通过“修订历史”确认旧版本仍然存在。

也可以只启动后端，然后运行无需 `jq` 或额外 SDK 的完整 API 示例：

```bash
uv run python examples/first_memory.py
```

更详细的逐步说明见 [快速开始指南](docs/getting-started.md)。

## API 一览

后端默认地址：`http://127.0.0.1:38089`

| 方法 | 路径 | 说明 | 是否写入状态 |
| --- | --- | --- | --- |
| `GET` | `/` | 服务发现与开发边界 | 否 |
| `GET` | `/health` | 服务状态、版本与存储方式 | 否 |
| `GET` | `/livez` | 进程存活检查 | 否 |
| `GET` | `/readyz` | 当前存储依赖就绪检查 | 否 |
| `POST` | `/v1/preferences` | 写入上下文偏好 | 是 |
| `GET` | `/v1/preferences` | 列出当前有效偏好 | 否 |
| `POST` | `/v1/preferences/{record_id}/corrections` | 追加偏好修订 | 是 |
| `GET` | `/v1/preferences/{record_id}/revisions` | 读取不可变历史 | 否 |
| `POST` | `/v1/recall` | 执行当前或双时间历史状态召回并产生 Trace | 仅追加 Trace |
| `POST` | `/v1/outcomes` | 提交可归因结果并更新效用 | 是 |

完整请求、响应、幂等规则、Outcome 类型和错误码见 [API 使用指南](docs/api-guide.md)。

## 架构与代码结构

依赖方向必须保持为：

```text
bootstrap  →  api / adapters  →  application  →  domain
```

```text
src/evolvable_memory/
├── bootstrap/       # 显式 API 进程组合根；导入 factory 不创建外部资源
├── domain/          # 纯 Python 领域值、状态转换和硬约束
├── application/     # 用例编排、权限/评测合同，以及存储/身份/授权端口
├── adapters/        # 存储、角色策略与伪名化授权审计适配器
├── evaluation/      # 评测 CLI、严格数据加载器和内置 synthetic 数据集
├── api/             # FastAPI、JWT 身份边界、Pydantic Schema 和 CORS
│   └── static/      # 原生 HTML/CSS/JavaScript 前端
├── config.py        # 后端与前端环境配置
├── frontend.py      # 独立静态前端进程
├── migrate.py       # PostgreSQL 版本化迁移入口
├── migrations/      # Alembic Schema 修订
└── main.py          # Uvicorn 后端入口

tests/               # 领域、应用、HTTP、隔离和前端测试
docs/                # 使用、架构、开发、安全和排错文档
examples/            # 可直接执行的 API 客户端示例
```

`domain` 保持框架无关，不导入 FastAPI、Pydantic、数据库驱动、HTTP 客户端或供应商 SDK。详细设计、不变量和持久化路线见 [架构说明](docs/architecture.md)。

## 开发与质量检查

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv build
```

项目启用严格类型检查和 85% 测试覆盖率门禁。每个行为变化都应覆盖业务规则、Scope 隔离、幂等行为和错误路径；召回相关测试还必须证明读取不会修改信念或效用。

GitHub Actions 会在 Python 3.12–3.14 上执行测试，在 3.12 上额外运行 PostgreSQL 适配器分支覆盖、内存/PostgreSQL 双存储 Chromium E2E、axe-core 自动化无障碍审计和内置评测门禁，并验证发行包、wheel 内评测资源、Compose 配置和容器构建。

## 运行内置评测门禁

无需启动前后端或数据库：

```bash
uv run evolvable-memory-eval list
uv run evolvable-memory-eval validate --dataset builtin:smoke-v1
uv run evolvable-memory-eval run --dataset builtin:smoke-v1
uv run evolvable-memory-eval validate --dataset builtin:temporal-v1
uv run evolvable-memory-eval run --dataset builtin:temporal-v1
```

`run` 使用全新进程内状态执行项目自有的 synthetic 契约集，并在 Recall@k/MRR、更新、双时间状态/Utility、拒答、forbidden/Scope 隔离或执行完整性不满足硬门禁时返回非零。`temporal-v1` 的 `run_at` 只推进隔离评测时钟，不是线上 API 字段。评测不会连接 PostgreSQL，报告也不输出 evidence 或 Memory value。

这是一项 retrieval/invariant 回归门禁，不是 LongMemEval、LoCoMo 或 SOTA 证明，也不代表端到端答案质量或生产就绪。指标解释、失败排查和安全边界见[记忆评测指南](docs/evaluation.md)。

## 配置

应用直接读取环境变量，不会自动加载 `.env` 文件。可参考 [.env.example](.env.example)：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `EMF_ENVIRONMENT` | `development` | `development`、`test`、`staging` 或 `production` |
| `EMF_HOST` | `127.0.0.1` | 后端监听地址 |
| `EMF_PORT` | `38089` | 后端端口 |
| `EMF_LOG_LEVEL` | `INFO` | Uvicorn 日志级别 |
| `EMF_STORE` | `memory` | 存储实现：`memory` 或 `postgres` |
| `EMF_DATABASE_URL` | 空 | PostgreSQL DSN；`EMF_STORE=postgres` 时必填 |
| `EMF_DATABASE_POOL_MIN_SIZE` | `1` | PostgreSQL 最小连接池大小 |
| `EMF_DATABASE_POOL_MAX_SIZE` | `10` | PostgreSQL 最大连接池大小 |
| `EMF_DATABASE_READINESS_TIMEOUT_SECONDS` | `1.0` | `/readyz` 专用连接池等待上限，范围 0.05–30 秒 |
| `EMF_PROJECTION_MODE` | `disabled` | `disabled` 或 `milvus`；默认 Compose 设为 `milvus` |
| `EMF_PROJECTION_REQUIRED` | `false` | 为 `true` 时 Milvus 故障会使 `/readyz` 失败；默认允许词法降级 |
| `EMF_MILVUS_URI` | `http://127.0.0.1:19530` | Milvus SDK 地址 |
| `EMF_MILVUS_COLLECTION` | `evolvable_memory_v1` | 可重建的共享投影 collection |
| `EMF_EMBEDDING_PROVIDER` | `hash` | 离线 `hash` 或 `openai_compatible` |
| `EMF_EMBEDDING_DIMENSIONS` | `384` | collection 向量维度；变更后应使用新 collection 或重建 |
| `EMF_MAX_REQUEST_BODY_BYTES` | `1048576` | API 请求体最大字节数 |
| `EMF_AUTH_MODE` | `development` | `development` 本机身份或 `jwt` 可信 access token |
| `EMF_AUTH_JWT_ISSUER` | 空 | JWT issuer；`jwt` 模式必填 |
| `EMF_AUTH_JWT_AUDIENCE` | 空 | 当前 API 的 JWT audience；`jwt` 模式必填 |
| `EMF_AUTH_JWT_JWKS_URL` | 空 | IdP 公钥集合 URL；`jwt` 模式必填 |
| `EMF_AUTH_JWT_ALGORITHMS` | `RS256` | 允许的非对称签名算法，逗号分隔 |
| `EMF_AUTH_REQUIRED_SCOPE` | `memory` | token 必须包含的粗粒度 OAuth scope |
| `EMF_AUTH_AUDIT_HMAC_KEY` | 空 | 授权审计引用 HMAC 密钥；`jwt` 模式至少 32 字符 |
| `EMF_FRONTEND_URL` | `http://127.0.0.1:33009` | 服务发现返回的前端地址 |
| `EMF_PUBLIC_API_URL` | `http://127.0.0.1:38089` | 后端服务发现、前端请求和前端 CSP 共用的对外 API 基础地址 |
| `EMF_CORS_ORIGINS` | 两个本机前端 Origin | 逗号分隔的允许来源 |
| `EMF_FRONTEND_HOST` | `127.0.0.1` | 前端监听地址 |
| `EMF_FRONTEND_PORT` | `33009` | 前端端口 |

## 当前边界

当前版本有意只实现一个语义完整的垂直切片，并不伪装成生产完成态：

- 原生快速开始的内存模式不持久化，后端重启即清空；PostgreSQL 是默认 Compose 的权威存储，但其故障恢复和生产运维仍处于开发预览。
- 默认 Compose 已实现词法 + Milvus 向量的混合召回；原生内存模式仍只使用词法召回。默认离线 hash embedding 是确定性基线，不等于生产语义模型质量。
- 双时间召回会按 `valid_at` / `known_at` 重建 Revision 和 Outcome 可见性，但仍使用执行时的 `StrategySnapshot`；它不是完整历史策略 replay。
- `0003_bitemporal_recall` 会为旧 Outcome 近似回填系统记录时间。旧 Schema 没有保存真实摄入时点，因此迁移结果只能用于 best-effort 历史边界，不能当作精确历史审计事实。
- 内置 `smoke-v1` / `temporal-v1` 只证明当前提交通过对应 synthetic 契约，不能外推为 LongMemEval、LoCoMo、SOTA 或真实业务质量；时间线回放也不是完整历史策略 replay。
- 开发模式仍把 `tenant_id` 和 `subject_id` 作为本地目标 Scope；JWT 模式只接受 token 中同一条 `memory_access` grant 明确覆盖的目标。
- 已实现类型化授权、内置角色、purpose 限制和 JWT 校验基线；尚未实现成员/角色管理 API、撤销控制、临时授权、RLS、同意/保留/抑制/删除执行与证明，以及独立生产审计存储。
- Milvus 专用 outbox 消费、租约/重试/死信、投影游标和全量重建已经实现；通用事件发布、授权后的运维重放 API、删除屏障与积压告警仍未完成。
- 尚未定义并验证生产 SLO、告警、备份恢复和灾难恢复目标。
- 演化模块只能调整有界策略参数，不能修改授权、隔离、删除或审计规则。
- 当前具备可信内部 `EvolutionApplication` 的阶段持久化、HMAC-SHA256 Gate Receipt 验证与原子 promotion/rollback，但没有授权后的 HTTP/API 控制面；系统只验证签发方声明，尚未抓取外部评测产物复核内容摘要、独立持久化完整 Receipt 或自动执行真实 shadow/canary 流量，不能把阶段标签当作质量证明。

计划中的生产方向包括权限治理控制面、数据库 RLS、隐私生命周期、PostgreSQL 故障恢复与迁移加固、通用事件发布与受控重放、图检索、生产 SLO 和代表性离线回放评估，详见 [路线图](ROADMAP.md)。Milvus 的运行与恢复见 [Milvus 投影指南](docs/milvus-projection.md)。

## 文档导航

| 文档 | 适合谁 | 内容 |
| --- | --- | --- |
| [文档首页](docs/index.md) | 所有人 | 按角色选择阅读路径 |
| [快速开始](docs/getting-started.md) | 第一次使用者 | 从零启动并完成首条闭环 |
| [前端工作台指南](docs/frontend-guide.md) | 产品与前端使用者 | 页面、状态和 API 映射 |
| [API 使用指南](docs/api-guide.md) | API 集成者 | 契约、双时间召回、幂等、归因和错误码 |
| [身份与权限设计](docs/authorization.md) | 平台、安全与集成者 | JWT、角色、动作、purpose、审计和生产门禁 |
| [架构说明](docs/architecture.md) | 开发者与架构师 | 分层、五平面、状态和不变量 |
| [Milvus 投影指南](docs/milvus-projection.md) | 开发者与运维 | 向量投影、混合召回、worker、重建与故障降级 |
| [开发指南](docs/development.md) | 贡献者 | 添加能力、测试和扩展方式 |
| [记忆评测指南](docs/evaluation.md) | 开发者与策略评审者 | synthetic 契约集、指标、硬门禁和结果边界 |
| [部署与运行指南](docs/deployment.md) | 新人与运维者 | uv、Docker Compose、健康检查和生产缺口 |
| [隐私生命周期设计](docs/privacy-lifecycle.md) | 产品、安全与数据治理者 | 同意、抑制、保留、删除与证明的设计验收标准 |
| [威胁模型](docs/threat-model.md) | 安全与平台开发者 | 资产、信任边界、威胁场景和生产安全门槛 |
| [故障排查](docs/troubleshooting.md) | 所有人 | 端口、CORS、空结果和环境问题 |
| [演化安全模型](docs/evolution-safety.md) | 策略与安全开发者 | 动作空间、门禁和回滚 |
| [路线图](ROADMAP.md) | 使用者与维护者 | 建议优先级、阶段目标和完成门槛 |
| [安全政策](SECURITY.md) | 使用者与安全研究者 | 当前边界、私密报告和生产要求 |
| [变更记录](CHANGELOG.md) | 使用者与集成者 | 版本与未发布变化 |
| [来源说明](PROVENANCE.md) | 维护者 | 设计谱系与实现原则 |
| [贡献指南](CONTRIBUTING.md) | 贡献者 | 变更与提交检查清单 |

## License

GNU Affero General Public License v3.0 only，详见 [LICENSE](LICENSE)。
