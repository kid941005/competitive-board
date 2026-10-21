#!/usr/bin/env bash
# 容器入口：等数据库就绪 → 按需灌入示例数据 → 交给 uvicorn。
# 所有行为都由环境变量控制，不改代码即可切换 SQLite / PostgreSQL。
set -euo pipefail

log() { printf '[entrypoint] %s\n' "$*"; }

# 解释器可用 CIB_PYTHON 覆盖，便于在非容器环境验证本脚本
PY_BIN="${CIB_PYTHON:-python}"

# ── 1. 等 PostgreSQL 就绪 ──────────────────────────────────────────────────
# 不依赖外部 wait-for-it 脚本，直接用应用的 engine 探测，失败重试 30 次（约 60 秒）。
if [[ "${DATABASE_URL:-}" == postgresql* ]]; then
  log "检测到 PostgreSQL，等待就绪…"
  "$PY_BIN" - <<'PY'
import sys
import time

from sqlalchemy import text

from app.db import engine

for attempt in range(1, 31):
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        print("[entrypoint] 数据库已就绪", flush=True)
        sys.exit(0)
    except Exception as exc:  # noqa: BLE001
        reason = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        print(f"[entrypoint] 未就绪 {attempt}/30：{reason}", flush=True)
        time.sleep(2)
print("[entrypoint] 数据库连接超时，退出", flush=True)
sys.exit(1)
PY
fi

# ── 2. 示例数据 ────────────────────────────────────────────────────────────
# app.seed 自带幂等判断：库里已有实体就返回 {"skipped": 1}，不会覆盖真实数据。
# 失败必须退出，不能「继续启动」：数据库都打不开的话 uvicorn 的 lifespan 也会炸，
# 结果是同一段 traceback 在日志里刷两遍、容器反复重启，真实原因被淹掉。
if [[ "${CIB_SEED_DEMO:-0}" == "1" ]]; then
  log "写入示例数据（已有数据会自动跳过）…"
  if ! "$PY_BIN" -m app.seed; then
    log "示例数据写入失败：数据库不可用，退出。上面的报错正文里有修复命令。"
    exit 1
  fi
fi

# ── 3. 交给容器 CMD ────────────────────────────────────────────────────────
log "启动：$*"
exec "$@"
