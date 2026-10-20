# AI 调用接口设计（/api/v1）

> 目标：让「AI / 脚本」能查数据、改数据、建产品，而**不需要浏览器、不需要解析 HTML、不会重复写入**。
> 原则：新增独立命名空间，旧路由一条不动；扩展点全部落在 `/api/v1` 内，后续接 MCP / Agent 不用改业务代码。

---

## 1. 先说清楚：原来的实现卡在哪

| 阻碍 | 具体表现 | 后果 |
|---|---|---|
| 写入是「表单 + 303 跳转」 | `POST /products/{id}` 收 `application/x-www-form-urlencoded`，成功返回 `303 Location: /products/1?ok=已保存…` | 程序只能靠状态码猜成败，拿不到结构化结果；错误信息还是 URL 编码的中文 |
| 强制 CSRF 双重提交 | 所有非幂等请求都要 `_csrf` 字段或 `X-CSRF-Token` 头，且必须先有会话 Cookie | AI 无浏览器会话，第一步就过不去 |
| 导入非原子 | 早期每建一个公司/型号就 `commit()`，中途报错留下半成品 | 重试会写两遍；「导入 100 行成功了 37 行」无法解释 |

这三条不是权限问题，是**接口形态问题**。所以本层不做「给旧接口加个 token」，而是新开一层：

- JSON in / JSON out，一律 `application/json`
- Bearer 令牌鉴权（不走 Cookie → 天然没有 CSRF 风险，也就不必放宽浏览器侧的任何限制）
- 写操作整批事务 + `dry_run` + `Idempotency-Key`

---

## 2. 接口怎么划分：按「用途」分四组，不按对象分

按对象分（products / specs / entities 各一组）会让 AI 难以判断「我现在该调哪个」。按用途分，模型先定意图，再找接口：

### 查询面 `read`
| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/v1/context` | **一次拿全骨架**：公司、型号、指标 key、各类数量 |
| GET | `/api/v1/metrics?category=` | 指标字典（含 `key`、`unit`、`better`、`applies_to`） |
| GET | `/api/v1/entities?kind=` | 公司清单 |
| GET | `/api/v1/products?entity_id=&category=&status=&q=&limit=&cursor=` | 型号筛选 + 游标分页 |
| GET | `/api/v1/products/{id}` | 型号详情：参数、缺失项、完整度、`ETag` |
| GET | `/api/v1/products/{id}/specs` | 只取参数 |
| GET | `/api/v1/matrix?p=1,2,3` | 横向对比（复用页面同一套计算） |
| GET | `/api/v1/changes?since=` | 增量变更流，供定期同步 |

### 写入面 `write`
| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/v1/entities` | 新建公司（近名会 409，`force=true` 放行） |
| POST | `/api/v1/products` | 新建型号（公司可传 id 或 name） |
| PATCH | `/api/v1/products/{id}` | 改型号元信息（支持 `If-Match`） |
| POST | `/api/v1/specs/batch` | **批量写参数**，逐项返回成败 |
| DELETE | `/api/v1/products/{id}?confirm=true` | 删除型号及参数 |

### 摄取面 `ingest`
| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/v1/import` | 按行导入（与页面 Excel/CSV **同一套校验**） |

### 发现面 `read`
| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/v1/health` | 存活（不鉴权，供探针） |
| GET | `/api/v1/tools` | **工具清单**：给 function-calling 用的 name/description/JSON Schema |
| GET | `/api/v1/tokens` · POST · DELETE | 令牌管理（需 `admin`） |

> 为什么查询和写入必须分开：AI 最常见的事故是「本来只想查，结果写了一遍」。分组后，给只读助手只签 `read` 一个 scope 就能从源头杜绝。

---

## 3. 输入输出结构

### 3.1 统一信封

成功：

```json
{
  "ok": true,
  "data": { "...": "..." },
  "meta": { "request_id": "b03fd87842fb", "total": 12, "next_cursor": 7 }
}
```

失败：

```json
{
  "ok": false,
  "error": {
    "code": "CATEGORY_CONFLICT",
    "message": "flow_liquid 不适用于 压电气泵",
    "field": "category",
    "hint": "GET /api/v1/metrics?category=pump_air 取适用指标"
  },
  "meta": { "request_id": "9c1e…" }
}
```

**`code` 是程序分支的唯一依据，`message/hint` 只给人看。** 现有错误码：

| code | HTTP | 含义 |
|---|---|---|
| `UNAUTHENTICATED` | 401 | 缺凭证 / 令牌无效、撤销、过期 |
| `FORBIDDEN` | 403 | scope 不足 |
| `NOT_FOUND` | 404 | 对象不存在 |
| `VALIDATION_FAILED` | 422 / 400 | 参数或业务规则不合法（带 `field`） |
| `MODEL_EXISTS` | 409 | 同公司下型号重复 |
| `ENTITY_EXISTS` / `ENTITY_SIMILAR` | 409 | 公司同名 / 近名 |
| `CATEGORY_CONFLICT` | 409 | 型号跨大类、指标不适用 |
| `CONFLICT_VERSION` | 409 | `If-Match` 与当前 `ETag` 不符 |
| `CONFIRM_REQUIRED` | 400 | 危险操作缺 `confirm=true` |

