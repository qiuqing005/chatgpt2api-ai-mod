# 部署与升级指南

本文介绍 ChatGPT2API 的常见部署方式，以及后续升级项目时需要保留的数据和执行步骤。

## 部署前准备

服务器需要安装：

- Docker
- Docker Compose v2
- Git

首次部署前建议确认：

```bash
docker version
docker compose version
git --version
```

项目核心持久化文件：

| 路径 | 作用 |
| --- | --- |
| `config.json` | 主配置、后台密钥、代理、图片、备份等配置；从 `config.example.json` 复制生成，不提交真实运行配置 |
| `.env` | Docker compose 环境变量 |
| `data/` | 账号、日志、图片、任务记录等运行数据 |

升级和迁移时重点保留以上内容。

## 方式一：普通 Docker 部署

适合不需要 WARP / FlareSolverr 清障的场景。

```bash
git clone https://github.com/qiuqing005/chatgpt2api-ai-mod.git
cd chatgpt2api-ai-mod
```

复制配置模板并设置 `auth-key`，或在 `docker-compose.yml` 中配置：

```bash
cp config.example.json config.json
# edit config.json and set auth-key
```

```yaml
environment:
  - CHATGPT2API_AUTH_KEY=your_secret_key
```

启动：

```bash
docker compose up -d
```

访问：

```text
http://localhost:3000
```

API 基础地址：

```text
http://localhost:3000/v1
```

查看日志：

```bash
docker logs -f chatgpt2api
```

停止：

```bash
docker compose down
```

## 方式二：WARP / FlareSolverr 部署

适合上游请求经常遇到 Cloudflare 拦截的场景。该方式会启动：

- `warp-proxy`
- `privoxy`
- `flaresolverr`
- `init-config`
- `app`

复制配置和环境变量模板：

```bash
cp config.example.json config.json
# edit config.json and set auth-key
cp .env.example .env
```

如需通过环境变量覆盖认证密钥，至少修改 `.env` 中的：

```text
CHATGPT2API_AUTH_KEY=your_secret_key_here
```

启动：

```bash
docker compose -f docker-compose.warp.yml up -d --build
```

访问：

```text
http://localhost:3000
```

FlareSolverr 相关配置可以在后台设置页的 `FlareSolverr` tab 中查看和测试。

查看容器状态：

```bash
docker compose -f docker-compose.warp.yml ps
```

查看日志：

```bash
docker logs -f chatgpt2api-warp
docker logs -f chatgpt2api-flaresolverr
```

停止：

```bash
docker compose -f docker-compose.warp.yml down
```

## 方式三：源码运行

适合本地开发或临时调试。

后端：

```bash
git clone https://github.com/qiuqing005/chatgpt2api-ai-mod.git
cd chatgpt2api-ai-mod
cp config.example.json config.json
# edit config.json and set auth-key
uv sync
uv run main.py
```

前端开发服务：

```bash
cd web
bun install
bun run dev
```

源码方式运行时，后端默认读取项目根目录的 `config.json` 和 `data/`。

## 存储后端

默认使用本地 JSON 文件：

```text
STORAGE_BACKEND=json
```

可选值：

| 值 | 说明 |
| --- | --- |
| `json` | 本地 JSON 文件，默认方式 |
| `sqlite` | 本地 SQLite，通常存放在 `data/accounts.db` |
| `postgres` | 外部 PostgreSQL |
| `mysql` | 外部 MySQL，使用 `mysql+pymysql://...` 连接 |
| `git` | Git 私有仓库存储账号数据 |

PostgreSQL 示例：

```yaml
environment:
  - STORAGE_BACKEND=postgres
  - DATABASE_URL=postgresql://user:password@host:5432/dbname
```

SQLite 示例：

```yaml
environment:
  - STORAGE_BACKEND=sqlite
  - DATABASE_URL=sqlite:////app/data/accounts.db
```

MySQL 示例：

```yaml
environment:
  - STORAGE_BACKEND=mysql
  - DATABASE_URL=mysql+pymysql://chatgpt2api:password@mysql:3306/chatgpt2api?charset=utf8mb4
```

