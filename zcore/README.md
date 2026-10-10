# zcore - Z-Agent 内核：基因组支持层

zcore 现在只放「被基因组静态 import 的支持层」。

## 目录布局

    ~/zagent.py   基因组主文件
    ~/zcore/      基因组支持（可导入）
        __init__.py
        util.py       纯函数，被 zagent 静态 import
    ~/zguard/     救援基础设施（永不随基因组进化）
        __init__.py
        boot_guard.py
        boot_guard_selftest.py
        shadow_test.py
        shadow_child.py

## 为什么分两层

救援器官必须在主进程之外运行：若 zagent.py 语法坏掉，Python 加载不了它
去自检。它们与「内腑」（util.py）的依赖约束完全相反，不该同屋。物理分居后，
boot_guard 的 genome_files() 天然只扫 zcore，无需再靠排除清单 hack。

## 承重梁：隐式路径契约

两目录的模块之间零 import 关系，靠 dirname(__file__) 与本文件同层兄弟耦合。
因 zguard 与 zcore 同处 ~ 之下，dirname 算术天然对称——但仍是静默契约：

| 契约 | 含义 |
| --- | --- |
| boot_guard.root() | 基因组根 = 自身上一级的上一级 = ~ |
| shadow_test.CHILD | 兄弟 shadow_child.py 必须同层 |
| shadow_test.DEFAULT_ROOT | 父目录须含 zagent.py 与 zcore/ |
| boot_guard sibling | 同层须有 shadow_test.py |
| zagent 引用 | zguard/shadow_test.py、zguard/boot_guard.py |

## 动手前必读

    python ~/work/zcore_path_contract.py

它把上面每条隐式契约显式验证一遍（自动适配 zcore 旧布局与 zguard 新布局）。
重组前全绿、重组后仍全绿，才算安全。看守者不与被看守者同处一地。
