"""档案室（archive）—— 原话的底账。

跟记忆的分工说清楚，别混：

- `memory` 记的是**提炼过的一句话**（"她说别熬夜"），会被分级、会被压、会被删；
- 档案室记的是那句话**当时**的原话和上下文（"23:41 主人：别熬夜了"），**永不删**。

她日常想不起细节时，就从这儿翻：先给目录（第几页、哪天、一句主题），
她点页码再看整页正文。

三条规矩：
1. **只翻本窗**：私聊对私聊必须同一扇窗（跟 `memory.scene_ok` 同一条铁律），
   跨窗口的页一律不给。`<rooms>` 那句"别的窗口里的话一句都不许带出来"在这里同样成立。
2. **塞回上下文有上限**：`open_page(max_chars=1200)`，档案涨到 5000 页也一样贵。
3. **页只增不删**：同一天同一窗同一类只保留最新一版，档案室不清库。

索引是二元组倒排（词 → 页），查档案 = "按词取候选页再排序"，
不逐页读、不调模型（2026-11-02 压测：5000 页 0.4ms、12.7MB）。
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Any, Iterable

from . import config, db, memory

# 一次翻页最多塞多少字进上下文。这条是成本闸门：档案再大，单次也不涨。
OPEN_MAX = 1200
# 目录给几条。每条「第 N 页 · 某天 · 一句主题」，约 40 字，5 条 ≈ 200 字。
CATALOG_TOPN = 5
# 目录里那一小段上下文（让她认得出是哪次聊天）。
SNIPPET = 48

KINDS = ("chat", "life", "self")
_KIND_LABEL = {"chat": "聊天", "life": "做的事", "self": "日记"}


def _conf() -> dict[str, Any]:
    """config.json 的 archive 段（没配就用下面这几个默认值）。

    `open_max` 是成本闸门：档案涨到多大，一次翻页塞进上下文的字都这么多。
    """
    try:
        return dict(config.cfg().get("archive") or {})
    except Exception:  # noqa: BLE001
        return {}


def open_max() -> int:
    try:
        return max(200, int(_conf().get("open_max") or OPEN_MAX))
    except Exception:  # noqa: BLE001
        return OPEN_MAX


def catalog_topn() -> int:
    try:
        return max(1, int(_conf().get("catalog_topn") or CATALOG_TOPN))
    except Exception:  # noqa: BLE001
        return CATALOG_TOPN


# ---------------------------------------------------------------- 时间

def day_of(ts: int) -> str:
    return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")


def bounds(day: str) -> tuple[int, int]:
    """一天的头尾（本地时区，整数秒）。"""
    d = datetime.strptime(day, "%Y-%m-%d")
    return int(d.timestamp()), int((d + timedelta(days=1)).timestamp())


def _hhmm(ts: int) -> str:
    return datetime.fromtimestamp(int(ts)).strftime("%H:%M")


# ---------------------------------------------------------------- 命名

async def _room_name(conv_key: str) -> str:
    row = await db.fetchone("SELECT name FROM room WHERE conv_key = ?", (conv_key,))
    name = str((row or {}).get("name") or "").strip()
    if name:
        return name
    tail = str(conv_key or "").rsplit(":", 1)[-1]
    return f"窗口{tail}" if tail else "这扇窗"


# ---------------------------------------------------------------- 建页

def _tokens(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for t in memory.tokenize(text or ""):
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


async def put_page(
    day: str,
    conv_key: str,
    kind: str,
    title: str,
    text: str,
    *,
    ts: int = 0,
    src: str = "",
    keep_longer: bool = False,
) -> dict[str, Any]:
    """写一页（同一天+同窗+同类覆盖旧版，并重建这一页的倒排）。

    `keep_longer=True`：已有一版更长的就留着不动。为什么需要——
    从 AstrBot 回填的历史页是**整天完整原话**，而生活流水只留了每阵聊天的
    最后 1200 字；夜里再按流水归一页会把完整历史覆盖成残缺版。
    """
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "空页不写"}
    conn = await db.connect()
    if keep_longer:
        old = await db.fetchone(
            "SELECT id, chars FROM archive_page WHERE day=? AND conv_key=? AND kind=?",
            (day, conv_key, kind),
        )
        if old and int(old["chars"] or 0) >= len(text):
            # 正文留着不动，但标题是索引的一部分（夜里会给它一个主题），该换还是要换。
            await conn.execute(
                "UPDATE archive_page SET title=? WHERE id=?", ((title or "")[:80], int(old["id"]))
            )
            await conn.commit()
            return {"ok": True, "id": int(old["id"]), "chars": int(old["chars"]), "kept": True}
    cur = await conn.execute(
        """INSERT INTO archive_page(day, conv_key, kind, title, text, chars, ts, src, built_at)
           VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(day, conv_key, kind) DO UPDATE SET
             title=excluded.title, text=excluded.text, chars=excluded.chars,
             ts=excluded.ts, src=excluded.src, built_at=excluded.built_at""",
        (day, conv_key, kind, (title or "")[:80], text, len(text), int(ts or 0),
         src or "", int(time.time())),
    )
    await conn.commit()
    row = await db.fetchone(
        "SELECT id FROM archive_page WHERE day=? AND conv_key=? AND kind=?", (day, conv_key, kind)
    )
    pid = int(row["id"])
    await conn.execute("DELETE FROM archive_token WHERE page_id = ?", (pid,))
    toks = _tokens(text)
    if toks:
        await conn.executemany(
            "INSERT INTO archive_token(token, page_id) VALUES(?,?)",
            [(t, pid) for t in toks],
        )
    await conn.commit()
    return {"ok": True, "id": pid, "chars": len(text), "tokens": len(toks)}


def _first_line(body: str) -> str:
    for line in (body or "").split("\n"):
        s = line.strip()
        if not s:
            continue
        return s.split("：", 1)[1] if "：" in s else s
    return ""


def _clean(s: str, n: int = 20) -> str:
    """把一句话压成能当标题短标签的样子：去空白、去两头引号、截断。

    原始流水里常有多行、表情、引号，直接塞进标题会让目录没法扫
    （第一版就出过「从「在吗」到「才十点，我这时候睡不着\n\n你去打你的…」」
    这种带换行的标题，还会出「??,??」这种没汉字的开场）。
    """
    body = re.sub(r"\s+", " ", str(s or "")).strip(" 「」『』\"'\n")
    body = body.replace("「", "").replace("」", "").strip()
    if not body:
        return ""
    return body if len(body) <= n else body[:n] + "…"


def theme_of(text: str, n: int = 20) -> str:
    """从日记里取第一句当这一天的主题（索引用的标题）。

    只取第一句：整篇日记太长，塞进目录会没法扫。取不到就返回空，
    标题退回「和谁聊了 N 句」那种确定性写法。
    """
    body = re.sub(r"^[#>*\-\s]+", "", str(text or "")).strip()
    if not body:
        return ""
    first = re.split(r"[。！？!?\n]", body, maxsplit=1)[0]
    return _clean(first, n)


async def set_title(page_id: int, title: str) -> dict[str, Any]:
    """只改一页的标题（重排索引时不碰正文）。"""
    if not page_id:
        return {"ok": False, "error": "没给页码"}
    conn = await db.connect()
    await conn.execute(
        "UPDATE archive_page SET title=? WHERE id=?", ((title or "")[:80], int(page_id))
    )
    await conn.commit()
    return {"ok": True, "page": int(page_id), "title": (title or "")[:80]}


def title_from_text(day: str, text: str, kind: str = "chat") -> tuple[int, str]:
    """从一页正文里数出"聊了几轮"，并给一个干净标题（给历史页重排用）。

    回填的历史页是整天完整原话，标题当初是拿第一句/最后一句硬拼的，
    带换行、带「??,??」这种没有汉字的开场，目录根本没法扫。
    kind 不是 chat 的页（做过的事、日记）标题本来就有意义，原样返回。
    """
    body = str(text or "")
    name_m = re.search(r"^#\s*\S+\s*·\s*(\S+)", body, re.M)
    name = (name_m.group(1) if name_m else "主人").strip() or "主人"
    # 历史页的长行格式是「11:36 主人：在吗」「11:36 我：…」（时间 + 说话人 + 冒号），
    # 也有「## 11:36」的分段头和没写说话人的续行；只把"时间 + 可选说话人 + 冒号"
    # 那种行数成一轮对话。
    msgs = []
    for ln in body.split("\n"):
        m = re.match(r"^\s*(?:\d{1,2}:\d{2})\s*(?:\S{1,10})?[：:]\s*(.*)$", ln)
        if m:
            msgs.append(m.group(1).strip())
    turns = len(msgs)
    if kind != "chat":
        return turns, ""
    # 开场常是乱码/表情（"??,??"），挑第一句有汉字的当"从哪说起"
    first = ""
    for m in msgs[:5]:
        c = _clean(m, 14)
        if re.search(r"[\u4e00-\u9fff]", c):
            first = c
            break
    last = ""
    for m in reversed(msgs[-5:]):
        c = _clean(m, 14)
        if re.search(r"[\u4e00-\u9fff]", c):
            last = c
            break
    if first and last and first != last:
        title = f"和{name}聊了 {turns} 句：从「{first}」到「{last}」"
    elif turns:
        title = f"和{name}聊了 {turns} 句"
    else:
        title = f"{day} · {name}的一页"
    return turns, title[:80]


async def build_conv_day(day: str, conv_key: str, theme: str = "") -> dict[str, Any]:
    """把某天某扇窗的流水归成页（聊天一页、做过的事一页）。

    `theme`：夜里复盘给的这一天的主题（一句人话）。有主题就用主题当索引标题，
    目录才像"档案室"而不是一堆"聊了 N 句"。
    """
    start, end = bounds(day)
    rows = await db.fetchall(
        "SELECT ts, kind, summary, detail, n_turns FROM life_log "
        "WHERE conv_key = ? AND ts >= ? AND ts < ? ORDER BY ts, id",
        (conv_key, start, end),
    )
    out: dict[str, Any] = {"ok": True, "day": day, "conv_key": conv_key, "pages": []}
    if not rows:
        return out

    chat = [r for r in rows if str(r["kind"]) == "chat"]
    acts = [r for r in rows if str(r["kind"]) != "chat"]
    name = await _room_name(conv_key)

    if chat:
        parts: list[str] = []
        turns = 0
        for r in chat:
            turns += int(r["n_turns"] or 1)
            body = str(r["detail"] or r["summary"] or "").strip()
            if body:
                parts.append(f"## {_hhmm(r['ts'])}\n{body}")
        if parts:
            first = _clean(_first_line(str(chat[0]["detail"] or chat[0]["summary"] or "")), 14)
            last = _clean(_first_line(str(chat[-1]["detail"] or chat[-1]["summary"] or "")), 14)
            if theme:
                title = f"{_clean(theme, 26)}｜和{name}聊了 {turns} 句"
            elif first or last:
                title = f"和{name}聊了 {turns} 句：从「{first}」到「{last}」"
            else:
                title = f"和{name}聊了 {turns} 句"
            text = f"# {day} · {name}\n\n" + "\n\n".join(parts)
            out["pages"].append(await put_page(day, conv_key, "chat", title, text,
                                               ts=int(chat[0]["ts"]), src="life",
                                               keep_longer=True))

    if acts:
        lines = []
        labels = []
        for r in acts:
            k = str(r["kind"])
            lab = {"reading": "看书", "writing": "写东西", "bili": "B站",
                   "mail": "门口的信", "chat": "聊天"}.get(k, k)
            lines.append(f"- {_hhmm(r['ts'])} {lab}：{str(r['summary'] or '').strip()}")
            if lab not in labels:
                labels.append(lab)
        title = f"那天做过的事：{'、'.join(labels)[:40]}"
        text = f"# {day} · {name} 做过的事\n\n" + "\n".join(lines)
        out["pages"].append(await put_page(day, conv_key, "life", title, text,
                                           ts=int(acts[0]["ts"]), src="life"))
    return out


async def build_day(day: str, conv_keys: Iterable[str] | None = None, theme: str = "") -> dict[str, Any]:
    """把某一天全部窗口的流水都归成页。不传 conv_keys 就从流水里自己找。"""
    start, end = bounds(day)
    if conv_keys is None:
        keys = [str(r["conv_key"]) for r in await db.fetchall(
            "SELECT DISTINCT conv_key FROM life_log WHERE ts >= ? AND ts < ? AND conv_key <> ''",
            (start, end),
        )]
    else:
        keys = [k for k in conv_keys if k]
    pages = 0
    made = []
    for k in keys:
        res = await build_conv_day(day, k, theme=theme)
        n = len(res.get("pages") or [])
        pages += n
        if n:
            made.append(k)
    return {"ok": True, "day": day, "conv_keys": made, "pages": pages}


async def put_diary(day: str, text: str, *, mood: str = "") -> dict[str, Any]:
    """把她的日记记成一页（kind='self'，conv_key='self'，永远只有她自己看得见）。"""
    title = f"日记 {day}" + (f"·{mood}" if mood else "")
    return await put_page(day, "self", "self", title, str(text or "").strip(), src="diary")


# ---------------------------------------------------------------- 查

async def _page_rows(ids: list[int], conv_key: str) -> list[dict]:
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    rows = await db.fetchall(
        f"SELECT id, day, conv_key, kind, title, text, chars, ts FROM archive_page "
        f"WHERE id IN ({marks}) AND conv_key = ?",
        (*ids, conv_key),
    )
    return list(rows or [])


def _snippet(text: str, tokens: list[str], width: int = SNIPPET) -> str:
    body = re.sub(r"\s+", " ", (text or "").strip())
    pos = -1
    for t in tokens:
        p = body.find(t)
        if p >= 0:
            pos = p
            break
    if pos < 0:
        return body[:width]
    a = max(0, pos - 8)
    return body[a:a + width].strip()


async def catalog(query: str, conv_key: str, topn: int = 0) -> list[dict[str, Any]]:
    """先给目录：按词倒排找候选页，只看本窗，按"命中词数 + 新旧"排。

    倒排里存的是**二元组**（"熬夜"），所以单字查询（"B站"、"猫"这种被切出
    一个字或英文短词的）走不通；这类查询退回到 `text LIKE`，页数不多时很便宜。
    """
    q = str(query or "").strip()
    if not q:
        return []
    topn = int(topn or catalog_topn())
    toks = _tokens(q)
    if not toks:
        return []
    order: dict[int, int] = {}
    strong = [t for t in toks if len(t) >= 2]
    if strong:
        marks = ",".join("?" for _ in strong)
        hits = await db.fetchall(
            f"SELECT page_id, COUNT(DISTINCT token) AS m FROM archive_token "
            f"WHERE token IN ({marks}) GROUP BY page_id ORDER BY m DESC LIMIT 200",
            tuple(strong),
        )
        for r in hits:
            order[int(r["page_id"])] = max(order.get(int(r["page_id"]), 0), int(r["m"]))
    weak = [t for t in toks if len(t) < 2]
    if weak and len(order) < 40:
        for t in weak:
            rows = await db.fetchall(
                "SELECT id FROM archive_page WHERE conv_key = ? AND text LIKE ? "
                "ORDER BY ts DESC LIMIT 40",
                (conv_key, f"%{t}%"),
            )
            for r in rows:
                pid = int(r["id"])
                order[pid] = max(order.get(pid, 0), 1)
    if not order:
        return []
    pages = await _page_rows(list(order.keys()), conv_key)
    pages.sort(key=lambda r: (order.get(int(r["id"]), 0), int(r["ts"] or 0)), reverse=True)
    out = []
    for r in pages[:max(1, int(topn))]:
        out.append({
            "page": int(r["id"]),
            "day": str(r["day"]),
            "title": str(r["title"] or ""),
            "kind": str(r["kind"]),
            "snippet": _snippet(str(r["text"]), toks),
            "chars": int(r["chars"] or 0),
            "matched": order.get(int(r["id"]), 0),
        })
    return out


async def open_page(page_id: int, conv_key: str, max_chars: int = 0) -> dict[str, Any]:
    """翻一整页。超出上限就截。

    长页（比如 9000 字的聊天日页）**不能只给开头**——"她最后一句说什么"这种问题
    就答不出来，还会让她以为档案只有前半段。所以按「开头 + 中间省略 + 结尾」给：
    开头占三分之二，结尾占三分之一，中间用一行注明省了多少字。
    """
    rows = await _page_rows([int(page_id)], conv_key)
    if not rows:
        return {"ok": False, "error": "那页不在这扇窗里"}
    r = rows[0]
    text = str(r["text"] or "")
    cut = int(max_chars or open_max())
    if len(text) <= cut:
        shown = text
    else:
        head = max(1, (cut * 2) // 3)
        tail_n = max(1, cut - head - 24)
        shown = (text[:head]
                 + f"\n…（中间省了 {len(text) - head - tail_n} 字）…\n"
                 + text[-tail_n:])
    return {
        "ok": True,
        "page": int(r["id"]),
        "day": str(r["day"]),
        "title": str(r["title"] or ""),
        "kind": str(r["kind"]),
        "chars": len(text),
        "shown": len(shown),
        "text": shown,
    }


_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_num(s: str) -> int:
    """「十」「十一」「二十」「三十一」→ 10 / 11 / 20 / 31。"""
    s = (s or "").strip()
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if s.startswith("十"):
        return 10 + _CN_DIGIT.get(s[1:2], 0)
    if "十" in s:
        a, _, b = s.partition("十")
        return _CN_DIGIT.get(a, 0) * 10 + (_CN_DIGIT.get(b[:1], 0) if b else 0)
    return _CN_DIGIT.get(s[:1], 0)


def parse_day(q: str, today: str = "") -> str:
    """从一句话里认出"她说的是哪天"，认不出就给空串。

    认这些写法：`2026-10-31`、`2026年10月31日`、`10月31日`、`10月31号`、
    `十月三十一日`、`10-31`、`10.31`、`今天/昨天/前天`。
    年份缺省用今年；算出来的日子比今天还晚一天以上，就当成去年（跨年时才对得上）。
    """
    s = str(q or "").strip()
    if not s:
        return ""
    base = today or day_of(int(time.time()))
    try:
        now = datetime.strptime(base, "%Y-%m-%d")
    except ValueError:
        now = datetime.now()
    for word, delta in (("今天", 0), ("昨天", 1), ("前天", 2)):
        if word in s:
            return day_of(int((now - timedelta(days=delta)).timestamp()))
    m = re.search(r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return _mk_day(y, mo, d)
    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", s)
    if m:
        return _mk_day(now.year, int(m.group(1)), int(m.group(2)), now)
    m = re.search(r"([一二三四五六七八九十]{1,3})\s*月\s*([一二三四五六七八九十]{1,3})\s*[日号]", s)
    if m:
        return _mk_day(now.year, _cn_num(m.group(1)), _cn_num(m.group(2)), now)
    # 只说"三十一号""31号"（没提哪个月）→ 当本月算。
    m = re.fullmatch(r"\s*([一二三四五六七八九十]{1,3}|\d{1,2})\s*[日号]\s*", s)
    if m:
        return _mk_day(now.year, now.month, _cn_num(m.group(1)), now)
    m = re.search(r"(\d{1,2})[-.](\d{1,2})", s)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return _mk_day(now.year, mo, d, now)
    return ""


def _mk_day(year: int, month: int, day: int, now: "datetime | None" = None) -> str:
    try:
        got = datetime(year, month, day)
    except ValueError:
        return ""
    if now is not None and got.date() > (now + timedelta(days=1)).date():
        try:
            got = got.replace(year=year - 1)
        except ValueError:
            return ""
    return got.strftime("%Y-%m-%d")


async def span(conv_key: str) -> str:
    """这扇窗的档案覆盖到哪天（"10 月 20 号"这种问早了的，得告诉她要到几号才有）。"""
    row = await db.fetchone(
        "SELECT MIN(day) AS a, MAX(day) AS b FROM archive_page WHERE conv_key = ?",
        (conv_key,),
    )
    a, b = (str(row["a"] or ""), str(row["b"] or "")) if row else ("", "")
    if not a:
        return "还空着"
    return a if a == b else f"{a} 到 {b}"


async def find_day(day: str, conv_key: str) -> list[dict[str, Any]]:
    """某天这扇窗存了哪几页（认得日期就直接点名，不靠模糊搜）。"""
    d = str(day or "")
    if not d:
        return []
    rows = await db.fetchall(
        "SELECT id, day, kind, title, chars FROM archive_page "
        "WHERE day = ? AND conv_key = ? ORDER BY kind, id",
        (d, conv_key),
    )
    return [{"page": int(r["id"]), "day": str(r["day"]), "kind": str(r["kind"]),
             "title": str(r["title"] or ""), "chars": int(r["chars"] or 0)} for r in rows]


async def recent(conv_key: str, limit: int = 20) -> list[dict[str, Any]]:
    """本窗最近的几页（只给页码/日期/标题，不带正文）。"""
    rows = await db.fetchall(
        "SELECT id, day, kind, title, chars FROM archive_page WHERE conv_key = ? "
        "ORDER BY ts DESC, id DESC LIMIT ?",
        (conv_key, int(limit)),
    )
    return [{"page": int(r["id"]), "day": str(r["day"]), "kind": str(r["kind"]),
             "title": str(r["title"] or ""), "chars": int(r["chars"] or 0)} for r in rows]


async def windows() -> list[dict[str, Any]]:
    """档案室里有哪些窗口、各存了多少（给控制台列目录用）。

    `room` 表里记着每扇窗响过什么名字（主人 / 听风耳语），有名字就用名字，
    没有就用会话键的尾巴，别把一长串 umo 糊到页面上。
    """
    rows = await db.fetchall(
        "SELECT conv_key, COUNT(*) AS n, SUM(chars) AS c, MIN(day) AS a, MAX(day) AS b "
        "FROM archive_page WHERE conv_key <> 'self' GROUP BY conv_key ORDER BY n DESC"
    )
    try:
        rooms = await db.fetchall("SELECT conv_key, name FROM room")
    except Exception:  # noqa: BLE001
        rooms = []
    names = {str(r["conv_key"]): str(r["name"] or "") for r in (rooms or [])}
    out = []
    for r in rows:
        ck = str(r["conv_key"])
        label = names.get(ck) or ck.rsplit(":", 1)[-1]
        out.append({
            "conv_key": ck, "name": label, "pages": int(r["n"] or 0),
            "chars": int(r["c"] or 0), "first_day": str(r["a"] or ""),
            "last_day": str(r["b"] or ""),
        })
    return out


async def stats() -> dict[str, Any]:
    total = await db.fetchone("SELECT COUNT(*) AS n, SUM(chars) AS c FROM archive_page")
    tok = await db.fetchone("SELECT COUNT(*) AS n FROM archive_token")
    last = await db.fetchone("SELECT MAX(day) AS d FROM archive_page")
    kinds = await db.fetchall(
        "SELECT kind, COUNT(*) AS n, SUM(chars) AS c FROM archive_page GROUP BY kind ORDER BY 2 DESC"
    )
    windows = await db.fetchall(
        "SELECT conv_key, COUNT(*) AS n FROM archive_page GROUP BY conv_key ORDER BY 2 DESC LIMIT 10"
    )
    return {
        "pages": int((total or {}).get("n") or 0),
        "chars": int((total or {}).get("c") or 0),
        "tokens": int((tok or {}).get("n") or 0),
        "last_day": (last or {}).get("d") or "",
        "kinds": {str(r["kind"]): int(r["n"]) for r in kinds},
        "windows": [{"conv_key": str(r["conv_key"]), "pages": int(r["n"])} for r in windows],
        "open_max": OPEN_MAX,
    }
