"""想起来，和四级升降（阶段②的策略层）。

职责边界（为什么单独一个文件）：
- `memory.py` 只负责**存取和渲染**（写进去、查出来、拼成七桶），它不管"这条该不该升级"；
- 这里只管**策略**：一句话进来 → 本地检索 → 命中就记一次"被想起" → 够阈值就往上升；
  每天一次的**降级扫描**也在这儿。
- 不调模型、不花钱、不发消息。她的"想起"是被动的：你说到哪儿，她就去库里找哪儿。

为什么 `hits` 单独一列：
`recall_count` 记的是"同一句话被重复说了几次"（旧数据里 '那这个说来话长了…' 被记了 56 次），
拿它判升级会把重复的口头禅顶成永久记忆（2026-11-02 实测踩过）。`hits` 只记"真被想起"。

四级（`L1 新鲜 → L2 记得 → L3 长久 → L4 刻下`）：
- 升：被想起 1 / 3 / 8 次 → L2 / L3 / L4（`promote_hits`，可调）
- 降：L2 有 `hold_days.l2` 天没被想起 → L1；L3 有 `hold_days.l3` 天 → L2
- L4 永不降级，只由人删（`memory.evict(force=True)`）
- 事实类（`DIRECT_KINDS`）一进来就是 L3，不靠频率赌运气
"""

from __future__ import annotations

import time
from typing import Any

from . import config, db, memory
from .log import log

# 话题去重用的 kv：同一条记忆在 `recall_same_topic_hours` 内只算一次，
# 免得"今天已经说过事"这类话把计数器刷爆。
KV_LAST_SWEEP = "recall.last_sweep"


def _mcfg() -> dict[str, Any]:
    return dict(config.cfg()["memory"])


def _thr(key: str) -> int:
    return int((_mcfg().get("promote_hits") or {}).get(key, {"l2": 1, "l3": 3, "l4": 8}[key]))


def _hold(key: str) -> int:
    return int((_mcfg().get("hold_days") or {}).get(key, {"l2": 7, "l3": 14}[key]))


# ---------------------------------------------------------------- 升


def _want(hits: int, last_hit_at: int, now: int, pinned: bool, is_first: bool) -> int:
    """该在哪一级 —— **升级要看"最近还想起过"**，不能只看累计次数。

    为什么（2026-11-02 实测踩到的）：只按累计次数判，降级当天就会被下一天的
    `promote_by_hits` 原封不动弹回去（一条被想起过两次的旧事，掉到 L1 之后
    第二天又回到 L2，永远掉不下去，降级等于没写）。所以 L2/L3 的升级都加一道
    "最近 `hold_days` 内被想起过"的门槛：久到降级了，就别再自动弹回来；
    她哪天真又说起它，`last_hit_at` 刷新了，自然升回去。
    L4 不设这道门：到 L4 就是刻下，按设计永不降级。
    """
    if pinned or is_first:
        return 4
    l2, l3, l4 = _thr("l2"), _thr("l3"), _thr("l4")
    if hits >= l4:
        return 4
    if hits >= l3 and now - int(last_hit_at or 0) <= _hold("l3") * 86400:
        return 3
    if hits >= l2 and now - int(last_hit_at or 0) <= _hold("l2") * 86400:
        return 2
    return 1


async def promote_by_hits(now: int | None = None) -> int:
    """把 hits 已经够线、级别还落后的记录提上去。返回改动条数。

    单独抽出来是因为两条路径都要用：刚命中一条时（on_message 里，只动那几条）和
    每天扫描时（补历史遗漏，比如阈值被人调低了）。
    """
    conn = await db.connect()
    now = int(now or time.time())
    rows = await db.fetchall(
        "SELECT id, hits, last_hit_at, tier, pinned, is_first FROM memory WHERE archived = 0"
    )
    moves: list[tuple[int, str]] = []
    for r in rows:
        cur = int(r["tier"] or 1)
        want = _want(int(r["hits"] or 0), int(r["last_hit_at"] or 0), now,
                     bool(r["pinned"]), bool(r["is_first"]))
        if want > cur:
            moves.append((min(want, memory.TIER_MAX), str(r["id"])))
    if moves:
        await conn.executemany("UPDATE memory SET tier = ? WHERE id = ?", moves)
        await conn.commit()
    if moves:
        log.info(f"[recall] 升级 {len(moves)} 条")
    return len(moves)


# ---------------------------------------------------------------- 自动想起


async def on_message(text: str, ctx: dict | None = None) -> list[dict[str, Any]]:
    """她说/他说的这句话，去库里找"像的事"。命中的算一次被想起。

    成本：一次 SQL（bigram 倒排），不调模型。默认最多算 `recall_topn` 条，
    且一条记忆 `recall_same_topic_hours` 内只算一次，防止同一话题刷升级。

    隔离：命中的候选**必须**过 `memory.scene_ok`。别的窗口里的话不能被这扇窗"想起来"
    ——那是 2026-11-01 修掉的私聊串味问题的另一条路。
    """
    mcfg = _mcfg()
    if not mcfg.get("auto_recall", True):
        return []
    body = (text or "").strip()
    if len(body) < 4:                 # 太短的话没有检索价值（"在吗"）
        return []

    topn = max(1, int(mcfg.get("recall_topn", 3)))
    min_hits = max(1, int(mcfg.get("recall_min_hits", 2)))
    window = max(1, int(mcfg.get("recall_same_topic_hours", 24))) * 3600

    try:
        cand = await memory.search(body, k=topn * 4)
    except Exception as exc:
        log.warning(f"[recall] 检索失败：{exc}")
        return []

    iso = dict(config.cfg()["privacy"].get("isolation") or {})
    now = int(time.time())
    picked: list[dict[str, Any]] = []
    for r in cand:
        if int(r.get("matched") or 0) < min_hits:
            continue
        if not memory.scene_ok(r, ctx, iso):
            continue
        if now - int(r.get("last_hit_at") or 0) < window:
            continue                  # 同一话题刚算过，不重复计数
        picked.append(r)
        if len(picked) >= topn:
            break

    if not picked:
        return []
    try:
        await memory.bump_hits([str(r["id"]) for r in picked])
    except Exception as exc:
        log.warning(f"[recall] 记命中失败：{exc}")
        return []

    # 只对刚命中的那几条判升级（不跑全表扫描）
    conn = await db.connect()
    for r in picked:
        new_hits = int(r.get("hits") or 0) + 1
        cur = int(r.get("tier") or 1)
        want = _want(new_hits, now, now, bool(r.get("pinned")), bool(r.get("is_first")))
        if want > cur:
            await conn.execute("UPDATE memory SET tier = ? WHERE id = ?", (want, r["id"]))
            log.info(f"[recall] 想起「{str(r.get('text') or '')[:20]}」"
                     f"第 {new_hits} 次 → L{want}")
    await conn.commit()
    return picked


