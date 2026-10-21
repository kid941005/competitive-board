# 竞品情报看板

[![build-image](https://github.com/kid941005/competitive-board/actions/workflows/build-image.yml/badge.svg)](https://github.com/kid941005/competitive-board/actions/workflows/build-image.yml)

压电领域友商竞品情报看板。当前已实现**第二部分：性能参数横向对比**（P0），覆盖三类器件：

| 产品大类 | 代码 | 型号数 | 友商 |
|---|---|---|---|
| 压电气泵 | `pump_air` | 6 | 村田 MZB 系列 4 型、巴特尔斯 mp6-gas / gas+ |
| 压电风扇 | `fan` | 2 | xMEMS XMC-2400、Frore AirJet Mini G2 |
| 压电液泵 | `pump_liquid` | 3 | 巴特尔斯 mp6-liq / pi / pp |

压电陶瓷（材料牌号 PZT-4 / 5H / 8，d33/Qm 那套典型值表）**不在本看板范围内**——
它是材料，不是器件，规格书形态与器件 datasheet 不同，混进来只会让指标池变成大片空白。

第一部分「友商 / 客户关联新闻动态」尚未开始；采集栈的编排草稿在 `deploy/`（含 RSSHub / changedetection），
**不参与看板本体部署**，看板用仓库根目录的单镜像编排即可。

---

## 快速开始

有仓库，一条命令拉镜像起服务：

```bash
docker compose up -d
```

没有仓库、只想跑起来：

```bash
docker run -d --name competitive-board \
  -p 127.0.0.1:8000:8000 \
  -v competitive-board-data:/app/data \
  ghcr.io/kid941005/competitive-board:0.9.9
```

镜像公开可拉，无需登录。首次启动的初始管理员密码在 `docker compose logs app`（或 `docker logs competitive-board`）里。
更多部署方式见 [Docker 部署](#docker-部署)。

### 默认是空库

启动后只有表结构和一个管理员账号，**没有任何示例数据**。第一次登录会看到一页提示：

> 这是一个空库 —— 还没有指标字典。对比页的每一行都来自字典，规格书识别也靠它做别名匹配。

先到**系统设置 → 指标字典 → 应用内置压电指标模板**，写入 17 个指标（含适用大类），
它**只写字典，不创建任何公司或型号**。之后用「批量导入」或「产品管理」录自己的数据。

想直接看到压电领域的示例公司/型号（10 家公司 / 11 个型号 / 81 条参数），启动时加 `CIB_SEED_DEMO=1`。

从源码跑：

```bash
cd backend

# 1) 写入指标字典（17 个指标，不含任何公司/型号）
python -m app.seed --metrics-only
#    想要完整示例数据（含 10 家公司 / 11 个型号 / 81 条参数）：python -m app.seed

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
| 账号管理：新建、改账号名/显示名、改角色、停用、重置密码 | `admin:users` | ✓ | ✗ |
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
- **改账号名不吊销会话**：会话按用户 ID 关联，不认账号名，所以管理员改自己的名字不会掉线；
  但旧账号名**立即无法登录**，新名生效。变更日志里的 `actor` 保留当时的名字（历史就是历史）
- 登录失败对「账号不存在」与「密码错误」返回同一提示，且都走一次哈希校验，避免时间侧信道枚举账号
- **登录失败限速**：按「IP + 账号」计数，连续失败 5 次后指数退避（30 秒起，封顶 15 分钟），
  成功即清零；同一 IP 对所有账号的失败总量另设上限（防换着用户名扫）。命中时返回 429 + `Retry-After`。
  状态在进程内存里 —— 单容器（`--workers 1`）够用，多副本部署要换成共享存储

### 防锁死

不能取消自己的管理员权限、不能停用自己的账号、系统至少保留一名启用状态的管理员。

---

## 页面能做什么

| 功能 | 说明 |
|---|---|
| 型号多选 | **默认收起**（只留一行「已选 N：型号…」），点「展开选择」才铺开；先按 **压电气泵 / 压电风扇 / 压电液泵** 分段、再按友商分组勾选，最多 8 个型号（`CIB_MAX_COMPARE` 可调）。同一次会话内记住展开状态，连点多个型号不用反复展开 |
| 最优值高亮 | 依指标字典的 `better` 方向自动判定；并列时同时高亮；只有一个值时**不标最优** |
| 差异过滤 | 「只看有差异」按容差判定，1.50 与 1.5 不算差异 |
| 缺失过滤 | 「只看有缺失」——谁在藏参数，本身即情报 |
| 完整度 | 每个型号表头的进度条 = 已公开指标占比 |
| 表头吸顶 | 参数表整块吸附在视口顶部，页面怎么滚，产品型号那一行都留在最上方；选择器展开或收起都不影响（窄屏自动退回普通流） |
| 型号搜索 | 选择器顶部按 **公司名 / 型号 / 产品名** 过滤，空格分隔多个词按 AND；`/` 聚焦、`Esc` 清空 |
| 指标搜索 | 支持中文指标名即时过滤 |
| 溯源浮层 | 点击任意数值 → 取值 / 测试条件 / 置信度 / 是否复核 / 抓取时间 / 来源链接 |
| 导出 | **Excel（带格式：最优加粗、缺失「—」、完整度行、说明页）**、CSV（带 BOM）、JSON |
| 规格书导入 | 上传 PDF，自动识别参数，人工复核后入库（见下节） |
| 批量导入 | Excel / CSV 一次写入多型号多参数，自动建档（见下节） |
| 产品管理 | 管理员直接改型号元信息与参数值、增删参数、标记复核、删除型号；列表按大类分段，支持关键词 + 公司 + 大类筛选；型号详情页可记录**客户采用**（谁在用这款泵） |
| AI / 脚本调用 | `/api/v1` 独立接口面：令牌鉴权、统一信封、幂等重试、dry-run 预演、批量与增量（见下节） |
| 变更日志 | 记录「谁 / 何时 / 从哪个入口 / 把什么从多少改成了多少」，含删除快照（见下节） |
| 页脚版本号 | 每页底部显示当前运行的 `vX.Y.Z`（含登录页），`/api/v1/health` 也回 `app_version` |

### 搜索与筛选

两个入口，都是**本地过滤**（数据已经在页面里，不发请求、不丢勾选状态）：

| 位置 | 搜什么 | 交互 |
|---|---|---|
| 对比页 · 选择对比型号 | 公司名 / 型号 / 产品名 | 输入即筛，公司行自动收起；`/` 聚焦，`Esc` 清空；顶部显示「匹配 N 个」 |
| 产品管理 · 全部型号 | 公司名 / 型号 / 产品名 + 公司下拉 + 大类下拉 | 输入即筛，三个条件叠加；无结果时给一行提示 |

AI 侧的对应入口是 `/api/v1/products?q=`、`/entities?q=`、`/metrics?q=` —— 语义与页面一致：
大小写不敏感、空格分隔多个词按 AND、`%` 与 `_` 按字面量匹配（不会被当成通配符）。
命中的字段会在结果里回 `matched`，例如 `["company","model"]`。

---

## 导入规格书，自动识别参数

入口：<http://127.0.0.1:8000/datasheet>（对比页右上角「导入规格书」）

流程三步：**公司 / 大类 / 型号 → 上传 PDF → 复核后写入**。识别结果**不会直接入库**，必须由人勾选确认。

### 为什么是「公司 + 大类 + 型号」而不是「先选已有型号」

这个入口的定位是**扩充数据库**：新发现的友商、新型号，都是从规格书进来的。所以公司和型号都允许不存在，提交时就地建档。

三格信息各有分工，缺一不可：

| 选择 | 约束什么 | 能否省 |
|---|---|---|
| 产品大类 | **指标池**（17 项 → 8~11 项） | 不能省，它是准确率的前提 |
| 公司 | 型号候选范围 | 可省（能自动推断），但省掉后型号选择面变大 |
| 型号 | 落库位置 `(product_id, metric_key)` | 不能省，但可新建 |

**只选公司是不够的**：公司是组织维度，指标适用性是大类维度，两者不正交。例如巴特尔斯既做气泵（mp6-gas）又做液泵（mp6-liq），按公司取指标池会同时纳入 `flow_air` 与 `flow_liquid`，而这两者的别名都是「流量」——一处文本会产出两条互斥候选，且都带 0.9 置信度。

**逐项提示**：填表时实时显示「将新建公司 / 已有型号，将更新」以及「该型号属于其他大类，提交会被拒绝」，不必等到提交才发现建错。

### 建档规则

- **公司同名直接归入**：「村田制作所」已存在时再填一次，不会建第二份档案。
- **公司近名要确认**：填「村田」会命中已有「村田制作所」，返回 409 并提示；确认是另一家时勾选「确认这是另一家公司」再提交。名称归一的前提是先别造出重复。
- **型号忽略大小写复用**：`mzB1001t02` 与 `MZB1001T02` 视为同一款，走更新。
- **同一型号不能跨大类**：`mp6-liq` 是液泵，按气泵导入会被 409 拒绝。

### 识别引擎怎么工作

`app/datasheet.py`，纯规则，不依赖大模型，因此结果可复现、可单测：

1. **限定指标范围**：只在该型号所属产品大类的指标里匹配。否则「流量」会在气泵（L/min）与液泵（µL/min）之间误配。
2. **别名匹配**：指标字典的 `aliases` 字段登记规格书里的常见写法（中英文、符号）。长别名优先，同一指标的别名命中的字符区间不重叠，避免「空气流量」与「流量」被算成两处。
3. **取值**：标签后可向后搜 90 字符，覆盖「标签在表头、数值在下一行」的版式。单位必须紧贴数值（最多隔两个空格），否则不认。
4. **单位换算**：mL/min → L/min、mbar → kPa、cm → mm 等显式换算并标记「换算」。
5. **区间与比较符**：`24~27 kHz` 存原文；`≥0.7` 把 `≥` 记入条件；数值型指标遇到 `0~70 ℃` 取上限并把原文写入条件。
6. **脚注与修饰语防护**：`(1)` 这类脚注不当取值；`压力型` 这种标签后紧跟「型/范围/比」的命中会降权并标「存疑」。

置信度分档：单位确认 **0.90**，比较符确认 **0.80**，仅数字 **0.70**；换算 −0.05，修饰语 −0.15。默认只勾选 ≥ 0.75 的行。

### 原文保留

上传的 PDF / 文本落在 `CIB_DATA_DIR/datasheets`（`<doc_id>.<ext>` + 同名 `.json`）。
**被参数引用的永久保留** —— 那是溯源链接的唯一凭据；上传了但没提交入库的，
超过 `datasheet.keep_days`（默认 30 天）在启动时清掉，连同解析结果一起删。
清理只删「没人引用且过期」的，不会碰任何已入库的出处。

### 复核页能改什么

每一行都可以改指标（下拉换成别的字典项）、改数值、改条件。写入后一律 `verified=False`，对比页会显示橙色「未复核」点，溯源浮层指向原 PDF 的对应页。

**已知边界**：扫描版 PDF（纯图片、无文本层）识别不到内容；三线表跨行合并的表头可能错位；一个指标在文档里出现多次时只保留置信度最高的一处（该处会标注「×N」提示重复出现）。

---


## 批量导入（Excel / CSV）

入口：`/import`（导航「批量导入」）。导入的用途是**扩充数据库**，所以型号和公司不存在时会自动建档。

模板：`/api/import/template.xlsx`（带「填写说明」页）、`/api/import/template.csv`

### 模板是宽表：一行一个型号

列：`企业名称 / 产品型号 / 产品类别 / 流量L/min / 静压Pa / 谐振频率 / 驱动电压 / 功耗 / 噪音 / 外形尺寸 / 重量 / 温度范围 / 寿命 / 来源链接 / 置信度 / 关联客户（有公开证据）/ 口径与备注`，末尾补了 5 列字典里有、但原表没有的指标（自吸、可泵介质、接触介质材质、厚度mm、散热能力W）。

**流量与静压两列按大类自动映射**：液泵行的 `0.0035 L/min` 会落成 `3500 µL/min`、`120000 Pa` 落成 `1200 mbar`（字典里液泵的量级小 10⁴，单列一项才好读）；气泵与风扇则落到 `空气流量(L/min)` 与 `静压(kPa)`。所以一套列头能录全部三大类，不用为液泵换表。

为什么是宽表而不是「一行一个指标」的长表：业务方对着规格书登记，就是一行一个型号、指标横向铺开；长表录一个型号要来回写十几行，没人愿意用。

识别规则：表头必须同时含**企业名称**与**产品型号**（列名允许 `公司`/`友商`、`型号`、`大类` 等常见别名）；表头不必在第一行；**只读第一个工作表** —— 「客户-友商对照」「友商官网索引」这类辅助表不会被导入；多余的列直接忽略。

| 规则 | 说明 |
|---|---|
| 列头带单位就换算 | `静压Pa` 列填 `50,000` 会落成 `50 kPa`；液体流量µL/min、背压mbar、厚度mm、散热能力W 同理。**不要自己换算** |
| 「待补充」= 未公开 | 留空或写 待补充 / 待定 / 无，跳过不写、**不算错误**。区间取上限（`0~45 ℃` → 45），原文进测试条件 |
| 单位认不出或跨维度就报错 | 把 `kPa` 填进流量列 → 该格跳过并说明；**整行只有非法值时不建型号** |
| 指标必须适用于该大类 | 「液体流量」写进气泵型号会被跳过，否则会写进一条对比页永远不显示的参数 |
| 已有型号不能改换大类 | 表里写冲突的大类 → 整行跳过 |
| 关联客户拆成采用记录 | `华为（WATCH D2 拆解确认）；欧姆龙（HeartGuide 2019）` → 2 条采用记录，状态「传闻」，括注进备注 |
| 口径与备注写进型号备注 | 如「静压@19.5Vdc、流量@18Vdc」 |

**Excel 与 CSV 走同一套校验**，只在「读文件」这一步分叉：否则很容易出现「CSV 拦住了、Excel 放过去了」的差异，而 Excel 恰恰是业务同事最常用的格式。

导入的参数一律 `verified=False`，需在「产品管理」里逐项确认。**重复导入是安全的**：值与条件都没变的格会被跳过（报告里的「无变化跳过」），既不写库、也不清掉已复核标记。宽表没有「测试条件」列，所以**表里没给条件时不会清空已有条件** —— 否则导出一份再导入，全库的 `@15Vpp` 会被静默抹掉。

报错里的行号是**文件里的真实行号**（表头不在第一行也准）。程序生成的 xlsx（openpyxl / pandas）若用了公式且没被 Excel 保存过，公式列读出来是空值，导入前请「复制 → 粘贴为值」。

> 旧的长表（`型号/友商/指标/数值/单位/条件…`）已不再支持，导入会明确报「模板已改为宽表，请重新下载」。这是刻意的取舍：两套格式并存时，校验与单位换算要在两处保持一致，早晚会漂。

### 单位归一的真相源在 `app/units.py`

三条写入通道（表格导入、`/api/v1/specs/batch`、规格书识别）共用同一份换算表，不各写一套。

| 原文 | 落库 | 说明 |
|---|---|---|
| `1100 mL/min` | `1.1 L/min` | 流量按体积/时间换算，µL/min、m³/h、sccm 同理 |
| `500 mbar` | `50 kPa` | 压力同理（Pa / MPa / bar / psi / mmH₂O） |
| `±25V` | `50 Vpp` | ±X V 是峰值幅度，峰峰值乘 2；`11.5~21 Vp` → `23~42 Vpp` |
| `3.3Vrms` | 原样保留，标「待确认」 | Vpp 与 Vrms 的关系取决于波形（正弦 2√2、方波 2），不猜 |
| `11.5~21Vdc` | 原样保留 | 直流乘 2 变成「峰峰值」没有物理意义 |
| `1.5 kPa`（指标是流量） | 报错跳过 | 跨维度不换算，宁可少一格，不要错一个量级 |

换算过的格在导入报告里逐条列出（`静压：50000 → 50 kPa`），口径有歧义的（Vrms、只写 `V`）单独列在「单位口径需人工确认」里。

---

## 导出 Excel

`/export.xlsx?p=...`，对比页右上「导出 Excel」。三张工作表：

| 工作表 | 内容 |
|---|---|
| 参数对比 | 指标组 / 指标 / 单位 / 优劣 + 各型号一列；最简单优值加粗浅绿、缺失「—」灰字、未复核橙字；冻结 `E2`、首行可筛选、末行完整度 |
| 型号清单 | 公司 / 型号 / 大类 / 类别 / 完整度 / 已公开项数 |
| 说明 | 生成时间、最优与缺失的判定口径 |

不用 CSV 顶替的原因：CSV 承载不了「谁最优、谁没公开、谁没复核」这三层信息，回到 Excel 还得手工上色。

### 导出全部（备份）

对比页工具栏和「产品管理」页都有 **「导出全部（备份）」** → `/export-all.xlsx`。与上面那份的区别是范围而不是格式：那个只导当前选中的几个型号（是报表），这个不管选择状态，把库里所有数据倒出来（是备份）。四个工作表：

| 工作表 | 内容 | 用途 |
|---|---|---|
| 参数导入 | 一行一个型号的宽表，与导入模板同形 | **可直接再导入**，所以它同时是备份包与迁移包 |
| 参数明细 | 一行一个「型号 × 指标」：单位 / 测试条件 / 来源链接 / 置信度 / 复核状态 / 更新时间 | 核对与追责；宽表看不出的东西都在这里 |
| 客户采用 | 客户 / 型号 / 所属公司 / 状态 / 年份 / 备注 | 采用关系备份 |
| 说明 | 生成时间、条目数、口径说明、不含什么 | 防止半年后拿到文件不知道是什么 |

数值按**列头单位**导出（静压按 Pa、液体流量按 µL/min），再导入时由 `importer` 换算回系统口径，往返不漂 —— 测试里有一条专门断言「导出后原样再导入 = 零写入」。

**不含**：登录账号、API 令牌、变更日志、系统参数。这些不属于业务数据，账号与令牌请另行保管。

---

## 产品管理（管理员）

| 页面 | 能做什么 |
|---|---|
| `/products` | 全部型号清单（含完整度，**按压电气泵 / 压电风扇 / 压电液泵分段**）+ 新建型号 + 删除 |
| `/products/{id}` | 改型号元信息；逐项改参数值 / 测试条件 / 复核状态；清空数值即删除该项；**客户采用**：记录谁在用这款泵 |

五条实现约定：

- **表单里没出现的指标不动**。早期版本把「没提交」当成「清空」，只要漏渲染一项就会静默删参数——现在区分「空值」与「未提交」。
- **只有真改了的那几项被写**。值、条件、复核标记都没变的行会跳过（提示里显示「未变 N」）—— 早前实现会把整页参数回写一遍，导致「只改一格」也把其余各项的出处改成 manual、置信度提到 100%，溯源链接就此丢失。
- **数值型填了非数字**：不覆盖旧值，明确报错返回。
- **已有参数的型号不能换大类**，要改先清空参数。
- 人工改动记 `confidence=1.0`、来源 `manual`、`verified` 按勾选；普通用户访问一律 403。

一处容易混淆的地方：公司列表有**两份**。`/products` 的「按公司筛选」只列**已有型号**的公司（列一家 0 型号的进去，点开就是个空表格）；「新建型号」的公司下拉则保留全部（包括还没型号的 —— 那正是给它建第一个型号的入口）。`/compare` 的型号选择区同理，有型号才成行。

---

## AI / 脚本调用（`/api/v1`）

完整设计见 **[docs/AI-API-设计.md](docs/AI-API-设计.md)**。这里只说要紧的几条。

### 为什么不复用页面路由

页面路由是**表单 + 303 跳转 + CSRF 双重提交**：程序拿不到结构化结果，也过不了 CSRF。所以另开一层 JSON 接口，页面一条未动。

### 上手

1. 管理员进「API 令牌」页签发一个令牌（明文只显示一次）；
2. 带 `Authorization: Bearer <token>` 调用。

```bash
TOKEN=cib_xxxxxxxx_yyyyyyyy
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/v1/context
curl -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8000/api/v1/products?category=pump_air&limit=5"
curl -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json"   -d '{"items":[{"product_id":1,"metric_key":"flow_air","value_num":0.72,"verified":true}]}'   http://127.0.0.1:8000/api/v1/specs/batch
```

### 四组划分（按用途，不按对象）

| 组 | 接口 | scope |
|---|---|---|
| 查询 | `context` `metrics` `entities` `products` `matrix` `changes` | `read` |
| 写入 | `POST/PATCH/DELETE products`、`specs/batch`、`POST/PATCH entities` | `write` |
| 摄取 | `POST import`（与 Excel 导入同一套校验） | `ingest` |
| 采用 | `GET/POST /adoptions`、`PATCH/DELETE /adoptions/{id}`（谁在用哪款泵） | `read` / `write` |
| 发现 | `tools`（function-calling 清单）、令牌管理 | `read` / `admin` |

### 检索（`q` 参数）

| 接口 | `q` 匹配的字段 | 可与 `q` 叠加 |
|---|---|---|
| `GET /products` | 型号、产品名、公司中文名、公司英文名 | `entity_id` `category` `status` `cursor` |
| `GET /entities` | 公司中文名、英文名 | `kind` |
| `GET /metrics` | 指标 key、中文名、识别别名、指标组 | `category` |

规则统一在 `app/search.py`：去空白、限长 64、`%`/`_` 转义、多词 AND。结果里带
`matched` 说明命中了哪个字段，`meta.q` 回显实际生效的关键词（没传就是没有该字段）。

### 三个关键约定

| 约定 | 为什么 |
|---|---|
| 统一信封 `{ok,data,meta}` / `{ok:false,error:{code,message,hint}}` | `code` 是稳定枚举供程序分支；`message/hint` 给人看 |
| `dry_run=true` 预演 | 让 AI 先展示「将要改什么」，人确认后再落库 |
| `Idempotency-Key` | 重试不写两遍；返回 `meta.replayed=true` |

另有 `ETag`/`If-Match` 防覆盖、`/changes?since=` 做增量同步、`/tools` 供 function-calling 直接消费。

> 令牌走 Bearer 头、不走 Cookie，所以**不校验 CSRF**——浏览器侧的防护一条都没放松。

### 把访问权交给外部 AI 或脚本

需要给的只有两样：

| 给什么 | 例子 | 说明 |
|---|---|---|
| 地址 | `https://cib.example.com` | 给根地址即可，不要带 `/api/v1`（那是路径前缀） |
| 令牌 | `cib_8d42aa34_…` | 在「API 令牌」页签发；明文只显示一次 |

**不要给账号密码。** 密码能登整个后台、能改系统参数、能再签发令牌；令牌只能做 scope 内的事，且可以单独吊销 —— 这就是它存在的理由。

三条硬前提，缺一条对方就用不了：

1. **对方能访问到那个地址。** 内网地址（`192.168.*`、`10.*`、`172.16–31.*`）在别人机器上不存在，除非做了端口映射、VPN 或内网穿透；同一台机器上的 `127.0.0.1` 同理。
2. **走 HTTPS。** 令牌是明文请求头，HTTP 下等于明文传输。公网部署必须加反向代理并启用证书。
3. **scope 最小化。** 只读就给 `read`；要替你录数据才加 `write`；`admin` 能签令牌、看全部审计，只在写脚本自用时才给。

按用途分开签（「情报助手」只读、「录入助手」读写），出问题时吊销那一个，其余不受影响。

对方拿到地址与令牌后，第一步应该调 `GET /api/v1/tools` —— 那是机器可读的能力清单（含参数的 JSON Schema），比让人读文档可靠。

### 作为 MCP server 接入（已实现）

`mcp/cib_mcp.py` 把上面这套接口包成 MCP 工具，任何 MCP 客户端（WorkBuddy、Claude Desktop、
Cursor、Cline…）都能直接发现并调用，不用自己拼 HTTP。**单文件、零第三方依赖、stdio 传输。**

```json
{
  "mcpServers": {
    "competitive-board": {
      "command": "python",
      "args": ["<仓库路径>/mcp/cib_mcp.py"],
      "env": {
        "CIB_BASE_URL": "http://192.168.1.10:8000",
        "CIB_TOKEN": "cib_xxxxxxxx_yyyyyyyy"
      }
    }
  }
}
```

工具清单从 `GET /api/v1/tools` **动态拉取**（当前 14 个），看板改了接口它跟着变，
不存在两份真相。配置细节与故障排查见 [mcp/README.md](mcp/README.md)。

> **它不解决「外部 AI 访问不到你的内网」。** MCP server 跑在 AI 客户端那台机器上，
> 由它去访问看板 —— AI 与看板之间那段网络仍然要通。它的价值是「让 AI 知道有哪些操作」，
> 而不是「打通网络」。

---

## 变更日志

入口：`/admin/logs`（导航「变更日志」）。看的是**数据层面的变化**，不是请求流水。

### 记什么

| 字段 | 说明 |
|---|---|
| 时间 / 操作者 | 用户名；AI 调用记为令牌名，并标「令牌」。时间按部署时区显示（库里存 UTC，`TZ` 决定展示时区） |
| 来源 | 页面 / 规格书 / 表格导入 / API |
| 动作 | 新建 / 修改 / 删除 / 导入 |
| 对象 | 型号 / 参数 / 公司 / 令牌 / 系统参数 |
| 变更内容 | **`旧值 → 新值`**，逐字段展开，最多 200 项 |

覆盖的写入点：改型号元信息、改参数值/条件/**复核状态**、批量写参数、导入（逐项）、规格书提交、建/删型号、建公司、签发/吊销令牌、改系统参数。

### 四条设计取舍

1. **记旧值，不只记动作**。「admin 更新了 MZB1001T02」在追查时没有用；要能回答「改成多少、原来多少、谁改的」。
2. **值与条件分开记**。只改 `@15Vpp → @18Vdc` 时，不应表现为「值从 0.7 变成 0.72」。
3. **删除留快照**。删之前把型号元信息和全部参数值写进日志 —— 否则删完就再也说不清丢了什么。
4. **没变化就不写**。重复提交同一份数据不产生日志，避免把真正的变更淹掉。
5. **复核状态单独记**。`已复核` 是「有人为这个数字背书」的唯一信号，只改勾选不改值时也必须留一条 `复核 否 → 是`。

### 几条使用建议

- 按「导入」筛 + 关键词搜型号，可以看清一次导入具体改了哪些格；
- 发现某个数值可疑，按型号搜，能看到它被谁改过几次、每次从多少到多少；
- 日志本身也会增长，默认保留 180 天（`audit.retention_days` 可调），启动时自动清一次，页面也有手动清理按钮。

> 日志要求**管理员**权限：它包含所有人的操作记录，属于敏感数据。AI 侧对应 `/api/v1/audit`，需要 `admin` scope。

---

## 数据模型

```
Entity（公司：自研 / 友商 / 客户）
  └─ Product（型号）
        └─ ProductSpec（参数值）──► SpecMetric（指标字典）
                               └──► SpecSource（来源 + 抓取时间）
        └─ Adoption（客户采用关系）──► Entity（kind=customer）
```

`kind=self` 的那家公司（左蓝微）在页面上**直接用公司名**，不加「本方 / 我方」之类的徽标：
对比页的型号选择器里，友商带「友商」标签，自家公司只显示名称；导入页与产品管理页的公司下拉同样不带后缀。

**指标字典是全项目的核心资产**，其中最关键的三个字段：

- `better`：`higher` / `lower` / `target` / `na`，决定最优值方向。`target` 与 `na` 不参与最优判定。
- `precision`：显示精度，同时作为差异判定的量化粒度。
- `applies_to`：该指标适用于哪些产品大类（逗号分隔），留空表示全类通用。

### 哪些指标覆盖全部大类（v3 / v4）

流量与静压**不按大类分家**：一套 `流量(L/min)`、`静压(kPa)` 覆盖气泵与风扇（液泵另有 µL/min 与 mbar 一项，量级差 10⁴），跨类对比时每个物理量只有一行 —— 这是 v3 合并的结果。

v4 又把四个**通用**指标放宽到三个大类：谐振频率、工作温度上限、寿命、噪音。依据是业务方实际登记的数据 —— 风扇与液泵同样有这四项，原来只标了气泵，导入时会被「不适用于该大类」整格拦掉。

### 流量与静压为什么不再按大类分家（v3）

原来气泵一套（`flow_air` / `pressure_air`）、风扇一套（`flow_fan` / `pressure_fan`）。问题是
**选一个气泵 + 一个风扇做对比时，表里会出现两行流量、两行静压** —— 物理量明明是同一个
（流量 L/min、压差 kPa），只因指标是按大类各建的，横向对比就读不出一条可比的行。

v3 把风扇那套并进气泵那套：`flow_fan → flow_air`、`pressure_fan → pressure_air`
（**1300 Pa → 1.3 kPa**，同一物理量，不是近似），两个指标各自补上 `fan` 适用大类。
液泵仍然单列 —— 它的量级差 10⁴（µL/min 与 mbar），混在一起没法读。

同时删掉了「真空度」：全库只有村田一条、且它来自官网参数表而非规格书，其余型号全空 ——
留在表里永远是一行孤点。泵「吹」的能力由静压代表，「吸」的能力等有真实数据再单独建模。

### 客户采用关系

一张 `adoptions` 表记「哪家客户在自己的产品里用了哪款泵」：客户 × 型号 × 进度 × 年份 × 备注。
状态是**进度**不是优劣：传闻 → 送样 → 导入设计 → 量产 → 已停用。

客户不另建主数据，就是 `kind=customer` 的公司实体 —— 同一家既可能是友商又可能是客户，
目前一个 `kind` 只能选一个，这是已知取舍。同一客户对同一型号只有一条记录（唯一索引兜底），
状态变了是「改那一条」，不是再记一条。

### 表结构与升级

`init_db()` 做两件事：先 `create_all` 补齐缺的表，再 `schema.migrate()` 把库升到
`CURRENT_SCHEMA_VERSION`。规则与 Alembic 一致，但只有 150 行：

1. 版本号记在 `schema_meta` 表里；
2. 每一步在自己的事务里执行，失败即回滚，不会留下半套结构；
3. **库比代码新就拒绝启动** —— 老镜像配新库，等到查询时才报 `no such column` 是最难查的故障。

v2 给两处加了唯一索引，并且**先合并存量重复再建索引**（顺序反了，启动会直接失败）：

| 索引 | 覆盖 | 为什么 |
|---|---|---|
| `ux_products_entity_model` | `products (entity_id, lower(trim(model)))` | 同一家公司内型号唯一；忽略大小写与首尾空白，`MP6` 与 `mp6 ` 是同一条 |
| `ux_specs_product_metric` | `product_specs (product_id, metric_id)` | 一格只能有一个值，否则矩阵按字典覆盖、静默丢一条 |

遇到重复数据时，迁移保留参数最多的一条、把不冲突的格子迁过去、写一条 `actor=system`
的审计（`origin=migration`），然后才建索引。所有 DDL 都带 `IF NOT EXISTS`，重复执行安全。

### 为什么要 `applies_to`

气泵看流量（L/min）/ 静压（kPa），风扇看风量 / 厚度，液泵看 µL 级流量 / 背压——**三套口径完全不通用**。不按大类裁剪，跨类对比会退化成一张大面积空白的表。

更关键的是别名会串味：「流量」同时是 `flow_air`（L/min）和 `flow_liquid`（µL/min）的识别别名，「压力」同时属于 `pressure_air` 和 `pressure_liquid`。池子混在一起时，一处文本会产出多条互斥候选，还都带 0.9 置信度。**按大类裁剪是识别准确率的前提，不是界面优化。**

页面按所选型号的产品大类取指标并集：只选气泵就只看到气泵的 11 项。

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

**驱动电压为什么是文本型**：它有三种互相不能换算的口径 —— 交流峰值（`Vp` / `Vpp`，Vp→Vpp 乘 2 是定义）、交流有效值（`Vrms`，换算要看波形）、直流（`Vdc`，乘 2 变成峰峰值没有意义）。这几家的数据正好三种都有（村田 MZB 是 Vdc、巴特尔斯 mp6 是 Vpp、xMEMS 写 Vpp），所以字典里单位保持 `V`、由值与溯源说明口径；导入时只做定义性的换算（Vp→Vpp、mV→V），其余原样保留并在报告里标「待人工确认」。

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
| POST | `/api/datasheet/analyze` | 上传规格书，返回识别候选与目标归属（**不落库**）。表单字段：`entity_id` 或 `entity_name`、`category`、`model` | `data:write` |
| POST | `/api/datasheet/commit` | 提交复核后的参数；公司/型号不存在时**就地建档**，返回 `entity_created` / `product_created` | `data:write` |
| GET | `/datasheet/{doc_id}/file` | 下载原始规格书（溯源链接指向这里） | `data:view` |
| GET | `/import` | 批量导入页（Excel / CSV） | `data:write` |
| POST | `/api/import` | 表格导入，`.xlsx` / `.xlsm` / `.csv` | `data:write` |
| GET | `/api/import/template.xlsx` | Excel 模板（含填写说明） | `data:write` |
| GET | `/api/import/template` | CSV 模板 | `data:write` |
| GET | `/export.xlsx?p=...` | 导出 Excel 对比表 | `data:export` |
| GET | `/products` | 产品管理页 | `data:write` |
| POST | `/products` | 新建型号 | `data:write` |
| GET | `/products/{id}` | 型号编辑页 | `data:write` |
| POST | `/products/{id}` | 保存型号元信息 | `data:write` |
| POST | `/products/{id}/specs` | 保存参数值 / 条件 / 复核状态 | `data:write` |
| POST | `/products/{id}/delete` | 删除型号及其参数 | `data:write` |
| GET | `/admin/users` | 账号管理页 | `admin:users` |
| POST | `/admin/users/create` · `/{uid}/profile` · `/{uid}/role` · `/{uid}/toggle` · `/{uid}/password` | 建号 / 改账号名与显示名 / 改角色 / 停用启用 / 重置密码 | `admin:users` |
| GET | `/admin/settings` | 系统设置页 | `admin:settings` |
| POST | `/admin/settings` | 保存系统参数 | `admin:settings` |
| GET | `/healthz` | 健康检查（容器探针，不鉴权） | — |

所有 POST 均需带 CSRF 令牌：表单加隐藏字段 `_csrf`，`fetch` 加请求头 `X-CSRF-Token`。

服务端渲染是有意选择：P0 不引入前端构建链，一条命令即可启动。P1 若要换成 Next.js，只需消费 `/api/matrix`，后端契约不变。

---

## Docker 部署

**一个镜像即可承载全部功能。** 展示层是服务端渲染（Jinja2 + 原生 JS），没有 Node 构建阶段，也不需要第二个镜像。

镜像内已处理：非 root 用户（uid 10001，可在 compose 的 `user:` 行改）、时区、数据卷、健康检查、启动时等待 PostgreSQL。默认不灌示例数据，见上文「默认是空库」。

镜像名统一为 `ghcr.io/kid941005/competitive-board`，由 GitHub Actions 在 `v*` 标签或手动触发时构建：**先跑静态检查与 310 项测试，通过才推**。标签有 `0.9.9`（版本号）、`latest`、`sha-<7位>`，仅 amd64，公开可拉、无需登录。

版本直接写在 `docker-compose.yml` 的 `image:` 那行，当前 `0.9.9` —— **固定版本而不是跟 `latest`**，避免某次 `up` 之后环境悄悄变了。升级就改这一行再 `docker compose pull`。

### 方式一：compose 拉镜像（推荐）

```bash
docker compose up -d
docker compose logs -f app      # 首次启动的初始管理员密码在这里
```

打开 <http://127.0.0.1:8000/compare>。数据落在命名卷 `competitive-board-data`（SQLite + 上传的规格书）。

### 方式二：compose 从源码构建

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

默认编排里**故意不写 `build`**：这样 `up` 只会拉镜像，不会出现「以为在跑自己改的代码，其实跑的是 Registry 上的」。要构建时显式叠加 `docker-compose.build.yml`，构建结果仍打同一个镜像名，不产生两套标签。

### 方式三：compose + PostgreSQL（团队共用 / 数据量上来后）

不用 .env。把 `docker-compose.pg.yml` 里三处 `ciboard-change-me` 一起改成你自己的密码，
建议用 `openssl rand -base64 24` 生成：

```bash
docker compose -f docker-compose.yml -f docker-compose.pg.yml up -d
```

⚠️ PostgreSQL 的密码**只在数据目录首次初始化时生效**。`pgdata` 卷已存在时改这里不起作用，
需要先进库 `ALTER USER ciboard WITH PASSWORD '…'`，或删卷重建（会丢数据）。

叠加关系：`docker-compose.pg.yml` 只追加 `db` 服务并把 `DATABASE_URL` 注入 `app`。切换数据库不改一行业务代码。要和本地构建一起用，三个文件按顺序叠加即可。

### 方式四：不用 compose 的裸镜像

拉现成的：

```bash
docker run -d --name competitive-board \
  -p 127.0.0.1:8000:8000 \
  -e TZ=Asia/Shanghai \
  -v competitive-board-data:/app/data \
  --restart unless-stopped \
  ghcr.io/kid941005/competitive-board:0.9.9
```

自己构建（标签沿用同一命名规则，便于之后切回官方镜像）：

```bash
docker build --build-arg APP_VERSION=0.9.9 -t competitive-board:0.9.9 ./backend

docker run -d --name competitive-board \
  -p 127.0.0.1:8000:8000 \
  -v competitive-board-data:/app/data \
  --restart unless-stopped \
  competitive-board:0.9.9
```

### 配置项在哪改

**没有 `.env` 文件**。所有配置都以字面值写在 `docker-compose.yml` 里，改哪个值就改哪一行，然后 `docker compose up -d` 生效。

刻意不用「环境变量 + 默认值兜底」那种写法（`${VAR:-default}` 配 `.env`）：那会让人以为改了 compose 就生效，实际被 `.env` 里的同名项悄悄覆盖 —— 这类问题排查起来很费时间。

| 位置 | 当前值 | 作用 |
|---|---|---|
| `image:` 的标签 | `0.9.9` | 镜像版本。升级就改这里，再 `docker compose pull` |
| `user:` | `10001:10001` | 容器运行身份。数据目录属主是别的 uid 时（NAS 共享目录常见）改这里，就不必 `chown` 目录；改 `"0:0"` 则以 root 运行 |
| `ports:` | `127.0.0.1:8000:8000` | 宿主端口。只绑回环，对外访问需反向代理 |
| `CIB_DATA_DIR` | `/app/data` | 数据目录（SQLite + 规格书文件） |
| `CIB_SEED_DEMO` | `0` | 设为 `1` 才灌压电示例公司/型号；默认 `0` 是空库 |
| `CIB_ADMIN_USER` | `admin` | 初始管理员账号（仅当库里无管理员时生效） |
| `CIB_ADMIN_PASSWORD` | 空 | 留空则随机生成并打印到日志：`docker compose logs app \| grep 初始密码` |
| `CIB_SESSION_HOURS` | `12` | 会话有效期（小时），运行期可在系统设置里改 |
| `CIB_COOKIE_SECURE` | `0` | HTTPS 部署必须改 `1`，否则 Cookie 会明文传输 |
| `CIB_MAX_COMPARE` | `8` | 单页最多对比型号数的初始默认 |
| `CIB_DATASHEET_DAYS` | `30` | 未入库规格书的保留天数（运行期可在系统设置里改） |
| `DATABASE_URL` | 未设 | 不设即 SQLite。启用 PG 时由 `docker-compose.pg.yml` 叠加注入 |
| `POSTGRES_PASSWORD` | `ciboard-change-me` | 仅 PG，在 `docker-compose.pg.yml` 里改；该文件有三处要一起改 |

`deploy/` 目录是「新闻动态流」的规划稿，**不参与看板部署**。那一份仍然用变量加自己的 `.env.example` —— 它要填一堆第三方服务凭据，不适合写死。

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

   非要 bind mount（比如在 NAS 上把数据放进共享文件夹）时，**宿主目录必须对容器内的 uid 10001 可写**，否则启动即失败：

   ```bash
   sudo chown -R 10001:10001 /path/to/your/data
   ```

   否则会看到 `sqlite3.OperationalError: unable to open database file` —— 这句话只有一种写法，却对应三种成因（目录不存在 / 没写权限 / 磁盘满），从日志里分不出来。现在启动时会先自检数据目录，报错正文直接给出当前 uid、目录属主、权限位和上面这条修复命令。

   不想手敲的话，仓库里有一个脚本会自动判断是命名卷还是 bind mount 并修正：

   ```bash
   bash scripts/fix-data-perms.sh           # 预演，只打印将要执行的动作
   bash scripts/fix-data-perms.sh --apply   # 执行 + 重启 + 等健康检查
   ```
2. **端口默认只绑 `127.0.0.1`。** 对外访问请加反向代理并启用 HTTPS。应用自带登录与双角色权限
   （见上文「账号与权限」），但仍不建议直接暴露公网：初始管理员密码会打印在容器日志里，
   第一次登录后务必先改掉。
3. **`--workers` 固定为 1。** SQLite 写入与规格书落盘都在进程内，多 worker 会争锁。切到 PostgreSQL 后可自行调大，但需确认上传文件落盘路径共享。

### 与 `deploy/` 的关系

| 目录/文件 | 内容 | 何时用 |
|---|---|---|
| `docker-compose.yml` | 仅本应用（SQLite），默认拉 `ghcr.io/kid941005/competitive-board` | 单机试用、正式部署 |
| `docker-compose.build.yml` | 叠加：改为本地源码构建（同名镜像标签） | 改了代码要重建 |
| `docker-compose.pg.yml` | 叠加 PostgreSQL | 团队共用 |
| `deploy/docker-compose.yml` | 采集栈规划稿（RSSHub / changedetection / Redis / PG） | 仅在推进「新闻动态」时用，不参与看板部署 |

采集栈里本应用对应 `api` 服务，`app` profile 才会启动，详见 `deploy/README.md`。

> 镜像由 GitHub Actions 真实构建并发布（run 记录见 [Actions](https://github.com/kid941005/competitive-board/actions)，当前 `0.9.9` 那次测试与构建均通过），`docker compose up -d` 拉到的是同一个产物。

---

## 测试

```bash
cd backend
pip install -r requirements-dev.txt   # 额外装 httpx，生产镜像不需要
python tests/test_compare.py          # 20 项：对比逻辑与 CSV 导入
python tests/test_datasheet.py        # 21 项：规格书识别（含单位跨界与别名边界）
python tests/test_datasheet_import.py #  9 项：规格书导入建档（新公司/新型号/三类拦截）
python tests/test_import_export.py    # 14 项：宽表导入、液泵列映射、全量备份导出（含往返）
python tests/test_product_edit.py     # 12 项：产品编辑、三类防护、公司列表口径
python tests/test_api_v1.py           # 24 项：令牌鉴权、幂等、dry-run、ETag、批量回执、单位换算
python tests/test_units.py            # 21 项：单位归一（同维度换算 / 跨维度拒绝 / Vp→Vpp / 口径歧义）
python tests/test_adoptions.py        # 11 项：客户采用关系（页面 + API、重复拒收、同名复用、审计）
python tests/test_audit.py            # 13 项：日志记录、旧值/新值、删除快照、保留期
python tests/test_labels.py           #  6 项：自家公司用公司名，不出现「本方 / 我方」
python tests/test_fixes.py            # 10 项：代码审查修复的回归（串号 / 幂等 / 置信度 / XSS）
python tests/test_auth.py             # 25 项：权限体系（含接口层拦截与 CSRF）
python tests/test_channel_parity.py   # 13 项：四条写入通道的规则一致性（applies_to / 置信度 / 出处 / 日志时间）
python tests/test_schema.py           #  7 项：表结构迁移（新库 / 老库升级去重 / v3 指标合并 / 版本过新拒启）
python tests/test_retention.py        #  4 项：规格书原文保留（被引用的不删、过期的删、未到期留着）
python tests/test_throttle.py         #  5 项：登录失败退避与成功清零
python tests/test_datadir.py          #  6 项：数据目录自检（不可写 / 建不出 / 磁盘满各有各的话）
python tests/test_empty_startup.py    #  7 项：空库启动、空状态文案、应用模板、幂等、权限
python tests/test_user_profile.py     #  7 项：账号名与显示名可编辑（改名生效、重名拒、审计留旧名）
python tests/test_mcp_server.py       # 20 项：MCP 适配层（协议、参数分流、真实 stdio 往返）
python tests/test_entity_meta.py      # 13 项：建公司不丢字段、导入认自家公司、PATCH 改元信息、客户校验、审计
python tests/test_ui_contract.py      # 10 项：页面渲染契约（StrictUndefined 下全页面、页脚版本号、选择器折叠与表头吸顶、flash 提示、403 页）
python tests/test_search.py           # 13 项：检索（公司名/产品名/型号、多词 AND、通配符转义、matched）
node tests/js/test_datasheet_ui.js    # 11 项：导入页交互（无浏览器依赖）
node tests/js/test_filter.js          #  8 项：关键词过滤纯逻辑（与后端同一套语义）
# 或 pytest tests/
```

共 **266 项**（后端 247 + 前端 19）。覆盖内容：

- **对比逻辑**：最优值方向与并列、单一值不给最优、`target`/文本指标不判定、缺失不计零、容差不误判、完整度、指标按大类裁剪、CSV 导入四类错误
- **规格书识别**：脚注防护、单位换算、**L/min 与 µL/min 必须换算而不是原样落库**、**紧贴数值的是别的单位时宁可不取**、**短英文别名不命中单词内部（kp ⊄ kPa）**、区间取上限、修饰语降权、别名不重复计数、多页页码
- **导入建档**：新公司+新型号一次建成并落参数、同型号忽略大小写走更新且不重复建档、近名公司被 409 拦住、显式确认后可放行、型号跨大类被拒、客户实体不可作为导入来源
- **权限体系**：密码哈希与策略、角色→权限位边界、匿名访问的页面跳转与接口 401、普通用户在页面与接口层被 403 拦截、导航项隐藏、CSRF 缺失被拒、停用账号会话立即失效、最后一个管理员不可降级/停用、系统参数改完即时生效
- **表格导入 / 导出**：Excel 与 CSV 必须拦住同样的坏行（指标不在字典、大类无法识别、跨大类冲突），空值算未公开；导出 xlsx 可被 openpyxl 解析，最优加粗、缺失为「—」、完整度行存在、`E2` 冻结
- **产品编辑**：改值并标记复核、清空即删除、非法数字不覆盖旧值、新增缺失项、换大类被拒（已有参数时）、建号与删除、viewer 一律 403
- **界面文案**：自家公司（左蓝微）在选择器里只显示名称、不带类别徽标、「只看 ××」用公司名、导入与产品管理页下拉无「（本方）」后缀、导出「类别」记为「自研」、全文不出现「本方 / 我方 / 本公司」
- **变更日志**：改参数记「旧值 → 新值」、只改条件不误报值变更、清空记为空、没变化不写日志、删除留快照、导入逐项且共享 request_id、viewer 看不到、`/api/v1/audit` 需 admin scope、保留期只清过期
- **AI 接口**：无凭证 401 / scope 不足 403 的统一信封、令牌 secret 含下划线仍可校验、dry_run 真不落库（连公司都不建）、幂等重放只写一条、批量写参数逐项回执、`If-Match` 过期 409、JSON 原生数字不崩、`/tools` 清单可被 function-calling 消费、旧页面路由未被影响
- **通道一致性**（`test_channel_parity.py`）：只改一格不该动别的参数的出处与置信度、导入与规格书提交必须和批量接口一样校验 `applies_to`、置信度在三条通道都钳 0~1、重复建同名公司不记假日志、日志按本地时区显示、勾选复核要留痕、导入不再每行一次查询、重复导入是 no-op、报错行号等于文件真实行号、出处链接只收 http(s)
- **表结构与迁移**：全新库补齐索引与版本号、0.7.x 老库带着重复数据升级（合并型号 / 去重参数 / 留审计）、库比代码新时拒绝启动、唯一索引在并发下回 409、同一批内重复项不炸
- **规格书保留**：被参数引用的永不删、过期且无引用的连原文一起删、未到期保留、`keep_days=0` 关闭清理
- **登录限速**：第 5 次失败后开始退避、第 6 次返回 429 + `Retry-After`、退避期内正确密码也不放行、成功登录清零、不影响其他账号
- **页面渲染契约**：用 `StrictUndefined` 渲染全部页面（漏传变量直接 500，而不是静默渲染成空）、空库页、404 页、登录失败分支、403 页的可选变量、CSRF 失效页的「重新登录」入口、清理日志后的「已清理 N 条」提示真的显示
- **公司元信息**：`POST /entities` 的 kind/name_en/region/tier 真的生效（曾经被静默丢弃）、`PATCH /entities` 改类型与档位、重名被拒、名下有型号不能改成客户、`dry_run` 不落库、没变化不写日志、审计 label 留旧名
- **MCP 适配层**：协议版本协商与回退、通知不产生响应、未知方法 -32601、`/tools` 映射成 `inputSchema` 且路径占位符自动补成 required、GET 参数进 query 而写操作 `dry_run` 进 query 其余进 body、`?confirm=true` 这类内联 query 不被吃掉、成功时剥掉 `request_id`、失败置 `isError`、令牌错与连不上各有各的话、**stdout 只有协议消息**（混一行日志就等于客户端 parse 失败）、一行坏 JSON 不会让进程退出
- **账号改名**：只改显示名不动账号名、改名后旧名立即无法登录而新名可登、重名与非法字符被拒且不落库、没变化不写日志、普通用户 403、管理员改自己名字不掉线且审计的 actor 记旧名
- **空库启动**：只有表与管理员的空库、空状态分「无字典」与「有字典无产品」两种话术、应用模板只写 20 个指标不建公司、重复应用不重复写、普通用户不能应用模板、幂等判断跟着模式走
- **数据目录自检**：可写时通过且不留探针文件、不可写时报出 uid/属主/权限位与修复命令、目录建不出与不可写不混成一句、路径是文件时说「不是目录」、磁盘满时报出剩余字节、`init_db` 顺手建出 `datasheets` 子目录
- **检索**：`q` 命中公司中文名/英文名/型号/产品名、多词按 AND、`%` 与 `_` 按字面量、`matched` 字段、与 `category`/`kind` 叠加、空关键词不过滤
- **前端过滤**（`test_filter.js`，无 DOM）：大小写、多词 AND、空串、`%` 不具通配含义
- **导入页交互**（Node + 最小 DOM 桩）：未填齐时点击必须说出缺什么而不是静默无反应、「将新建 / 将更新」判断、近名公司要露出确认勾选、跨大类冲突提前预警、就绪后请求要带公司/大类/型号与 CSRF 头、超大文件被拦截

`test_auth.py` 与 `test_datasheet_import.py` 各用独立临时数据目录，不碰开发库。

---

## 暂未实现

- 网页参数自动抓取（changedetection.io 已就绪，接入在 P1）
- 多条件参数透视（如同时看 @1.9GHz 与 @2.4GHz）
- 细粒度权限（当前是两角色；如需「只读某几个友商」这类行级控制，需在 `auth` 之上再加数据域）
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
│   ├── units.py          单位归一的唯一真相源（维度 / 因子 / 电压口径归一）
│   ├── importer.py       Excel / CSV 导入（宽表，一行一个型号；列头单位换算）+ 模板生成
│   ├── adoptions.py      客户采用关系的共用逻辑（按名字找客户、文本批量落库）
│   ├── backup.py         全量备份导出（参数宽表 + 参数明细 + 客户采用）
│   ├── api_v1.py         机器调用接口面（信封 / 令牌 / 幂等 / dry-run / tools）
│   ├── search.py         检索约定：归一 / LIKE 转义 / 多词 AND / 命中字段
│   ├── tokens.py         API 令牌签发与校验（只存 sha256）
│   ├── audit.py          变更日志：快照 / 差异 / 查询 / 保留期清理
│   ├── exporters.py      Excel 导出（冻结窗格 / 最优高亮 / 缺失灰显 / 完整度 / 说明页）
│   ├── seed.py           示例数据
│   ├── main.py           装配层：app / lifespan / 异常处理 / 健康检查
│   ├── selection.py      选中与矩阵组装（页面、导出、/api/v1 共用）
│   ├── web_shared.py     模板环境、权限依赖、表单校验、公司/型号查找
│   ├── routes_auth.py    登录 / 登出 / 首页
│   ├── routes_compare.py 对比页、只读 JSON、导出
│   ├── routes_products.py 产品管理与批量导入
│   ├── routes_datasheet.py 规格书导入
│   ├── routes_admin.py   账号 / 令牌 / 日志 / 系统参数
│   ├── schema.py         表结构版本与迁移（唯一索引 + 存量去重）
│   ├── throttle.py       登录失败退避（IP + 账号）
│   ├── datasheet_store.py 规格书原文的保留与清理
│   ├── timeutil.py       库里存 UTC、界面按 TZ 渲染
│   ├── sanitize.py       出处链接协议白名单
│   ├── templates/        登录 / 对比 / 规格书 / 批量导入 / 产品管理 / 账号 / 令牌 / 设置 / 403
│   └── static/           app.css, filter.js, compare.js, products.js, datasheet.js,
│                         import.js, product-edit.js（零依赖）
├── tests/                test_compare / test_datasheet / test_datasheet_import
│                         test_import_export / test_product_edit / test_api_v1
│                         test_audit / test_labels / test_fixes / test_auth
│                         test_channel_parity / test_schema / test_retention / test_throttle
│                         test_datadir / test_empty_startup / test_user_profile
│                         test_mcp_server（测 mcp/ 那一层，走真实 stdio + 假 API）
│                         test_entity_meta / test_ui_contract
│                         js/test_datasheet_ui.js（Node + 最小 DOM 桩，无需浏览器）
├── tools/                make_sample_datasheet.py（生成合成规格书，用于验证识别链路）
├── requirements.txt
└── Dockerfile
docker-compose.yml        默认编排：拉 ghcr 镜像 + SQLite
docker-compose.build.yml  叠加：改为本地源码构建（同名镜像标签）
mcp/                      MCP server（stdio，零依赖）：把 /api/v1 暴露成 MCP 工具
docker-compose.pg.yml     叠加：换成 PostgreSQL
.github/workflows/        build-image.yml（先测试，通过才构建并推 GHCR）
docs/                     AI-API-设计.md（接口划分、I/O、扩展点）
deploy/                   Docker 编排（infra + app 双 profile）
```
