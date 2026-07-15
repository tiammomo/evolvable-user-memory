# 部署与运行指南

本指南覆盖本机开发、Docker Compose 评估和未来生产部署前的检查项。

> 当前 `0.1.x` 已提供 PostgreSQL 适配器，以及 JWT 身份和 application 授权基线；请求中的 Scope 在 JWT 模式下只是目标选择器，必须由可信 grant 覆盖。项目仍缺完整权限治理、隐私治理和生产运维基线。以下容器配置用于本机演示和集成验证，不是生产部署模板。不要暴露到公网或写入真实敏感数据。

## 选择运行方式

| 方式 | 适合场景 | 数据持久化 |
| --- | --- | --- |
| `uv` 双进程 | 日常开发、调试和运行测试 | 后端重启即清空 |
| 默认 Docker Compose | 新人快速体验、PostgreSQL 集成验证 | 保存在命名 volume |
| 内存模式 Compose | 快速临时测试 | 后端重启即清空 |
| 单独 Docker 容器 | 镜像和入口检查 | 容器或进程重启即清空 |
| 生产部署 | 当前不支持 | JWT 基线已具备，仍缺权限治理/RLS、隐私治理与运维基线 |

## 使用 Docker Compose

### 1. 前置条件

- Docker Engine；
- Docker Compose v2（使用 `docker compose` 命令）；
- 本机端口 `33009` 和 `38089` 未被占用。

如果已经用 `uv` 启动了两个服务，请先在对应终端按 `Ctrl+C`，避免端口冲突。

### 2. 校验并启动

```bash
docker compose config --quiet
docker compose up --build -d
```

Compose 会按依赖关系启动：

- `postgres`：仅位于 Compose 内部网络，数据保存在命名 volume；
- `migrate`：等待数据库健康后执行一次 `evolvable-memory-migrate`；
- `backend`：迁移成功后，以 PostgreSQL 权威存储启动 FastAPI；
- `frontend`：后端健康后启动静态工作台。

应用镜像中的迁移、后端和前端进程都以非 root、只读根文件系统运行。宿主入口为：

- `backend`：FastAPI，发布到 <http://127.0.0.1:38089>；
- `frontend`：静态工作台，发布到 <http://127.0.0.1:33009>。

前端在用户浏览器中通过当前 hostname 的 `38089` 端口访问后端。因此这套 Compose 配置面向本机 `localhost`/`127.0.0.1` 体验；它不代表任意域名部署已经完成 CORS 和运行时地址配置。

### 3. 检查状态

```bash
docker compose ps
curl http://127.0.0.1:38089/health
curl http://127.0.0.1:38089/readyz
curl -I http://127.0.0.1:33009/
```

健康接口会说明当前存储，依赖就绪接口会真实检查存储连接：

```json
{"status":"ok","version":"0.1.0","storage":"postgres","auth_mode":"development","scope_source":"request"}
{"status":"ready","storage":"postgres"}
```

然后打开：

- 工作台：<http://127.0.0.1:33009>
- OpenAPI：<http://127.0.0.1:38089/docs>

### 4. 查看日志

```bash
docker compose logs -f postgres migrate backend frontend
```

不要把真实原始证据复制到 Issue 或公开日志。应用访问日志只记录 request ID、方法、路由模板、状态、耗时和字节数，不记录 body 或 query string；项目后端入口也关闭了 Uvicorn 默认的 request-line 日志。反向代理、Ingress、自定义进程管理器、调试中间件和调用方必须遵守同一规则，不要重新记录完整 URL、query 或正文。

### 5. 停止

```bash
docker compose down
```

普通 `docker compose down` 会停止并删除容器和网络，但保留 `postgres-data` 命名 volume；再次启动后记忆仍在。只有确认不再需要本机演示数据时才执行：

```bash
docker compose down --volumes
```

该命令会永久删除 Compose 中的 PostgreSQL 数据。

### 使用显式内存模式

如果只想运行可随时丢弃的临时环境：

