# Evolvable User Memory 完整项目手册

本文是一份可以独立阅读的本地手册，面向第一次接触本项目的开发者、教师、架构师和 AI 应用集成者。它说明项目解决什么问题、适合哪些场景、内部如何工作、怎样在本地启动、如何通过网页和 API 使用，以及当前距离生产环境还有哪些边界。

适用版本：`0.1.x`。

## 1. 项目是什么

Evolvable User Memory 是一个为 AI 应用提供长期用户记忆的独立服务。

它不负责生成聊天回答，也不等同于聊天记录、文档知识库或向量数据库。它负责把用户在长期交互中表达的偏好，转化为：

- 有原始证据来源的记忆；
- 有不可变版本历史的当前信念；
- 可以根据问题和上下文召回的结果；
- 能归因到一次具体召回的真实反馈；
- 可以在严格边界内优化的召回策略。

项目最核心的一句话是：

> 记忆必须有证据，变化必须有历史，召回必须可解释，学习必须来自可归因的真实结果，演化不能突破治理边界。

完整闭环如下：

```text
用户表达事实
    ↓
Observation / Evidence：记录原始证据
    ↓
MemoryRevision：形成或修正记忆
    ↓
Recall：按问题、上下文和时间召回
    ↓
RecallTrace：冻结本次召回依据
    ↓
Outcome：记录真实使用结果
    ↓
Utility：学习该记忆在当前上下文中的效用
    ↓
Evolution：在门禁约束下优化召回策略
```

![Evolvable User Memory 完整记忆链路](assets/diagrams/memory-lifecycle-v1.png)

## 2. 为什么不能只保存聊天记录

聊天记录只能回答“用户说过什么”，但一个长期记忆系统还需要回答：

- 哪句话是值得长期保留的证据；
- 当前应该相信哪个版本；
- 用户是否后来修正过；
- 这条记忆在哪些上下文中成立；
- 系统在某个历史时点已经知道什么；
- 某次推荐为什么使用了这条记忆；
- 推荐最后是否真的有帮助；
- 如何防止不同企业或用户之间串数据；
- 如何在向量数据库不可用时继续服务。

因此，本项目把“原始事实”“当前信念”“使用经验”“检索投影”和“策略演化”分开建模，而不是把所有内容混进一张聊天表或一个向量 collection。

## 3. 适用场景

![Evolvable User Memory 使用场景](assets/diagrams/usage-scenarios-v1.png)

### 3.1 AI 个人助手

可记录用户的长期偏好，例如：

- 饮食和饮品偏好；
- 回答长短和表达风格；
- 常用语言；
- 工作时间和提醒习惯；
- 特定场景下的设备或服务偏好。

示例：

```text
用户：晚上我只喝低因咖啡。

key: drink.preference
value: decaf coffee
context:
  time_of_day: evening
```

### 3.2 AI 客服

可以为企业内客户保存：

- 沟通语言；
- 常用服务渠道；
- 回答详细程度；
- 已确认的个性化设置；
- 与具体场景相关的服务偏好。

```text
tenant_id: company-a
subject_id: customer-10086
key: communication.style
value: concise
context:
  channel: chat
```

`tenant_id` 表示租户，`subject_id` 表示租户内用户。所有有状态操作必须携带明确 Scope。

### 3.3 AI 教育与学习助手

可保存：

- 学生偏好的讲解方式；
- 特定学科下的内容呈现方式；
- 用户明确表达的学习节奏；
- 某种教学建议是否真正有效。

涉及成绩、健康、诊断等高风险信息时，不能把普通偏好流程直接当作完整治理方案。

### 3.4 推荐和内容助手

可保存内容题材、时长、风格和场景偏好，并通过 Outcome 判断推荐是否被真实接受，而不是把“被展示过”误当成“用户喜欢”。

### 3.5 多 Agent 工作流

多个 Agent 可以共享同一个记忆服务：

```text
对话 Agent ─┐
客服 Agent ─┼─→ Evolvable User Memory API
推荐 Agent ─┘
```

