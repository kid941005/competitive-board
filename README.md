# 竞品情报看板

压电领域友商竞品情报看板。当前已实现**第二部分：性能参数横向对比**（P0），覆盖四条产品线：

| 产品大类 | 代码 | 型号数 | 友商 |
|---|---|---|---|
| 压电陶瓷 | `ceramic` | 3 | 摩根先进材料（PZT-4 / 5H / 8） |
| 压电气泵 | `pump_air` | 6 | 村田 MZB 系列 4 型、巴特尔斯 mp6-gas / gas+ |
| 压电风扇 | `fan` | 2 | xMEMS XMC-2400、Frore AirJet Mini G2 |
| 压电液泵 | `pump_liquid` | 3 | 巴特尔斯 mp6-liq / pi / pp |

第一部分「友商 / 客户关联新闻动态」尚未开始，规划见 `../竞品情报看板-开源调研与技术方案.md`。

---

## 快速开始

```bash
cd backend

# 1) 写入示例数据（28 个指标 / 10 家公司 / 14 个型号 / 105 条参数）
python -m app.seed

# 2) 启动
python -m uvicorn app.main:app --reload --port 8000
```

打开 <http://127.0.0.1:8000/compare>，会先跳到登录页。

首次启动若库里没有任何管理员，会自动创建一个，密码打印在启动日志里：

```
==============================================================
  已创建初始管理员：admin
  初始密码：xxxxxxxxxxxx
  登录后请立刻在「账号管理」里修改密码。
==============================================================
```

也可显式指定：`CIB_ADMIN_PASSWORD=你的密码`。密码需 ≥10 位且同时包含字母与数字，
不合规会拒绝启动，而不是静默降级成弱口令。

默认使用 SQLite（`backend/data/ciboard.db`），无需任何外部依赖。切换 PostgreSQL 只需设 `DATABASE_URL`：

```bash
export DATABASE_URL="postgresql+psycopg://user:pass@localhost:5432/ciboard"
```

---

## 账号与权限

### 两类角色的边界

| 能力 | 权限位 | 管理员 | 普通用户 |
|---|---|---|---|
| 查看对比矩阵、参数溯源 | `data:view` | ✓ | ✓ |
| 导出 CSV / JSON | `data:export` | ✓ | ✓ |
| 导入规格书、CSV 导入、写入参数 | `data:write` | ✓ | ✗ |
| 账号管理：新建、改角色、停用、重置密码 | `admin:users` | ✓ | ✗ |
| 系统参数配置 | `admin:settings` | ✓ | ✗ |

一句话概括：**普通用户是只读的** —— 能看、能按条件筛选、能导出，但改不了任何数据，也进不了管理面。

### 为什么这样切

系统参数（对比型号上限、上传体积、置信度阈值、会话时长）与账号体系，属于一旦误改就影响所有人的东西，必须收在管理员手里。
而「看数据」是产品主路径，不该多设一道门槛。

若业务上需要让普通用户也能导入规格书，在 `app/auth.py` 的 `ROLE_PERMISSIONS["viewer"]` 里加上 `P_WRITE` 即可
—— **路由层一行都不用改**，因为路由只声明所需权限位，从不判断角色名。

### 访问控制规则

| 场景 | 界面层 | 接口层 |
|---|---|---|
| 未登录访问页面 | 303 跳 `/login?next=原路径` | 401 `{"code":"unauthenticated"}` |
| 已登录但权限不足 | 403 页面，写明缺哪个权限位与当前角色 | 403 `{"code":"forbidden","required":"data:write","role":"viewer"}` |
| 管理功能入口 | 普通用户**看不到**导航项 | — |
| 直访管理 URL | 403 页面（不静默跳走，边界要看得见） | — |

页面与接口的错误形态按**路径**判定而非 `Accept` 头：`/api/*` 与 `/export.csv` 一律 JSON，
`/admin/*` 与页面路由一律 HTML。这样浏览器与 curl 的行为一致，排查时不会混淆「被拦了」与「路由不存在」。

### 会话与密码

- 密码 `pbkdf2_sha256` + 每用户 16 字节随机盐 + 24 万次迭代
- 会话表只存 token 的 sha256，泄库也无法直接冒用
- Cookie `HttpOnly` + `SameSite=Lax`；HTTPS 部署必须设 `CIB_COOKIE_SECURE=1`
- 所有非幂等请求校验 CSRF 双重提交令牌（表单字段 `_csrf` 或请求头 `X-CSRF-Token`）
- 改密码、把管理员降级、停用账号，都会**立即吊销该用户全部会话**
- 登录失败对「账号不存在」与「密码错误」返回同一提示，且都走一次哈希校验，避免时间侧信道枚举账号