```bash
docker compose -f compose.memory.yaml up --build -d
```

内存模式和默认模式使用相同的宿主端口，不能同时运行；切换前先停止当前模式。内存模式只启动后端和前端，并设置 `EMF_STORE=memory`。后端容器重启或重新创建后数据清空。停止命令必须使用相同的 `-f` 参数：

```bash
docker compose -f compose.memory.yaml down
```

## 单独构建与运行镜像

构建：

```bash
docker build -t evolvable-user-memory:local .
```

启动后端：

```bash
docker run --rm --name emf-backend \
  -e EMF_HOST=0.0.0.0 \
  -e EMF_PORT=38089 \
  -e EMF_STORE=memory \
  -p 127.0.0.1:38089:38089 \
  evolvable-user-memory:local
```

在另一终端启动前端：

```bash
docker run --rm --name emf-frontend \
  -e EMF_FRONTEND_HOST=0.0.0.0 \
  -e EMF_FRONTEND_PORT=33009 \
  -p 127.0.0.1:33009:33009 \
  evolvable-user-memory:local \
  evolvable-memory-frontend
```

镜像默认以固定的非 root UID/GID `10001` 运行。Compose 另外移除 Linux capabilities、启用 `no-new-privileges`，并把根文件系统设为只读。

## 环境变量

| 变量 | 容器值 | 说明 |
| --- | --- | --- |
| `EMF_ENVIRONMENT` | `development` | `development`、`test`、`staging` 或 `production` |
| `EMF_HOST` | `0.0.0.0` | 仅在容器内监听；宿主仍绑定到 `127.0.0.1` |
| `EMF_PORT` | `38089` | 后端容器端口 |
| `EMF_LOG_LEVEL` | `INFO` | Uvicorn 日志等级 |
| `EMF_STORE` | `postgres` | 默认 Compose 使用 PostgreSQL；也支持显式 `memory` |
| `EMF_DATABASE_URL` | Compose 内部 URL | PostgreSQL 连接串；`EMF_STORE=postgres` 时必填 |
| `EMF_DATABASE_POOL_MIN_SIZE` | `1` | 最小连接池大小 |
| `EMF_DATABASE_POOL_MAX_SIZE` | `10` | 最大连接池大小 |
| `EMF_MAX_REQUEST_BODY_BYTES` | `1048576` | 应用层请求体上限；同时检查声明长度和实际流量 |
| `EMF_AUTH_MODE` | `development` | Compose 本机身份；生产必须改为 `jwt` |
| `EMF_AUTH_JWT_ISSUER` | 空 | JWT issuer；`jwt` 模式必填 |
| `EMF_AUTH_JWT_AUDIENCE` | 空 | API audience；`jwt` 模式必填 |
| `EMF_AUTH_JWT_JWKS_URL` | 空 | IdP JWKS URL；`jwt` 模式必填 |
| `EMF_AUTH_JWT_ALGORITHMS` | `RS256` | 逗号分隔的非对称签名算法 |
| `EMF_AUTH_REQUIRED_SCOPE` | `memory` | token 必须包含的 OAuth scope |
| `EMF_AUTH_AUDIT_HMAC_KEY` | 空 | 授权审计引用密钥；`jwt` 模式至少 32 字符 |
| `EMF_FRONTEND_URL` | `http://127.0.0.1:33009` | 服务发现返回的前端入口 |
| `EMF_PUBLIC_API_URL` | `http://127.0.0.1:38089` | 服务发现和文档链接使用的外部 API 地址 |
| `EMF_CORS_ORIGINS` | 两个本机前端 Origin | 逗号分隔的精确允许来源 |
| `EMF_FRONTEND_HOST` | `0.0.0.0` | 前端容器监听地址 |
| `EMF_FRONTEND_PORT` | `33009` | 前端容器端口 |

应用不会自动加载 `.env`。Compose 已显式声明所需变量；原生运行时可参考项目根目录 `.env.example` 手动导出。

