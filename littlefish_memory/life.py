"""生活流水（L2）：她做过的事，和"哪扇窗"的名册。

和 memory.py 的分工——这条线必须守住，否则两边会长成两坨：

- `memory.py` 管**人和话**：谁说了什么、有多重要、会忘得多快、注入时挑哪几条。
- `life.py` 管**事**：她看了什么、写了什么、世界里发生了什么。按时间排，不衰减，
  不进记忆配额。注入时只借它一两句当"气口"，不搬正文。

写入只走这里。能力侧（阅读/写作/B站）一律 `bus.emit("life.read"/"life.write"/"life.bili")`，
不 import 内核内部 —— 这是 L1 之间不许互相 import 那条硬规矩的延伸：
能力不知道自己被记到了哪张表上，拔掉 reading 不影响 life_log 的历史。
"""

from __future__ import annotations

import re
import time
from typing import Any, Iterable

from . import bus, config, db
from .log import log

# 生活事件的规范话题。能力侧只用这三类；要加新的，先在这里登记。
TOPIC_PREFIX = "life."
TOPICS = {
    "life.read": "reading",     # 看书
    "life.write": "writing",    # 写作
    "life.bili": "bili",        # 在 B站 看了什么
    "life.mail": "mail",        # 她出门时有人来找（门口的信）
    "life.world": "world",      # 世界里的其它事（自己 emit 也行）
}


# ---------------------------------------------------------------- 窗口名册


_KIND_BY_KEY = (
    (re.compile(r"^qq:private:(\d+)$"), "private"),
    (re.compile(r"^qq:group:(\d+)$"), "group"),
    (re.compile(r"^(?:snowluma:)?FriendMessage:(\d+)$"), "private"),
    (re.compile(r"^(?:snowluma:)?GroupMessage:(\d+)$"), "group"),
    # 老库里还有不带 snowluma: 前缀的 private:<qq> / group:<gid>（QQ 插件早期写法）
    (re.compile(r"^private:(\d+)$"), "private"),
    (re.compile(r"^group:(\d+)$"), "group"),
)
_PRIVATE_KEY = (re.compile(r"^qq:private:(\d+)$"), "private")


def parse_conv_key(conv_key: str) -> tuple[str, str]:
    """把各种入口写法拆成 (kind, uid)。认不出返回 ('', '')，不猜。

    这里**只负责拆**，不负责改。收敛是 memory 写入时的事；life 这边遇到老写法
    也认，免得历史数据读不出来。
    """
    k = (conv_key or "").strip()
    if not k:
        return "", ""
    for pat, kind in _KIND_BY_KEY:
        m = pat.fullmatch(k)
        if m:
            return kind, m.group(1)
    return "", ""


def self_conv_key(conv_key: str) -> str:
    """同样含义的窗口，统一成一个写法，用来比"是不是同一扇窗"。

    老库里同一扇私聊窗有过 `private:X` 和 `snowluma:FriendMessage:X` 两种写法。
    """
    kind, uid = parse_conv_key(conv_key)
    if not kind:
        return (conv_key or "").strip()
    return f"qq:{kind}:{uid}"


async def touch_room(conv_key: str, name: str = "", *, at: int | None = None,
                     count_delta: int = 1) -> None:
    """第一次见到某扇窗就登记；名字/时间/条数随最新一次更新。

    名字规则（宁可显示号码，也不让她编一个）：
    主人 → 用 owner.label；好友 → 昵称，没有昵称就号码。
    `name` 传空时保留已有名字（她自己的回声不该把对方的名字冲掉）。

    ⚠️ conv_key 必须**先归一**再存（2026-11-01 修）：入口给的是
    `snowluma:FriendMessage:10001`，而流水里记的是 `qq:private:10001`。
    同一扇窗两种写法，门牌号就和流水对不上，「她的一天」按窗口筛会漏。
    """
    conv = self_conv_key(conv_key)
    kind, uid = parse_conv_key(conv)
    if not kind or not uid:
        return
    now = int(at or time.time())
    cfg = config.cfg()
    owner_qq = str(cfg["owner"].get("qq") or "")
    owner_label = str(cfg["owner"].get("label") or "主人")

    if kind == "group":
        display = name or f"群{uid}"
    elif owner_qq and uid == owner_qq:
        display = owner_label
    else:
        display = name or uid

    conn = await db.connect()
    await conn.execute(
        """INSERT INTO room(conv_key, kind, name, uid, first_seen, last_seen, msg_count, note)
           VALUES(?,?,?,?,?,?,?,'')
           ON CONFLICT(conv_key) DO UPDATE SET
               last_seen = excluded.last_seen,
               msg_count = msg_count + ?,
               name = CASE WHEN excluded.name <> '' THEN excluded.name ELSE room.name END""",
        (conv, kind, display, uid, now, now, max(0, int(count_delta)), max(0, int(count_delta))),
    )
    await conn.commit()


