"""SQLite 底座：一个库，一次迁移，append-only 归档。

设计约束（来自旧系统的教训）：
1. 状态只追加或小事务更新，**绝不整文件重写**。旧系统每次维护重写 308KB JSON，
   断电即坏；这里每条记忆是一次 INSERT/UPDATE，WAL 保证崩溃后一致。
2. 每张表都有明确唯一写者，避免多处同时写。
3. schema 版本写在 PRAGMA user_version，升级只加不删。
"""

from __future__ import annotations

import json
import time
from typing import Any, Iterable

import aiosqlite

from . import config

SCHEMA_VERSION = 9

_SCHEMA = """
CREATE TABLE IF NOT EXISTS persona_card (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    name        TEXT    NOT NULL DEFAULT '',
    body        TEXT    NOT NULL DEFAULT '',
    version     INTEGER NOT NULL DEFAULT 0,
    updated_at  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS memory (
    id              TEXT PRIMARY KEY,
    platform        TEXT NOT NULL DEFAULT 'qq',
    scope           TEXT NOT NULL DEFAULT 'qq',
    kind            TEXT NOT NULL DEFAULT 'chat',
    title           TEXT NOT NULL DEFAULT '',
    text            TEXT NOT NULL,
    detail          TEXT NOT NULL DEFAULT '',
    tags            TEXT NOT NULL DEFAULT '[]',
    importance      INTEGER NOT NULL DEFAULT 10,
    reasons         TEXT NOT NULL DEFAULT '[]',
    emotional       INTEGER NOT NULL DEFAULT 0,
    pinned          INTEGER NOT NULL DEFAULT 0,
    anchor          INTEGER NOT NULL DEFAULT 0,
    clarity         TEXT NOT NULL DEFAULT 'vivid',
    created_at      INTEGER NOT NULL,
    last_seen_at    INTEGER NOT NULL,
    decay_steps     INTEGER NOT NULL DEFAULT 0,
    recall_count    INTEGER NOT NULL DEFAULT 0,
    tier            INTEGER NOT NULL DEFAULT 1,   -- 记忆分级 L1..L4（1=新鲜 4=刻下）
    hits            INTEGER NOT NULL DEFAULT 0,   -- 真被想起过几次（自动检索命中计数）
    last_hit_at     INTEGER NOT NULL DEFAULT 0,   -- 最后一次被想起的时间（降级按它算）
    speaker         TEXT NOT NULL DEFAULT '',
    group_id        TEXT NOT NULL DEFAULT '',
    conversation_key TEXT NOT NULL DEFAULT '',
    is_self         INTEGER NOT NULL DEFAULT 0,
    sensitive       INTEGER NOT NULL DEFAULT 0,
    is_first        INTEGER NOT NULL DEFAULT 0,
    first_tier      TEXT NOT NULL DEFAULT '',
    was_first       INTEGER NOT NULL DEFAULT 0,
    merged_from     TEXT NOT NULL DEFAULT '[]',
    archived        INTEGER NOT NULL DEFAULT 0,
    speaker_name    TEXT NOT NULL DEFAULT '',
    pending         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_memory_scope   ON memory(scope, archived, clarity);
CREATE INDEX IF NOT EXISTS idx_memory_recent  ON memory(last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_conv    ON memory(conversation_key);
CREATE INDEX IF NOT EXISTS idx_memory_kind    ON memory(kind, created_at DESC);

-- 生活流水（L2）：能力侧发生的事都往这里留一句。
-- 和 memory 分工：memory 是"她记住的人和话"，life_log 是"她做过的事"。
-- 写者只有 life.py；能力侧一律走 bus.emit("life.*")，不直接碰这张表。
CREATE TABLE IF NOT EXISTS life_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    kind        TEXT    NOT NULL DEFAULT '',      -- read/write/bili/world/chat/mail
    summary     TEXT    NOT NULL DEFAULT '',      -- 一句话，给注入用（≤80 字）
    detail      TEXT    NOT NULL DEFAULT '',      -- 展开细节（不给注入）
    conv_key    TEXT    NOT NULL DEFAULT '',      -- 发生在哪扇窗（空=不属于任何窗口）
    ref         TEXT    NOT NULL DEFAULT '',      -- 外部句柄：书名/slug/bvid
    visibility  TEXT    NOT NULL DEFAULT 'all',   -- all / self / owner
    consumed    INTEGER NOT NULL DEFAULT 0,       -- 已被夜间复盘读过（1=可清）
    v           REAL,                             -- 写这条时她的精神值快照（只抄不算）
    n_turns     INTEGER NOT NULL DEFAULT 0,       -- chat 折叠了几句话
    seen        INTEGER NOT NULL DEFAULT 0        -- mail 专用：她回来读过这封信了吗
);
CREATE INDEX IF NOT EXISTS idx_life_ts   ON life_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_life_conv ON life_log(conv_key, ts DESC);

-- 窗口名册：每扇窗叫什么。以前靠 memory 里的 speaker 反推，
-- 空的/昵称/号码混在一起；现在第一次见到就登记，名字随最新一次自动更新。
CREATE TABLE IF NOT EXISTS room (
    conv_key    TEXT PRIMARY KEY,
    kind        TEXT    NOT NULL DEFAULT '',      -- private / group
    name        TEXT    NOT NULL DEFAULT '',      -- 显示名（主人/昵称/QQ号）
    uid         TEXT    NOT NULL DEFAULT '',      -- 对端 QQ 号或群号
    first_seen  INTEGER NOT NULL DEFAULT 0,
    last_seen   INTEGER NOT NULL DEFAULT 0,
    msg_count   INTEGER NOT NULL DEFAULT 0,
    note        TEXT    NOT NULL DEFAULT ''       -- 留给人看的（比如"只在群里见过"）
);

-- 中文二元组倒排索引（不用向量：我们要"她真说过这句"，不是"意思相近"）
CREATE TABLE IF NOT EXISTS memory_token (
    token   TEXT NOT NULL,
    mem_id  TEXT NOT NULL,
    PRIMARY KEY (token, mem_id)
);
CREATE INDEX IF NOT EXISTS idx_token ON memory_token(token);

-- 自我卡：她对自己的叙述，只在新会话第一次唤醒读一次
CREATE TABLE IF NOT EXISTS self_note (
    id           TEXT PRIMARY KEY,
    text         TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    hits         INTEGER NOT NULL DEFAULT 0
);

-- 出站队列：所有"要说话"的动作先进这里，发送成功才落 sent
CREATE TABLE IF NOT EXISTS outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    platform    TEXT NOT NULL,
    umo         TEXT NOT NULL DEFAULT '',
    chain       TEXT NOT NULL DEFAULT '[]',
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  INTEGER NOT NULL,
    sent_at     INTEGER NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT ''
);

-- 用量账本：三个 token 字段分开记账（缓存价和普通价不是一个价）
CREATE TABLE IF NOT EXISTS usage (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           INTEGER NOT NULL,
    provider     TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL DEFAULT '',
    input_other  INTEGER NOT NULL DEFAULT 0,
    input_cached INTEGER NOT NULL DEFAULT 0,
    output       INTEGER NOT NULL DEFAULT 0,
    cost         REAL    NOT NULL DEFAULT 0,
    scope        TEXT NOT NULL DEFAULT '',
    ref          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts DESC);

-- 事件总线落盘：进程内 bus 之外再留一份可回放的事件流
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    topic   TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_topic ON events(topic, ts DESC);

-- 跨插件小状态（每个 key 只有一个写者，value 是 JSON）
CREATE TABLE IF NOT EXISTS kv (
    k          TEXT PRIMARY KEY,
    v          TEXT NOT NULL DEFAULT 'null',
    updated_at INTEGER NOT NULL DEFAULT 0
);

-- 档案室：按「一扇窗 × 一天 × 一类」切页，存的是**原话**，永不清。
-- 生活流水（life_log）7 天后会物理删，档案室是它的底账；另外也收她的日记
-- （kind='self'）和做过的事（kind='life'：看书/写作/B站/出门）。
-- 索引 archive_token 是二元组倒排（词 → 页），查档案就是"按词取候选页"，
-- 所以页数涨十倍、耗时几乎不变（2026-11-02 压测：5000 页 0.4ms / 12.7MB）。
CREATE TABLE IF NOT EXISTS archive_page (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    day      TEXT NOT NULL,
    conv_key TEXT NOT NULL,
    kind     TEXT NOT NULL DEFAULT 'chat',
    title    TEXT NOT NULL DEFAULT '',
    text     TEXT NOT NULL,
    chars    INTEGER NOT NULL DEFAULT 0,
    ts       INTEGER NOT NULL DEFAULT 0,
    src      TEXT NOT NULL DEFAULT '',
    built_at INTEGER NOT NULL DEFAULT 0,
    UNIQUE(day, conv_key, kind)
);
CREATE TABLE IF NOT EXISTS archive_token (
    token   TEXT NOT NULL,
    page_id INTEGER NOT NULL
);
"""

