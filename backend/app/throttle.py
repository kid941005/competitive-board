"""登录失败限速（F-13）。

pbkdf2 24 万次迭代只能把单次尝试拖到 ~100ms —— 挡不住「慢一点但一直试」的
账号枚举。这里按 **IP + 账号** 计数，失败 5 次后指数退避（30s 起，封顶 15 分钟），
登录成功即清零。另外单独按 IP 记一个总量，防止换着账号名扫。

为什么放内存：这是「几台机器、单进程（`--workers 1`）」的内网部署，重启一次就清空
完全可接受；落库反而要给认证路径加写操作（正是 F-10 修掉的那类问题）。
代价写清楚：**多副本部署时每个副本各算各的**，要跨副本生效得换 Redis，
`deploy/` 里那个 Redis 就是给这种情况准备的。

不按「只按账号」限速：那等于给攻击者一个「把管理员账号锁死」的开关。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

# 连续失败多少次开始退避
MAX_FAILURES = 5
# 第一次退避的秒数，之后每多失败一次翻倍，直到封顶
BASE_DELAY = 30
MAX_DELAY = 15 * 60
# 超过这么久没有新的失败，计数自然作废（避免陈年失败把正常用户越积越久）
WINDOW = 15 * 60
# 同一 IP 对所有账号的失败总量上限（防换着用户名扫）
IP_MAX_FAILURES = 20
IP_WINDOW = 10 * 60
# 表比这个大就顺手清理一遍，内存不会随扫描无限涨
MAX_ENTRIES = 2000


@dataclass
class _Bucket:
    failures: int = 0
    locked_until: float = 0.0
    touched: float = field(default_factory=time.monotonic)


_lock = threading.Lock()
_buckets: dict[str, _Bucket] = {}


def _key(ip: str, username: str | None = None) -> str:
    return f"{ip}|{(username or '*').strip().casefold()}"


def _now() -> float:
    return time.monotonic()


def _prune(now: float) -> None:
    if len(_buckets) <= MAX_ENTRIES:
        return
    for key in [k for k, b in _buckets.items() if now - b.touched > WINDOW]:
        _buckets.pop(key, None)


def check(ip: str, username: str) -> int:
    """还要等多少秒才能再试；0 表示可以尝试。"""
    now = _now()
    wait = 0.0
    with _lock:
        for key in (_key(ip, username), _key(ip)):
            bucket = _buckets.get(key)
            if bucket is None:
                continue
            if now - bucket.touched > WINDOW:
                _buckets.pop(key, None)
                continue
            wait = max(wait, bucket.locked_until - now)
    return max(0, int(wait + 0.999))


def fail(ip: str, username: str) -> int:
    """记一次失败，返回本次触发的等待秒数（0 = 还没到退避阈值）。"""
    now = _now()
    with _lock:
        _prune(now)
        wait = 0.0

        bucket = _buckets.setdefault(_key(ip, username), _Bucket())
        if now - bucket.touched > WINDOW:
            bucket.failures = 0
        bucket.failures += 1
        bucket.touched = now
        if bucket.failures >= MAX_FAILURES:
            delay = min(BASE_DELAY * (2 ** (bucket.failures - MAX_FAILURES)), MAX_DELAY)
            bucket.locked_until = now + delay
            wait = max(wait, delay)

        ip_bucket = _buckets.setdefault(_key(ip), _Bucket())
        if now - ip_bucket.touched > IP_WINDOW:
            ip_bucket.failures = 0
        ip_bucket.failures += 1
        ip_bucket.touched = now
        if ip_bucket.failures >= IP_MAX_FAILURES:
            delay = min(BASE_DELAY * (ip_bucket.failures - IP_MAX_FAILURES + 1), MAX_DELAY)
            ip_bucket.locked_until = now + delay
            wait = max(wait, delay)
        return int(wait + 0.999)


def succeed(ip: str, username: str) -> None:
    """登录成功：把该账号的计数清零（不动 IP 总量，否则换账号扫就没痕迹了）。"""
    with _lock:
        _buckets.pop(_key(ip, username), None)


def reset_all() -> None:
    """仅供测试与运维排查使用。"""
    with _lock:
        _buckets.clear()
