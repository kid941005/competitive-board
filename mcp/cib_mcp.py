#!/usr/bin/env python3
"""competitive-board 的 MCP server —— 把 `/api/v1` 暴露成 MCP 工具（stdio 传输）。

为什么是「薄适配层」而不是重写一套
------------------------------------
本仓库的 `/api/v1` 已经是一个完整的机器接口面：统一信封、scope 鉴权、幂等键、
`dry_run` 预演、ETag 乐观并发，而且 `GET /api/v1/tools` 返回的工具清单本来就是
function-calling 形态（name / description / parameters）。所以这一层只做三件事：

    1. 把 `/tools` 的每一项换成 MCP 的 tool 定义（`parameters` → `inputSchema`）；
    2. 把 `tools/call` 的 arguments 换成一次 HTTP 请求（路径占位符 / query / body）；
    3. 把 `{ok,data,meta}` 换成 MCP 的 `content` + `isError`。

业务逻辑一行都没有。看板改了接口，这里跟着 `/tools` 自动变，不会漂移。

配置（全部走环境变量）
----------------------
    CIB_BASE_URL        看板地址，默认 http://127.0.0.1:8000
    CIB_TOKEN           API 令牌，必填。在「API 令牌」页签发（cib_xxx_yyy）
    CIB_TIMEOUT         单次请求超时秒数，默认 30
    CIB_USE_ENV_PROXY   设为 1 时沿用 http_proxy/https_proxy，默认**不走**

两个刻意的设计取舍
------------------
- **零第三方依赖**：只用标准库。用户在自己机器上 `python mcp/cib_mcp.py` 就能跑，
  不用 pip install，也不用和看板同机 —— 它只是一次 HTTP 调用。
- **默认不走系统代理**：设了 `http_proxy` 的机器上，urllib 会把 `127.0.0.1` 与
  `192.168.*` 也送进代理然后超时，表现得像「服务没起来」。局域网部署必须绕开。
- **stdout 只放协议消息**：所有日志走 stderr。混进一行日志客户端就 parse 失败。
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

SERVER_NAME = "competitive-board"
SERVER_VERSION = "1.0.1"

# 支持的协议版本，新的在前。客户端请求哪个就回哪个，都不认时回最新的。
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = (
    "这是压电竞品参数看板的接口（公司 / 型号 / 参数矩阵 / 规格书导入）。\n"
    "动手前先调 get_context 拿骨架（有哪些公司、型号、指标 key）；\n"
    "写参数前先用 dry_run=true 预演，确认后再落库；重试写操作时带同一个 "
    "Idempotency-Key，不会写两遍。\n"
    "所有响应都是 {ok,data,meta}，失败看 error.code 而不要解析 message；\n"
    "metric_key 用稳定的 key（如 flow_air），不要用中文指标名。"
)

# 写请求里这些参数服务端从 query 读（_is_dry 先查 query），放 body 虽然也认，
# 但 query 是明确生效的路径 —— 少一层「取决于模型定义」的赌博。
_QUERY_FOR_WRITES = {"dry_run"}


def log(message: str) -> None:
    """日志一律走 stderr —— stdout 是协议通道，混一行就废。"""
    print(f"[cib-mcp] {message}", file=sys.stderr, flush=True)


# ── HTTP 客户端 ────────────────────────────────────────────────────────────
class ApiUnreachable(Exception):
    """连不上看板（DNS / 拒绝连接 / 超时）。与「服务端返回了错误」是两回事。"""


def _qs_value(value: object) -> str:
    """query 里的布尔要写成 true/false：urlencode 会把 True 编成 "True"。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _parse_json(raw: bytes) -> dict | None:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


class Api:
    """对 `/api/v1` 的最小封装：一次调用 = 一次 HTTP 请求。"""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 30.0,
        use_env_proxy: bool = False,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.token = token
        self.timeout = timeout
        # 空 ProxyHandler 显式禁用环境代理。默认值就是「不走代理」，
        # 理由见模块开头：内网地址被送进代理是最常见的假故障。
        handlers = [] if use_env_proxy else [urllib.request.ProxyHandler({})]
        self._opener = urllib.request.build_opener(*handlers)

    def call(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        body: dict | None = None,
    ) -> tuple[int, dict]:
        url = self.base_url + path
        if params:
            query = urllib.parse.urlencode(
                {k: _qs_value(v) for k, v in params.items() if v is not None}
            )
            if query:
                url += ("&" if "?" in url else "?") + query

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": f"{SERVER_NAME}-mcp/{SERVER_VERSION}",
        }
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=data, method=method.upper(), headers=headers)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return response.status, _parse_json(response.read()) or {}
        except urllib.error.HTTPError as exc:
            # 4xx/5xx 的 body 通常仍是 {ok:false,error:{...}}，比状态码有用得多
            raw = exc.read()
            payload = _parse_json(raw)
            if payload is None:
                payload = {
                    "ok": False,
                    "error": {
                        "code": f"HTTP_{exc.code}",
                        "message": raw[:400].decode("utf-8", "replace") or str(exc.reason),
                    },
                }
            return exc.code, payload
        except urllib.error.URLError as exc:
            raise ApiUnreachable(_unreachable_hint(self.base_url, exc.reason)) from exc
        except TimeoutError as exc:
            raise ApiUnreachable(
                f"{self.base_url} 在 {self.timeout:.0f}s 内没有响应。"
                f"确认看板在跑，或调大 CIB_TIMEOUT。"
            ) from exc

    def tool_manifest(self) -> list[dict]:
        _status, payload = self.call("GET", "/api/v1/tools")
        if not payload.get("ok"):
            raise ApiError(_error_text(payload))
        tools = (payload.get("data") or {}).get("tools") or []
        if not tools:
            raise ApiError("服务端返回的 /api/v1/tools 是空的，看板版本可能太旧")
        return tools


