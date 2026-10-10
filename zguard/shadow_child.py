#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""zcore.shadow_child —— 影子沙箱驱动器（在被测基因组的进程内运行）

它回答的问题是 Z-Agent 自评里的第 1、4 条：
  「启动自检只能拦『倒下』（语法/导入错误），拦不住『站错』——
   TODOS 被改成 None 这类逻辑错误，静态全绿、运行即崩。」
  「缺一套能在不惊动真身的前提下，把改动真跑一遍的隔离沙箱。」

工作方式：
  在一次性沙箱目录内，加载被测的 zagent.py 基因组，用**假 LLM**
  （替换 _post——它是唯一 API 出口）注入剧本化对话，让 agent_loop、
  脚本执行、待办、验收合同、反思、会话持久化等**真实跑一遍**，
  最后断言一批运行时不变量。任何一步崩溃或违约 → SHADOW_FAIL。

两层用法：
  standard —— 标准剧本：T1 脚本执行 / T2 待办 / T3 验收全链路
              （注册→产出→客观验收→收割→反思入库）/ T4 命令通道 /
              T5 会话往返 / 全局不变量。
  gate     —— 进化闸门端到端剧本：站错型坏进化必须被闸门拦截
              （基因不落盘），正常好进化必须放行（防闸门恒 False）。

本文件是救援基础设施（与 boot_guard 同级），不属于被守护基因组：
  - 只用标准库，绝不 import 主体模块的任何符号（只动态加载它）；
  - 纯 stdlib + 无网络 + 无真实 API Key，子进程隔离，随时可杀。

输出：最后一行 "SHADOW_RESULT {json}"（ok/checks/errors），
退出码 0/1。由 zcore/shadow_test.py（编排器）调用，也可手工运行：
  python zcore/shadow_child.py <沙箱目录> [standard|gate]