Agent 不需要各自重复实现 Scope 隔离、幂等写入、不可变版本、历史查询、Outcome 归因和向量投影恢复。

## 4. 不适用场景

本项目不应直接当作：

- 完整聊天记录归档；
- 企业文档知识库；
- 通用文档 RAG 平台；
- CRM、支付、订单等核心业务数据库；
- 模型训练数据仓库；
- 可以无限自我修改的 Agent；
- 当前阶段直接面向公网的敏感数据生产系统。

文档、合同、产品手册等知识检索应使用独立知识库。Evolvable User Memory 管理的是“某个用户当前偏好什么、为什么这样相信、在什么上下文中有用”。

## 5. 五个概念平面

### 5.1 Evidence：证据平面

Evidence 回答：

> 实际观察到了什么？

证据是追加式事实。系统不应为了让当前结论看起来合理而修改旧证据。

典型对象包括：

- `Observation`：一次输入包络；
- `EvidenceSpan`：原始证据；
- `Candidate`：从证据得出的结构化候选解释。

### 5.2 Belief：信念平面

Belief 回答：

> 根据现有证据，系统当前相信什么？

典型对象包括：

- `MemoryRecord`：一条记忆的稳定身份；
- `MemoryRevision`：不可变的具体版本；
- `BeliefState`：置信度和支持信息。

修正会追加新 Revision，而不是覆盖旧版本：

```text
MemoryRecord: drink.preference + evening
  ├─ Revision 1: decaf coffee
  └─ Revision 2: herbal tea（supersedes Revision 1）
```

### 5.3 Experience：经验平面

Experience 回答：

> 记忆被实际使用以后，结果怎么样？

典型对象包括：

- `RecallTrace`：一次召回的冻结记录；
- `OutcomeEvent`：真实业务结果；
- `UtilityEstimate`：某条 Revision 在特定上下文中的效用。

重要不变量：

> 列表和召回都是读取操作，不会强化信念或效用。只有引用某次 RecallTrace 的真实 Outcome 才能学习 Utility。

### 5.4 Projection：投影平面

Projection 回答：

> 如何高效找到可能相关的记忆？

当前项目包含：

- 词法候选；
- Milvus 向量候选；
- 上下文匹配；
- 信念、Utility 和时效评分；
- PostgreSQL 最终可见性复核。

投影必须可以删除和确定性重建，不能成为权威事实来源。

### 5.5 Evolution：演化平面

Evolution 回答：

> 召回策略如何在安全边界内优化？

可以演化的是有界策略参数，例如语义、上下文、信念、Utility 和时效权重。

不允许演化的是：

- 授权规则；
- tenant/subject 隔离；
- 删除和保留政策；
- 审计规则；
- 隐私边界。

候选策略注册不会自动激活。晋升或回滚必须经过可信内部编排、阶段顺序和 Gate Receipt 验证。

## 6. 分层架构

依赖方向为：

```text
bootstrap → api / adapters → application → domain
```

![Evolvable User Memory 模块架构](assets/diagrams/architecture-modules-v1.png)

### Domain

纯 Python 领域模型，不依赖 FastAPI、Pydantic、数据库驱动、HTTP 客户端或 Milvus SDK。

### Application

负责用例编排和端口定义，包括：

- 写入偏好；
- 修正记忆；
- 执行召回；
- 记录 Outcome；
- 评测；
- 投影；
- 演化实验。

### Adapters

实现应用端口，例如：

- 内存存储；
- PostgreSQL 存储；
- Milvus；
- embedding 提供方；
- Gate Receipt 签发与验证；
- projection outbox。

### API

FastAPI HTTP 适配器，负责输入输出格式、请求关联、错误语义和执行点调用，不应承载领域规则。

### Bootstrap / Composition

显式组合应用服务和基础设施资源。导入 API factory 不会自动连接外部依赖。

## 7. PostgreSQL 和 Milvus 的分工

![PostgreSQL 与 Milvus 权威及投影数据流](assets/diagrams/postgres-milvus-flow-v1.png)

