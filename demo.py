"""跑一遍记忆闭环：记 → 想起 → 注入 → 归档。

    python demo.py

它会在系统临时目录里造一个一次性的库，不碰任何真实数据；
跑完把整个过程打印出来，最后可选择保留目录（默认删掉）。

想看清的四件事：
    1. 一条话是怎么被打分、落位到 L1~L4 的
    2. "被想起"是怎么记账的（hits），以及什么时候升级
    3. 注入块长什么样（七桶 + <recall> 段 + 私聊硬隔离）
    4. 白天的原话是怎么变成"一天一页"的档案室的
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import tempfile
import time

# 日志走 stdout（log.py 里只有"没被配置过"时才会自己 basicConfig 到 stderr，
# 这里先配好，Windows 下就不会因为 stderr 输出被当成错误）。
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# 关键：ROOT 必须在 import littlefish_memory 之前设好，
# 因为 config.py 在 import 时就把 ROOT 解析成常量了。
ROOT = tempfile.mkdtemp(prefix="littlefish-memory-demo-")
os.environ["LITTLEFISH_ROOT"] = ROOT

from littlefish_memory import archive, config, db, life, memory, recall  # noqa: E402

OWNER = "qq:private:10001"
FRIEND = "qq:private:20002"


def rule(title: str = "") -> None:
    line = "─" * 60
    print(f"\n{line}\n{title}\n{line}" if title else f"\n{line}")


def ctx(conv_key: str, is_group: bool = False, group_id: str = "") -> dict:
    """场景上下文。私聊与私聊之间靠 conversation_key 硬隔离。"""
    return {"is_group": is_group, "group_id": group_id, "conversation_key": conv_key}


async def main() -> None:
    db_ready = await db.connect()

    # 换成一个能认得出主人的配置（默认是 character="test"、owner.qq 空）
    full = dict(config.DEFAULT_CONFIG)
    full["character"] = "demo"
    full["owner"] = {"qq": "10001", "label": "主人"}
    config.save(full)
    await db_ready.close()          # 换角色 = 换库路径，重开一次
    await db.connect()

    print(f"根目录：{ROOT}")
    print(f"库文件：{config.db_path()}")
    print(f"角色：{config.cfg()['character']}｜主人：{config.cfg()['owner']['qq']}")

    # ---------------------------------------------------------------- ① 记事
    rule("① 记四件事，看她把每件放进了哪一级（L1 新鲜 / L2 记得 / L3 长久 / L4 刻下）")
    seeds = [
        dict(text="主人说不许熬夜，一点前必须睡", kind="promise",
             conversation_key=OWNER, speaker_is_owner=True, is_private=True),
        dict(text="今天中午吃的是番茄鸡蛋面", kind="chat",
             conversation_key=OWNER, speaker_is_owner=True, is_private=True),
        dict(text="主人第一次开口，13:18 说的「你好」", kind="firstMeet",
             conversation_key=OWNER, speaker_is_owner=True, is_private=True),
        dict(text="加湿器关一下，吵", kind="chat",
             conversation_key=FRIEND, speaker="20002", is_private=True),
    ]
    for s in seeds:
        r = await memory.add(**s)
        row = await db.fetchone(
            "SELECT tier, title, text, importance FROM memory WHERE id = ?", (r["id"],))
        shown = (row["title"] or row["text"] or "").replace("\n", " ")
        print(f"  [L{row['tier']}] {shown[:40]}（imp {row['importance']}）")

    rule("② 四级分布")
    print("  " + str(await memory.tier_dist()))

    # ------------------------------------------------------------ ② 白天流水
    rule("② 白天说过的话进流水（这些原话不进注入，只作当天上下文 / 明天复盘用）")
    chat = [
        ("我到一点就睡，真的", "主人", OWNER, False),
        ("你上次也这么说", "她", OWNER, True),
    ]
    for text, who, key, is_self in chat:
        await life.log_chat(text, conv_key=key, speaker_name=who, is_self=is_self)
    print(f"  流水条数：{len(await life.pending(0, int(time.time()) + 1))}")

    # --------------------------------------------------------- ③ 被想起（hits）
    rule("③ 主人回来提了一句「熬夜」，看她会不会想起来")
    hits_before = (await memory.search("熬夜", k=8))[0]["hits"]
    picked = await recall.on_message("熬夜 一点前必须睡 你还记得吗", ctx(OWNER))
    print(f"  命中 {len(picked)} 条：")
    for r in picked:
        shown = (r.get("title") or r.get("text") or "").replace("\n", " ")
        # 命中的行是"递过来那一刻"的样子；升级是在这之后发生的，所以下面再查一次。
        after = await db.fetchone(
            "SELECT hits, tier FROM memory WHERE id = ?", (r["id"],))
        print(f"    · {shown[:36]}（hits {after['hits']}，tier=L{after['tier']}）")
    print(f"  hits：{hits_before} → {(await memory.search('熬夜', k=8))[0]['hits']}"
          "（门槛 l2=1 / l3=3 / l4=8；「最初的事」直接算 L4，所以第一次想起就可能跳到 L4）")

    # ------------------------------------------------------------ ④ 注入块
    rule("④ 这一刻她脑子里被塞进去的东西（注：这是原样输出）")
    block = await memory.render("qq", ctx=ctx(OWNER), recall=picked)
    print(block)
    print(f"\n  ↑ 总长 {len(block)} 字（预算 inject_max_chars.qq=2200）")

    rule("④-b 换一扇窗（好友私聊）：主人窗里的原话一句都不该出现")
    friend_block = await memory.render("qq", ctx=ctx(FRIEND))
    leak = [w for w in ("熬夜", "番茄", "你好") if w in friend_block]
    print(friend_block if friend_block.strip() else "  （空的 —— 好友窗里还没有任何记忆）")
    print(f"\n  跨窗泄漏检查：{'❌ 漏了 ' + str(leak) if leak else '✅ 一句都没带出来'}")

    # ------------------------------------------------------------ ⑤ 档案室
    rule("⑤ 晚上把今天的原话归成「一天一页」")
    day = time.strftime("%Y-%m-%d")
    res = await archive.build_day(day)
    print(f"  归档结果：{res}")
    for page in await archive.recent(OWNER, limit=5):
        print(f"    第 {page['page']} 页 · {page['day']} · {page['title']} · {page['chars']} 字")

    rule("⑤-b 第二天她想去翻档案（日期是索引的一部分，不用模糊撞）")
    hit = await archive.find_day(day, OWNER)
    print(f"  find_day({day}) → {len(hit)} 页")
    if hit:
        page = await archive.open_page(hit[0]["page"], OWNER, max_chars=200)
        print("  开头：" + page["text"][:80].replace("\n", " "))
        print(f"  （整页 {page['chars']} 字，这里只给头尾两段）")

    # --------------------------------------------------------------- 收尾
    rule("跑完了")
    print(f"  临时目录：{ROOT}")
    print("  想留着继续玩：加 --keep；默认会删掉。")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        import traceback
        traceback.print_exc()
    finally:
        if "--keep" not in sys.argv and os.path.isdir(ROOT):
            shutil.rmtree(ROOT, ignore_errors=True)
            print(f"\n（已删除临时目录 {ROOT}）")
    # aiosqlite 的工作线程不会自己退出，脚本跑完会挂在那儿不返回。
    # 这里显式刷缓冲再硬退，是"跑脚本"而不是"当服务"时的正确收尾。
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
