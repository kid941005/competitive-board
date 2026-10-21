# MCP server —— 让 AI 客户端直接操作看板

把看板的 `/api/v1` 包成 [MCP](https://modelcontextprotocol.io) 工具，任何支持 MCP 的
客户端（WorkBuddy、Claude Desktop、Cursor、Cline…）都能直接发现并调用：查型号、比参数、
写数据、导 Excel 行，不用自己拼 HTTP 请求。

**它是薄适配层，不是第二套后端。** 工具清单从 `GET /api/v1/tools` 动态拉取，看板改了接口，
这里跟着变，不存在两份真相。业务逻辑一行都没有。

- 单文件、**零第三方依赖**（只用 Python 标准库，3.10+）
- 传输：**stdio**（客户端拉起子进程）
- 不需要和看板同机 —— 它只是一次 HTTP 调用

## 前置：先拿一个令牌

看板 →「API 令牌」→ 签发。按用途选 scope：

| scope | 能做什么 |
|---|---|
| `read` | 查全部数据、检索、对比、变更流 |
| `write` | 建/改/删型号、批量写参数 |
| `ingest` | 按行导入（与 Excel 导入同规则） |
| `admin` | 查变更日志（含所有人的操作） |

明文只显示一次。**只给 AI 只读用就只勾 `read`。**

## 配置

三处要填：脚本路径、看板地址、令牌。

```json
{
  "mcpServers": {
    "competitive-board": {
      "command": "python",
      "args": ["C:/Users/you/competitive-board/mcp/cib_mcp.py"],
      "env": {
        "CIB_BASE_URL": "http://192.168.1.10:8000",
        "CIB_TOKEN": "cib_xxxxxxxx_yyyyyyyy"
      }
    }
  }
}
```

- **WorkBuddy**：`~/.workbuddy/mcp.json` 的 `mcpServers` 下加这一段，重启客户端。
- **Claude Desktop**：`claude_desktop_config.json`（Windows 在
  `%APPDATA%\Claude\`，macOS 在 `~/Library/Application Support/Claude/`）。
- **Cursor / Cline**：`~/.cursor/mcp.json` 或各自 UI 里的 MCP 配置，格式同上。

路径用正斜杠最省事（Windows 下 `\\` 要写两遍）。`command` 若不是全局可用的 `python`，
换成绝对路径，例如 `C:/Python312/python.exe`。

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `CIB_BASE_URL` | `http://127.0.0.1:8000` | 看板地址。端口不是 8000 时按实际填 |
| `CIB_TOKEN` | 无（必填） | `cib_<prefix>_<secret>` |
| `CIB_TIMEOUT` | `30` | 单次请求超时秒数。大库导出可调大 |
| `CIB_USE_ENV_PROXY` | 不设 | 设 `1` 时沿用 `http_proxy` / `https_proxy` |

> **为什么默认不走代理**：设了系统代理的机器上，urllib 会把 `127.0.0.1` 和 `192.168.*`
> 也送进代理然后超时，表现得非常像「服务没起来」。看板在内网时必须绕开 —— 这是默认行为。
> 看板在公网且必须走代理时，才设 `CIB_USE_ENV_PROXY=1`。

## 自测（不装客户端也能验）

```bash
export CIB_BASE_URL=http://127.0.0.1:8000
export CIB_TOKEN=cib_xxxxxxxx_yyyyyyyy

printf '%s\n' \
 '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}' \
 '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
 '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_context","arguments":{}}}' \
| python mcp/cib_mcp.py
```

看到三行 JSON 就通了（第三行的 `text` 里是库的骨架：公司数、型号数、指标数）。
日志走 stderr，stdout 只有协议消息 —— 混一行客户端就 parse 失败。

## 工具清单

从 `/api/v1/tools` 动态拉，当前 13 个：

| 用途 | 工具 |
|---|---|
| 摸骨架 | `get_context` |
| 检索 | `search_products`、`search_companies`、`search_metrics` |
| 读详情 | `get_product`、`compare_products`、`get_changes` |
| 写产品 | `create_product`、`update_product`、`delete_product` |
| 写参数 | `write_specs`、`bulk_import` |
| 审计 | `get_change_log`（需 `admin` scope） |

每个工具的描述里标了后端要求的 scope。写操作记得先用 `dry_run: true` 预演。

## 故障排查

| 现象 | 多半是 | 怎么办 |
|---|---|---|
| 客户端里一个工具都没有 | `tools/list` 失败 | 客户端日志里找 `[cib-mcp]` 那几行（stderr） |
| `连不上 <地址>` | 地址错 / 服务没起 / 走了代理 | `curl <地址>/healthz`；确认没设 `CIB_USE_ENV_PROXY` |
| `UNAUTHENTICATED` | 令牌错、过期或已吊销 | 重签一个 |
| `FORBIDDEN 缺少 write 权限` | scope 不够 | 重签时勾上 `write` / `ingest` |
| 工具调用返回 `isError: true` 但服务正常 | 业务校验没过 | 看 `code`：`CATEGORY_CONFLICT` 是指标不适用该大类 |
| Windows 下启动即退出 | `command` 里 `python` 不在 PATH | 换成 python.exe 的绝对路径 |

## 边界

- **只做 stdio**。没有 HTTP 传输 —— 要给多人共用就每人配一份，令牌各自签发、各自吊销。
- **不代理文件上传**。规格书 PDF 上传仍是页面上做的事，MCP 侧只有按行导入（`bulk_import`）。
- **不透传 `If-Match`**。并发保护的乐观锁只在直接调 HTTP 时用；AI 场景下「先 GET 再 PATCH」
  已经够，因为它是串行对话。