### 7.1 PostgreSQL 是权威数据库

PostgreSQL 保存：

- Observation 和 Evidence；
- MemoryRecord 和 MemoryRevision；
- RecallTrace；
- Outcome 和 Utility；
- 策略快照和演化实验；
- outbox、投影任务和游标；
- Scope、幂等、版本和归因约束。

任何最终返回给用户的记忆，都必须通过 PostgreSQL 的真实 Scope 和时间可见性检查。

### 7.2 Milvus 是可重建向量投影

Milvus 保存：

- projection、record、revision 和 source event 标识；
- 哈希化 tenant、subject 和模型；
- `valid_from`、`recorded_at`；
- source hash 和 dense vector。

Milvus 不保存原始 Evidence 正文、memory key/value 或明文 Scope。

向量仍可能泄露语义特征，因此 Milvus 仍属于受保护的个人数据投影。

### 7.3 写入投影链

```text
PostgreSQL 权威事务
  └─ MemoryRevision + outbox_events
       └─ projection_jobs
            └─ projector 获取租约
                 ├─ 从 PostgreSQL 读取 source revision
                 ├─ 生成 embedding
                 └─ 幂等 upsert 到 Milvus
```

### 7.4 召回链

```text
查询请求
  ├─ 词法候选
  └─ Milvus 向量候选
          ↓
PostgreSQL Scope 和双时间复核
          ↓
上下文 + 信念 + Utility + 时效评分
          ↓
最终排序并冻结 RecallTrace
```

Milvus 中的跨 Scope、过期、未来、重复或孤儿实体不能直接进入最终响应。

默认 `EMF_PROJECTION_REQUIRED=false`。Milvus 不可用时，API 降级到词法召回；只有业务明确要求语义投影必须可用时才将其设置为 `true`。

## 8. 运行组件

默认 Docker Compose 启动：

| 服务 | 作用 | 宿主入口 |
| --- | --- | --- |
| `postgres` | 权威记忆、历史、Trace、Outcome、策略 | 不发布 |
| `migrate` | 执行数据库迁移 | 一次性任务 |
| `etcd` | Milvus 元数据依赖 | 不发布 |
| `minio` | Milvus 对象存储依赖 | 不发布 |
| `milvus` | 向量投影 | `127.0.0.1:19530` |
| `projector` | 消费 outbox 并写入 Milvus | 不发布 |
| `backend` | FastAPI API | `127.0.0.1:38089` |
| `frontend` | 记忆工作台 | `127.0.0.1:33009` |

Milvus 健康端口是 `127.0.0.1:19091`。

## 9. 本地启动

### 9.1 完整 Compose：推荐体验方式

前置要求：

- Docker Engine；
- Docker Compose v2；
- 本机端口 `33009`、`38089`、`19530`、`19091` 未占用。

启动：

```bash
docker compose config --quiet
docker compose up --build -d
docker compose ps
```

检查：

```bash
curl http://127.0.0.1:38089/health
curl http://127.0.0.1:38089/readyz
curl -I http://127.0.0.1:33009/
```

入口：

- 工作台：<http://127.0.0.1:33009>
- Swagger UI：<http://127.0.0.1:38089/docs>
- API：<http://127.0.0.1:38089>

查看日志：

```bash
docker compose logs -f postgres milvus migrate projector backend frontend
```

停止并保留数据：

```bash
docker compose down
```

永久删除本地 PostgreSQL 和 Milvus 数据：

```bash
docker compose down --volumes
```

最后一个命令是破坏性操作，只应在确认不再需要演示数据时执行。

### 9.2 显式内存 Compose

适合第一堂课和临时演示：

```bash
docker compose -f compose.memory.yaml up --build -d
```

该模式只启动前后端，后端重启或重建后数据清空。

停止：

```bash
docker compose -f compose.memory.yaml down
```

完整 Compose 和内存 Compose 使用相同宿主端口，不能同时运行。

### 9.3 原生开发模式

```bash
uv sync
uv run evolvable-memory
```

