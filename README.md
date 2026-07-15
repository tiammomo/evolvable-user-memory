# Evolvable User Memory

一套面向 AI 应用的、证据驱动且结果感知的用户记忆服务。

它不把“记忆”简化成向量库中的文本片段，而是分别管理输入证据、当前信念、召回轨迹、真实结果和受控策略演化，从而避免“因为读得多，所以越来越相信”的自我强化循环。

> **项目状态：开发预览版 `0.1.0`**
>
> 已提供完整可运行的偏好记忆闭环、Web 工作台、OpenAPI、进程内存与 PostgreSQL 适配器。原生快速开始默认使用内存，后端重启后数据会清空；默认 Compose 使用 PostgreSQL。API 已具备本地开发身份与 JWT 授权基线，但完整权限治理、删除证明和生产运维仍未完成，请勿直接暴露到公网或写入真实敏感数据。

[快速开始](#快速开始) · [容器运行](docs/deployment.md) · [首次体验](#完成第一条记忆闭环) · [API](#api-一览) · [架构](docs/architecture.md) · [路线图](ROADMAP.md) · [完整文档](docs/index.md)

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
| Projection | 如何高效检索？ | 当前为可替换的词法检索；向量/图索引属于路线图 |
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
- 按词法相关性、上下文、信念、效用和时效进行可解释召回。
- 为每次召回保存策略版本、候选、评分拆解和 `trace_id`。
- 只接受能归因到对应 Trace 的 Outcome，并更新上下文效用。
- 生成有界策略提案，并验证演化实验的合法状态转换。
- 提供 PostgreSQL 权威存储、版本化迁移、数据库约束和显式内存模式。
- PostgreSQL 会为 Observation 摄入、Revision 变更和 Outcome 记录在同一事务追加不含原始证据正文的 outbox 事件。
- 前端根据后端状态动态展示当前存储，并在 Scope 切换时取消旧请求、隔离旧响应。
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

## 快速开始

### 1. 环境要求

- Python 3.12 或更高版本
- [uv](https://docs.astral.sh/uv/) 包与环境管理器
- 本地空闲端口：前端 `33009`、后端 `38089`

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
{"status":"ok","version":"0.1.0","storage":"memory","auth_mode":"development","scope_source":"request"}
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
| `POST` | `/v1/recall` | 执行召回并产生 Trace | 仅追加 Trace |
| `POST` | `/v1/outcomes` | 提交可归因结果并更新效用 | 是 |

完整请求、响应、幂等规则、Outcome 类型和错误码见 [API 使用指南](docs/api-guide.md)。

## 架构与代码结构

依赖方向必须保持为：

```text
api / adapters  →  application  →  domain
```

```text
src/evolvable_memory/
├── domain/          # 纯 Python 领域值、状态转换和硬约束
├── application/     # 用例编排、权限执行点，以及存储/身份/授权端口
├── adapters/        # 存储、角色策略与伪名化授权审计适配器
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

GitHub Actions 会在 Python 3.12–3.14 上执行测试，在 3.12 上额外运行 PostgreSQL 适配器分支覆盖与 Chromium 前端 E2E，并验证发行包、Compose 配置和容器构建。

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
| `EMF_MAX_REQUEST_BODY_BYTES` | `1048576` | API 请求体最大字节数 |
| `EMF_AUTH_MODE` | `development` | `development` 本机身份或 `jwt` 可信 access token |
| `EMF_AUTH_JWT_ISSUER` | 空 | JWT issuer；`jwt` 模式必填 |
| `EMF_AUTH_JWT_AUDIENCE` | 空 | 当前 API 的 JWT audience；`jwt` 模式必填 |
| `EMF_AUTH_JWT_JWKS_URL` | 空 | IdP 公钥集合 URL；`jwt` 模式必填 |
| `EMF_AUTH_JWT_ALGORITHMS` | `RS256` | 允许的非对称签名算法，逗号分隔 |
| `EMF_AUTH_REQUIRED_SCOPE` | `memory` | token 必须包含的粗粒度 OAuth scope |
| `EMF_AUTH_AUDIT_HMAC_KEY` | 空 | 授权审计引用 HMAC 密钥；`jwt` 模式至少 32 字符 |
| `EMF_FRONTEND_URL` | `http://127.0.0.1:33009` | 服务发现返回的前端地址 |
| `EMF_PUBLIC_API_URL` | `http://127.0.0.1:38089` | 对外 API 基础地址 |
| `EMF_CORS_ORIGINS` | 两个本机前端 Origin | 逗号分隔的允许来源 |
| `EMF_FRONTEND_HOST` | `127.0.0.1` | 前端监听地址 |
| `EMF_FRONTEND_PORT` | `33009` | 前端端口 |

## 当前边界

当前版本有意只实现一个语义完整的垂直切片，并不伪装成生产完成态：

- 原生快速开始的内存模式不持久化，后端重启即清空；PostgreSQL 是默认 Compose 的权威存储，但其故障恢复和生产运维仍处于开发预览。
- 当前检索器是简单词法检索，不是 embedding 或图检索。
- 开发模式仍把 `tenant_id` 和 `subject_id` 作为本地目标 Scope；JWT 模式只接受 token 中同一条 `memory_access` grant 明确覆盖的目标。
- 已实现类型化授权、内置角色、purpose 限制和 JWT 校验基线；尚未实现成员/角色管理 API、撤销控制、临时授权、RLS、同意/保留/抑制/删除执行与证明，以及独立生产审计存储。
- transactional outbox 的同事务写入已经实现；异步消费者、发布重放、投影游标和投影重建尚未实现。
- 尚未定义并验证生产 SLO、告警、备份恢复和灾难恢复目标。
- 演化模块只能调整有界策略参数，不能修改授权、隔离、删除或审计规则。

计划中的生产方向包括权限治理控制面、数据库 RLS、隐私生命周期、PostgreSQL 故障恢复与迁移加固、outbox 消费与重放、异步投影、embedding/graph 检索、生产 SLO 和离线回放评估，详见 [路线图](ROADMAP.md)。

## 文档导航

| 文档 | 适合谁 | 内容 |
| --- | --- | --- |
| [文档首页](docs/index.md) | 所有人 | 按角色选择阅读路径 |
| [快速开始](docs/getting-started.md) | 第一次使用者 | 从零启动并完成首条闭环 |
| [前端工作台指南](docs/frontend-guide.md) | 产品与前端使用者 | 页面、状态和 API 映射 |
| [API 使用指南](docs/api-guide.md) | API 集成者 | 契约、幂等、归因和错误码 |
| [身份与权限设计](docs/authorization.md) | 平台、安全与集成者 | JWT、角色、动作、purpose、审计和生产门禁 |
| [架构说明](docs/architecture.md) | 开发者与架构师 | 分层、五平面、状态和不变量 |
| [开发指南](docs/development.md) | 贡献者 | 添加能力、测试和扩展方式 |
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