### 防锁死

不能取消自己的管理员权限、不能停用自己的账号、系统至少保留一名启用状态的管理员。

---

## 页面能做什么

| 功能 | 说明 |
|---|---|
| 型号多选 | 按友商分组勾选，最多 8 个型号（`CIB_MAX_COMPARE` 可调） |
| 最优值高亮 | 依指标字典的 `better` 方向自动判定；并列时同时高亮；只有一个值时**不标最优** |
| 差异过滤 | 「只看有差异」按容差判定，1.50 与 1.5 不算差异 |
| 缺失过滤 | 「只看有缺失」——谁在藏参数，本身即情报 |
| 完整度 | 每个型号表头的进度条 = 已公开指标占比 |
| 指标搜索 | 支持中文指标名即时过滤 |
| 溯源浮层 | 点击任意数值 → 取值 / 测试条件 / 置信度 / 是否复核 / 抓取时间 / 来源链接 |
| 导出 | CSV（带 BOM，Excel 直接打开不乱码）、JSON |
| 规格书导入 | 上传 PDF，自动识别参数，人工复核后入库（见下节） |

---

## 导入规格书，自动识别参数

入口：<http://127.0.0.1:8000/datasheet>（对比页右上角「导入规格书」）

流程三步：**选型号 → 上传 PDF → 复核后写入**。识别结果**不会直接入库**，必须由人勾选确认。

### 识别引擎怎么工作

`app/datasheet.py`，纯规则，不依赖大模型，因此结果可复现、可单测：

1. **限定指标范围**：只在该型号所属产品大类的指标里匹配。否则「流量」会在气泵（L/min）与液泵（µL/min）之间误配。
2. **别名匹配**：指标字典的 `aliases` 字段登记规格书里的常见写法（中英文、符号）。长别名优先，同一指标的别名命中的字符区间不重叠，避免「空气流量」与「流量」被算成两处。
3. **取值**：标签后可向后搜 90 字符，覆盖「标签在表头、数值在下一行」的版式。单位必须紧贴数值（最多隔两个空格），否则不认。
4. **单位换算**：mL/min → L/min、mbar → kPa、cm → mm 等显式换算并标记「换算」。
5. **区间与比较符**：`24~27 kHz` 存原文；`≥0.7` 把 `≥` 记入条件；数值型指标遇到 `0~70 ℃` 取上限并把原文写入条件。
6. **脚注与修饰语防护**：`(1)` 这类脚注不当取值；`压力型` 这种标签后紧跟「型/范围/比」的命中会降权并标「存疑」。

置信度分档：单位确认 **0.90**，比较符确认 **0.80**，仅数字 **0.70**；换算 −0.05，修饰语 −0.15。默认只勾选 ≥ 0.75 的行。

### 复核页能改什么

每一行都可以改指标（下拉换成别的字典项）、改数值、改条件。写入后一律 `verified=False`，对比页会显示橙色「未复核」点，溯源浮层指向原 PDF 的对应页。

**已知边界**：扫描版 PDF（纯图片、无文本层）识别不到内容；三线表跨行合并的表头可能错位；一个指标在文档里出现多次时只保留置信度最高的一处（该处会标注「×N」提示重复出现）。

---


## 数据模型

```
Entity（公司：本方 / 友商 / 客户）
  └─ Product（型号）
        └─ ProductSpec（参数值）──► SpecMetric（指标字典）
                               └──► SpecSource（来源 + 抓取时间）
```

**指标字典是全项目的核心资产**，28 个字段中最关键的三个：

- `better`：`higher` / `lower` / `target` / `na`，决定最优值方向。`target` 与 `na` 不参与最优判定。
- `precision`：显示精度，同时作为差异判定的量化粒度。
- `applies_to`：该指标适用于哪些产品大类（逗号分隔），留空表示全类通用。

### 为什么要 `applies_to`

压电陶瓷看 d33 / kp / Qm，压电泵看流量 / 静压 / 谐振频率——**两套参数体系完全不通用**。若不按产品大类裁剪指标行，跨类对比会退化成一张大面积空白的表。