class ApiError(Exception):
    """服务端明确返回了失败（令牌错、scope 不足、参数不合法…）。"""


def _unreachable_hint(base_url: str, reason: object) -> str:
    return (
        f"连不上看板 {base_url}：{reason}\n"
        f"检查顺序：① 看板在跑吗（curl {base_url}/healthz）；"
        f"② 地址对吗（内网地址在别的机器上不存在）；"
        f"③ 这台机器设了 http_proxy 吗（默认已绕过，设 CIB_USE_ENV_PROXY=1 才走）。"
    )


def _error_text(payload: dict) -> str:
    error = payload.get("error") or {}
    code = error.get("code") or "UNKNOWN"
    message = error.get("message") or "服务端返回了失败但没有 message"
    parts = [f"{code}: {message}"]
    if error.get("hint"):
        parts.append(f"提示：{error['hint']}")
    if error.get("field"):
        parts.append(f"字段：{error['field']}")
    return "\n".join(parts)


# ── /tools → MCP tool ──────────────────────────────────────────────────────
_PATH_PARAM = re.compile(r"\{(\w+)\}")


def path_params(path: str) -> list[str]:
    return _PATH_PARAM.findall(path or "")


def to_mcp_tool(spec: dict) -> dict:
    """把 `/tools` 的一项映射成 MCP 的 tool 定义。

    `path` 里的占位符要补进 `inputSchema.required` —— 否则模型不知道必须传，
    调用时才报「缺少 product_id」，白白浪费一轮。类型统一给 string：
    FastAPI 的路径参数声明是 int，传 "12" 照样解析，而声明 integer 反而可能
    被客户端的 schema 校验挡在门外。
    """
    schema = spec.get("parameters")
    schema = json.loads(json.dumps(schema)) if schema else {"type": "object", "properties": {}}
    schema.setdefault("type", "object")
    props = schema.setdefault("properties", {})

    required = list(schema.get("required") or [])
    for name in path_params(spec.get("path", "")):
        props.setdefault(
            name,
            {"type": "string", "description": f"路径参数 {name}（数字 id 直接传字符串即可）"},
        )
        if name not in required:
            required.append(name)
    if required:
        schema["required"] = required

    scope = spec.get("scope") or "read"
    description = spec.get("description") or spec["name"]
    return {
        "name": spec["name"],
        "description": f"{description}\n（后端权限：{scope}）",
        "inputSchema": schema,
    }


def build_request(spec: dict, arguments: dict | None) -> tuple[str, str, dict | None, dict | None]:
    """把一次 `tools/call` 拆成 (method, path, query, body)。

    分流规则：
    - GET / DELETE：参数全放 query（`/products/{id}?confirm=true` 这种内联 query 也合并进去）
    - POST / PATCH：`dry_run` 放 query（服务端先查 query，明确生效），其余放 body
    """
    path = spec.get("path") or ""
    args = dict(arguments or {})

    for name in path_params(path):
        if name not in args or args[name] in (None, ""):
            raise ValueError(f"缺少路径参数 {name}（{spec['name']} 需要它定位对象）")
        path = path.replace("{" + name + "}", urllib.parse.quote(str(args.pop(name)), safe=""))

    method = (spec.get("method") or "GET").upper()
    bare_path, _, inline = path.partition("?")
    query: dict = dict(urllib.parse.parse_qsl(inline))

    if method in ("GET", "DELETE", "HEAD"):
        query.update({k: v for k, v in args.items() if v is not None})
        return method, bare_path, query or None, None

    body = {k: v for k, v in args.items() if k not in _QUERY_FOR_WRITES and v is not None}
    for key in _QUERY_FOR_WRITES:
        if key in args and args[key] is not None:
            query[key] = args[key]
    return method, bare_path, query or None, body


def _qs_suffix(query: dict | None) -> str:
    """日志里带上 query，排查「参数没传过去」时省一轮。"""
    if not query:
        return ""
    return "?" + urllib.parse.urlencode({k: _qs_value(v) for k, v in query.items()})