async def rooms(active_within_hours: float = 12.0, limit: int = 3) -> dict[str, Any]:
    """列出现在"开着"的窗。

    返回 {"active": [...], "others": N}：active 里每扇窗只给名字、最后说话时间、
    一句气口；others 是没列出来的数量（只报数，不给名字）。
    """
    now = int(time.time())
    since = now - int(active_within_hours * 3600)
    rows = await db.fetchall(
        "SELECT * FROM room WHERE last_seen >= ? ORDER BY last_seen DESC", (since,)
    )
    return {"active": rows[:limit], "others": max(0, len(rows) - limit)}


# ---------------------------------------------------------------- 生活流水


def _clip(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


async def _snap_v() -> float | None:
    """抄一份"写这条流水时她的精神值"。

    **只读**：vitals.snapshot() 不落库、不推演写回，抄完就丢。
    抄失败一律给 None，绝不能让流水因为疲劳度坏掉写不进去。
    """
    try:
        from . import vitals  # 局部 import：vitals 少了也不该拖垮 life
        snap = await vitals.snapshot()   # 注意是 async（vitals.py:533）
        if isinstance(snap, dict):
            v = snap.get("vigor")
            return None if v is None else round(float(v), 2)
    except Exception as exc:  # noqa: BLE001
        log.debug(f"[life] 抄精神值失败（不影响流水）：{exc}")
    return None


async def log_life(
    kind: str,
    summary: str,
    *,
    detail: str = "",
    conv_key: str = "",
    ref: str = "",
    visibility: str = "all",
    at: int | None = None,
) -> int | None:
    """写一条生活流水（当天的工作记忆）。summary 是给复盘看的那一句，必须短。"""
    summary = _clip(summary, 80)
    if not summary:
        return None
    v = await _snap_v()
    conn = await db.connect()
    cur = await conn.execute(
        """INSERT INTO life_log(ts, kind, summary, detail, conv_key, ref, visibility, v)
           VALUES(?,?,?,?,?,?,?,?)""",
        (int(at or time.time()), kind, summary, detail or "", conv_key or "", ref or "",
         visibility, v),
    )
    await conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------- 对话流水（折叠）


# 同一扇窗里，隔多久的两句还算是"同一次聊天"。超了另起一条。
CHAT_FOLD_SEC = 600
# 一条折叠记录最多留多少字（超了从前面截，复盘只读最近这一阵）。
CHAT_DETAIL_MAX = 1200


async def log_chat(
    text: str,
    *,
    conv_key: str,
    speaker: str = "",
    speaker_name: str = "",
    is_self: bool = False,
    at: int | None = None,
) -> int | None:
    """记一句对话。**同一窗口 10 分钟内折叠进同一条**，防爆表。

    折叠规则（用户 2026-11-01 批准）：
    - 同窗口、距上一条 `CHAT_FOLD_SEC` 内 → 累加到那一条的 detail 上，不新增行；
    - summary 只留「聊了 N 句」+ 最先/最后一句的开头，够复盘认出是哪一段；
    - 隔太久 → 另起一条。

    折叠只省行数，不丢内容：detail 里 `我：…\n对方：…` 交替记，最多 CHAT_DETAIL_MAX 字。
    """
    now = int(at or time.time())
    conv = self_conv_key(conv_key)
    text = _clip(text, 120)
    if not text:
        return None
    name = _clip(speaker_name, 16) or speaker or "某人"
    line = ("我：" if is_self else f"{name}：") + text

    conn = await db.connect()
    row = await db.fetchone(
        "SELECT id, ts, detail, summary, n_turns FROM life_log "
        "WHERE kind = 'chat' AND conv_key = ? AND consumed = 0 "
        "ORDER BY ts DESC, id DESC LIMIT 1",
        (conv,),
    )
    if row and now - int(row["ts"]) <= CHAT_FOLD_SEC:
        detail = (str(row["detail"] or "") + "\n" + line)[-CHAT_DETAIL_MAX:]
        n = int(row["n_turns"] or 0) + 1
        # ts 要跟着推进：它是"这一阵最后一次说话"的时间。
        # 不推的话，"距上一条多久"永远拿这一阵的第一句在算，
        # 聊到第十一分钟也不会断开（2026-11-01 实测踩过）。
        await conn.execute(
            "UPDATE life_log SET detail = ?, n_turns = ?, summary = ?, ts = ? WHERE id = ?",
            (detail, n, _chat_summary(detail, n), now, row["id"]),
        )
        await conn.commit()
        return int(row["id"])

    cur = await conn.execute(
        """INSERT INTO life_log(ts, kind, summary, detail, conv_key, ref, visibility, v, n_turns)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (now, "chat", "", line, conv, "", "all", await _snap_v(), 1),
    )
    rid = cur.lastrowid
    await conn.execute(
        "UPDATE life_log SET summary = ? WHERE id = ?", (_chat_summary(line, 1), rid)
    )
    await conn.commit()
    return rid


def _chat_summary(detail: str, n: int) -> str:
    """一条折叠记录的抬头：几句话 + 开头那一句（复盘时够认出是哪一段）。"""
    lines = [x for x in (detail or "").split("\n") if x.strip()]
    if not lines:
        return f"聊了 {n} 句"
    first = lines[0]
    if n <= 1:
        return _clip(first, 60)
    return _clip(f"{first}（这一阵聊了 {n} 句）", 80)


# ---------------------------------------------------------------- 复盘与清理


async def pending(day_start: int, day_end: int, limit: int = 2000) -> list[dict]:
    """还没被夜间复盘读过的流水。复盘失败就留着，下次开机接着用。"""
    return await db.fetchall(
        "SELECT * FROM life_log WHERE ts >= ? AND ts < ? AND consumed = 0 "
        "ORDER BY ts ASC LIMIT ?",
        (day_start, day_end, limit),
    )


async def days_pending() -> list[str]:
    """哪些天还有没消费的流水（开机复盘按这个顺序补）。"""
    rows = await db.fetchall(
        "SELECT ts FROM life_log WHERE consumed = 0 ORDER BY ts ASC"
    )
    out: list[str] = []
    for r in rows:
        d = _day_of(int(r["ts"]))
        if not out or out[-1] != d:
            out.append(d)
    return out


async def consume(ids: Iterable[int]) -> int:
    """复盘读过了就标记（**不物理删**）。物理删只发生在 purge()。"""
    xs = [int(i) for i in ids if i is not None]
    if not xs:
        return 0
    conn = await db.connect()
    n = 0
    for i in range(0, len(xs), 400):
        chunk = xs[i:i + 400]
        cur = await conn.execute(
            "UPDATE life_log SET consumed = 1 WHERE id IN (%s)" % ",".join("?" * len(chunk)),
            tuple(chunk),
        )
        n += cur.rowcount or 0
    await conn.commit()
    return n


async def purge(keep_days: int = 7) -> int:
    """把"已经复盘过、且超过 keep_days 的"流水物理删掉（冷数据归档的终点）。

    **只会删 consumed = 1 的**。没复盘过的哪怕超过 keep_days 也留着 ——
    宁可占几天空间，也不能让她的记忆在没被读过的情况下消失。
    """
    cutoff = int(time.time() - max(1, int(keep_days)) * 86400)
    conn = await db.connect()
    cur = await conn.execute(
        "DELETE FROM life_log WHERE consumed = 1 AND ts < ?", (cutoff,)
    )
    await conn.commit()
    return cur.rowcount or 0


def _day_of(ts: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


# ---------------------------------------------------------------- 门口的信
#
# 她出门时别人来找她，那几句话**不是她听到的**，所以不进记忆（她不在家，
# 没听见）。但也不能凭空消失 —— 主人 2026-11-01 定的：像在门口留了一封信，
# 她回来能看到谁找了她、说了什么。
#
# 存在 life_log 里，`kind="mail"`：
#   - 已经在用的表，不新建表、不改 schema；
#   - 第 3 步「她的一天」能直接画出来；
#   - 夜里复盘看得到（她第二天翻日记时记得"昨天有人来找过我"）。

MAIL_KIND = "mail"


async def mail(conv_key: str, sender: str, text: str, *, name: str = "",
               at: int | None = None) -> int | None:
    """记一封门口的信。原话照存，不做摘要 —— 摘要会把"问的是什么"磨掉。"""
    conv = self_conv_key(conv_key)
    body = _clip(text, 200)
    if not body:
        return None
    who = _clip(name or sender, 24) or "有人"
    return await log_life(MAIL_KIND, f"{who}：{body}", detail=f"speaker={sender}",
                          conv_key=conv, at=at)


async def mails(conv_key: str, since_ts: int = 0, limit: int = 20) -> list[dict]:
    """某扇窗里、某个时间点之后**还没读过**的信（按到达顺序，先来的先读）。

    排序必须带 `id`（2026-11-01 实测踩过）：同一秒里可能有两封信，
    只按 `ts ASC` 时数据库给谁先谁后不确定，人读信会看到先后颠倒。
    """
    conv = self_conv_key(conv_key)
    rows = await db.fetchall(
        "SELECT * FROM life_log WHERE kind = ? AND conv_key = ? AND ts >= ? AND seen = 0 "
        "ORDER BY ts ASC, id ASC LIMIT ?",
        (MAIL_KIND, conv, int(since_ts or 0), max(1, int(limit))),
    )
    return rows


async def mail_seen(ids: Iterable[int]) -> int:
    """把读过的信标记一下。不删 —— 那是她这一天的账，复盘还要看。"""
    xs = [int(i) for i in ids if i is not None]
    if not xs:
        return 0
    conn = await db.connect()
    n = 0
    for i in range(0, len(xs), 400):
        chunk = xs[i:i + 400]
        cur = await conn.execute(
            "UPDATE life_log SET seen = 1 WHERE id IN (%s)" % ",".join("?" * len(chunk)),
            tuple(chunk),
        )
        n += cur.rowcount or 0
    await conn.commit()
    return n


async def recent(
    kinds: Iterable[str] | None = None, limit: int = 5, *, within_hours: float = 24.0
) -> list[dict]:
    """最近做过的事（给注入用的气口）。"""
    since = int(time.time() - within_hours * 3600)
    if kinds:
        ks = [k for k in kinds if k]
        q = "SELECT * FROM life_log WHERE ts >= ? AND kind IN (%s) ORDER BY ts DESC LIMIT ?" % (
            ",".join("?" * len(ks))
        )
        return await db.fetchall(q, (*ks, since, limit))
    return await db.fetchall(
        "SELECT * FROM life_log WHERE ts >= ? ORDER BY ts DESC LIMIT ?", (since, limit)
    )


async def day(day_start: int, day_end: int, limit: int = 500) -> list[dict]:
    """某一天的全部流水（控制台「她的一天」用）。"""
    return await db.fetchall(
        "SELECT * FROM life_log WHERE ts >= ? AND ts < ? ORDER BY ts ASC LIMIT ?",
        (day_start, day_end, limit),
    )


async def stats() -> dict[str, Any]:
    total = (await db.fetchone("SELECT COUNT(*) AS c FROM life_log")) or {"c": 0}
    by_kind = {}
    for r in await db.fetchall(
        "SELECT kind, COUNT(*) AS c FROM life_log GROUP BY kind ORDER BY c DESC"
    ):
        by_kind[r["kind"]] = r["c"]
    return {"total": total["c"], "by_kind": by_kind}


# ---------------------------------------------------------------- bus 接线


async def _on_life(topic: str, payload: dict[str, Any]) -> None:
    """把能力侧发来的事件翻译成生活流水。

    约定（能力侧只要照这个填就能被看见，别的什么都不用做）：

        await bus.emit("life.read", {"slug": ..., "title": ..., "summary": ...})
        await bus.emit("life.write", {"slug": ..., "title": ..., "summary": ...})
        await bus.emit("life.bili", {"bvid": ..., "title": ..., "summary": ...})

    summary 缺了就退回 title；两个都没有就跳过（不编）。
    """
    kind = TOPICS.get(topic) or topic[len(TOPIC_PREFIX):] or "world"
    summary = str(payload.get("summary") or payload.get("title") or "").strip()
    if not summary:
        log.info(f"[life] {topic} 没带 summary/title，跳过")
        return
    conv = self_conv_key(str(payload.get("conv_key") or payload.get("conversation_key") or ""))
    ref = str(payload.get("ref") or payload.get("slug") or payload.get("bvid") or "")
    detail = str(payload.get("detail") or "")
    await log_life(kind, summary, detail=detail, conv_key=conv, ref=ref)


def wire() -> None:
    """在 kernel.init() 里调一次。重复调只会多挂一份 handler，所以加标记防重。"""
    if getattr(wire, "_done", False):
        return
    bus.subscribe("life.*", _on_life)
    wire._done = True  # type: ignore[attr-defined]
    log.info("[life] 已接上 bus: life.*")


def unwire() -> None:
    bus.unsubscribe("life.*", _on_life)
    wire._done = False  # type: ignore[attr-defined]