# ---------------------------------------------------------------- 降


async def sweep(now: int | None = None) -> dict[str, int]:
    """每天跑一次：太久没被想起的往下掉一级。L4 不动。

    计时起点取 `max(last_hit_at, created_at)`：**从没被想起过的新记忆**不该
    在写进来当天就被判"太久没用"，那是把"新鲜"当成"过期"。
    """
    mcfg = _mcfg()
    now = int(now or time.time())
    drop_l2 = _hold("l2") * 86400
    drop_l3 = _hold("l3") * 86400
    out = {"l2_to_l1": 0, "l3_to_l2": 0, "promoted": 0}

    conn = await db.connect()
    rows = await db.fetchall(
        "SELECT id, tier, hits, pinned, is_first, created_at, last_hit_at "
        "FROM memory WHERE archived = 0"
    )
    moves: list[tuple[int, str]] = []
    for r in rows:
        tier = int(r["tier"] or 1)
        if tier >= 4:
            continue                  # 刻下的永不降
        ref = max(int(r["last_hit_at"] or 0), int(r["created_at"] or 0))
        if tier == 3 and now - ref >= drop_l3:
            moves.append((2, str(r["id"])))
            out["l3_to_l2"] += 1
        elif tier == 2 and now - ref >= drop_l2:
            moves.append((1, str(r["id"])))
            out["l2_to_l1"] += 1
    if moves:
        await conn.executemany("UPDATE memory SET tier = ? WHERE id = ?", moves)
        await conn.commit()
    out["promoted"] = await promote_by_hits(now)
    await db.kv_set(KV_LAST_SWEEP, {"at": now, **out})
    log.info(f"[recall] 每日扫描：{out}")
    return out


def next_sweep_at(now: int) -> int:
    """下一次每日扫描的时间戳（每天 daily_hour:10）。

    为什么由后端算：前端算就得知道 daily_hour，配置一改就错；
    这里跟 scheduler.py 里的 register_defaults 用同一个配置项。
    """
    hour = int(config.cfg()["memory"].get("daily_hour", 4))
    lt = time.localtime(now)
    target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, 10, 0, 0, 0, -1))
    if target <= now:
        # tm_mday 越界会被 mktime 自己进位，跨月不用手算
        target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, hour, 10, 0, 0, 0, -1))
    return int(target)


async def stats() -> dict[str, Any]:
    """控制台/体检用：分级分布 + 有多少条被想起过 + 扫描情况。

    2026-11-02 加后五个字段，为的是控制台能一眼看出"四级到底在不在动"，
    不用去翻 kv 或跑脚本。都是只读查询。
    """
    out = await memory.tier_dist()
    now = int(time.time())
    conn = await db.connect()
    cur = await conn.execute(
        "SELECT COUNT(*) AS n, SUM(CASE WHEN hits > 0 THEN 1 ELSE 0 END) AS touched "
        "FROM memory WHERE archived = 0"
    )
    row = await cur.fetchone()
    out["hits_touched"] = int((row[1] if row else 0) or 0)
    out["total"] = int((row[0] if row else 0) or out.get("total") or 0)

    # 最近被想起的那一条：显示"她现在还把哪件事记在心上"最直接的证据
    cur = await conn.execute(
        "SELECT COALESCE(NULLIF(title, ''), text) AS t, last_hit_at, hits FROM memory "
        "WHERE archived = 0 AND hits > 0 ORDER BY last_hit_at DESC LIMIT 1"
    )
    last = await cur.fetchone()
    out["last_hit_at"] = int(last[1] or 0) if last else 0
    out["last_hit_text"] = (str(last[0] or "")[:40].replace("\n", " ") if last else "")

    # 单条最多被想起几次：必须单独取 MAX。上面那条是"最近一条"，拿它的 hits 当最大值
    # 会报错口径（最近那条可能只想起过 1 次，而真正最高的是另一条）。
    cur = await conn.execute("SELECT MAX(hits) FROM memory WHERE archived = 0")
    r3 = await cur.fetchone()
    out["hits_max"] = int((r3[0] if r3 else 0) or 0)

    cur = await conn.execute(
        "SELECT COUNT(*) FROM memory WHERE archived = 0 AND last_hit_at >= ?",
        (now - 86400,),
    )
    r2 = await cur.fetchone()
    out["hits_recent"] = int((r2[0] if r2 else 0) or 0)

    out["last_sweep"] = await db.kv_get(KV_LAST_SWEEP)
    out["next_sweep_at"] = next_sweep_at(now)
    return out
