"""小鱼记忆系统（记忆子集）。

这里只打包"记忆"这一块，不含 AstrBot 插件、QQ 接入、B站 插件等。
所以它不是 `pip install` 就能接进 QQ 的包，而是"能自己跑一遍"的最小闭环：

    config  一个根目录、一份 config.json
    db      sqlite（四张表：memory / memory_token / life_log / archive_page）
    bus     进程内事件总线（life.py 往上面发事件用）
    log     日志
    memory  长期记忆 + 四级分层 + 注入装配
    recall  "被想起"计数与升降级
    archive 原话档案室（一天一页）
    life    白天流水（隔天由夜间复盘提炼进长期记忆）

演示跑法见仓库根目录的 demo.py。
"""

from . import archive, bus, config, db, life, log, memory, recall

__all__ = ["archive", "bus", "config", "db", "life", "log", "memory", "recall"]