_conn: aiosqlite.Connection | None = None
_conn_path: str = ""


async def connect() -> aiosqlite.Connection:
    """进程内单连接。切换角色时自动换库（路径变了就重开）。"""
    global _conn, _conn_path
    want = str(config.db_path())
    if _conn is not None and _conn_path == want:
        return _conn
    if _conn is not None:
        await _conn.close()
        _conn = None
    config.ensure_dirs()
    conn = await aiosqlite.connect(want)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute("PRAGMA busy_timeout=5000")
    await conn.executescript(_SCHEMA)
    await _migrate(conn)
    await conn.commit()
    _conn = conn
    _conn_path = want
    return conn


async def _migrate(conn: aiosqlite.Connection) -> None:
    cur = await conn.execute("PRAGMA user_version")
    row = await cur.fetchone()
    version = int(row[0]) if row else 0
    if version < 2:
        # v2：用量账本多一列"这笔钱算在哪个功能上"（按 L1 拆账要用）
        cols = [r[1] for r in await (await conn.execute("PRAGMA table_info(usage)")).fetchall()]
        if "feature" not in cols:
            await conn.execute(
                "ALTER TABLE usage ADD COLUMN feature TEXT NOT NULL DEFAULT ''"
            )
    if version < 4:
        # v4：记忆多了"发言人显示名"和"待审"，另加 life_log / room 两张新表。
        # 只加列不删列；老库在 connect() 的 executescript 里已经按 _SCHEMA 建好新表，
        # 这里只需要给老 memory 表补列。
        cols = {r[1] for r in await (await conn.execute("PRAGMA table_info(memory)")).fetchall()}
        if "speaker_name" not in cols:
            await conn.execute("ALTER TABLE memory ADD COLUMN speaker_name TEXT NOT NULL DEFAULT ''")
        if "pending" not in cols:
            await conn.execute("ALTER TABLE memory ADD COLUMN pending INTEGER NOT NULL DEFAULT 0")
        # 索引必须等列补完再建：老库的 memory 表上还没有 pending 列，
        # 放在 _SCHEMA 的 executescript 里会在补列之前就炸（2026-11-01 踩过）。
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_pending ON memory(pending, created_at DESC)"
        )
    if version < 5:
        # v5：life_log 从"只增的流水"改成"当天的工作记忆"：
        #   consumed = 夜间复盘读过没有（1 才允许 purge 物理删）
        #   v        = 写这条时她的精神值快照（只抄不算，绝不影响 vitals）
        #   n_turns  = 对话折叠了几句话
        cols = {r[1] for r in await (await conn.execute("PRAGMA table_info(life_log)")).fetchall()}
        if "consumed" not in cols:
            await conn.execute("ALTER TABLE life_log ADD COLUMN consumed INTEGER NOT NULL DEFAULT 0")
        if "v" not in cols:
            await conn.execute("ALTER TABLE life_log ADD COLUMN v REAL")
        if "n_turns" not in cols:
            await conn.execute("ALTER TABLE life_log ADD COLUMN n_turns INTEGER NOT NULL DEFAULT 0")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_life_pend ON life_log(consumed, ts)"
        )
    if version < 6:
        # v6：门口的信。她出门时别人来找她，那几句话不是她听到的（不进记忆），
        # 但要留成一封信，等她回来读。`seen` 保证同一封信只读一次。
        cols = {r[1] for r in await (await conn.execute("PRAGMA table_info(life_log)")).fetchall()}
        if "seen" not in cols:
            await conn.execute("ALTER TABLE life_log ADD COLUMN seen INTEGER NOT NULL DEFAULT 0")
    if version < 7:
        # v7：记忆分级。四个档位不是"写得更好看"，是四种**行为**：
        #   L1 新鲜  = 只进"最近聊到的"桶，14 天没被用到就压成一行离场
        #   L2 记得  = 排队注入，7 天没用降回 L1
        #   L3 长久  = 优先注入，14 天没用降回 L2
        #   L4 刻下  = 必注入、永不降级、只由人手动删
        # 分级只加列；真正的升降逻辑在 memory.py 的 sweep()（阶段②才接）。
        cols = {r[1] for r in await (await conn.execute("PRAGMA table_info(memory)")).fetchall()}
        if "tier" not in cols:
            await conn.execute("ALTER TABLE memory ADD COLUMN tier INTEGER NOT NULL DEFAULT 1")
    if version < 8:
        # v8：`hits` = 真被想起过几次。旧 `recall_count` 记的是"同一句话被重复说了几次"
        # （2026-11-02 实测：'那这个说来话长了…' 被记了 56 次），不能拿来判升级，
        # 所以另开一列，只由自动检索（recall.on_message）和她的主动回忆累加。
        # `last_hit_at` 是降级计时起点：被想起过就重新计时，没被想起才往下掉。
        cols = {r[1] for r in await (await conn.execute("PRAGMA table_info(memory)")).fetchall()}
        if "hits" not in cols:
            await conn.execute("ALTER TABLE memory ADD COLUMN hits INTEGER NOT NULL DEFAULT 0")
        if "last_hit_at" not in cols:
            await conn.execute(
                "ALTER TABLE memory ADD COLUMN last_hit_at INTEGER NOT NULL DEFAULT 0"
            )
    if version < 9:
        # v9：档案室。两张表本身由 _SCHEMA 建（老库的 executescript 已经先建好了），
        # 这里只补倒排索引：没有索引时 archive_token 会全表扫，页数一多就慢。
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_atok_t ON archive_token(token)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_atok_p ON archive_token(page_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_apage_ck ON archive_page(conv_key, ts DESC)")
    if version < SCHEMA_VERSION:
        # 所有"用到新列"的索引都必须在这之后建 —— _SCHEMA 的 executescript 跑在
        # _migrate 之前，那时老库还没有这些列（2026-11-01 连踩两次：
        # idx_memory_pending 和 idx_life_pend 都栽在这上面）。
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_life_pend ON life_log(consumed, ts)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_life_mail ON life_log(kind, conv_key, seen)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_tier ON memory(tier, archived, last_seen_at DESC)"
        )
        await conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