"""
import hashlib
import importlib.util
import json
import os
import re
import sys
import traceback


def _p(msg):
    print(msg, flush=True)


class Checker(object):
    """收集断言与崩溃现场，最后一次性吐出 JSON。"""

    def __init__(self):
        self.checks = []      # [[名称, 是否通过, 详情], ...]
        self.errors = []      # 崩溃 traceback 列表

    def check(self, name, ok, detail=""):
        self.checks.append([name, bool(ok), str(detail)[:300]])
        return bool(ok)


def load_module(sandbox):
    """动态加载被测基因组（必须在设置 HOME 之后调用——路径在导入期定型）。"""
    spec = importlib.util.spec_from_file_location(
        "zag_shadow", os.path.join(sandbox, "zagent.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)          # __name__ != __main__，main() 不触发
    return m


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return ""


# ============================================================
# 标准剧本：把一轮真实会话完整跑一遍
# ============================================================
def run_standard(m, sandbox, ck):
    # —— 假 LLM：_post 是唯一 API 出口，剧本从这里注入 ——
    queue = []

    def fake_post(msgs, temperature=0.3):
        if queue:
            return queue.pop(0)
        # 剧本耗尽后的安全终止符：纯文字、无任何控制标签
        return "（影子剧本已耗尽，本轮直接收尾，不再输出任何标签。）"

    m._post = fake_post
    m.AUTO_EXECUTE = True       # 影子会话无人值守，绕过 y/n 交互确认

    # 等价 main() 的启动准备（目录 + system prompt），但不进 REPL
    for d in (m.SKILLS_DIR, m.PLUGINS_DIR, m.MIND_DIR,
              m.EVOLUTION_DIR, m.CONTRACTS_DIR):
        os.makedirs(d, exist_ok=True)
    m.refresh_system()

    def turn(user_text, replies, tag):
        m.note_task(user_text)
        m.messages.append({"role": "user", "content": user_text})
        queue.extend(replies)
        try:
            return m.agent_loop()
        except Exception:
            ck.errors.append(tag + " agent_loop 崩溃:\n"
                             + traceback.format_exc())
            return "crash"

    # ---- T0 API 异常鲁棒性（真实 LLM 会超时/限流/畸形——假 _post 模拟）----
    m.messages.append({"role": "user", "content": "鲁棒性探针"})
    queue.append(None)                      # 模拟一次 API 失败（_post → None）
    try:
        r0 = m.agent_loop()
        ck.check("T0 API失败返回 api_error 不崩溃",
                 r0 == "api_error", repr(r0))
    except Exception:
        ck.errors.append("T0 API 失败路径崩溃:\n" + traceback.format_exc())
        ck.check("T0 API失败返回 api_error 不崩溃", False)

    # ---- T1 脚本执行链路（真实子进程跑 print）----
    r1 = turn("请计算 40+2 并告诉我结果", [
        "[desc]计算 40+2[/desc][script:]print(40+2)[/script]",
        "计算结果是 42，任务完成。",
    ], "T1")
    ck.check("T1 agent_loop 返回 ok", r1 == "ok", r1)
    ck.check("T1 脚本已执行（LAST_SCRIPT）",
             "print(40+2)" in (m.LAST_SCRIPT or ""))
    ck.check("T1 执行结果已回传（42）", any(
        x.get("role") == "user" and "42" in x.get("content", "")
        for x in m.messages))

    # ---- T2 待办清单（TODOS 全局状态——站错型缺陷高发区）----
    r2 = turn("给出两步计划", ["计划如下：[todo]步骤甲|步骤乙[/todo]"], "T2")
    ck.check("T2 agent_loop 返回 ok", r2 == "ok", r2)
    ck.check("T2 TODOS 为 list 且 2 项",
             isinstance(m.TODOS, list) and len(m.TODOS) == 2,
             repr(m.TODOS)[:120])

    # ---- T3 验收合同全链路（注册→产出→客观验收→收割）----
    art = os.path.join(sandbox, "artifact.txt")
    r3 = turn("创建 artifact.txt 内容为 shadow-hello，并声明验收", [
        "[accept:]\n产物: %s\n检查: import os; assert open(os.path.expanduser("
        "'~/artifact.txt')).read().strip() == 'shadow-hello'\n标准: 文件内容正确\n"
        "[/accept]" % art,
        "[desc]写产物[/desc][script:]open('artifact.txt','w')"
        ".write('shadow-hello')[/script]",
        "产物已生成，申请验收。[reflect]影子沙箱经验：验收全链路可正常运转[/reflect]",
    ], "T3")
    ck.check("T3 agent_loop 返回 verified", r3 == "verified", r3)
    ck.check("T3 产物真实存在", os.path.exists(art))
    ck.check("T3 合同状态 passed",
             bool(m.ACTIVE_CONTRACT)
             and m.ACTIVE_CONTRACT.get("status") == "passed",
             repr(getattr(m, "ACTIVE_CONTRACT", None))[:120])
    archived = False
    if os.path.isdir(m.CONTRACTS_DIR):
        for fn in os.listdir(m.CONTRACTS_DIR):
            if fn.endswith(".json"):
                try:
                    with open(os.path.join(m.CONTRACTS_DIR, fn),
                              encoding="utf-8") as f:
                        if json.load(f).get("status") == "passed":
                            archived = True
                except Exception:
                    pass
    ck.check("T3 合同已存档（contracts/*.json=passed）", archived)
    refl_ok = False
    if os.path.isdir(m.SKILLS_DIR):
        for fn in os.listdir(m.SKILLS_DIR):
            if fn.startswith("反思_") and fn.endswith(".md"):
                if "影子沙箱经验" in _read(os.path.join(m.SKILLS_DIR, fn)):
                    refl_ok = True
    ck.check("T3 反思已入库（skills/反思_*.md）", refl_ok)
    ck.check("T3 journal 记录验收通过", "验收通过" in _read(m.JOURNAL_PATH))
    ck.check("T3 未竟待办转问题队列", "步骤甲" in _read(m.PROBLEMS_PATH))

    # ---- T4 命令通道 ----
    try:
        m.handle_command("/help")
        m.handle_command("/contract")
        ck.check("T4 命令通道无异常", True)
    except Exception:
        ck.errors.append("T4 handle_command 崩溃:\n" + traceback.format_exc())
        ck.check("T4 命令通道无异常", False)

    # ---- T5 会话持久化往返（按 load/save 的真实语义断言）----
    try:
        limit = getattr(m, "SESSION_MAX_TURNS", None)
        tail = m.messages[-limit:] if limit else m.messages
        expected = [x for x in tail
                    if x.get("role") in ("user", "assistant")]
        m.save_session(m.messages)
        restored = m.load_session()
        ck.check("T5 会话往返一致", isinstance(restored, list)
                 and len(restored) == len(expected),
                 "期望 %d 条，实际 %s 条"
                 % (len(expected),
                    len(restored) if isinstance(restored, list) else "?"))
    except Exception:
        ck.errors.append("T5 会话往返崩溃:\n" + traceback.format_exc())
        ck.check("T5 会话往返一致", False)

    # ---- T6 字面区隔离（v6.1：字面标签不被误抓执行）----
    # 历史：正文里出现字面 [script:] 即被误抓执行 → SyntaxError 频发，
    # AI 只能「不写字面方括号」自我阉割。字面区机制的四向断言：
    # 字面不执行 / 真脚本照执行 / 字面 todo 不污染 / 原文完整入会话。
    sent6 = (
        "协议示例如下：\n[literal]\n示例标签形态：[script:]"
        "open('literal_trap.txt','w').write('leaked')[/script]\n"
        "示例待办：[todo]字面甲|字面乙[/todo]\n[/literal]\n"
        "下面是真正要执行的脚本：\n[desc]写字面区探针[/desc]"
        "[script:]open('literal_probe.txt','w').write('real')[/script]\n"
        "以上，字面区内容仅供展示，真实脚本请执行。"
    )
    r6 = turn("解释标签协议并执行真实脚本", [sent6, "演示完成。"], "T6")
    trap = os.path.join(sandbox, "literal_trap.txt")
    probe = os.path.join(sandbox, "literal_probe.txt")
    ck.check("T6 agent_loop 返回 ok", r6 == "ok", r6)
    ck.check("T6 字面区脚本未执行（无泄露写入）", not os.path.exists(trap))
    ck.check("T6 真实脚本已执行（literal_probe=real）",
             _read(probe).strip() == "real", _read(probe)[:80])
    todo_names = [t[1] for t in (m.TODOS or [])]
    ck.check("T6 字面待办未污染 TODOS",
             "字面甲" not in todo_names and "字面乙" not in todo_names,
             repr(m.TODOS)[:150])
    ck.check("T6 原文完整进入会话（含字面区）", any(
        x.get("role") == "assistant" and "[literal]" in x.get("content", "")
        for x in m.messages))

    # ---- 全局不变量 ----
    ck.check("INV messages[0] 为 system",
             bool(m.messages) and m.messages[0].get("role") == "system")
    ck.check("INV 角色全部合法",
             all(x.get("role") in ("system", "user", "assistant")
                 for x in m.messages))
    ck.check("INV 含 assistant 回复",
             any(x.get("role") == "assistant" for x in m.messages))


# ============================================================
# 进化闸门端到端剧本（配合 zagent.shadow_gate 使用）
# ============================================================
def run_gate(m, sandbox, ck):
    """在本沙箱内真实调用 apply_evolution：
    坏进化（站错型）必须被影子闸门拦截且基因不落盘；
    好进化必须放行落盘（防闸门恒 False）。"""
    src_path = m.SELF_PATH
    src = _read(src_path)
    ck.check("GATE 基因可读", bool(src))

    def bump(text, ver):
        return re.sub(r"(?m)^VERSION\s*=\s*[0-9.]+",
                      "VERSION = " + ver, text, count=1)

    bad = bump(src.replace("for done, task in TODOS:",
                           "for task in TODOS:", 1),
               "%.1f" % (m.VERSION + 0.1))
    good = bump(src, "%.1f" % (m.VERSION + 0.2))
    ck.check("GATE 坏进化变异已生效",
             "for task in TODOS:" in bad and bad != src)

    def sha(t):
        return hashlib.sha256(t.encode("utf-8")).hexdigest()[:16]

    # —— 坏进化：静态验证+冒烟全绿，只有影子沙箱能拦 ——
    ok_bad, msg_bad = m.apply_evolution(bad, "闸门测试：站错型坏进化")
    ck.check("GATE 坏进化被拦截", ok_bad is False, str(msg_bad)[:200])
    ck.check("GATE 拦截原因指向影子沙箱", "影子" in str(msg_bad),
             str(msg_bad)[:200])
    ck.check("GATE 坏进化未落盘（基因完好）",
             _read(src_path) == src,
             "sha %s -> %s" % (sha(src), sha(_read(src_path))))

    # —— 好进化：必须放行（闸门不是只会说 No）——
    ok_good, msg_good = m.apply_evolution(good, "闸门测试：正常好进化")
    ck.check("GATE 好进化被放行", ok_good is True, str(msg_good)[:200])
    ck.check("GATE 好进化已落盘",
             _read(src_path).strip() == good.strip())


def main():
    if len(sys.argv) < 2:
        _p("用法: shadow_child.py <沙箱目录> [standard|gate]")
        return 2
    sandbox = os.path.abspath(sys.argv[1])
    scenario = sys.argv[2] if len(sys.argv) > 2 else "standard"
    # 环境必须在加载基因组之前定型（路径常量在导入期求值）
    os.environ["HOME"] = sandbox
    os.environ["DEEPSEEK_API_KEY"] = "shadow-dummy-key"
    os.environ["RAIN_AUTO_NOTIFY"] = "0"       # 关掉 termux 通知噪音
    os.chdir(sandbox)
    sys.path.insert(0, sandbox)                # from zcore.util import ... 用沙箱内副本
    ck = Checker()
    try:
        m = load_module(sandbox)
        if scenario == "gate":
            run_gate(m, sandbox, ck)
        else:
            run_standard(m, sandbox, ck)
    except Exception:
        ck.errors.append("加载/驱动阶段崩溃:\n" + traceback.format_exc())
    payload = {
        "ok": bool(ck.checks) and not ck.errors
        and all(c[1] for c in ck.checks),
        "scenario": scenario,
        "checks": ck.checks,
        "errors": [e[-800:] for e in ck.errors],
    }
    _p("SHADOW_RESULT " + json.dumps(payload, ensure_ascii=False))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
