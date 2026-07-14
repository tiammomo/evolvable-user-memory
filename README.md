# Evolvable User Memory

面向 AI 应用的证据驱动用户记忆服务。它不把“记忆”简化成一段被反复召回的文本，而是明确区分：系统实际观察到了什么、当前相信什么、这条记忆在特定场景中是否真的有用。

> 当前版本：`0.1.0`，可运行的开发垂直切片。默认数据保存在进程内存中，后端重启后会清空；尚未达到生产部署状态。

## 先用 30 秒理解它

一条偏好会经历以下闭环：

```text
原始输入
  ↓
Observation + EvidenceSpan       事实：系统实际看到了什么
  ↓
Candidate → MemoryRevision       信念：系统目前相信什么
  ↓
RecallResult + RecallTrace       使用：在当前语境召回了什么
  ↓
OutcomeEvent → UtilityEstimate   经验：实际结果是否有帮助
```

最重要的规则是：**读取一条记忆不会自动强化它**。只有引用某次 `RecallTrace` 的真实结果，才能更新该记忆在对应上下文中的效用。

## 5 分钟启动

### 环境要求

- Python 3.12 或更高版本
- [uv](https://docs.astral.sh/uv/) 包与环境管理器
- 两个空闲本地端口：前端 `33009`、后端 `38089`

### 安装依赖

```bash
uv sync
```

### 启动后端

在第一个终端运行：

```bash
uv run evolvable-memory
```

### 启动前端

在第二个终端运行：

```bash
uv run evolvable-memory-frontend
```

### 打开入口

| 入口 | 地址 | 用途 |
| --- | --- | --- |
| 记忆工作台 | <http://127.0.0.1:33009> | 推荐的首次体验入口 |
| OpenAPI 文档 | <http://127.0.0.1:38089/docs> | 查看和调试全部接口 |
| 服务发现 | <http://127.0.0.1:38089/> | 确认版本、存储和入口 |
| 健康检查 | <http://127.0.0.1:38089/health> | 检查后端是否在线 |

## 第一次体验

打开记忆工作台后，按页面顶部的四步指引操作：

1. 保持默认作用域 `demo / alice`，或填写自己的开发租户与用户。
2. 进入“写入记忆”，点击“载入示例”，保存晚间饮品偏好。
3. 进入“当前记忆”，查看当前有效修订、置信度与证据数量。
4. 进入“记忆召回”，查询“晚上应该准备什么饮料？”。
5. 对召回结果点击“有帮助”或“无帮助”，观察上下文效用变化。
6. 点击“修正记忆”，追加新版本，再打开“修订历史”检查旧版本仍然存在。

也可以在后端启动后直接运行完整 API 示例：

```bash
uv run python examples/first_memory.py
```

## 项目现在能做什么

- 写入带原始证据、置信度和上下文的用户偏好。
- 使用作用域内幂等键安全重试写入与结果反馈。
- 为相同偏好追加不可变修订，不覆盖历史。
- 列出指定租户和用户当前有效的偏好记忆。
- 按语义、上下文、信念、效用和时效进行召回。
- 为每次召回保存策略版本、评分拆解和 `trace_id`。
- 只接受能归因到召回结果的 Outcome，并学习上下文效用。
- 生成有界检索策略提案，并校验演化实验的合法状态转换。

## 五个概念平面

| 平面 | 回答的问题 | 当前实现 |
| --- | --- | --- |
| Evidence | 系统实际观察到了什么？ | Observation、EvidenceSpan、Candidate |
| Belief | 系统当前相信什么？ | MemoryRecord、不可变 MemoryRevision、BeliefState |
| Experience | 使用记忆后结果如何？ | RecallTrace、OutcomeEvent、UtilityEstimate |
| Projection | 如何高效检索？ | 当前为可替换的词法扫描；向量/图索引是路线图 |
| Evolution | 如何安全优化策略？ | StrategySnapshot、有界提案、实验状态机 |

详细说明见 [架构文档](docs/architecture.md)。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/` | 服务发现与开发边界 |
| `GET` | `/health` | 存活检查 |
| `POST` | `/v1/preferences` | 写入上下文偏好 |
| `GET` | `/v1/preferences` | 列出当前有效偏好 |
| `POST` | `/v1/preferences/{record_id}/corrections` | 追加偏好修订 |
| `GET` | `/v1/preferences/{record_id}/revisions` | 读取不可变历史 |
| `POST` | `/v1/recall` | 执行召回并产生 Trace |
| `POST` | `/v1/outcomes` | 提交可归因结果并更新效用 |

请求、响应、幂等规则和完整示例见 [API 使用指南](docs/api-guide.md)。

## 代码结构

```text
src/evolvable_memory/
├── domain/          # 纯 Python 领域值、状态转换和硬约束
├── application/     # 用例编排，以及存储/时钟/ID 端口
├── adapters/        # 端口实现；当前为线程安全内存存储
├── api/             # FastAPI 边界、Pydantic Schema、CORS
│   └── static/      # 原生 HTML/CSS/JavaScript 前端
├── config.py        # 后端与前端环境配置
├── frontend.py      # 独立静态前端进程
└── main.py          # Uvicorn 后端入口

tests/               # 领域、应用、HTTP、隔离和前端启动测试
docs/                # 使用、架构、开发与安全文档
examples/            # 可直接运行的客户端示例
```

依赖方向必须保持为：

```text
api / adapters  →  application  →  domain
```

`domain` 不导入 FastAPI、Pydantic、数据库驱动、HTTP 客户端或供应商 SDK。

## 常用开发命令

```bash
uv sync                    # 同步环境
uv run pytest              # 测试与覆盖率门禁
uv run ruff check .        # 静态检查
uv run ruff format --check .
uv run mypy                # 严格类型检查
uv build                   # 构建发布产物
```

## 配置

复制 `.env.example` 中的变量到运行环境即可覆盖默认值；项目不会自动读取 `.env` 文件。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `EMF_ENVIRONMENT` | `development` | 运行环境名称 |
| `EMF_HOST` | `127.0.0.1` | 后端监听地址 |
| `EMF_PORT` | `38089` | 后端端口 |
| `EMF_LOG_LEVEL` | `INFO` | Uvicorn 日志级别 |
| `EMF_STORE` | `memory` | 存储实现；当前只支持 `memory` |
| `EMF_FRONTEND_HOST` | `127.0.0.1` | 前端监听地址 |
| `EMF_FRONTEND_PORT` | `33009` | 前端端口 |

## 当前边界与安全提醒

- 当前内存存储不持久化，后端重启即清空。
- 当前检索器是简单词法检索，不是 embedding 或图检索。
- API 请求体中的 `tenant_id` 和 `subject_id` 仅是开发合同；生产适配器必须从可信认证上下文得到作用域。
- 当前没有认证、授权、删除证明、保留策略或生产审计存储。
- 不要把当前版本直接暴露到公网，也不要写入真实敏感数据。
- 演化模块只能调整有界策略参数，不能修改授权、隔离、删除或审计规则。

## 文档导航

- [文档首页](docs/index.md)：按使用者角色选择阅读路径
- [快速开始](docs/getting-started.md)：从零启动并完成第一条闭环
- [前端工作台指南](docs/frontend-guide.md)：页面、操作和 API 映射
- [API 使用指南](docs/api-guide.md)：契约、幂等、错误与完整调用流程
- [架构说明](docs/architecture.md)：分层、五平面、状态与不变量
- [开发指南](docs/development.md)：添加能力、测试要求和扩展方式
- [故障排查](docs/troubleshooting.md)：端口、CORS、空结果与环境问题
- [演化安全模型](docs/evolution-safety.md)：可写动作空间与晋升门禁
- [来源说明](PROVENANCE.md)：设计谱系与实现原则
- [贡献指南](CONTRIBUTING.md)：提交变更前的检查清单

## License

GNU Affero General Public License v3.0 only，详见 [LICENSE](LICENSE)。