async def close() -> None:
    """关库前把 WAL 落回主库。

    为什么必须 checkpoint（2026-11-01 实测）：WAL 模式下迁移和写入先落在
    `-wal` 文件里，进程正常退出时 sqlite 自己会 checkpoint，但**被强杀或崩溃
    时不会** —— 用另一个连接打开的库看起来就"没迁移/没写进去"。
    迁移（ALTER TABLE）这种结构性改动尤其不能悬在 WAL 里。
    """
    global _conn, _conn_path
    if _conn is not None:
        try:
            await _conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[db] WAL checkpoint 失败（不影响已提交的数据）：{exc}")
        await _conn.close()
        _conn = None
    _conn_path = ""


# ------------------------------------------------------------ query helpers


async def fetchone(sql: str, params: tuple = ()) -> dict | None:
    conn = await connect()
    cur = await conn.execute(sql, params)
    row = await cur.fetchone()
    return dict(row) if row else None


async def fetchall(sql: str, params: tuple = ()) -> list[dict]:
    conn = await connect()
    cur = await conn.execute(sql, params)
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def execute(sql: str, params: tuple = (), *, commit: bool = True) -> int:
    conn = await connect()
    cur = await conn.execute(sql, params)
    if commit:
        await conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------- kv helpers


async def kv_get(key: str, default: Any = None) -> Any:
    conn = await connect()
    cur = await conn.execute("SELECT v FROM kv WHERE k = ?", (key,))
    row = await cur.fetchone()
    if not row:
        return default
    try:
        return json.loads(row["v"])
    except Exception:
        return default