另一个终端：

```bash
uv run evolvable-memory-frontend
```

默认使用进程内存存储。项目不会自动加载 `.env`，需要手工导出环境变量，或使用 Compose 中的显式配置。

### 9.4 原生 PostgreSQL + Milvus

```bash
export EMF_STORE=postgres
export EMF_DATABASE_URL='postgresql://user:password@host:5432/database'
export EMF_PROJECTION_MODE=milvus
export EMF_MILVUS_URI=http://127.0.0.1:19530

uv run evolvable-memory-migrate
uv run evolvable-memory-projector run
uv run evolvable-memory
```

一次性处理和依赖检查：

```bash
uv run evolvable-memory-projector once
uv run evolvable-memory-projector check
```

## 10. 网页操作流程

打开 <http://127.0.0.1:33009> 后，按以下顺序体验：

1. 在顶部确认 `tenant` 和 `subject`；
2. 点击“写入一条记忆”；
3. 填写 key、value、context、证据和置信度；
4. 在“当前记忆”中查看当前有效版本；
5. 在“记忆召回”中输入自然语言问题和上下文；
6. 查看命中结果、分数和 Trace；
7. 对结果提交 helpful/unhelpful 等真实反馈；
8. 在记忆详情中追加修正并查看版本历史。

首页的三个编号标签不是按钮，它们说明：

```text
01 记录证据
02 形成记忆
03 结果反馈
```

## 11. API 基本约定

### 11.1 Scope

每个有状态操作都必须明确携带：

```json
{
  "tenant_id": "demo",
  "subject_id": "alice"
}
```

开发模式下，Scope 适合本机体验。JWT 模式下，payload 只是目标资源选择器，token 中同一条可信 grant 必须同时覆盖 action、tenant、subject 和 purpose。

### 11.2 幂等键

偏好写入、修正和 Outcome 都需要 `idempotency_key`。

- 同一业务动作的网络重试必须复用原键；
- 新事实、新修正或新结果必须使用新键；
- 同一 Scope 内复用键但改变业务内容会返回 `409 Conflict`；
- 不同 Scope 可以使用相同键。

推荐格式：

```text
<业务事件 ID>:<动作>:<序号>
```

例如：

```text
turn-42:preference:1
task-9:outcome:1
```

### 11.3 时间

所有显式时间必须带 UTC offset。

- `occurred_at`：业务事件实际发生时间；
- `recorded_at`：系统获知并保存的时间；
- `valid_at`：要查询的业务有效时点；
- `known_at`：系统知识截止时点。

显式 `known_at` 不能晚于请求执行时间，因为系统不能读取尚未获知的状态。

### 11.4 Purpose

请求默认 purpose 是 `personalization`。JWT 模式下，purpose 必须被同一条执行授权 grant 允许，不能把只获准个性化的数据改用于模型训练。

## 12. API 完整使用闭环

### 12.1 服务发现和探针

| 端点 | 用途 |
| --- | --- |
| `GET /` | 返回版本、存储、身份模式和入口 |
| `GET /health` | 进程状态和当前配置，不检查外部依赖 |
| `GET /livez` | 只检查 API 进程存活 |
| `GET /readyz` | 检查当前权威存储是否就绪 |

### 12.2 写入偏好

```bash
curl -X POST http://127.0.0.1:38089/v1/preferences \
  -H 'content-type: application/json' \
  -d '{
    "tenant_id": "demo",
    "subject_id": "alice",
    "source": "conversation",
    "idempotency_key": "turn-42:preference-1",
    "key": "drink.preference",
    "value": "decaf coffee",
    "context": {"time_of_day": "evening"},
    "evidence_text": "晚上我只喝低因咖啡",
    "confidence": 0.92
  }'
```

响应中的关键字段：

| 字段 | 含义 |
| --- | --- |
| `observation_id` | 原始输入包络 ID |
| `candidate_id` | 证据解释候选 ID |
| `record_id` | 记忆稳定身份 |
| `revision_id` | 本次不可变版本 ID |
| `sequence` | 修订序号 |
| `idempotent_replay` | 是否为安全重放 |

