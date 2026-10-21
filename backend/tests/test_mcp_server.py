"""MCP 适配层（`mcp/cib_mcp.py`）的测试。

分两类：
- **纯逻辑**：`to_mcp_tool` / `build_request` / `render_result`，直接喂 dict；
- **往返**：起一个假 `/api/v1`（标准库 HTTP server），再起 MCP server 子进程，
  真的走一遍 stdio → HTTP → 服务端 → 回去。

第二类不可省：协议层最常见的致命 bug 是「往 stdout 打了一行日志」，
单元测试完全测不出来 —— 客户端会直接 parse 失败。

    python tests/test_mcp_server.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MCP_SCRIPT = ROOT / "mcp" / "cib_mcp.py"

sys.path.insert(0, str(ROOT / "mcp"))

import cib_mcp

TOKEN = "cib_test_tok"

# 与服务端 GET /api/v1/tools 同形（含路径占位符与内联 query 两种写法）
TOOLS = [
    {
        "name": "get_context", "method": "GET", "path": "/api/v1/context",
        "description": "拿骨架", "scope": "read",
    },
    {
        "name": "search_products", "method": "GET", "path": "/api/v1/products",
        "description": "检索型号", "scope": "read",
        "parameters": {
            "type": "object",
            "properties": {"q": {"type": "string"}, "limit": {"type": "integer"}},
        },
    },
    {
        "name": "get_product", "method": "GET", "path": "/api/v1/products/{product_id}",
        "description": "型号详情", "scope": "read",
    },
    {
        "name": "write_specs", "method": "POST", "path": "/api/v1/specs/batch",
        "description": "批量写参数", "scope": "write",
        "parameters": {
            "type": "object",
            "required": ["items"],
            "properties": {
                "dry_run": {"type": "boolean"},
                "items": {"type": "array", "items": {"type": "object"}},
            },
        },
    },
    {
        "name": "delete_product", "method": "DELETE",
        "path": "/api/v1/products/{product_id}?confirm=true",
        "description": "删除型号", "scope": "write",
    },
]


# ── 假 API ─────────────────────────────────────────────────────────────────
class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 别把访问日志打到测试输出里
        pass

    def _send(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _denied(self) -> bool:
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._send(401, {"ok": False, "error": {
                "code": "UNAUTHENTICATED", "message": "令牌无效", "hint": "重新签发"}})
            return True
        return False

    def do_GET(self) -> None:
        path, _, query = self.path.partition("?")
        if path == "/api/v1/tools":
            self._send(200, {"ok": True, "data": {"tools": TOOLS},
                             "meta": {"request_id": "aaa"}})
            return
        if self._denied():
            return
        if path == "/api/v1/context":
            self._send(200, {"ok": True, "data": {"counts": {"products": 11}},
                             "meta": {"request_id": "aaa"}})
        elif path == "/api/v1/products/12":
            self._send(200, {"ok": True, "data": {"id": 12, "model": "MZB1001T02"},
                             "meta": {"request_id": "aaa"}})
        elif path == "/api/v1/products":
            # 必须用 parse_qs：中文会被 urlencode 成 %E6%9D%91…，手写 split 拿不到原值
            q = urllib.parse.parse_qs(query)
            self._send(200, {"ok": True, "data": [{"model": "MZB1001T02"}],
                             "meta": {"count": 1,
                                      "q": (q.get("q") or [None])[0],
                                      "request_id": "aaa"}})
        else:
            self._send(404, {"ok": False, "error": {"code": "NOT_FOUND", "message": "没有"}})

    def do_POST(self) -> None:
        if self._denied():
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self._send(200, {
            "ok": True,
            "data": {"applied": len(body.get("items") or [])},
            "meta": {"dry_run": "dry_run=true" in self.path, "request_id": "aaa"},
        })

    def do_DELETE(self) -> None:
        if self._denied():
            return
        path, _, query = self.path.partition("?")
        pid = path.rsplit("/", 1)[-1]
        self._send(200, {
            "ok": True,
            "data": {"id": pid, "deleted": True, "confirm": "confirm=true" in query},
            "meta": {"request_id": "aaa"},
        })


_server: ThreadingHTTPServer | None = None


def _port() -> int:
    global _server
    if _server is None:
        _server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        _server.daemon_threads = True
        threading.Thread(target=_server.serve_forever, daemon=True).start()
    return _server.server_address[1]


def _api() -> cib_mcp.Api:
    return cib_mcp.Api(f"http://127.0.0.1:{_port()}", TOKEN, timeout=10)


# ── 纯逻辑 ─────────────────────────────────────────────────────────────────
def test_manifest_maps_to_input_schema():
    tool = cib_mcp.to_mcp_tool(TOOLS[1])
    assert tool["name"] == "search_products"
    assert tool["inputSchema"]["properties"]["q"]["type"] == "string"
    assert "（后端权限：read）" in tool["description"]


def test_path_placeholder_becomes_required_param():
    tool = cib_mcp.to_mcp_tool(TOOLS[2])
    schema = tool["inputSchema"]
    assert "product_id" in schema["properties"], "路径占位符必须补进 properties"
    assert "product_id" in schema.get("required", []), "路径参数不声明必填，模型不会传"
    # 深拷贝：不能改动 /tools 传进来的原对象
    assert "product_id" not in (TOOLS[2].get("parameters") or {}).get("properties", {})


def test_missing_path_param_raises():
    try:
        cib_mcp.build_request(TOOLS[2], {})
    except ValueError as exc:
        assert "product_id" in str(exc)
    else:
        raise AssertionError("缺路径参数应当报错")


def test_get_args_go_to_query():
    method, path, query, body = cib_mcp.build_request(TOOLS[1], {"q": "村田", "limit": 3})
    assert (method, path, body) == ("GET", "/api/v1/products", None)
    assert query == {"q": "村田", "limit": 3}


def test_write_args_go_to_body_but_dry_run_to_query():
    method, path, query, body = cib_mcp.build_request(
        TOOLS[3], {"items": [{"product_id": 1}], "dry_run": True})
    assert method == "POST" and path == "/api/v1/specs/batch"
    assert body == {"items": [{"product_id": 1}]}, body
    assert query == {"dry_run": True}, query


def test_inline_query_in_path_is_kept():
    """`/products/{id}?confirm=true` 这种写法不能被路径替换吃掉。"""
    method, path, query, body = cib_mcp.build_request(TOOLS[4], {"product_id": 7})
    assert method == "DELETE"
    assert path == "/api/v1/products/7"
    assert query == {"confirm": "true"}
    assert body is None


def test_render_success_strips_request_id():
    out = cib_mcp.render_result(200, {"ok": True, "data": {"a": 1},
                                      "meta": {"request_id": "zzz", "count": 1}})
    assert out["isError"] is False
    payload = json.loads(out["content"][0]["text"])
    assert payload["data"] == {"a": 1}
    assert payload["meta"] == {"count": 1}, "request_id 是噪音，不该给模型看"


def test_render_error_sets_is_error():
    out = cib_mcp.render_result(403, {"ok": False, "error": {
        "code": "FORBIDDEN", "message": "缺少 write 权限", "hint": "当前 scope：read"}})
    assert out["isError"] is True
    text = out["content"][0]["text"]
    assert "FORBIDDEN" in text and "缺少 write 权限" in text and "当前 scope" in text


# ── 协议（直接喂 dict）─────────────────────────────────────────────────────
def test_initialize_negotiates_protocol():
    server = cib_mcp.McpServer(_api())
    resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2025-03-26"}})
    assert resp["result"]["protocolVersion"] == "2025-03-26"
    assert resp["result"]["capabilities"]["tools"] == {"listChanged": False}
    assert resp["result"]["serverInfo"]["name"] == "competitive-board"


def test_initialize_falls_back_on_unknown_version():
    server = cib_mcp.McpServer(_api())
    resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "1999-01-01"}})
    assert resp["result"]["protocolVersion"] == cib_mcp.SUPPORTED_PROTOCOLS[0]


def test_notifications_get_no_response():
    server = cib_mcp.McpServer(_api())
    for method in ("notifications/initialized", "notifications/cancelled"):
        assert server.handle({"jsonrpc": "2.0", "method": method}) is None, method


def test_unknown_method_is_method_not_found():
    server = cib_mcp.McpServer(_api())
    resp = server.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/explode"})
    assert resp["error"]["code"] == -32601


def test_tools_call_roundtrip():
    server = cib_mcp.McpServer(_api())
    resp = server.handle({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "search_products", "arguments": {"q": "村田"}},
    })
    assert resp["result"]["isError"] is False
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["meta"]["q"] == "村田", "query 没传到服务端"


def test_tools_call_path_param_roundtrip():
    server = cib_mcp.McpServer(_api())
    resp = server.handle({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "get_product", "arguments": {"product_id": 12}},
    })
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["data"]["id"] == 12


def test_tools_call_write_roundtrip():
    server = cib_mcp.McpServer(_api())
    resp = server.handle({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "write_specs",
                   "arguments": {"items": [{"product_id": 1}, {"product_id": 2}],
                                 "dry_run": True}},
    })
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["data"]["applied"] == 2
    assert payload["meta"]["dry_run"] is True, "dry_run 必须真的进到 query"


def test_tools_call_unknown_tool_lists_options():
    server = cib_mcp.McpServer(_api())
    resp = server.handle({
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {"name": "nope", "arguments": {}},
    })
    assert resp["result"]["isError"] is True
    text = resp["result"]["content"][0]["text"]
    assert "get_context" in text, "报错要列出可用工具，方便模型纠正"


def test_tools_call_surfaces_403_as_is_error():
    server = cib_mcp.McpServer(cib_mcp.Api(f"http://127.0.0.1:{_port()}", "wrong", timeout=10))
    resp = server.handle({
        "jsonrpc": "2.0", "id": 6, "method": "tools/call",
        "params": {"name": "get_context", "arguments": {}},
    })
    # 令牌错时连 tools/list 都拉不到 —— 必须给出可操作的原因，而不是空列表
    assert resp["result"]["isError"] is True
    assert "UNAUTHENTICATED" in resp["result"]["content"][0]["text"]


def test_unreachable_host_is_reported_clearly():
    server = cib_mcp.McpServer(cib_mcp.Api("http://127.0.0.1:1", TOKEN, timeout=2))
    resp = server.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/list"})
    assert "error" in resp
    assert "连不上" in resp["error"]["message"]


# ── stdio 往返（真子进程）──────────────────────────────────────────────────
def _run_stdio(messages: list[dict]) -> tuple[list[dict], str]:
    env = {**os.environ, "CIB_BASE_URL": f"http://127.0.0.1:{_port()}",
           "CIB_TOKEN": TOKEN}
    env.pop("http_proxy", None)
    env.pop("HTTP_PROXY", None)
    proc = subprocess.run(
        [sys.executable, str(MCP_SCRIPT)],
        input="\n".join(json.dumps(m, ensure_ascii=False) for m in messages) + "\n",
        capture_output=True, text=True, timeout=120, env=env,
    )
    out = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        # 每一行都必须是 JSON —— 混进日志就是客户端 parse 失败
        out.append(json.loads(line))
    return out, proc.stderr


def test_stdio_end_to_end():
    out, stderr = _run_stdio([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "get_context", "arguments": {}}},
    ])

    assert len(out) == 3, f"通知不该产生响应，实际拿到 {len(out)} 条"
    assert out[0]["result"]["protocolVersion"] == "2025-06-18"
    names = [t["name"] for t in out[1]["result"]["tools"]]
    assert names == [t["name"] for t in TOOLS], names
    payload = json.loads(out[2]["result"]["content"][0]["text"])
    assert payload["data"]["counts"]["products"] == 11
    # 日志确实走了 stderr（内容不重要，存在即可）
    assert "启动" in stderr


def test_stdio_survives_bad_input():
    """一行坏 JSON 不该让整个 server 退出 —— 客户端会一直往同一个进程写。"""
    env = {**os.environ, "CIB_BASE_URL": f"http://127.0.0.1:{_port()}", "CIB_TOKEN": TOKEN}
    proc = subprocess.run(
        [sys.executable, str(MCP_SCRIPT)],
        input="not json\n" + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n",
        capture_output=True, text=True, timeout=120, env=env,
    )
    lines = [json.loads(x) for x in proc.stdout.splitlines() if x.strip()]
    assert lines[0]["error"]["code"] == -32700
    assert lines[1]["result"] == {}, "坏消息之后必须还能继续服务"


# ── 运行器 ─────────────────────────────────────────────────────────────────
ORDER = [
    test_manifest_maps_to_input_schema,
    test_path_placeholder_becomes_required_param,
    test_missing_path_param_raises,
    test_get_args_go_to_query,
    test_write_args_go_to_body_but_dry_run_to_query,
    test_inline_query_in_path_is_kept,
    test_render_success_strips_request_id,
    test_render_error_sets_is_error,
    test_initialize_negotiates_protocol,
    test_initialize_falls_back_on_unknown_version,
    test_notifications_get_no_response,
    test_unknown_method_is_method_not_found,
    test_tools_call_roundtrip,
    test_tools_call_path_param_roundtrip,
    test_tools_call_write_roundtrip,
    test_tools_call_unknown_tool_lists_options,
    test_tools_call_surfaces_403_as_is_error,
    test_unreachable_host_is_reported_clearly,
    test_stdio_end_to_end,
    test_stdio_survives_bad_input,
]


def main() -> int:
    failed = 0
    for fn in ORDER:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {fn.__name__}  {exc}")
        except Exception as exc:
            failed += 1
            print(f"  ERROR {fn.__name__}  {type(exc).__name__}: {exc}")
    print(f"\n{len(ORDER) - failed}/{len(ORDER)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