页面按所选型号的产品大类取指标并集：只选气泵就只看到气泵的 11 项；同时选陶瓷与气泵，则两类指标都出现（各自另一列为「—」）。这是刻意的——它把「这两个东西不可直接比较」可视化出来，而不是悄悄给出一个错误结论。

### 已知的数据口径

| 公司 | 来源 | confidence | verified |
|---|---|---|---|
| 村田（MZB 系列） | 官网「微型风扇（空气泵）」参数表 | 1.0 | 是 |
| 巴特尔斯（mp6 系列） | mp6 Datasheet v2.1 | 0.95 | 是 |
| 摩根（PZT-4/5H/8） | Uchino《Ultrasonic Transducers》表 3.3 典型值 | 0.90 | 否 |
| xMEMS / Frore | 2026 固态散热横向对比（转引厂商数据） | 0.75 | 否 |

`verified=否` 的数值会在单元格右上角标橙点，提示**投放使用前必须用厂商现行数据手册逐项复核**。

### P0 的一个明确取舍

一条 `ProductSpec` 只对应「(产品, 指标) 一个值」，测试条件以文本随值保存（如 `@15Vpp`、`水·250Vpp/100Hz`）。

理由：多维条件基数会让最优值计算和表格布局迅速复杂化，而 P0 更需要结果可读、可校验。需要多条件时，**先把条件并入指标定义**（如「插入损耗 @2.4GHz」），P1 再引入 `condition_json` 做透视。

另外，厂商常以区间给出参数（如谐振频率 `24~27 kHz`、驱动电压 `11.5~21 Vdc`），这类指标用 `value_type=text` 存原文、`better=na` 不判定优劣——把区间硬压成一个数就是造假。

---

## 用真实数据替换示例

### 方式一：CSV 导入（推荐，Excel 另存为 CSV 即可）

```bash
curl -X POST http://127.0.0.1:8000/api/import -F "file=@specs.csv"
```

格式（表头必须存在，顺序不限）：

```csv
型号,友商,指标,数值,单位,条件,来源链接,置信度
MZB6001T01,村田制作所,空气流量,1.2,L/min,@15Vpp,https://www.murata.com/...,0.95
MZB6001T01,村田制作所,静压,2.5,kPa,@15Vpp,https://www.murata.com/...,0.95
mp6-liq,巴特尔斯微技术,液体流量,8000,µL/min,水·250Vpp/100Hz,https://...,0.95
```

指标列可直接写 `key`（如 `flow_air`）或中文名（如 `空气流量`）。

模板下载：`GET /api/import/template`

三条约定：

1. **指标按 key 或中文名匹配字典；匹配不到直接报错跳过，绝不自动新建**。指标字典必须先想清楚，否则会退化成每个友商一套字段，半年后无法横向对比。
2. 产品按型号匹配，不存在则连同友商一起创建。
3. 数值留空 = 该型号未公开此项，跳过写入，不覆盖已有值。

### 方式二：改 `app/seed.py`

改 `METRICS` / `ENTITIES` / `PRODUCTS` / `SPECS` 四个常量，然后：

```bash
python -m app.seed --force
```

---

## API

| 方法 | 路径 | 用途 | 需要权限 |
|---|---|---|---|
| POST | `/login` · `/logout` | 登录 / 登出 | — |
| GET | `/compare?p=1,2,3` | 对比页（服务端渲染） | `data:view` |
| GET | `/api/matrix?p=1,2,3` | 矩阵 JSON，供前端替换或 MCP 复用 | `data:view` |
| GET | `/api/metrics` | 指标字典 | `data:view` |
| GET | `/api/products` | 产品清单 | `data:view` |
| GET | `/api/trace/{spec_id}` | 单条参数溯源 | `data:view` |
| GET | `/export.csv?p=...` | 导出对比表 | `data:export` |
| GET | `/datasheet` | 规格书导入页 | `data:write` |
| POST | `/api/datasheet/analyze` | 上传规格书，返回识别候选（**不落库**） | `data:write` |
| POST | `/api/datasheet/commit` | 提交复核后的参数，写入并标记未复核 | `data:write` |
| GET | `/datasheet/{doc_id}/file` | 下载原始规格书（溯源链接指向这里） | `data:view` |
| POST | `/api/import` | CSV 导入 | `data:write` |
| GET | `/api/import/template` | CSV 模板 | `data:write` |
| GET | `/admin/users` | 账号管理页 | `admin:users` |
| POST | `/admin/users/create` · `/{uid}/role` · `/{uid}/toggle` · `/{uid}/password` | 建号 / 改角色 / 停用启用 / 重置密码 | `admin:users` |
| GET | `/admin/settings` | 系统设置页 | `admin:settings` |
| POST | `/admin/settings` | 保存系统参数 | `admin:settings` |
| GET | `/healthz` | 健康检查（容器探针，不鉴权） | — |