相同 `key + context` 的新证据会追加 Revision。值相同可增加支持，值不同会形成替代版本。

### 12.3 列出当前记忆

```bash
curl \
  'http://127.0.0.1:38089/v1/preferences?tenant_id=demo&subject_id=alice'
```

该操作不生成 RecallTrace，也不修改 Belief 或 Utility。

### 12.4 召回记忆

```bash
curl -X POST http://127.0.0.1:38089/v1/recall \
  -H 'content-type: application/json' \
  -d '{
    "tenant_id": "demo",
    "subject_id": "alice",
    "query": "晚上应该准备什么饮料",
    "context": {"time_of_day": "evening"},
    "limit": 5
  }'
```

召回响应包含：

- `trace_id`：后续 Outcome 必须引用；
- `policy_id`、`policy_version`：本次使用的策略；
- `valid_at`、`known_at`：冻结的双时间边界；
- `items`：命中的记忆版本；
- 总分及词法/向量、上下文、信念、Utility、时效拆解。

召回即使没有结果也会生成 Trace，但不会更新信念或 Utility。

### 12.5 回传真实 Outcome

使用召回返回的 `trace_id` 和某个命中项的 `revision_id`：

```bash
curl -X POST http://127.0.0.1:38089/v1/outcomes \
  -H 'content-type: application/json' \
  -d '{
    "tenant_id": "demo",
    "subject_id": "alice",
    "trace_id": "TRACE_ID",
    "revision_id": "REVISION_ID",
    "kind": "helpful",
    "idempotency_key": "task-9:outcome-1",
    "weight": 1.0,
    "note": "The recommendation was accepted."
  }'
```

服务会验证 Trace、Revision 和 Scope 的归因关系。只有出现在该 Trace 中的 Revision 才能接收该 Outcome。

### 12.6 修正记忆

```bash
curl -X POST \
  http://127.0.0.1:38089/v1/preferences/RECORD_ID/corrections \
  -H 'content-type: application/json' \
  -d '{
    "tenant_id": "demo",
    "subject_id": "alice",
    "source": "explicit-feedback",
    "idempotency_key": "turn-43:correction-1",
    "value": "herbal tea",
    "evidence_text": "其实晚上我最近改喝花草茶了",
    "reason": "user corrected an outdated preference",
    "expected_revision_id": "CURRENT_REVISION_ID"
  }'
```

`expected_revision_id` 是乐观并发条件。当前版本已变化时返回 `409`，防止旧页面覆盖新修订。

### 12.7 查看版本历史

```bash
curl \
  'http://127.0.0.1:38089/v1/preferences/RECORD_ID/revisions?tenant_id=demo&subject_id=alice'
```

其他 Scope 中的记录不会被泄露。

## 13. 双时间召回

项目区分：

- 事实在业务上何时有效；
- 系统在什么时候已经知道该事实。

例如：

```text
6 月 1 日：用户开始喝花草茶
6 月 3 日：系统收到并记录这条修正
```

历史请求示例：

```bash
curl -X POST http://127.0.0.1:38089/v1/recall \
  -H 'content-type: application/json' \
  -d '{
    "tenant_id": "demo",
    "subject_id": "alice",
    "query": "当时晚上应该准备什么饮料",
    "context": {"time_of_day": "evening"},
    "limit": 5,
    "valid_at": "2026-06-01T20:00:00+08:00",
    "known_at": "2026-06-02T00:00:00Z"
  }'
```

适用场景包括：

- 迟到数据；
- 追溯生效的修正；
- 计划未来生效的偏好；
- 解释系统在历史时点为什么做出某个推荐；
- 故障复盘和审计。

该能力重建 Revision 和 Outcome 的历史可见状态，不会自动重放过去的模型、索引实现或历史策略。

## 14. 接入真实 AI 应用

推荐调用链：

