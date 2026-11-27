"""内置新闻源清单：空库时预置，用户可在「消息源管理」里增删改。

选源的三条标准（不满足就别往里加）：

1. **官方优先**。友商官网的新闻/产品公告是最权威的一手信息。
2. **必须真的能解析**。下面每条都在 2026-11-26 实抓验证过（状态 200 + 有条目）。
   宁可少一个源，也不要让用户第一次打开就看一排「失败」。
3. **宁缺毋滥**。行业媒体的通稿（EE Times / EDN / IT之家 这类）不进预置：
   它们每天几十条，会把友商消息挤出第一屏。需要时在「消息源管理」里自己加，
   已验证可用的地址见 README。

官网没有 feed 的友商（Frore Systems 是 Webflow 站、Bartels 官方站无新闻栏目），
用 Google News 的检索 RSS 兜底 —— 免费、无需 key、中英文都覆盖。

kind 只用于展示（rss / atom / rdf），真实格式由解析器按内容判定。
entity 写公司名，seed 时按 name / name_en 匹配现有档案，匹配不上就留空
（消息仍在时间线上，只是不归属任何公司）。
"""

from __future__ import annotations

from urllib.parse import quote_plus

# Google News 检索 RSS。hl / gl / ceid 三个参数要一起改，否则语言和地区会错位。
GOOGLE_NEWS_ZH = "https://news.google.com/rss/search?q={query}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
GOOGLE_NEWS_EN = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"


def google_news(query: str, *, lang: str = "en") -> str:
    """按关键词生成 Google News 检索地址。加引号即精确短语，+ 即「且」。"""
    template = GOOGLE_NEWS_ZH if lang == "zh" else GOOGLE_NEWS_EN
    return template.format(query=quote_plus(query))


BUILTIN_SOURCES: list[dict] = [
    # ── 友商官方源 ────────────────────────────────────────────────────────
    {
        "name": "村田制作所 · 官方新闻",
        # 注意是 /en-us/news/rssfeed，页面上 <link rel=alternate> 暴露的就是它；
        # /en-global/rss/news、/rss/news 都是 404（2026-11-26 实测）
        "url": "https://www.murata.com/en-us/news/rssfeed",
        "kind": "rss",
        "lang": "en",
        "entity": "Murata",
    },
    {
        "name": "xMEMS · 官方博客",
        "url": "https://xmems.com/feed/",
        "kind": "rss",
        "lang": "en",
        "entity": "xMEMS",
    },
    # ── 友商检索（官网无 feed 或只有英文源时补中文/补全量）──────────────
    {
        "name": "村田 · 中文检索",
        "url": google_news("村田 压电", lang="zh"),
        "kind": "rss",
        "lang": "zh",
        "entity": "Murata",
    },
    {
        "name": "Murata · 英文检索",
        "url": google_news('"Murata" piezoelectric'),
        "kind": "rss",
        "lang": "en",
        "entity": "Murata",
    },
    {
        "name": "xMEMS · 英文检索",
        "url": google_news("xMEMS"),
        "kind": "rss",
        "lang": "en",
        "entity": "xMEMS",
    },
    {
        "name": "Frore Systems · 英文检索",
        "url": google_news('"Frore Systems"'),
        "kind": "rss",
        "lang": "en",
        "entity": "Frore Systems",
    },
    {
        "name": "Bartels · 英文检索",
        "url": google_news('"Bartels Mikrotechnik"'),
        "kind": "rss",
        "lang": "en",
        "entity": "Bartels Mikrotechnik",
    },
    # ── 品类动态（不归属具体公司，用来接住行业里的新玩家）──────────────
    {
        "name": "压电泵 · 中文检索",
        "url": google_news("压电泵", lang="zh"),
        "kind": "rss",
        "lang": "zh",
    },
    {
        "name": "压电风扇 · 中文检索",
        "url": google_news("压电风扇", lang="zh"),
        "kind": "rss",
        "lang": "zh",
    },
    {
        "name": "微泵 · 中文检索",
        "url": google_news("微泵", lang="zh"),
        "kind": "rss",
        "lang": "zh",
    },
    {
        "name": "压电散热 · 英文检索",
        "url": google_news("piezoelectric cooling"),
        "kind": "rss",
        "lang": "en",
    },
]
