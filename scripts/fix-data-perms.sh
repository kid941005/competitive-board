#!/usr/bin/env bash
# 修复「容器内数据目录不可写」导致的启动失败。
#
# 症状：容器反复重启，日志里反复出现
#   sqlite3.OperationalError: unable to open database file
# 原因：镜像以 uid 10001（非 root）运行，而数据目录的属主不是它 ——
#   命名卷是手工建的、或 bind mount 挂到了 root 拥有的宿主目录。
#
# 用法（在 docker-compose.yml 所在目录执行）：
#   bash scripts/fix-data-perms.sh           # 预演：只打印将要执行的动作
#   bash scripts/fix-data-perms.sh --apply   # 真的改，然后重启并验证
#
# 可覆盖的环境变量：CONTAINER / VOLUME / SERVICE / TARGET_UID
#
# 说明：这个脚本必须在**部署机**上跑（Linux / NAS 的 shell），
# Windows 侧只能通过 WSL 或 SSH 进入部署机执行。
set -euo pipefail

CONTAINER="${CONTAINER:-competitive-board}"
VOLUME="${VOLUME:-competitive-board-data}"
SERVICE="${SERVICE:-app}"
TARGET_UID="${TARGET_UID:-10001}"
APPLY=0
[[ "${1:-}" == "--apply" ]] && APPLY=1

say() { printf '%s\n' "$*"; }
act() {
  if [[ $APPLY -eq 1 ]]; then
    say "  \$ $*"
    "$@"
  else
    say "  [预演] $*"
  fi
}

command -v docker >/dev/null 2>&1 || { say "找不到 docker 命令"; exit 1; }

say "== 1. 定位数据目录 =="
mount_info=""
if docker inspect "$CONTAINER" >/dev/null 2>&1; then
  mount_info=$(docker inspect "$CONTAINER" \
    --format '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Type}}|{{.Source}}{{end}}{{end}}' 2>/dev/null || true)
fi

if [[ -z "$mount_info" ]]; then
  say "  容器 $CONTAINER 未运行或未挂载 /app/data，按默认命名卷 $VOLUME 处理"
  kind=volume
  source="$VOLUME"
else
  kind="${mount_info%%|*}"
  source="${mount_info#*|}"
  say "  挂载类型：$kind"
  say "  实际来源：$source"
fi

say
say "== 2. 当前状态 =="
case "$kind" in
  volume)
    docker volume inspect "$source" >/dev/null 2>&1 \
      || { say "  卷 $source 不存在，先 docker compose up -d 让它建出来"; exit 1; }
    probe=$(docker run --rm -v "$source:/d" alpine sh -c \
      'stat -c "%u:%g %a" /d 2>/dev/null || echo "unknown"')
    say "  卷根目录属主:权限 = $probe（目标 $TARGET_UID:$TARGET_UID）"
    ;;
  bind)
    if [[ -d "$source" ]]; then
      say "  宿主目录 = $source"
      say "  宿主属主:权限 = $(stat -c '%u:%g %a' "$source" 2>/dev/null || echo unknown)"
    else
      say "  宿主目录 $source 不存在"; exit 1
    fi
    ;;
  *)
    say "  未识别的挂载类型：$kind"; exit 1
    ;;
esac

say
say "== 3. 修正属主 =="
# 优先用 docker compose run 复用同一份卷配置。
# 直接 `docker run -v 卷名:/d` 有个陷阱：卷名写错时 Docker 会**静默创建一个新卷**，
# chown 成功退出，改的却是一份空卷 —— 命令看起来完全成功，问题一点没解决。
compose_file=""
for f in docker-compose.yml docker-compose.yaml compose.yml compose.yaml; do
  [[ -f "$f" ]] && { compose_file="$f"; break; }
done

if [[ -n "$compose_file" ]]; then
  say "  使用 $compose_file 复用同一份卷配置（避免卷名猜错）"
  act docker compose run --rm --user root --entrypoint sh "$SERVICE" -c \
    "chown -R $TARGET_UID:$TARGET_UID /app/data && stat -c '  改后：属主=%u:%g 权限=%a' /app/data"
else
  say "  未找到 compose 文件，退回按已识别的挂载操作"
  case "$kind" in
    volume)
      act docker run --rm -v "$source:/d" alpine chown -R "$TARGET_UID:$TARGET_UID" /d
      ;;
    bind)
      if [[ "$(id -u)" -ne 0 ]]; then
        say "  bind mount 改属主需要 root，请手动执行："
        say "    sudo chown -R $TARGET_UID:$TARGET_UID $source"
        [[ $APPLY -eq 1 ]] && { say "  （脚本不是以 root 运行，已跳过）"; exit 1; }
      else
        act chown -R "$TARGET_UID:$TARGET_UID" "$source"
      fi
      ;;
  esac
fi

if [[ $APPLY -eq 0 ]]; then
  say
  say "以上是预演。确认无误后加 --apply 真正执行。"
  exit 0
fi

say
say "  复核属主是否真的变了（还是 0:0 说明这个文件系统不支持 chown）："
after=""
if [[ -n "$compose_file" ]]; then
  after=$(docker compose run --rm --user root --entrypoint sh "$SERVICE" -c \
    'stat -c "%u:%g %a" /app/data' 2>/dev/null || true)
else
  after=$(docker run --rm -v "$source:/d" alpine sh -c 'stat -c "%u:%g %a" /d' 2>/dev/null || true)
fi
say "  当前 = ${after:-未取到}（期望 $TARGET_UID:$TARGET_UID）"
case "$after" in
  "$TARGET_UID:"*)
    say "  属主已修正"
    ;;
  *)
    say
    say "  !! 属主没有变成 $TARGET_UID，说明 chown 在这个文件系统上无效。"
    say "     常见于 SMB/NFS 共享目录（服务端强制映射属主），或 NAS 把卷存在受限文件系统上。"
    say "     两条出路："
    say "       a) 让容器以该目录的属主身份运行 —— 在 compose 的 app 服务下加："
    say "            user: \"<目录属主 uid>:<目录属主 gid>\""
    say "       b) 把数据目录挪到本地磁盘上的路径（不要用网络共享）"
    exit 1
    ;;
esac

say
say "== 4. 重启并验证 =="
docker restart "$CONTAINER" >/dev/null
say "  已重启 $CONTAINER，等待健康检查（最多 90 秒）…"
for i in $(seq 1 18); do
  sleep 5
  status=$(docker inspect "$CONTAINER" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' 2>/dev/null || echo unknown)
  printf '  [%02d] %s\n' "$i" "$status"
  case "$status" in
    healthy) say "  OK：服务已就绪"; exit 0 ;;
    unhealthy) say "  仍不健康，看日志：docker logs --tail 40 $CONTAINER"; exit 1 ;;
  esac
done
say "  超时未见 healthy，看日志：docker logs --tail 60 $CONTAINER"
exit 1