```text
用户发送消息
    ↓
上层应用识别值得长期保存的偏好候选
    ↓
敏感信息、授权和 purpose 检查
    ↓
规范化 key / value / context
    ↓
POST /v1/preferences
    ↓
下一次回答前 POST /v1/recall
    ↓
把召回项作为受控上下文提供给 LLM
    ↓
LLM 回答或 Agent 执行动作
    ↓
获得真实结果
    ↓
POST /v1/outcomes 或 corrections
```

本项目当前接收结构化偏好，不会自动把整段聊天交给 LLM 并无条件写入。真实产品通常需要在前面增加“记忆候选提取器”，并执行敏感信息、同意、授权和质量检查。

不要把每句话都写成长期记忆。

召回结果可作为提示上下文：

```text
以下是经过作用域检查的用户长期偏好：
- 晚上偏好低因咖啡。

请结合当前请求使用这些信息，但不要把记忆当作不可质疑的事实。
如果用户当前表达与记忆冲突，以当前明确表达为准，并触发修正流程。
```

## 15. Embedding 和投影运维

### 15.1 默认 hash embedding

`EMF_EMBEDDING_PROVIDER=hash` 是离线、确定性基线，不需要 API key。它适合教学、测试和故障复现，但不代表生产级语义质量。

### 15.2 OpenAI-compatible embedding

`EMF_EMBEDDING_PROVIDER=openai_compatible` 会调用：

```text
POST {EMF_EMBEDDING_BASE_URL}/embeddings
```

需要配置：

- `EMF_EMBEDDING_MODEL`；
- `EMF_EMBEDDING_DIMENSIONS`；
- `EMF_EMBEDDING_BASE_URL`；
- 可选 `EMF_EMBEDDING_API_KEY`。

外部提供方的数据保留、地域、训练使用和访问政策必须单独治理。

### 15.3 重建投影

```bash
uv run evolvable-memory-projector rebuild
```

该命令会重建 collection 并重新排队 Revision，是运维写操作。生产环境必须限制在受审计的平台权限内。

embedding 维度变化时，应使用新 collection 名，或在维护窗口执行 rebuild。模型变化但维度不变时仍建议使用版本化 collection。

## 16. 策略演化

策略演化不是普通用户 API，而是可信内部能力。

基本流程：

```text
当前活动策略
    ↓
生成有界候选策略
    ↓
离线评测
    ↓
影子阶段
    ↓
灰度阶段
    ↓
Gate Receipt 验证
    ↓
原子晋升或回滚
```

Gate Receipt 会绑定：

- 实验和策略身份；
- 唯一阶段边；
- 决策；
- 硬门禁结果；
- 外部产物摘要；
- issuer、key ID 和有效期。

系统拒绝篡改、未知密钥、错绑和过期凭证，并保留幂等重放行为。

当前没有授权后的 HTTP 演化控制面，也没有自动执行真实 shadow/canary 流量。阶段标签不能被当成生产质量证明。

## 17. 测试与质量门禁

安装依赖：

```bash
uv sync
```

完整测试和覆盖率：

```bash
uv run pytest
```

代码质量：

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

确定性记忆评测：

```bash
uv run evolvable-memory-eval run --dataset builtin:smoke-v1
uv run evolvable-memory-eval run --dataset builtin:temporal-v1
```

评测覆盖：

- Recall@k 和 MRR；
- 修正准确性；
- 拒答；
- forbidden/Scope 隔离；
- 双时间状态；
- Outcome Utility；
- 执行失败门禁。

这些是项目回归契约，不代表 LongMemEval、LoCoMo、SOTA 或真实业务质量。

## 18. 常见故障排查

### 18.1 前端显示 API 离线

检查：

```bash
curl http://127.0.0.1:38089/health
curl http://127.0.0.1:38089/readyz
docker compose logs backend frontend
```

确认前端和后端使用一致、且浏览器可达的 `EMF_PUBLIC_API_URL`，并检查 CORS。

### 18.2 `/health` 正常但 `/readyz` 返回 503