建议在已有 MySQL 实例中为本项目创建独立数据库和专用用户，不要直接使用 `newapi` 数据库。

### 已有 MySQL 快速接入

下面流程适用于已经运行 MySQL 容器、希望保留现有账号和用户密钥的部署。只创建独立的 `chatgpt2api` 数据库和用户，
不会停止 MySQL、OpenResty 或其他业务容器，也不会修改现有业务库。

#### 1. 备份当前 JSON 数据

在项目目录执行，源文件会保留不变，作为回滚依据：

```bash
set -eu
stamp=$(date -u +%Y%m%dT%H%M%SZ)
cp -p config.json "config.json.bak-mysql-$stamp"
cp -p data/accounts.json "data/accounts.json.bak-mysql-$stamp"
cp -p data/auth_keys.json "data/auth_keys.json.bak-mysql-$stamp"
```

#### 2. 在已有 MySQL 中创建独立库和专用用户

将下面的 `MYSQL_CONTAINER` 和 `MYSQL_NETWORK` 换成实际名称。密码只在受保护的终端会话中设置，不要提交到 Git：

```bash
MYSQL_CONTAINER=1Panel-mysql-qkod
MYSQL_NETWORK=1panel-network
MYSQL_ROOT_PASSWORD='在受保护终端中输入 MySQL root 密码'
DB_PASSWORD=$(openssl rand -hex 32)

docker exec -i -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" "$MYSQL_CONTAINER" mysql -uroot <<SQL
CREATE DATABASE IF NOT EXISTS chatgpt2api CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'chatgpt2api_store'@'%' IDENTIFIED BY '$DB_PASSWORD';
ALTER USER 'chatgpt2api_store'@'%' IDENTIFIED BY '$DB_PASSWORD';
GRANT ALL PRIVILEGES ON chatgpt2api.* TO 'chatgpt2api_store'@'%';
FLUSH PRIVILEGES;
SQL
```

这里创建的是共享 MySQL 实例中的独立数据库，不得把 `DATABASE_URL` 指向 `newapi` 或其他业务库。

#### 3. 配置应用和 Docker 网络

在项目 `.env` 中保存连接串。示例中的密码由上一步生成，十六进制密码可以直接放入 URL：

```bash
CHATGPT2API_DATABASE_URL="mysql+pymysql://chatgpt2api_store:$DB_PASSWORD@$MYSQL_CONTAINER:3306/chatgpt2api?charset=utf8mb4"
touch .env
if grep -q '^CHATGPT2API_DATABASE_URL=' .env; then
  sed -i "s#^CHATGPT2API_DATABASE_URL=.*#CHATGPT2API_DATABASE_URL=$CHATGPT2API_DATABASE_URL#" .env
else
  printf 'CHATGPT2API_DATABASE_URL=%s\n' "$CHATGPT2API_DATABASE_URL" >> .env
fi
chmod 600 .env
```

`docker-compose.yml` 的应用服务需要同时加入应用默认网络和 MySQL 所在的外部网络：

```yaml
services:
  app:
    environment:
      STORAGE_BACKEND: mysql
      DATABASE_URL: "${CHATGPT2API_DATABASE_URL}"
    networks:
      - default
      - existing-mysql-network

networks:
  existing-mysql-network:
    name: 1panel-network
    external: true
```

将 `existing-mysql-network` 和 `1panel-network` 替换为实际网络名。先验证配置，不要立即启动：

```bash
docker compose config --quiet
```

#### 4. 迁移账号和用户密钥

先停止应用服务，避免迁移时 JSON 快照继续变化；不要执行 `docker compose down`，也不要停止 MySQL：

```bash
docker compose stop app

docker run --rm --network "$MYSQL_NETWORK" \
  -v "$PWD/data:/app/data:ro" \
  -e STORAGE_BACKEND=json \
  -e DATABASE_URL="$CHATGPT2API_DATABASE_URL" \
  ghcr.io/qiuqing005/chatgpt2api:1.10.1 \
  uv run python scripts/migrate_storage.py --from json --to mysql
```