所有 POST 均需带 CSRF 令牌：表单加隐藏字段 `_csrf`，`fetch` 加请求头 `X-CSRF-Token`。

服务端渲染是有意选择：P0 不引入前端构建链，一条命令即可启动。P1 若要换成 Next.js，只需消费 `/api/matrix`，后端契约不变。

---

## Docker 部署

**一个镜像即可承载全部功能。** 展示层是服务端渲染（Jinja2 + 原生 JS），没有 Node 构建阶段，也不需要第二个镜像。

镜像内已处理：非 root 用户（uid 10001）、时区、数据卷、健康检查、启动时等待 PostgreSQL、首次启动自动灌示例数据。

### 方式一：单容器 + SQLite（零配置，推荐先跑通）

```bash
docker compose up -d --build
```

打开 <http://127.0.0.1:8000/compare>。数据落在命名卷 `competitive-board-data`（SQLite 数据库 + 上传的规格书）。

### 方式二：加 PostgreSQL（团队共用 / 数据量上来后）

```bash
cp .env.example .env        # 填 POSTGRES_PASSWORD：openssl rand -base64 24
docker compose -f docker-compose.yml -f docker-compose.pg.yml up -d --build
```

两个文件是叠加关系：`docker-compose.pg.yml` 只负责追加 `db` 服务并把 `DATABASE_URL` 注入 `app`。切换数据库不改一行业务代码。

### 方式三：不用 compose 的裸镜像

```bash
docker build -t competitive-board:0.2.0 ./backend

docker run -d --name competitive-board \
  -p 127.0.0.1:8000:8000 \
  -e CIB_SEED_DEMO=1 \
  -e TZ=Asia/Shanghai \
  -v competitive-board-data:/app/data \
  --restart unless-stopped \
  competitive-board:0.2.0
```

### 环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `CIB_DATA_DIR` | `/app/data` | 数据目录（SQLite + 规格书文件） |
| `DATABASE_URL` | 空 | 空则用 SQLite；填 `postgresql+psycopg://…` 则用 PG |
| `CIB_SEED_DEMO` | `1` | 首次启动写入示例数据；库里已有数据自动跳过 |
| `CIB_ADMIN_USER` | `admin` | 初始管理员账号（仅当库里无管理员时生效） |
| `CIB_ADMIN_PASSWORD` | 空 | 留空则随机生成并打印到日志：`docker compose logs app \| grep 初始密码` |
| `CIB_SESSION_HOURS` | `12` | 会话有效期（小时），运行时可在系统设置里改 |
| `CIB_COOKIE_SECURE` | `0` | HTTPS 部署必须置 `1`，否则 Cookie 会明文传输 |
| `CIB_MAX_COMPARE` | `8` | 单页最多对比型号数的初始默认 |
| `CIB_FLOAT_EPS` | `1e-9` | 数值相等判定容差 |
| `APP_PORT` | `8000` | 宿主端口（只绑 127.0.0.1） |
| `POSTGRES_PASSWORD` | — | 启用 PG 时必填 |

### 常用运维

```bash
docker compose logs -f app                       # 日志
docker compose ps                                # 健康状态（healthy 表示 /healthz 通过）
docker compose exec app python -m app.seed --force   # 重置为示例数据（会清空现有数据）
docker compose down                              # 停止，数据保留
docker compose down -v                           # 连数据卷一起删（不可恢复）
```

备份：

```bash
# SQLite 直接打包数据卷
docker run --rm -v competitive-board-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/cb-data-$(date +%F).tar.gz -C /data .

# PostgreSQL
docker compose -f docker-compose.yml -f docker-compose.pg.yml exec -T db \
  pg_dump -U ciboard -d ciboard | gzip > cb-$(date +%F).sql.gz
```

### 三个要注意的点

