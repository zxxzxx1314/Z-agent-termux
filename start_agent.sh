#!/data/data/com.termux/files/usr/bin/bash

# 1. 加载你通过 switch_api.sh 设置的配置
if [ -f ~/.rain_env ]; then
    source ~/.rain_env
fi

# 2. 启动 Z-Agent —— 经由启动看门狗（zguard/boot_guard.py）：
#    启动前三层自检（AST 语法 / 关键符号 / 子进程隔离导入冒烟）
#    → 通过则快照全基因组并放行；
#    → 失败则自动回滚上一个验证过的快照再放行；
#    → 快照也坏则 FATAL 停机待人工，绝不运行坏躯体。
#    手动直跑主体（跳过自检，不推荐）：python ~/zagent.py
exec python ~/zguard/boot_guard.py