迁移工具会同时处理 `accounts.json` 和 `auth_keys.json`，并采用增量写入，不会删除源 JSON 文件。

#### 5. 启动并验证

```bash
docker compose up -d --no-deps app
docker compose ps
docker logs --tail 50 chatgpt2api
curl -fsS http://127.0.0.1:3000/version
```

日志中应出现 `Initializing storage backend: mysql`，并显示账号监视器加载了现有账号。若应用端口不是 `3000`，按实际映射修改
`curl` 地址。

#### 6. 快速回滚

回滚只需让应用恢复读取 JSON，数据库保留不动：

```bash
docker compose stop app
# 将 STORAGE_BACKEND 改回 json，并移除或注释 DATABASE_URL
docker compose up -d --no-deps app
```

如果 JSON 文件被误改，再从 `accounts.json.bak-mysql-时间戳` 和 `auth_keys.json.bak-mysql-时间戳` 恢复。确认 MySQL 数据无误前，
不要删除 `chatgpt2api` 数据库。

## 升级前备份

升级前建议备份：

```bash
mkdir -p backups
tar -czf backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).tgz config.json .env data
```

如果没有 `.env`，可以去掉：

```bash
tar -czf backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).tgz config.json data
```

也可以在后台设置页配置 Cloudflare R2 备份，用于定时备份关键数据。

## 升级：普通 Docker 部署

进入项目目录：

```bash
cd chatgpt2api-ai-mod
```

升级时使用带版本的镜像或 `latest` 标签，并保留现有 `config.json` 与 `data/` 挂载目录。不要用仓库里的配置模板覆盖生产配置：

```bash
docker compose pull
docker compose up -d --remove-orphans
```

账号存储采用增量保存，普通保存不会删除数据库或 JSON 中缺失于本次内存快照的账号；删除账号请使用后台显式删除操作。

备份：

```bash
mkdir -p backups
tar -czf backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).tgz config.json .env data
```

拉取最新代码并重新构建本地镜像：

```bash
git pull
docker compose build --pull
docker compose up -d
```

查看状态：

```bash
docker compose ps
docker logs -f chatgpt2api
```

## 升级：WARP / FlareSolverr 部署

进入项目目录：

```bash
cd chatgpt2api-ai-mod
```

备份：

```bash
mkdir -p backups
tar -czf backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).tgz config.json .env data
```

拉取最新代码并重新构建：

```bash
git pull
docker compose -f docker-compose.warp.yml up -d --build
```

查看状态：

```bash
docker compose -f docker-compose.warp.yml ps
docker logs -f chatgpt2api-warp
```

## 升级：源码运行

```bash
cd chatgpt2api-ai-mod
git pull
uv sync
```

如果需要重新构建前端静态产物：

```bash
cd web
bun install
bun run build
```

然后按你的进程管理方式重启后端服务。

## 回滚

如果升级后需要回滚代码：

```bash
git log --oneline -n 20
git checkout <旧版本commit>
```

普通 Docker 部署：

```bash
docker compose up -d
```

WARP / FlareSolverr 部署：

```bash
docker compose -f docker-compose.warp.yml up -d --build
```

如果需要恢复数据：

```bash
tar -xzf backups/你的备份文件.tgz
```

恢复数据前建议先停止容器，避免运行中写入覆盖：

```bash
docker compose down
```

或：

```bash
docker compose -f docker-compose.warp.yml down
```

## 常用维护命令

查看容器：

```bash
docker compose ps
```

查看主服务日志：

```bash
docker logs -f chatgpt2api
```

查看 WARP 部署主服务日志：

```bash
docker logs -f chatgpt2api-warp
```

重启普通部署：

```bash
docker compose restart
```

重启 WARP 部署：

```bash
docker compose -f docker-compose.warp.yml restart
```

清理未使用镜像：

```bash
docker image prune
```

不要直接删除 `data/`、`config.json`、`.env`，除非已经确认有可用备份。