`/health` 不检查外部依赖，`/readyz` 会检查权威存储。查看 PostgreSQL 和 backend 日志：

```bash
docker compose ps postgres backend
docker compose logs postgres backend
```

### 18.3 Milvus 不可用

默认配置会降级到词法召回。检查：

```bash
docker compose ps milvus projector
docker compose logs milvus projector backend
curl http://127.0.0.1:19091/healthz
```

不要在故障时把 Milvus 结果绕过 PostgreSQL 直接返回。

### 18.4 投影积压

可在 PostgreSQL 中观察 `projection_jobs` 和 `projection_cursors`。任务失败使用指数退避，达到最大次数后进入 `dead_letter`。当前没有授权后的远程重放控制面。

### 18.5 重试得到 409

确认重试是否复用了同一个业务动作的幂等键。如果使用相同键但改变了 key、value、context 或结果内容，`409` 是正确行为。

### 18.6 修正返回 409

页面持有的 `expected_revision_id` 已过期。重新读取当前记忆，让用户确认后再提交修正。

## 19. 安全与隐私约束

应用默认不记录原始请求正文或 query string。访问日志只记录 request ID、方法、路由模板、状态、耗时和字节数。

但完整安全仍要求：

- TLS 和可信反向代理；
- 生产 JWT 配置；
- 秘密管理；
- 网络隔离；
- 数据库最小权限；
- 独立审计存储；
- 同意、保留、抑制和删除生命周期；
- 备份恢复和故障演练。

默认 Compose 凭据、development 身份和单机 Milvus 只适用于本机开发。

## 20. 当前生产边界

当前版本适合：

- 本机教学；
- 架构学习；
- 原型验证；
- 内部集成实验；
- PostgreSQL/Milvus 混合记忆方案验证。

当前不应直接承载真实敏感生产数据，因为仍缺少：

1. 完整同意和授权生命周期；
2. 保留、抑制、删除和投影清理证明；
3. PostgreSQL 强制 RLS；
4. 独立防篡改授权审计存储；
5. IdP 成员、角色、撤销和临时授权治理；
6. 备份恢复和灾难演练；
7. projection lag、死信和降级告警；
8. 完整生产 SLO；
9. 受授权的演化和投影运维控制面。

## 21. 教学建议

不要按代码目录从上到下讲。建议围绕“一条记忆如何安全成长”教学：

1. 先用内存模式体验写入、召回和反馈；
2. 讲五平面和“不因读取而强化”的不变量；
3. 使用应用端口和内存适配器理解分层；
4. 引入 Scope、幂等和权限；
5. 用 PostgreSQL 解释权威状态、事务和双时间；
6. 用 Milvus 故障降级解释投影数据库；
7. 用 Outcome 解释可归因学习；
8. 用 Gate Receipt 解释受控演化；
9. 最后使用前端、评测和 Compose 串起完整系统。

每次课程都建议遵循：失败场景 → 不变量 → 测试 → 实现 → 故障注入 → 复盘。

## 22. 相关文档

- [文档导航](index.md)
- [快速开始](getting-started.md)
- [API 使用指南](api-guide.md)
- [架构说明](architecture.md)
- [分层与五平面 ADR](adr/0001-layer-and-plane-boundaries.md)
- [Milvus 投影指南](milvus-projection.md)
- [记忆评测指南](evaluation.md)
- [演化安全](evolution-safety.md)
- [前端指南](frontend-guide.md)
- [部署与运行](deployment.md)
- [隐私生命周期](privacy-lifecycle.md)
- [威胁模型](threat-model.md)
- [故障排查](troubleshooting.md)

## 23. 总结

Evolvable User Memory 不是让 AI 简单地“记住更多”，而是让 AI 在明确的 Scope、证据、版本、时间、反馈和治理边界下，安全地形成、使用、修正和优化长期用户记忆。

在真实 AI 产品中，它应当作为独立记忆基础设施，由上层 Agent 或应用负责候选提取、当前交互和最终动作；本项目负责长期记忆的权威状态、可解释召回、真实结果学习和受控演化。