def render_result(status: int, payload: dict) -> dict:
    """`{ok,data,meta}` → MCP 的 content / isError。

    成功时只回 data 与去掉 request_id 的 meta —— request_id 对模型没有意义，
    留着只是噪音。失败时回 error 的 code/message/hint，并置 isError。
    """
    if payload.get("ok"):
        out: dict = {"ok": True, "data": payload.get("data")}
        meta = {k: v for k, v in (payload.get("meta") or {}).items() if k != "request_id"}
        if meta:
            out["meta"] = meta
        return {
            "content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, indent=2)}],
            "isError": False,
        }

    error = payload.get("error")
    if not isinstance(error, dict):
        text = f"HTTP {status}，响应不是预期的信封结构：{json.dumps(payload, ensure_ascii=False)[:400]}"
    else:
        text = _error_text(payload)
    return {"content": [{"type": "text", "text": text}], "isError": True}


# ── 协议处理 ───────────────────────────────────────────────────────────────
class McpServer:
    """JSON-RPC over stdio。把「协议」与「传输」分开，测试才能直接喂 dict。"""

    def __init__(self, api: Api):
        self.api = api
        self._tools: list[dict] | None = None
        self._specs: dict[str, dict] = {}

    def _load_tools(self) -> list[dict]:
        if self._tools is None:
            specs = self.api.tool_manifest()
            self._specs = {s["name"]: s for s in specs}
            self._tools = [to_mcp_tool(s) for s in specs]
        return self._tools

    def handle(self, message: dict) -> dict | None:
        """处理一条消息。返回 None 表示这是通知、不该有响应。"""
        method = message.get("method")
        request_id = message.get("id")
        is_notification = "id" not in message

        if method == "initialize":
            params = message.get("params") or {}
            requested = params.get("protocolVersion")
            version = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
            return _result(request_id, {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": INSTRUCTIONS,
            })

        # initialized / cancelled / 未来新增的通知：一律静默吞掉。
        # 对通知回错误会让客户端认为连接有问题。
        if method and method.startswith("notifications/"):
            return None

        if method == "ping":
            return _result(request_id, {})

        if method == "tools/list":
            try:
                return _result(request_id, {"tools": self._load_tools()})
            except ApiError as exc:
                return _error(request_id, -32603, str(exc))
            except ApiUnreachable as exc:
                return _error(request_id, -32603, str(exc))

        if method == "tools/call":
            return self._call_tool(request_id, message.get("params") or {})

        if method in ("resources/list", "prompts/list"):
            # 明确告知「没有这个能力」，比报错更友好；客户端一般不会走到这里
            key = "resources" if method.startswith("resources") else "prompts"
            return _result(request_id, {key: []})

        if is_notification:
            return None

        return _error(request_id, -32601, f"不支持的方法：{method}")

    def _call_tool(self, request_id: object, params: dict) -> dict:
        name = params.get("name")
        arguments = params.get("arguments") or {}

        try:
            self._load_tools()
        except (ApiError, ApiUnreachable) as exc:
            return _result(request_id, {
                "content": [{"type": "text", "text": str(exc)}], "isError": True,
            })

        spec = self._specs.get(name or "")
        if spec is None:
            known = "、".join(sorted(self._specs))
            return _result(request_id, {
                "content": [{"type": "text", "text": f"没有名为 {name!r} 的工具。可选：{known}"}],
                "isError": True,
            })

        try:
            method, path, query, body = build_request(spec, arguments)
        except ValueError as exc:
            return _result(request_id, {
                "content": [{"type": "text", "text": str(exc)}], "isError": True,
            })

        try:
            status, payload = self.api.call(method, path, params=query, body=body)
        except ApiUnreachable as exc:
            return _result(request_id, {
                "content": [{"type": "text", "text": str(exc)}], "isError": True,
            })

        log(f"{name} -> {method} {path}{_qs_suffix(query)} {status}")
        return _result(request_id, render_result(status, payload))


def _result(request_id: object, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: object, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


# ── stdio 主循环 ───────────────────────────────────────────────────────────
def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def build_api_from_env() -> Api:
    base_url = _env("CIB_BASE_URL", "http://127.0.0.1:8000")
    token = _env("CIB_TOKEN")
    if not token:
        log("缺少 CIB_TOKEN：在「API 令牌」页签发一个，把 cib_xxx_yyy 写进环境变量")
    try:
        timeout = float(_env("CIB_TIMEOUT", "30") or 30)
    except ValueError:
        timeout = 30.0
    return Api(
        base_url,
        token,
        timeout=timeout,
        use_env_proxy=_env("CIB_USE_ENV_PROXY") in ("1", "true", "yes"),
    )


def serve(server: McpServer, stdin, stdout) -> int:
    """逐行读写。MCP 的 stdio 传输就是「一行一个 JSON-RPC 消息」。"""
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            # 请求都解析不出来，id 无从得知，只能按规范回 id=null
            stdout.write(json.dumps(_error(None, -32700, f"JSON 解析失败：{exc.msg}")) + "\n")
            stdout.flush()
            continue
        if not isinstance(message, dict):
            stdout.write(json.dumps(_error(None, -32600, "消息必须是 JSON 对象")) + "\n")
            stdout.flush()
            continue

        response = server.handle(message)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()
    return 0


def main() -> int:
    api = build_api_from_env()
    log(f"启动：base={api.base_url} token={'已配置' if api.token else '缺失'}")
    return serve(McpServer(api), sys.stdin, sys.stdout)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0) from None
