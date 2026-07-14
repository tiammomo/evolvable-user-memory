# Evolvable User Memory

一套面向 AI 应用的、证据驱动且结果感知的用户记忆服务。

它不把“记忆”简化成向量库中的文本片段，而是分别管理输入证据、当前信念、召回轨迹、真实结果和受控策略演化，从而避免“因为读得多，所以越来越相信”的自我强化循环。

> **项目状态：开发预览版 `0.1.0`**
>
> 已提供完整可运行的偏好记忆闭环、Web 工作台和 OpenAPI 文档。当前使用进程内存，后端重启后数据会清空；尚未实现生产认证、持久化和删除证明，请勿直接暴露到公网或写入真实敏感数据。

[快速开始](#快速开始) · [首次体验](#完成第一条记忆闭环) · [API](#api-一览) · [架构](docs/architecture.md) · [完整文档](docs/index.md) · [贡献指南](CONTRIBUTING.md)

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
- 为相同记忆追加不可变修订，不原地覆盖历史。
- 列出指定租户与用户当前有效的偏好记忆。
- 按语义、上下文、信念、效用和时效进行可解释召回。
- 为每次召回保存策略版本、候选、评分拆解和 `trace_id`。
- 只接受能归因到对应 Trace 的 Outcome，并更新上下文效用。
- 生成有界策略提案，并验证演化实验的合法状态转换。
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
| 健康检查 | <http://127.0.0.1:38089/health> | 检查后端是否在线 |

快速验证后端：

```bash
curl http://127.0.0.1:38089/health
```

预期响应：

```json
{"status":"ok","version":"0.1.0"}
```

## 完成第一条记忆闭环

打开工作台后，按页面中的四步引导操作：

1. 保持默认开发 Scope `demo / alice`。
2. 进入“写入记忆”，点击“载入示例”并保存晚间饮品偏好。
3. 进入“当前记忆”，查看当前修订、置信度和证据数量。
4. 进入“记忆召回”，查询“晚上应该准备什么饮料？”。
5. 对召回结果提交“有帮助”或“无帮助”，观察上下文效用变化。
6. 点击“修正记忆”追加新版本，再通过“修订历史”确认旧版本仍然存在。

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
| `GET` | `/health` | 存活检查 | 否 |
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
├── application/     # 用例编排，以及存储/时钟/ID 端口
├── adapters/        # 基础设施端口实现；当前为线程安全内存存储
├── api/             # FastAPI 边界、Pydantic Schema 和 CORS
│   └── static/      # 原生 HTML/CSS/JavaScript 前端
├── config.py        # 后端与前端环境配置
├── frontend.py      # 独立静态前端进程
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

## 配置

应用直接读取环境变量，不会自动加载 `.env` 文件。可参考 [.env.example](.env.example)：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `EMF_ENVIRONMENT` | `development` | 运行环境名称 |
| `EMF_HOST` | `127.0.0.1` | 后端监听地址 |
| `EMF_PORT` | `38089` | 后端端口 |
| `EMF_LOG_LEVEL` | `INFO` | Uvicorn 日志级别 |
| `EMF_STORE` | `memory` | 存储实现；当前只支持 `memory` |
| `EMF_FRONTEND_HOST` | `127.0.0.1` | 前端监听地址 |
| `EMF_FRONTEND_PORT` | `33009` | 前端端口 |

## 当前边界

当前版本有意只实现一个语义完整的垂直切片，并不伪装成生产完成态：

- 内存存储不持久化，后端重启即清空。
- 当前检索器是简单词法检索，不是 embedding 或图检索。
- API 中的 `tenant_id` 和 `subject_id` 仅是开发合同；生产 Scope 必须来自可信认证上下文。
- 尚未实现认证、授权、删除证明、保留策略和生产审计存储。
- 演化模块只能调整有界策略参数，不能修改授权、隔离、删除或审计规则。

计划中的生产方向包括 PostgreSQL 权威存储、outbox、异步投影、embedding/graph 检索、删除证明和离线回放评估。

## 文档导航

| 文档 | 适合谁 | 内容 |
| --- | --- | --- |
| [文档首页](docs/index.md) | 所有人 | 按角色选择阅读路径 |
| [快速开始](docs/getting-started.md) | 第一次使用者 | 从零启动并完成首条闭环 |
| [前端工作台指南](docs/frontend-guide.md) | 产品与前端使用者 | 页面、状态和 API 映射 |
| [API 使用指南](docs/api-guide.md) | API 集成者 | 契约、幂等、归因和错误码 |
| [架构说明](docs/architecture.md) | 开发者与架构师 | 分层、五平面、状态和不变量 |
| [开发指南](docs/development.md) | 贡献者 | 添加能力、测试和扩展方式 |
| [故障排查](docs/troubleshooting.md) | 所有人 | 端口、CORS、空结果和环境问题 |
| [演化安全模型](docs/evolution-safety.md) | 策略与安全开发者 | 动作空间、门禁和回滚 |
| [来源说明](PROVENANCE.md) | 维护者 | 设计谱系与实现原则 |
| [贡献指南](CONTRIBUTING.md) | 贡献者 | 变更与提交检查清单 |

## License

GNU Affero General Public License v3.0 only，详见 [LICENSE](LICENSE)。
