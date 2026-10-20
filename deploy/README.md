# Docker 部署说明

> 结论：**本项目适合 Docker 部署，且难度低。** 全部推荐组件都有官方镜像，需要自行构建的只有后端与前端两个服务；单机 `docker compose` 即可承载，无需 Kubernetes。

## 一、为什么适合

| 因素 | 说明 |
|---|---|
| 组件全部容器化就绪 | PostgreSQL（pgvector 镜像）、Redis、RSSHub、changedetection.io、sockpuppetbrowser 均为一等公民官方镜像 |
| 只需自建 2 个镜像 | `backend`（FastAPI）与 `frontend`（Next.js），且都是常规多阶段构建 |
| 架构天然可拆分 | 采集层与业务层解耦，某个采集器挂了不影响看板展示 |
| 与既有实践一致 | 工作区内 `reminders` 已是纯 Docker 自托管形态，运维方式可复用 |
| 规模匹配 | 单团队、内网、日均抓取量千级 → 单机足够，引入 K8s 属过度设计 |

## 二、编排结构

```
deploy/
├── docker-compose.yml      # profile: infra（现成组件） / app（自建服务）
├── .env.example            # 复制为 .env 后填写
└── initdb/01-extensions.sql  # 首次启动自动建 vector / pg_trgm 扩展
```

两个 profile 的意义：**P0 阶段只起 `infra`，采集侧马上能跑**，不必等后端代码写完。

| 服务 | 镜像 | 端口 | 常驻内存（约） |
|---|---|---|---|
| postgres | `pgvector/pgvector:pg16` | 内部 5432 | 300–600 MB |
| redis | `redis:7-alpine` | 内部 6379 | 60–120 MB |
| rsshub | `diygod/rsshub:latest` | 127.0.0.1:1200 | 300–600 MB |
| changedetection | `dgtlmoon/changedetection.io` | 127.0.0.1:5000 | 200–400 MB |
| sockpuppetbrowser | `dgtlmoon/sockpuppetbrowser` | 内部 3000 | 600 MB–1.5 GB |
| api | 本地构建 | 127.0.0.1:8000 | 200–500 MB |
| web | 本地构建 | 127.0.0.1:3001 | 150–300 MB |

**资源建议**：最低 2C4G（浏览器并发限制为 1）；推荐 4C8G；磁盘起步 40 GB（页面快照与 PDF 是主要增长项，按每个 watch 每日 1 份、单份 200 KB 估算，100 个 watch 一年约 7 GB）。

## 三、启动步骤

```bash
cd deploy
cp .env.example .env
# 填写 POSTGRES_PASSWORD / CDIO_PASS（必填）
openssl rand -base64 24

docker compose --profile infra up -d
docker compose ps
```

验证：

```bash
# PostgreSQL 与 pgvector
docker compose exec postgres psql -U ciboard -d ciboard -c "\dx" | grep vector
# RSSHub
curl -s "http://127.0.0.1:1200/github/trending/daily/any" | head -5
# changedetection
curl -s -u admin:<CDIO_PASS> http://127.0.0.1:5000/api/v1/watch | head -20
```

后端与前端就绪后：

```bash
docker compose --profile infra --profile app up -d --build
```

## 四、六个必须提前知道的坑

1. **别用官方 `postgres` 镜像。** 必须用 `pgvector/pgvector:pg16`，否则 `CREATE EXTENSION vector` 会报扩展不存在——这是本项目最高频的启动失败原因。

2. **Chromium 必须给足共享内存。** `sockpuppetbrowser` 若不加 `shm_size`（编排里已设 1 GB），抓大页面时会随机崩溃，且报错信息指向 Playwright 超时，极易误判为网络问题。

3. **Windows 下不要用宿主目录 bind mount。** 当前机器是 Windows + Docker Desktop，PG 数据文件、SQLite、大量小快照放在 bind mount 上性能折损明显，且偶发权限与文件锁问题。编排已全部改用具名卷。代价是宿主机不便直接翻文件，需要时用 `docker cp` 或 `docker compose exec`。

4. **changedetection.io 自身鉴权很弱。** 必须设 `AUTH_BASIC_USER` / `AUTH_BASIC_PASS`，且端口只绑 `127.0.0.1`。它默认允许配置任意 URL 抓取，直接暴露公网等于开放一个 SSRF 入口。

5. **定时采集只能单实例。** `api` 里已固定 `replicas: 1`。若将来要扩容，需改用 PostgreSQL advisory lock 选主，不能简单加副本，否则会重复采集、重复计费、重复告警。

6. **国内拉镜像与出网。** Docker Hub 直连不稳，建议配置镜像加速；同时 RSSHub / changedetection 需要能正常访问目标站点，若走过代理，注意容器内 `HTTP_PROXY` 与 Playwright 的代理配置是两套（后者需在 watch 级单独设）。

## 五、备份与升级

```bash
# 数据库（每日跑一次即可）
docker compose exec -T postgres pg_dump -U ciboard -d ciboard | gzip > backup-$(date +%F).sql.gz

# 卷整体备份（含 changedetection 的 watch 配置与快照）
docker run --rm -v ci-board_cdiodata:/data -v "$PWD":/backup alpine \
  tar czf /backup/cdio-$(date +%F).tar.gz -C /data .
```

恢复与升级注意：

- 升级 changedetection.io / RSSHub 前先备份对应卷，两者都存在跨版本数据结构变更。
- 升级 PostgreSQL 大版本（如 16→17）不能直接换镜像，需 `pg_dump` 导出后重建。
- 建议给镜像打固定 tag（如 `pgvector/pgvector:pg16`、`diygod/rsshub:2026-10`）而非 `latest`，避免某天自动拉到不兼容版本。

## 六、对外访问

默认全部只绑 `127.0.0.1`。需要给团队访问时，二选一：

- **仅内网**：编排里已注释的 `caddy` 服务，配一个内网域名 + 自签或内部 CA 证书。
- **公网**：必须先在反代上加认证（Basic Auth 或 OIDC），并单独为 changedetection 限制路径。**不建议**把 changedetection 的 5000 端口映射到公网。

## 七、下一步

`backend/` 与 `frontend/` 目录尚未创建。若要进入 P0 编码，需要先确认：

1. 首批友商 / 客户数量与型号数
2. 核心指标清单（30–50 个）
3. 10–20 个必看新闻源、5–10 个必抓参数页 URL

三项确认后即可产出可运行的最小闭环：数据表 + 采集脚本 + 首页新闻流 + 参数对比页。