1. **数据目录优先用命名卷，不要 bind mount 宿主目录。** 当前环境是 Windows + Docker Desktop，bind mount 对 SQLite 的 I/O 折损明显，还可能遇到文件锁与权限问题。需要取文件时用 `docker cp`。
2. **端口默认只绑 `127.0.0.1`。** 要对外访问必须在前面加反向代理并启用认证——应用本身没有登录体系。
3. **`--workers` 固定为 1。** SQLite 写入与规格书落盘都在进程内，多 worker 会争锁。切到 PostgreSQL 后可自行调大，但需确认上传文件落盘路径共享。

### 与 `deploy/` 的关系

| 目录/文件 | 内容 | 何时用 |
|---|---|---|
| `docker-compose.yml` | 仅本应用（SQLite） | 日常开发、单机试用 |
| `docker-compose.pg.yml` | 叠加 PostgreSQL | 团队共用 |
| `deploy/docker-compose.yml` | 完整采集栈（RSSHub / changedetection / Redis / PG） | 启用新闻动态流之后 |

采集栈里本应用对应 `api` 服务，`app` profile 才会启动，详见 `deploy/README.md`。

> ⚠️ 本机没有 Docker CLI，以上编排只做了静态校验（YAML 结构、引用文件存在性、入口脚本 `bash -n` 与三场景实跑）。**镜像构建未在本机执行**，首次 `docker compose up --build` 若有报错请贴给我。

---

## 测试

```bash
cd backend
pip install -r requirements-dev.txt   # 额外装 httpx，生产镜像不需要
python tests/test_compare.py       # 20 项：对比逻辑与 CSV 导入
python tests/test_datasheet.py     # 18 项：规格书识别
python tests/test_auth.py          # 25 项：权限体系（含接口层拦截与 CSRF）
node tests/js/test_datasheet_ui.js # 7 项：导入页交互（无浏览器依赖）
# 或 pytest tests/
```

覆盖内容：

- **对比逻辑**：最优值方向与并列、单一值不给最优、`target`/文本指标不判定、缺失不计零、容差不误判、完整度、指标按大类裁剪、CSV 导入四类错误
- **规格书识别**：脚注防护、单位换算、区间取上限、修饰语降权、别名不重复计数、多页页码
- **权限体系**：密码哈希与策略、角色→权限位边界、匿名访问的页面跳转与接口 401、普通用户在页面与接口层被 403 拦截、导航项隐藏、CSRF 缺失被拒、停用账号会话立即失效、最后一个管理员不可降级/停用、系统参数改完即时生效
- **导入页交互**（Node + 最小 DOM 桩）：按钮未就绪时点击必须给出「缺什么」的提示而不是静默无反应、条件齐备后进入就绪态并发出带 CSRF 头的请求、超大文件被拦截、无产品时给前置提示

`test_auth.py` 使用独立临时数据目录，不碰开发库。

---

## 暂未实现

- 网页参数自动抓取（changedetection.io 已就绪，接入在 P1）
- 多条件参数透视（如同时看 @1.9GHz 与 @2.4GHz）
- 细粒度权限（当前是两角色；如需「只读某几个友商」这类行级控制，需在 `auth` 之上再加数据域）
- 操作审计日志（改参数、改账号目前只有 `updated_by` 字段，无完整审计流）
- 参数变更历史与趋势（表结构已留 `captured_at`，视图未做）
- 新闻动态流（第一部分）

---

## 目录

```
backend/
├── app/
│   ├── models.py         数据模型 + 指标字典设计取舍
│   ├── auth.py           密码 / 会话 / 权限位与角色映射 / CSRF
│   ├── settings_store.py 运行期系统参数（白名单 + 校验 + 回落默认值）
│   ├── compare.py        纯计算：最优值 / 差异 / 完整度（无框架依赖，可直接单测）
│   ├── datasheet.py      规格书识别引擎（PDF 解析 + 别名匹配 + 单位换算）
│   ├── importer.py       CSV 导入与校验
│   ├── seed.py           示例数据
│   ├── main.py           路由 + 鉴权异常处置
│   ├── templates/        登录 / 对比 / 规格书 / 账号管理 / 系统设置 / 403 / 空状态
│   └── static/           app.css, compare.js, datasheet.js（零依赖）
├── tests/                test_compare.py, test_datasheet.py, test_auth.py
├── tools/                make_sample_datasheet.py（生成合成规格书，用于验证识别链路）
├── requirements.txt
└── Dockerfile
deploy/                   Docker 编排（infra + app 双 profile）
```
