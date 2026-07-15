# 故障排查

## 前端打不开

确认前端进程：

```bash
uv run evolvable-memory-frontend
```

检查：

```bash
curl -I http://127.0.0.1:33009/
```

正常情况下返回 `200`，并包含：

```text
Cache-Control: no-store
X-Content-Type-Options: nosniff
```

## 前端显示“API 离线”

确认后端进程：

```bash
uv run evolvable-memory
```

检查健康接口：

```bash
curl http://127.0.0.1:38089/health
```

如果直接打开了本地 `index.html` 文件，请改用 `http://127.0.0.1:33009`，不要使用 `file://`。

## 端口已被占用

Linux 下查看监听进程：

```bash
ss -ltnp '( sport = :33009 or sport = :38089 )'
```

停止旧进程，或通过环境变量修改端口：

```bash
EMF_PORT=39000 uv run evolvable-memory
EMF_FRONTEND_PORT=34000 uv run evolvable-memory-frontend
```

如果修改端口，当前版本还需要同步调整：

- `index.html` 中的 `api-port`。
- 后端环境变量 `EMF_PUBLIC_API_URL` 和 `EMF_CORS_ORIGINS`；其中 CORS Origin 必须是完整的前端协议、主机与端口。

默认端口能避免这项额外配置，推荐新人保持默认值。

## 浏览器出现 CORS 错误

后端默认只允许：

```text
http://127.0.0.1:33009
http://localhost:33009
```

确认浏览器地址与允许来源完全一致。协议、主机和端口任一不同都会成为不同 Origin。

## 写入成功但列表为空

检查页面右上角的 Scope 是否在写入后发生变化。列表严格按 `tenant_id + subject_id` 隔离。

也可以直接检查：

```bash
curl 'http://127.0.0.1:38089/v1/preferences?tenant_id=demo&subject_id=alice'
```

## 召回没有结果

依次检查：

1. 当前 Scope 是否有记忆。
2. 查询是否包含与 `key` 或 `value` 相关的词。
3. 请求上下文是否与保存上下文冲突。
4. 结果得分是否低于当前策略的 `min_score`。

当前检索是简单词法检索。中文查询会使用单字和连续双字 token，不具备大模型语义理解能力。

空召回仍会生成 Trace，但没有可用于 Outcome 的 revision。

## Outcome 返回 422

常见原因：

- `trace_id` 不存在于当前 Scope。
- `revision_id` 不在该 Trace 的 items 中。
- UUID 格式错误或 kind 不在允许枚举中。

必须使用同一次召回响应中的 `trace_id` 和 `revision_id`，不能使用列表接口中的任意修订搭配旧 Trace。

## 写入返回 409

最常见原因是相同 Scope 下复用了幂等键，但 key、value 或 context 已改变。

- 如果这是同一个业务动作的网络重试，恢复原请求内容并复用键。
- 如果这是新事实，生成新的业务幂等键。
- 不要收到 409 后无条件随机换键，这可能掩盖上游重复事件。

## 后端重启后数据消失

这是 `EMF_STORE=memory` 的预期行为，所有权威数据都在后端进程中。需要跨重启保留时，使用默认 PostgreSQL Compose；或设置 `EMF_STORE=postgres`、`EMF_DATABASE_URL`，先运行 `uv run evolvable-memory-migrate` 再启动后端。详见 [部署指南](deployment.md)。

需要重新体验时，可以点击前端“使用示例开始”，或运行：

```bash
uv run python examples/first_memory.py
```

## `uv sync` 或命令找不到包

确认当前目录包含 `pyproject.toml` 和 `uv.lock`，然后重新运行：

```bash
uv sync
uv run python -c 'import evolvable_memory; print(evolvable_memory.__file__)'
```

## TestClient 提示缺少 `httpx2`

当前 Starlette TestClient 使用 `httpx2`，浏览器 E2E 的就绪探测仍使用 `httpx`；两者都已固定在开发依赖与 `uv.lock` 中。先运行 `uv sync --locked`。如果提示仍存在，确认命令使用的是项目 `.venv`，不要在全局 Python 环境单独安装或混用版本。