生产反向代理也应配置请求体上限，且不得高于业务明确允许的值。应用层限制仍需保留，因为 `Content-Length` 可以缺失或不可信；代理限制不能替代服务对实际 ASGI 请求流的累计检查。

## 生产身份与授权

默认 Compose 显式使用 `EMF_AUTH_MODE=development`，只适合本机体验。设置
开发身份只允许 `EMF_ENVIRONMENT=development` 或 `test`。其他环境如果仍使用开发身份，后端会拒绝启动；未知环境名称同样拒绝启动，避免拼写错误绕过生产门禁。

JWT 模式最小配置：

```bash
export EMF_ENVIRONMENT=production
export EMF_AUTH_MODE=jwt
export EMF_AUTH_JWT_ISSUER='https://identity.example'
export EMF_AUTH_JWT_AUDIENCE='evolvable-memory-api'
export EMF_AUTH_JWT_JWKS_URL='https://identity.example/.well-known/jwks.json'
export EMF_AUTH_JWT_ALGORITHMS='RS256'
export EMF_AUTH_REQUIRED_SCOPE='memory'
export EMF_AUTH_AUDIT_HMAC_KEY='load-this-from-a-secret-manager'
```

不要把审计 HMAC 密钥写入镜像、Compose 文件或 Git。JWT `memory_access` grant、角色、purpose、
错误语义和仍未完成的生产门禁见[身份与权限设计](authorization.md)。JWT 模式可验证调用身份和
当前 API 授权，但不会自动提供 IdP 成员治理、撤销、浏览器登录、RLS、双人审批或隐私生命周期。

## PostgreSQL 与迁移

默认 Compose 使用开发专用凭据创建 PostgreSQL，并且不把 `5432` 发布到宿主机。`migrate` 服务通过项目入口执行 Schema 升级：

```bash
docker compose run --rm migrate
```

原生运行现有 PostgreSQL 时，先导出连接串，再使用同一入口：

```bash
export EMF_STORE=postgres
export EMF_DATABASE_URL='postgresql://user:password@host:5432/database'
uv run evolvable-memory-migrate
uv run evolvable-memory
```

不要绕过项目入口直接调用裸 Alembic 命令；入口负责从统一运行时配置读取数据库 URL。

进入生产前仍需继续验证：

- 迁移升级/回滚、备份恢复和故障注入；
- 数据库级 Scope、幂等、修订和归因约束的故障与并发验证；
- 现有同事务 outbox 的消费、重放、投影恢复和生产连接池容量；
- 隔离、并发冲突、数据库中断和重启恢复测试。

进展顺序见 [路线图](../ROADMAP.md)。

## 生产部署前必须完成

当前版本缺少以下生产前置条件：

1. PostgreSQL 迁移门禁、备份和恢复演练；
2. IdP 角色治理、撤销/临时授权、增强认证、数据库 RLS 与独立授权审计存储；
3. 同意、保留、抑制、删除和投影清理证明；
4. 生产 CORS、TLS、反向代理、秘密管理与网络策略；
5. 持久审计、指标、追踪、告警和 SLO；
6. 依赖、镜像和基础设施安全扫描。

安全问题处理方式见 [安全政策](../SECURITY.md)。生产信任边界和隐私治理的目标验收标准见[威胁模型](threat-model.md)与[隐私生命周期设计](privacy-lifecycle.md)；这两项目前仍是设计，不是已交付能力。架构约束见[架构说明](architecture.md)与[开发指南](development.md)。

## 常见问题

### 端口已经被占用

```bash
ss -ltnp '( sport = :33009 or sport = :38089 )'
docker compose ps
```

停止旧的本机进程或 Compose 项目后重试。当前前端 API 端口和后端开发 CORS 有固定默认值，不建议只修改一侧端口。

### 容器健康检查失败

```bash
docker compose logs backend
docker compose logs frontend
docker compose logs postgres migrate
docker compose config
```

首先确认镜像构建成功、两个端口未占用，并检查后端是否因非法环境变量退出。更多场景见 [故障排查](troubleshooting.md)。