async def kv_set(key: str, value: Any) -> None:
    conn = await connect()
    await conn.execute(
        "INSERT INTO kv(k, v, updated_at) VALUES(?, ?, ?) "
        "ON CONFLICT(k) DO UPDATE SET v = excluded.v, updated_at = excluded.updated_at",
        (key, json.dumps(value, ensure_ascii=False), int(time.time())),
    )
    await conn.commit()


async def kv_del(key: str) -> None:
    conn = await connect()
    await conn.execute("DELETE FROM kv WHERE k = ?", (key,))
    await conn.commit()


# ------------------------------------------------------------- event helpers


async def log_event(topic: str, payload: dict[str, Any] | None = None) -> None:
    conn = await connect()
    await conn.execute(
        "INSERT INTO events(ts, topic, payload) VALUES(?, ?, ?)",
        (int(time.time()), topic, json.dumps(payload or {}, ensure_ascii=False)),
    )
    await conn.commit()


async def recent_events(topic: str | None = None, limit: int = 50) -> list[dict]:
    conn = await connect()
    if topic:
        cur = await conn.execute(
            "SELECT * FROM events WHERE topic = ? ORDER BY id DESC LIMIT ?",
            (topic, limit),
        )
    else:
        cur = await conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ----------------------------------------------------------- append-only log


def append_jsonl(folder: str, prefix: str, record: dict[str, Any]) -> None:
    """按月分片、append-only。断电最多丢最后一行，不会毁掉整份历史。"""
    d = config.archive_dir() / folder
    d.mkdir(parents=True, exist_ok=True)
    month = time.strftime("%Y%m")
    path = d / f"{prefix}-{month}.jsonl"
    line = json.dumps(record, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def iter_jsonl(folder: str, prefix: str) -> Iterable[dict[str, Any]]:
    d = config.archive_dir() / folder
    if not d.exists():
        return
    for path in sorted(d.glob(f"{prefix}-*.jsonl")):
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    continue