### 3.2 三个典型调用

**① 查询：这个公司有哪些气泵，参数补全到什么程度**

```http
GET /api/v1/products?entity_id=2&category=pump_air&limit=2
Authorization: Bearer cib_xxx
```

```json
{"ok": true, "data": [
  {"id": 1, "model": "MZB1001T02", "category": "pump_air", "category_label": "压电气泵",
   "status": "active", "entity": {"id": 2, "name": "村田制作所", "kind": "competitor"},
   "specs_filled": 7, "specs_total": 11, "completeness": 0.6364}
], "meta": {"request_id": "a4fc…", "total": 1, "next_cursor": 1}}
```

**② 改数据：批量写参数，逐项回执**

```http
POST /api/v1/specs/batch
Authorization: Bearer cib_xxx
Content-Type: application/json

{"items": [
  {"product_id": 15, "metric_key": "flow_air", "value_num": 0.66, "condition": "@15Vpp", "verified": true},
  {"product_id": 15, "metric_key": "flow_liquid", "value_num": 1}
]}
```

```json
{"ok": true, "data": {"applied": 1, "results": [
  {"index": 0, "product_id": 15, "metric_key": "flow_air", "ok": true, "action": "created"},
  {"index": 1, "product_id": 15, "metric_key": "flow_liquid", "ok": false,
   "code": "CATEGORY_CONFLICT", "message": "flow_liquid 不适用于 压电气泵"}
]}, "meta": {"failed": 1}}
```

**③ 新增产品：先预演，再落库，重试不重复**

```http
POST /api/v1/products?dry_run=true
Idempotency-Key: create-mzb4002-1

{"entity_id": 2, "model": "MZB4002T01", "category": "pump_air"}
```

```json
{"ok": true, "data": {"id": null, "model": "MZB4002T01", "...": "..."},
 "meta": {"dry_run": true, "request_id": "1f2a…"}}
```

去掉 `dry_run` 再发一次，带同一个 `Idempotency-Key`：首次 `201`，重放 `200` 且 `meta.replayed=true`，库里只有一条。

### 3.3 认证与权限

```
Authorization: Bearer cib_<prefix>_<secret>
```

- 库里只存 `sha256(secret)`，`prefix` 用于定位；明文只在签发时显示一次。
- scope：`read` / `write` / `ingest` / `admin`（`admin` 隐含全部）。
- **令牌请求不校验 CSRF**：CSRF 的前提是浏览器自动携带 Cookie；令牌是显式头，不存在被跨站自动携带的可能。Cookie 会话那条路保持不变。
- 浏览器会话也能调 `/api/v1`（按角色折算成 scope），方便手工验证。

---

## 4. 预留的扩展点

| 扩展点 | 现在的作用 | 后续可承接 |
|---|---|---|
| `Idempotency-Key` | 写接口重试不重复 | Agent 自动重试、消息队列至少一次投递 |
| `dry_run` | 预演，不落库 | 「先给用户看将要改什么，再确认」的交互范式 |
| `ETag` / `If-Match` | 乐观并发 | 多 Agent 并行编辑同一型号 |
| `/tools` | 静态工具清单（含 JSON Schema） | 直接喂给 function-calling；接 MCP 时映射为 tool 定义 |
| `/context` | 一次拿骨架 | 模型冷启动，省掉十几轮探查 |
| `/changes?since=` | 增量变更流 | 定时同步、触发下游（周报 / 告警） |
| 统一 `code` 枚举 | 程序分支 | 失败重试策略、可观测性统计 |
| `metric_key` 稳定标识 | 写参数只认 key | 指标改名不影响历史调用 |

### 接 MCP 的方式（已预留，未实现）

`/api/v1/tools` 的每一项都能一对一映射成 MCP tool：`name` → tool name，`description` → description，`parameters` → inputSchema，`method+path` → 内部 HTTP 调用。届时只需写一个薄适配层，**业务代码零改动**。

---

## 5. 为什么说「当前实现不阻碍后续接入」

1. **独立前缀**：`/api/v1/*` 与页面路由完全分离，页面怎么改都不影响机器接口；旧接口一个未删。
2. **共用内核**：`/matrix` 直接调用页面同一套 `build_matrix`，`/import` 复用 `import_rows`。人看到的与 AI 看到的是同一份数据，不会出现两套口径。
3. **标识符稳定**：写参数只认 `metric_key`（如 `flow_air`），中文名可以改；对象用自增 `id`，型号名可改。
4. **错误可分支**：`code` 是封闭枚举，新增只会追加，不改语义。
5. **不引入新前端**：全部是 JSON，无模板、无 JS 依赖。

---

## 6. 明确不做的事

- **不让 AI 写 SQL**：所有读写都走受控接口，字段与范围由代码决定。
- **不做细粒度行级权限**（如「只能看某几个友商」）：需要时在 `require_scope` 之后加一层 domain 过滤，位置已预留。
- **不做审计日志**：`ApiToken.last_used_at` 只记最近一次；完整审计需要单独的表与保留策略。
- **不开放 webhook**：`/changes` 是拉模式，推模式等有明确消费方再加。
