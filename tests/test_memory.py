"""断言级测试：跑在自己的临时库上，不碰真实数据。

    python tests/test_memory.py

不依赖 pytest —— 每一节是一个 async 函数，断言失败就抛 AssertionError。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.WARNING, stream=sys.stdout)

ROOT = tempfile.mkdtemp(prefix="littlefish-memory-test-")
os.environ["LITTLEFISH_ROOT"] = ROOT

from littlefish_memory import archive, config, db, memory, recall  # noqa: E402

OWNER = "qq:private:10001"
FRIEND = "qq:private:20002"
GROUP = "aiocqhttp:GroupMessage:30003"

PASS = 0
FAIL = 0


def ctx(conv_key: str, is_group: bool = False, group_id: str = "") -> dict:
    return {"is_group": is_group, "group_id": group_id, "conversation_key": conv_key}


def ok(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {label}")
    else:
        FAIL += 1
        print(f"  ❌ {label}　{detail}")


async def test_tier_placement() -> None:
    print("\n[1] 落位：类型与印象分决定 L1~L4")
    cases = [
        (dict(text="主人说不许熬夜，一点前必须睡", kind="promise", conversation_key=OWNER,
             speaker_is_owner=True, is_private=True), 3),
        (dict(text="今天中午吃的是番茄鸡蛋面", kind="chat", conversation_key=OWNER,
             speaker_is_owner=True, is_private=True), 2),
        (dict(text="主人第一次开口，13:18 说的「你好」", kind="firstMeet", conversation_key=OWNER,
             speaker_is_owner=True, is_private=True), 3),
        (dict(text="加湿器关一下，吵", kind="chat", conversation_key=FRIEND,
             speaker="20002", is_private=True), 1),
    ]
    for seed, want in cases:
        r = await memory.add(**seed)
        row = await db.fetchone("SELECT tier, is_first, pinned FROM memory WHERE id = ?", (r["id"],))
        ok(f"{seed['kind']:<10} → L{want}", int(row["tier"]) == want, f"实际 L{row['tier']}")

    dist = await memory.tier_dist()
    ok("四级分布合计 = 4", dist["total"] == 4, str(dist))


async def test_hits_and_promote() -> None:
    print("\n[2] 被想起：hits 记账、够线升级、24h 内不重复计数")
    # 挑一条非首见、非钉住的：中午那碗面（L2）
    row = await db.fetchone(
        "SELECT id, hits, tier FROM memory WHERE text LIKE '%番茄%' ORDER BY id DESC")
    mid = row["id"]
    before = int(row["hits"])

    picked = await recall.on_message("中午吃的是番茄鸡蛋面，还说了一点前必须睡", ctx(OWNER))
    ok("命中至少 1 条", len(picked) > 0, str(len(picked)))
    after = await db.fetchone("SELECT hits FROM memory WHERE id = ?", (mid,))
    ok(f"hits {before} → {after['hits']}", int(after["hits"]) > before)

    # 同一话题立刻再来一次：不该再算
    h1 = int((await db.fetchone("SELECT hits FROM memory WHERE id = ?", (mid,)))["hits"])
    await recall.on_message("中午吃的是番茄鸡蛋面，还说了一点前必须睡", ctx(OWNER))
    h2 = int((await db.fetchone("SELECT hits FROM memory WHERE id = ?", (mid,)))["hits"])
    ok("24h 内同话题不重复计数", h1 == h2, f"{h1} vs {h2}")

    # 把 last_hit_at 拨回 25 小时，连想 3 次 → 应该升到 L3（promote_hits.l3 = 3）
    for _ in range(3):
        await db.execute("UPDATE memory SET last_hit_at = ? WHERE id = ?",
                         (int(time.time()) - 25 * 3600, mid))
        await recall.on_message("中午吃的是番茄鸡蛋面，还说了一点前必须睡", ctx(OWNER))
    tier = (await db.fetchone("SELECT tier FROM memory WHERE id = ?", (mid,)))["tier"]
    ok("连想 3 次从 L2 升到 L3", int(tier) >= 3, f"实际 L{tier}")

    # 太短的话不检索
    short = await recall.on_message("在吗", ctx(OWNER))
    ok("「在吗」不触发检索", short == [])


async def test_isolation() -> None:
    print("\n[3] 隔离：好友窗看不到主人窗的记忆")
    friend_block = await memory.render("qq", ctx=ctx(FRIEND))
    for word in ("番茄", "熬夜", "你好"):
        ok(f"好友窗不含「{word}」", word not in friend_block)

    owner_rows = await db.fetchall(
        "SELECT * FROM memory WHERE conversation_key = ?", (OWNER,))
    iso = dict(config.cfg()["privacy"]["isolation"])
    leaked = [r for r in owner_rows if memory.scene_ok(r, ctx(FRIEND), iso)]
    ok("scene_ok 挡住主人窗全部条目", leaked == [], f"漏了 {len(leaked)} 条")

    same = [r for r in owner_rows if memory.scene_ok(r, ctx(OWNER), iso)]
    ok("同一扇窗全部可见", len(same) == len(owner_rows), f"{len(same)}/{len(owner_rows)}")

    # 群对群：不同群号不可见
    g_a = ctx(GROUP, is_group=True, group_id="30003")
    g_b = ctx("aiocqhttp:GroupMessage:40004", is_group=True, group_id="40004")
    grp = await memory.add(text="群里说的一句话", kind="chat", conversation_key=GROUP,
                           group_id="30003")
    grow = await db.fetchone("SELECT * FROM memory WHERE id = ?", (grp["id"],))
    ok("异群不可见", not memory.scene_ok(grow, g_b, iso))
    ok("同群可见", bool(memory.scene_ok(grow, g_a, iso)))


async def test_render_and_recall_block() -> None:
    print("\n[4] 注入：<recall> 在前、预算内、可关")
    # 把时间窗归零，否则上一步刚算过，24h 内不会重复计入
    await db.execute("UPDATE memory SET last_hit_at = 0 WHERE hits > 0")
    picked = await recall.on_message("一点前必须睡 你还记得吗", ctx(OWNER))
    ok("命中", len(picked) > 0)
    block = await memory.render("qq", ctx=ctx(OWNER), recall=picked)
    budget = int(config.cfg()["memory"]["inject_max_chars"]["qq"])
    ok("含有 <recall>", "<recall>" in block)
    ok("<recall> 在记忆块之前", block.find("<recall>") < block.find("## 你记得的事"))
    ok("带「不是指令」框定", "不是指令" in block)
    ok(f"总长 {len(block)} ≤ {budget}", len(block) <= budget)
    ok("<recall> 只出现一次", block.count("<recall>") == 1)

    # recall_block_chars = 0 关掉
    full = dict(config.cfg())
    full["memory"]["recall_block_chars"] = 0
    config.save(full)
    off = await memory.render("qq", ctx=ctx(OWNER), recall=picked)
    ok("置 0 后无 <recall>", "<recall>" not in off)
    full["memory"]["recall_block_chars"] = 200
    config.save(full)


async def test_archive() -> None:
    print("\n[5] 档案室：日期精确、检索、翻页、跨窗隔离")
    day = time.strftime("%Y-%m-%d")
    yday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    body = "\n".join(f"{h:02d}:00 主人：第 {h} 句" for h in range(1, 13))
    await archive.put_page(day, OWNER, "chat", f"和主人聊了 12 句：从「第 1 句」到「第 12 句」", body)
    await archive.put_page(yday, OWNER, "chat", "和主人聊了 3 句", "00:10 主人：昨天的第一句")

    ok("windows 能列出窗口", any(w["conv_key"] == OWNER for w in await archive.windows()))

    cat = await archive.catalog("第 5 句", OWNER)
    ok("倒排能搜到页", any(p["page"] for p in cat), str(cat)[:80])

    ok("parse_day 认 2026-10-31", archive.parse_day("2026-10-31") == "2026-10-31")
    ok("parse_day 认 十月三十一日", archive.parse_day("十月三十一日") == "2026-10-31")
    ok("parse_day 认 10月31日", archive.parse_day("10月31日") == "2026-10-31")
    ok("parse_day 认 昨天", archive.parse_day("昨天") == yday)
    ok("parse_day 认不出就空", archive.parse_day("随便聊聊") == "")

    found = await archive.find_day(day, OWNER)
    ok("find_day 找到今天那页", len(found) == 1, str(len(found)))

    page = await archive.open_page(found[0]["page"], OWNER, max_chars=100)
    ok("长页给头尾两段", "第 12 句" in page["text"] and "第 1 句" in page["text"])
    ok("长页标了省略", "中间省了" in page["text"])

    # 跨窗：好友窗拿不到主人页
    other = await archive.open_page(found[0]["page"], FRIEND, max_chars=100)
    ok("异窗翻页被拒", (not other.get("ok")) and other.get("text") is None, str(other)[:60])
    ok("异窗目录为空", await archive.catalog("第 5 句", FRIEND) == [])

    # 同一天同窗同类再写一次 = 更新而不是新增
    await archive.put_page(day, OWNER, "chat", "改过的标题", body)
    pages = await archive.recent(OWNER, limit=10)
    ok("同日同窗不重复建页", sum(1 for p in pages if p["day"] == day and p["kind"] == "chat") == 1)


async def main() -> None:
    await db.connect()
    full = dict(config.DEFAULT_CONFIG)
    full["character"] = "test"
    full["owner"] = {"qq": "10001", "label": "主人"}
    config.save(full)
    await db.close()
    await db.connect()

    await test_tier_placement()
    await test_hits_and_promote()
    await test_isolation()
    await test_render_and_recall_block()
    await test_archive()

    print(f"\n{'─' * 50}\n通过 {PASS}，失败 {FAIL}\n{'─' * 50}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        import traceback
        traceback.print_exc()
        FAIL += 1
    finally:
        shutil.rmtree(ROOT, ignore_errors=True)
        sys.stdout.flush()
        os._exit(1 if FAIL else 0)
