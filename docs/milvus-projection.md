# Milvus 向量投影

本项目把 Milvus 用作可丢弃、可重建的召回加速层，不把它当作记忆数据库。PostgreSQL 仍是 Observation、Evidence、MemoryRevision、RecallTrace、Outcome 和策略状态的唯一权威来源。

## 数据流

```text
权威写事务
  └─ MemoryRevision + outbox_events（同事务）
       └─ projection_jobs（幂等发现）
            └─ projector 租约领取
                 ├─ 从 PostgreSQL 读取当前 source revision
                 ├─ 生成 embedding
                 └─ 按 revision + model 幂等 upsert 到 Milvus

Recall
  ├─ Milvus 按 tenant_hash + subject_hash + model_hash + 双时间上界生成候选
  ├─ PostgreSQL 按真实 Scope、valid_at、known_at 重建可见 Revision
  ├─ 只把 Milvus 分数应用到仍然权威可见的 revision
  └─ 与词法、上下文、信念、效用、时效合并并冻结 RecallTrace
```

这意味着 Milvus 中的跨 Scope 结果、过期 revision、未来记录、重复实体或孤儿实体都不能直接进入最终响应。Milvus 超时或不可用时，API 返回原有词法结果，并把进程内投影状态标记为 `degraded`；召回读取本身仍不修改信念或效用。

## Collection 数据边界

共享 collection 使用 `tenant_hash` 作为 Milvus partition key，并用 `subject_hash` 和 `model_hash` 做标量过滤。实体只包含：

- projection、record、revision、source event 标识；
- 哈希化 tenant/subject/model；
- `valid_from`、`recorded_at` 微秒时间；
- source hash 与 dense vector。

Milvus 不保存原始 Evidence、Observation 正文、memory key、memory value 或明文 Scope。向量本身仍可能泄露语义特征，因此它仍属于受保护的个人数据投影，不能公开暴露、跨环境复用或绕过删除流程。

## 启动与检查

默认 Compose 已启用 PostgreSQL、etcd、MinIO、Milvus、projector、backend 和 frontend：

```bash
docker compose up --build -d
docker compose ps
curl http://127.0.0.1:38089/health
```

Milvus SDK 端口为 `127.0.0.1:19530`，健康端口为 `127.0.0.1:19091`。etcd、MinIO 与 PostgreSQL 不发布到宿主机。

原生进程模式需要先迁移数据库，再分别启动 projector 和 API：

```bash
export EMF_STORE=postgres
export EMF_DATABASE_URL=postgresql://...
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

## 重建与模型变更

全量重建会先删除并重新创建 collection，再把所有 revision outbox job 重新排队：

```bash
uv run evolvable-memory-projector rebuild
```

该命令是运维写操作。当前尚未提供授权后的远程控制面；生产环境应限制为受审计的平台操作。重建期间 API 会继续使用词法召回，并逐步恢复向量命中。

embedding 维度变化时，旧 collection schema 不再兼容。应更新 `EMF_MILVUS_COLLECTION` 到新版本名，或在维护窗口执行 rebuild。模型变化但维度不变时，`model_hash` 会阻止混用旧实体，但仍建议使用版本化 collection 并完成重建后再切换。

## Embedding 提供方

`EMF_EMBEDDING_PROVIDER=hash` 是默认的离线确定性基线，不依赖 API key，适合本地运行和故障可复现测试。它主要表达词元相似性，不应被宣传为高质量语义模型。

`EMF_EMBEDDING_PROVIDER=openai_compatible` 会调用：

```text
POST {EMF_EMBEDDING_BASE_URL}/embeddings
```

并使用 `EMF_EMBEDDING_MODEL`、`EMF_EMBEDDING_DIMENSIONS` 和可选 `EMF_EMBEDDING_API_KEY`。客户端不记录输入正文或 API key；提供方自身的保留、地域、训练与访问政策仍需单独治理。

## 队列、重试与可观测性

`projection_jobs` 是每个 projection 独立的消费回执，不复用 outbox 的全局 `published_at`。领取使用 PostgreSQL `FOR UPDATE SKIP LOCKED`，处理采用至少一次语义；Milvus projection ID 保证重复执行可幂等覆盖。

失败按指数退避重试，达到 `EMF_PROJECTION_WORKER_MAX_ATTEMPTS` 后进入 `dead_letter`。`last_error` 只保存异常类型，不保存可能包含正文或供应商响应的错误详情。可用以下只读查询观察积压：

```sql
SELECT projection_name, status, count(*)
FROM projection_jobs
GROUP BY projection_name, status
ORDER BY projection_name, status;

SELECT projection_name, last_event_occurred_at, updated_at
FROM projection_cursors;
```

`EMF_PROJECTION_REQUIRED=false` 是推荐默认值：PostgreSQL 就绪即可对外服务，Milvus 故障降级。只有业务明确要求“没有语义召回就不接流量”时才设为 `true`，此时 `/readyz` 会检查 Milvus。

## 尚未完成的生产门禁

- ProcessingGrant 撤销、抑制和在线删除已覆盖 PostgreSQL、outbox 与 Milvus；备份/导出副本仍需部署方执行到期与恢复屏障；
- 尚无授权后的 dead-letter 重放、暂停、切换和清理控制面；
- 尚无 projection lag、失败率、模型供应商延迟和召回降级的指标/告警 SLO；
- 默认 Compose 凭据和单机 Milvus 只适用于本机开发；
- hash embedding 不是线上语义质量证明。

在这些门禁完成前，不应将默认栈直接暴露到公网或写入真实敏感数据。
