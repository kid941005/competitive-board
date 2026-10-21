"""把实例上已建公司的元信息对齐到 seed 口径（kind / name_en / region / tier）。

存在的理由：0.9.5 之前 `POST /import`、`POST /entities` 会把 kind 一律建成
competitor、tier 一律建成 C，name_en/region 直接丢掉。数据已经进去了，
改代码救不回来，只能补一遍。

口径来源：backend/app/seed.py 的 ENTITIES 表 —— 那是对外声明过的「官方值」，
不是这里随手编的。TARGET 按需增删（实例上多出来的公司不写就跳过）。

用法（默认预演，加 --apply 才写）：
    CIB_BASE_URL=https://host:port CIB_TOKEN=cib_xxx python scripts/align-entity-meta.py
    ... --apply
幂等：值已一致的公司不会被写入，也不产生审计记录。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("CIB_BASE_URL", "https://sappland.hqgg.top:59826").rstrip("/")
TOKEN = os.environ["CIB_TOKEN"]
APPLY = "--apply" in sys.argv

# 只放「与 seed 一致」的权威值。seed 里 self 的 name_en 本来就是 None，这里也留空。
TARGET = {
    "左蓝微":       {"kind": "self", "tier": "A", "region": "中国", "name_en": None},
    "村田制作所":   {"tier": "A", "region": "日本", "name_en": "Murata"},
    "巴特尔斯微技术": {"tier": "B", "region": "德国", "name_en": "Bartels Mikrotechnik"},
    "xMEMS Labs":   {"tier": "B", "region": "美国", "name_en": "xMEMS"},
    "Frore Systems": {"tier": "B", "region": "美国", "name_en": "Frore Systems"},
    "摩根先进材料": {"tier": "A", "region": "英国", "name_en": "Morgan Advanced Materials"},
    "广东奥迪威":   {"tier": "B", "region": "中国", "name_en": "AUDIOWELL"},
    "苏州攀特电陶": {"tier": "B", "region": "中国", "name_en": "PANT"},
    "浙江嘉康电子": {"tier": "C", "region": "中国", "name_en": "JIAKANG"},
    "江苏江佳电子": {"tier": "C", "region": "中国", "name_en": "JIANGJIA"},
    # 汉得利不在 seed 里（回填时按 CSV 建出来的），英文名没查实，先只补地区与档位
    "汉得利":       {"tier": "C", "region": "中国"},
}

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with _opener.open(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"ok": False, "http": e.code, "body": e.read().decode(errors="replace")}


ents = call("GET", "/api/v1/entities?limit=100")["data"]
print(f"实例上共 {len(ents)} 家公司，目标 {len(TARGET)} 家\n")

unknown = [e["name"] for e in ents if e["name"] not in TARGET]
if unknown:
    print(f"⚠ 未列入目标的公司：{unknown}\n")

plan = []
for e in ents:
    want = TARGET.get(e["name"])
    if not want:
        continue
    diff = {k: v for k, v in want.items() if (e.get(k) or None) != (v or None)}
    if diff:
        plan.append((e["id"], e["name"], diff))

for eid, name, diff in plan:
    print(f"#{eid:<3} {name}")
    for k, v in diff.items():
        print(f"      {k}: → {v!r}")
print(f"\n需要改动 {len(plan)} 家公司")

if not APPLY:
    print("\n[预演] 未写入。加 --apply 执行。")
    sys.exit(0)

print("\n开始写入…")
fail = 0
for eid, name, diff in plan:
    r = call("PATCH", f"/api/v1/entities/{eid}", {**diff, "dry_run": False})
    if r.get("ok"):
        d = r["data"]
        print(f"  ✓ #{eid} {name} → kind={d['kind']} tier={d['tier']} "
              f"region={d['region']} name_en={d['name_en']}")
    else:
        fail += 1
        print(f"  ✗ #{eid} {name} 失败：{r}")

print(f"\n完成：成功 {len(plan) - fail} / {len(plan)}")
sys.exit(1 if fail else 0)
