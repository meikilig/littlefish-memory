"""全局配置：一个根目录，一份真相。

所有路径都从 ROOT 派生，不允许散落在其他盘符。
配置本体存放在 <ROOT>/config.json，缺失时自动写入默认值。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# 根目录：默认是本包所在目录的上一级（也就是仓库根），可用环境变量覆盖。
# 想放到别处（比如 ~/.littlefish）就设 LITTLEFISH_ROOT，所有路径都从它派生。
_DEFAULT_ROOT = Path(__file__).resolve().parent.parent
ROOT = Path(os.environ.get("LITTLEFISH_ROOT", _DEFAULT_ROOT)).resolve()

DEFAULT_CONFIG: dict[str, Any] = {
    "character": "test",          # 当前角色目录名，切换角色只改这里
    "owner": {
        "qq": "",                 # 主人 QQ 号，空 = 不特殊对待
        "label": "主人",
    },
    "platforms": {
        "qq": {"enabled": True},
        "bili": {"enabled": True},
    },
    "memory": {
        "inject_max_chars": {"qq": 2200, "bili": 2000},
        # 门牌号（<room>/<rooms>）最多占多少字。从上面这份预算里**先扣**，
        # 剩下的才给七桶记忆。320 大约是省着来的两倍，够一扇本窗 + 三个别的窗；
        # 设 0 = 完全关掉门牌号（她回到"只有一扇窗"的状态，2026-11-01 加）。
        "room_max_chars": 320,
        "rooms_active_hours": 12,   # 多久没动静的窗不算"还开着"
        "rooms_max": 3,             # 最多报几扇别的窗；再多的只报数量不报名字
        "warn_chars": 60000,
        "max_chars": 0,           # 0 = 不限
        "max_records": 600,
        "daily_hour": 4,          # 每天几点跑衰减
        "decay_per_day": 1,       # 每天最多降几档
        "decay_min_age_days": 1,  # 今天刚记的事今天不降档
        "fuzzy_after_days": 7,
        "forget_after_days": 60,
        "min_owner_importance": 65,
        "first_window_days": 30,
        "first_slots": 3,          # 「最初的事」最多留几条
        "maintain_interval_min": 10,
        # ---- 四级记忆的升降规则（2026-11-02 阶段②）----
        # 自动检索：每收到一句，本地 bigram 搜一遍记忆库，命中就记一次"被想起"。
        # 零 API 成本（不调模型），只查本地倒排表。
        "auto_recall": True,
        "recall_topn": 3,          # 一条消息最多算命中几条（多了会通胀）
        "recall_min_hits": 2,      # 至少命中几个词才算"想起来"，防单字误中
        "recall_same_topic_hours": 24,  # 同一话题这么久内只计一次，防刷
        "promote_hits": {"l2": 1, "l3": 3, "l4": 8},  # 被想起几次升到哪一级
        "hold_days": {"l2": 7, "l3": 14},             # 多久没被想起就降一级
        "l1_keep_days": 14,        # L1 放多久还没人用 → 压成一行进本子（阶段③接）
    },
    "privacy": {
        "include_qq_in_bili": False,   # 默认不把 QQ 私事带进 B站 提示词
        "redact": True,
        # 场景隔离：她记得的事，只在她记得的那个场景里提。
        "isolation": {
            "enabled": True,
            "private_in_group": False,   # 私聊里的记忆，默认不在群里提
            "cross_group": False,        # A 群的事，默认不在 B 群提
            "group_in_private": False,   # 群里的记忆，默认不在私聊里提
        },
    },
    "budget": {
        "enabled": True,
        "currency": "CNY",
        "prices": {
            # 每百万 token 的价格，按需改；单位：currency
            "default": {"input": 2.0, "cached": 0.5, "output": 8.0}
        },
        "daily_limit": 0,          # 0 = 不限制
        "warn_ratio": 0.8,
        "per_feature": {           # 单个一级功能每天最多花多少，0 = 不限
            "llm": 0,
            "bili": 0,
            "voice": 0,
        },
        "hard_stop": True,         # 超了是直接停，还是只报警
    },
    "self_note": {"max_entries": 40},
    # 睡前那一页（L1-10）：00:05 写昨天；主人关电脑没写成，下次开机补（最多 3 天）
    # learned_max = 一晚上最多挑几条进永久记忆卡（主人 2026-11-02：额度大点、要可调）
    "nightly": {
        "enabled": True, "hour": 0, "minute": 5, "catchup_delay_sec": 180,
        "learned_max": 6,          # 原来写死 2 条；改成可调，默认 6
        "fix_max": 2,              # "以后该怎么做" 最多几条
        "learned_chars": 60,       # 每条多长（原来 40，放宽）
        "fix_chars": 40,
    },
    # 身体（能力-疲劳与作息）：电量 100（满）～ -100（极限透支），读时按时间差推演。
    # 2026-11-01 主人把"精神值/疲劳度"整体翻转成**电量（Battery）**：
    #   - 面板不再显示倒挂的"疲劳百分比"，直接说还剩多少电；
    #   - 阈值改成绝对点数，不再做 100-x 换算；
    #   - 档位从三档改成"正常 / 低功耗 / 强制关机"，中间带迟滞防抖。
    #
    # 数值怎么来的（16h 清醒 = 8h 睡眠，循环不漂移）：
    #   醒着 4.0/h × 16.5h = 66  ←→  睡 8h × 8.0/h = 64（封顶 70 留余量）
    #   正常作息一整天：100 → 睡前往返回到 100，收支刚好平。
    #   聊天/看书/出门是**额外**开销，会把当天变成亏损 —— 那才是"她今天累了"。
    "vitals": {
        "enabled": True,
        "awake_rate": 4.0,          # 醒着每小时掉多少电
        "oversleep_penalty": 1.0,   # 该睡没睡，额外每小时再掉多少（合计 5.0/h）
        "onset_at": 60.0,           # 窗口内低于这个值就想睡
        "wake_at": 100.0,           # 睡到这么多自然醒
        # 三档 + 迟滞（主人 2026-11-01 定）
        "low_at": 15.0,             # ≤ 这个值进低功耗（回复变短、不出门、不主动找事）
        "low_exit_at": 25.0,        # > 这个值才退出低功耗（10 点迟滞，防抖）
        "shutdown_at": -20.0,       # ≤ 这个值强制关机（叫不醒，只回梦话）
        "shutdown_exit_at": 0.0,    # 电量 > 这个值才醒（醒了还晕，说完又睡）
        "dizzy_at": 30.0,           # 刚醒（< 这个值）算"晕"，注入一句委屈的叙事
        "quiet_minutes": 10,        # 夜里窗口内：最近这么多分钟没人说话才会自动睡
        "resleep_minutes": 25,      # 被叫醒后这么久内不再睡回去
        "wake_penalty": 3.0,        # 每次被叫醒额外扣多少
        "short_sleep_minutes": 90,  # 短于这个时长的那段算浅睡
        "short_sleep_rate": 2.0,    # 浅睡每小时算回多少
        # 白天补觉（主人 2026-11-01 定：让她更自由，没事干自己补会儿觉）
        "nap_enabled": True,
        "nap_below": 40.0,          # 电量 ≤ 这个值、且安静够久 → 自己去眯一会
        "nap_quiet_minutes": 30,    # 安静多久才睡（别在你正跟她说话时说睡就睡）
        "nap_minutes": 60,          # 一次最多眯多久
        "nap_gain": 10.0,           # 一次最多充多少（睡到够或到点，哪个先到算哪个）
        # 深睡：电量很低时不能按"补 10 点就叫醒"办，否则会陷入
        # "补 10 → 还是低电量 → 又补 10"的死循环，整个下午都在打盹（2026-11-01 实测）。
        # 低于 nap_deep_below 就睡深一点：一次补 nap_deep_gain，最长 nap_deep_minutes。
        "nap_deep_below": 20.0,
        "nap_deep_gain": 30.0,
        "nap_deep_minutes": 120,
        # 自己决定眯一会儿（工具 take_nap / 面板 /body 眯）。和自动补觉的区别：
        # 不用等安静 30 分钟、不用电量 ≤40，时长自己定；醒来不算"被吵醒"，不套短睡克扣。
        "nap_tool": {
            "enabled": True,
            "min_minutes": 10,      # 少于这个没意义（刚躺下就起）
            "max_minutes": 45,      # 白天睡太久会压到晚上
            "daily_limit": 3,       # 一天最多几次，防她一个下午睡过去
            "max_gain": 8.0,        # 一次最多充多少（45 分钟 × 8/h = 6，够用）
            "max_vigor": 70.0,      # 电量高于这个就别睡了，躺下也睡不着
        },
        # 关机/休眠补算（2026-11-02）：关机时她不在跑，别用 60 秒步长把一夜切成很多
        # "短睡"（那会被 90 分钟线反复扣回，实测 8.5 小时只补了 8.4）。
        # 规则：只补睡眠窗口里那一段，时间与电量双重封顶。
        "offline": {
            "enabled": True,
            "min_gap_minutes": 15,    # 小于它当普通停顿，照旧推演
            "charge_until": "08:00",  # 补到几点（关机后第一个这个时刻）
            "max_hours": 10.0,        # 一趟最多补几小时（时间上限）
            "hard_cap": 100.0,        # 补完最高到多少（电量上限）
            "rate": 8.0,              # 每小时回多少
        },
        "max_dt_hours": 36,         # 单次推演最多按这么多小时算（防时间跳变）
        "tick_minutes": 5,          # 每几分钟落一次盘
        "history": True,            # tick 时顺手记一条 vitals.tick（看趋势用；一天 288 条）
        # 超速疲劳：当天花的钱超了这条线，电量掉得比平常快。
        # 这是**独立**的口子，不去动 budget 的闸门（那边 daily_limit 一旦有数会直接拦回复）。
        "overload_yuan": 0.0,       # 每天花超这个数（元）就开始超速；0 = 关
        "overload_per_hour": 6.0,   # 超线后额外每小时掉多少点（醒着 4 + 6 = 10/h）
        # 活动消耗（单位：点）。写 `vitals.spend(-x)`，由 settle() 汇总扣掉。
        "cost_reply": 0.5,          # 回一句短话（≤120 字）
        "cost_reply_long": 1.0,     # 回一句长话（>120 字）
        "cost_read": 1.5,           # 看一章书
        "cost_write": 2.0,          # 写一页字
        "cost_bili": 3.0,           # B站 出门一趟
        "cost_nightly": 2.0,        # 夜里复盘写日记
        # 睡眠窗口：先只有两段，想加就往后追加一条
        # {"name","start","end","rate","recovery_cap"}
        "windows": [
            {"name": "夜里长睡", "start": "23:30", "end": "07:30",
             "rate": 8.0, "recovery_cap": 70},
            {"name": "午睡", "start": "13:00", "end": "14:00",
             "rate": 8.0, "recovery_cap": 10},
        ],
        # 咖啡（2026-11-01 主人要的）：强行提神 2 小时，结束后超快掉电、直接关机。
        # 靠 apply_effect 的三件套（instant / rate / rebound）拼出来，不写新机制：
        #   喝下去 +25 电 → 2 小时内掉电打 1/4 → 2 小时一到一次性 -55，跌破 -20 强制关机。
        "coffee": {
            "instant": 25.0,       # 灌下去立刻补多少电
            "hours": 2.0,          # 提神维持多久
            "rate_mult": 0.25,     # 期间掉电打折（4.0 → 1.0/h）
            "crash_delta": -55.0,  # 到期一把扣多少（"结束就关灯"）
            "crash_floor": -25.0,  # 到期后电量不低于…也就是一定压到 -25（关机线是 -20）
            "wake": True,          # 睡着时也能被灌醒
        },
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def ensure_dirs() -> None:
    for sub in ("", "data", "data/archive", "data/persona", "backups", "logs"):
        (ROOT / sub).mkdir(parents=True, exist_ok=True)


def load_config() -> dict[str, Any]:
    ensure_dirs()
    path = ROOT / "config.json"
    if not path.exists():
        path.write_text(
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    return _deep_merge(DEFAULT_CONFIG, raw)


_cfg: dict[str, Any] | None = None


def cfg() -> dict[str, Any]:
    global _cfg
    if _cfg is None:
        _cfg = load_config()
    return _cfg


def reload() -> dict[str, Any]:
    global _cfg
    _cfg = load_config()
    return _cfg


def save(full: dict[str, Any]) -> dict[str, Any]:
    """把整份配置写回 config.json（临时文件 + 原子替换），再刷新内存里的那份。"""
    ensure_dirs()
    path = ROOT / "config.json"
    tmp = path.with_name(f"config.json.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(full, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return reload()


def data_dir() -> Path:
    return ROOT / "data"


def archive_dir() -> Path:
    """全量归档也按角色分家：换角色 = 换一整个包，不会串味。"""
    d = character_dir() / "archive"
    d.mkdir(parents=True, exist_ok=True)
    return d


def character_dir(name: str | None = None) -> Path:
    name = name or str(cfg()["character"])
    d = ROOT / "data" / "persona" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    """每个角色一个库：切角色只改 config.character，记忆/人格/自我卡整包隔离。"""
    return character_dir() / "littlefish.db"
