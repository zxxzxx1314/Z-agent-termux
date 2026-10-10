#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""zcore.boot_guard_selftest —— 看门狗好/坏文件对照自测（双向证伪）

证明「看门狗真的在判断、真的能救援」，而不是恒真摆设。全部用例在
一次性沙箱 HOME 中进行，绝不触碰真实家目录：

  用例 1  GOOD_FIRST_BOOT      好基因组：自检过 → 快照生成 → 真实启动 main
  用例 2  BAD_MAIN_ROLLBACK    主文件导入即崩：自动回滚快照 → 重新自检 → 真实启动
  用例 3  BAD_SNAPSHOT_FATAL   主文件与快照同时坏：FATAL(2)，绝不运行坏躯体
  用例 4  NO_SNAPSHOT_FATAL    无快照且主文件坏：FATAL(3)，不掩盖不启动
  用例 5  BAD_ZCORE_ROLLBACK   zcore 依赖坏：证明回滚是全基因组级，不止主文件
  用例 6  PROBE_BOTH_WAYS      --check 探针：好文件 PASS / 坏文件 FAIL（双向证伪）
  用例 7  LOGIC_LAYERING       站错型缺陷（运行时才崩）：静态/旧冒烟全绿，影子沙箱必拦
  用例 8  GATE_E2E             进化闸门端到端：坏进化被拦且基因零损伤，好进化放行
  用例 9  LITERAL_ZONE         字面区机制端到端：字面标签不被误抓（T0/T6 断言）
  用例 10 RESTART_LOOP         /restart→boot_guard→新我横幅（进化后观察闭环）

关键点：用例 1/2/5 的「真实启动」是跑 zagent.py 的 main() 本体
（dummy key + stdin=EOF 无交互），直接补上自我评估第 1 条抱怨的盲区——
「main() 从来没法真正跑一遍」。启动横幅出现 + 退出码 0 = 下次启动能跑起来。

用法：python zcore/boot_guard_selftest.py
环境：ZAGENT_SELFTEST_TMPDIR 可指定沙箱根目录（默认系统临时目录）。
退出码：0 = 全部通过；1 = 存在失败（有失败必须修，不许静默）。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

PY = sys.executable
SELF_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_ROOT = os.path.dirname(SELF_DIR)      # 被拷贝的真实基因组来源（部署时 = ~）
CASE_TIMEOUT = 150                        # 单用例超时（秒）

RESULTS = []


def _log(tag, msg):
    print("  [%s] %s" % (tag, msg), flush=True)


def make_sandbox():
    """新建一次性沙箱：拷贝真实 zagent.py + zcore/*.py（不含缓存与看门狗运行数据）。"""
    sb = tempfile.mkdtemp(prefix="bootguard_case_",
                          dir=os.environ.get("ZAGENT_SELFTEST_TMPDIR") or None)
    shutil.copy2(os.path.join(SRC_ROOT, "zagent.py"), os.path.join(sb, "zagent.py"))
    zg_dst = os.path.join(sb, "zguard")
    os.makedirs(zg_dst, exist_ok=True)
    for name in sorted(os.listdir(SELF_DIR)):
        if name.endswith(".py"):
            shutil.copy2(os.path.join(SELF_DIR, name), os.path.join(zg_dst, name))
    zc_src = os.path.join(SRC_ROOT, "zcore")
    if os.path.isdir(zc_src):
        shutil.copytree(zc_src, os.path.join(sb, "zcore"),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return sb


def corrupt(path, kind):
    """制造坏文件。kind: syntax | import | missing_func"""
    with open(path, encoding="utf-8") as f:
        src = f.read()
    if kind == "syntax":
        src = "def broken(:\n" + src            # 第 1 层即拦：语法错误
    elif kind == "import":
        src = "import __no_such_module_zz__\n" + src   # 第 3 层才拦：导入即崩
    elif kind == "missing_func":
        src = src.replace("def main(", "def main_x(", 1)  # 第 2 层才拦：关键符号缺失
    else:
        raise ValueError(kind)
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)


def file_sha(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def run_guard(sb, launch=True, check=False, timeout=CASE_TIMEOUT):
    """在沙箱中以黑盒方式运行看门狗子进程。HOME 一律指向沙箱。"""
    env = dict(os.environ)
    env["HOME"] = sb                          # 沙箱 HOME：agent 的所有读写都在沙箱内
    env["ZAGENT_HOME"] = sb                   # 显式指定宿主根，顺带覆盖该开关
    env["DEEPSEEK_API_KEY"] = "dummy-key-for-boot-selftest"   # 无交互启动
    env["PYTHONUTF8"] = "1"
    env["ZAGENT_LAUNCH"] = "1" if launch else "0"
    argv = [PY, os.path.join(sb, "zguard", "boot_guard.py")]
    if check:
        argv.append("--check")
    t0 = time.time()
    r = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=timeout)
    out = (r.stdout or b"").decode("utf-8", "replace")
    err = (r.stderr or b"").decode("utf-8", "replace")
    return r.returncode, out, err, time.time() - t0


def read_log(sb):
    p = os.path.join(sb, "work", "boot_guard.log")
    if not os.path.exists(p):
        return ""
    with open(p, encoding="utf-8") as f:
        return f.read()


def case(name):
    def deco(fn):
        def wrapper():
            _log(name, "开始")
            sb = make_sandbox()
            try:
                ok, detail = fn(sb)
            except subprocess.TimeoutExpired:
                ok, detail = False, "用例超时(%ds)" % CASE_TIMEOUT
            except Exception as e:
                ok, detail = False, "异常:" + repr(e)
            finally:
                shutil.rmtree(sb, ignore_errors=True)
            RESULTS.append((name, ok, detail))
            _log(name, ("PASS" if ok else "FAIL") + " —— " + detail)
        return wrapper
    return deco


# ------------------------------------------------------------
@case("1_GOOD_FIRST_BOOT 好基因组→快照→真实启动")
def case1(sb):
    rc, out, err, dt = run_guard(sb)
    if rc != 0:
        return False, "退出码 %d（期望 0）stderr=%s" % (rc, err[-300:])
    if "Z-Agent" not in out:
        return False, "未见启动横幅——main() 没有真正跑起来"
    mf = os.path.join(sb, "work", "boot_guard", "last_good", "manifest.json")
    if not os.path.exists(mf):
        return False, "快照清单未生成"
    n = len(json.load(open(mf, encoding="utf-8"))["files"])
    if "self_check -> True" not in read_log(sb):
        return False, "日志缺少自检通过记录"
    return True, "自检过 + 快照 %d 文件 + 真实启动(%.1fs)" % (n, dt)


@case("2_BAD_MAIN_ROLLBACK 主文件导入即崩→自动回滚→真实启动")
def case2(sb):
    rc, _, _, _ = run_guard(sb, launch=False)          # 先正常快照（不启动）
    if rc != 0:
        return False, "预快照阶段退出码 %d" % rc
    main_py = os.path.join(sb, "zagent.py")
    good_sha = file_sha(os.path.join(sb, "work", "boot_guard", "last_good", "zagent.py"))
    corrupt(main_py, "import")                          # 导入即崩（最深层故障）
    rc, out, err, dt = run_guard(sb)
    if rc != 0:
        return False, "回滚启动退出码 %d stderr=%s" % (rc, err[-300:])
    if "Z-Agent" not in out:
        return False, "回滚后未见启动横幅"
    if file_sha(main_py) != good_sha:
        return False, "主文件哈希与快照不一致——回滚不完整"
    log = read_log(sb)
    if "rollback_ok" not in log:
        return False, "日志缺少 rollback_ok"
    return True, "导入级故障被拦 + 回滚哈希一致 + 真实启动(%.1fs)" % dt


@case("3_BAD_SNAPSHOT_FATAL 主文件与快照都坏→FATAL 不运行坏躯体")
def case3(sb):
    run_guard(sb, launch=False)
    main_py = os.path.join(sb, "zagent.py")
    corrupt(main_py, "syntax")
    corrupt(os.path.join(sb, "work", "boot_guard", "last_good", "zagent.py"), "syntax")
    rc, out, err, _ = run_guard(sb)
    if rc != 2:
        return False, "退出码 %d（期望 2）" % rc
    if "Z-Agent" in out:
        return False, "FATAL 路径竟然启动了坏躯体！"
    if "FATAL" not in read_log(sb):
        return False, "日志缺少 FATAL 标记"
    return True, "双坏 → FATAL(2)，坏躯体未被执行"


@case("4_NO_SNAPSHOT_FATAL 无快照且主文件坏→FATAL(3)")
def case4(sb):
    main_py = os.path.join(sb, "zagent.py")
    corrupt(main_py, "syntax")
    rc, out, err, _ = run_guard(sb)
    if rc != 3:
        return False, "退出码 %d（期望 3）" % rc
    if "Z-Agent" in out:
        return False, "无快照却启动了？！"
    with open(main_py, encoding="utf-8") as f:
        if "def broken(:" not in f.read():
            return False, "坏文件被意外改动"
    return True, "无快照 → FATAL(3)，未掩盖也未启动"


@case("5_BAD_ZCORE_ROLLBACK zcore 坏→全基因组回滚（单文件快照救不了的场景）")
def case5(sb):
    run_guard(sb, launch=False)
    util = os.path.join(sb, "zcore", "util.py")
    good_sha = file_sha(os.path.join(sb, "work", "boot_guard", "last_good", "zcore", "util.py"))
    corrupt(util, "syntax")
    rc, out, err, dt = run_guard(sb)
    if rc != 0:
        return False, "退出码 %d stderr=%s" % (rc, err[-300:])
    if "Z-Agent" not in out:
        return False, "回滚后未见启动横幅"
    if file_sha(util) != good_sha:
        return False, "zcore/util.py 未被恢复——快照不是全基因组级"
    return True, "zcore 依赖坏被拦 + 全基因组恢复 + 真实启动(%.1fs)" % dt


@case("6_PROBE_BOTH_WAYS --check 探针：好 PASS / 坏 FAIL（双向证伪）")
def case6(sb):
    rc, out, _, _ = run_guard(sb, check=True)
    if rc != 0 or not out.startswith("PASS"):
        return False, "好文件探针未 PASS（rc=%d out=%s）" % (rc, out[:120])
    corrupt(os.path.join(sb, "zagent.py"), "missing_func")
    rc2, out2, _, _ = run_guard(sb, check=True)
    if rc2 != 1 or not out2.startswith("FAIL"):
        return False, "坏文件探针未 FAIL（rc=%d out=%s）——探针疑似恒真" % (rc2, out2[:120])
    return True, "PASS/FAIL 双向成立，探针在真判断"


# ------------------------------------------------------------
# 用例 7/8：影子沙箱层（v6.0）——拦「站错」，验证进化闸门
# ------------------------------------------------------------
def run_shadow(root, scenario=None, timeout=600):
    """以黑盒方式运行影子沙箱编排器。"""
    argv = [PY, os.path.join(SELF_DIR, "shadow_test.py"), "--genome", root]
    if scenario:
        argv += ["--scenario", scenario]
    env = dict(os.environ)
    env["HOME"] = root
    env["DEEPSEEK_API_KEY"] = "dummy-key-for-shadow-selftest"
    env["PYTHONUTF8"] = "1"
    t0 = time.time()
    r = subprocess.run(argv, cwd=root, env=env, stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=timeout)
    return (r.returncode, (r.stdout or b"").decode("utf-8", "replace"),
            (r.stderr or b"").decode("utf-8", "replace"), time.time() - t0)


@case("7_LOGIC_LAYERING 站错型缺陷：静态/冒烟全绿，影子沙箱必拦")
def case7(sb):
    """用户点名的盲区：TODOS 类逻辑错误（「站错」）语法正常、能启动、
    运行即崩。本用例注入 render_todos 解包缺陷，证明三层分层：
    boot_guard 静态 PASS（诚实展示盲区）→ 旧冒烟 PASS（真实缺口）
    → 影子沙箱 FAIL（新层补位）。缺任何一层论证都不成立。"""
    main_py = os.path.join(sb, "zagent.py")
    with open(main_py, encoding="utf-8") as f:
        src = f.read()
    marker = "for done, task in TODOS:"
    n = src.count(marker)
    if n != 1:
        return False, "锚点行出现 %d 处（期望 1），用例自身失效" % n
    with open(main_py, "w", encoding="utf-8") as f:
        f.write(src.replace(marker, "for task in TODOS:", 1))
    # (a) 静态三层必须仍然全绿——这是它的诚实盲区，不是 bug
    rc, out, _, _ = run_guard(sb, check=True)
    if rc != 0 or not out.startswith("PASS"):
        return False, "静态层意外拦截（rc=%d %s）——用例前提被打破" % (rc, out[:120])
    # (b) 旧冒烟层必须仍然通过——证明「站错」是旧体系的真实缺口
    code = (
        "import sys, importlib.util\n"
        "sys.path.insert(0, %r)\n"
        "spec = importlib.util.spec_from_file_location('cand', %r)\n"
        "m = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(m)\n"
        "ok, msg = m.smoke_test(%r)\n"
        "print('SMOKE_RC', ok, '|', msg)\n"
        "sys.exit(0 if ok else 1)\n"
    ) % (sb, main_py, main_py)
    env = dict(os.environ)
    env["HOME"] = sb
    env["DEEPSEEK_API_KEY"] = "dummy"
    env["PYTHONUTF8"] = "1"
    r = subprocess.run([PY, "-c", code], cwd=sb, env=env,
                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=CASE_TIMEOUT)
    if r.returncode != 0:
        err = (r.stderr or b"").decode("utf-8", "replace")
        return False, "旧冒烟层已拦截该缺陷——与分层论证不符: %s" % err[-200:]
    # (c) 影子沙箱必须拦下——运行时验证层补位
    rc3, out3, err3, dt = run_shadow(sb)
    if rc3 == 0 or "SHADOW_FAIL" not in out3:
        return False, ("影子沙箱未拦截站错型缺陷！rc=%d out=%s err=%s"
                       % (rc3, out3[-200:], err3[-200:]))
    return True, "静态 PASS + 旧冒烟 PASS + 影子 FAIL —— 分层互补成立(%.1fs)" % dt


@case("8_GATE_E2E 进化闸门端到端：坏进化被拦且零损伤，好进化放行")
def case8(sb):
    """看门狗拦的是「启动前」；进化闸门拦的是「落盘前」。本用例在沙箱里
    走 Agent 自己的 apply_evolution 全流程：站错型候选必须被影子闸门
    拦下（磁盘基因零改动），正常候选必须放行（防闸门恒 False）。"""
    rc, out, err, dt = run_shadow(sb, scenario="gate")
    if rc != 0 or "SHADOW_PASS" not in out:
        return False, ("gate 剧本未通过 rc=%d out=%s err=%s"
                       % (rc, out[-300:], err[-300:]))
    line = ""
    for ln in out.splitlines():
        if ln.startswith("SHADOW_RESULT"):
            line = ln
    if not line:
        return False, "未找到 SHADOW_RESULT 判定行"
    try:
        payload = json.loads(line[len("SHADOW_RESULT"):])
    except Exception as e:
        return False, "SHADOW_RESULT 解析失败: %r" % e
    names = {c[0]: c[1] for c in payload.get("checks", [])}
    need = ("GATE 坏进化被拦截", "GATE 拦截原因指向影子沙箱",
            "GATE 坏进化未落盘（基因完好）", "GATE 好进化被放行",
            "GATE 好进化已落盘")
    missing = [k for k in need if names.get(k) is not True]
    if missing:
        return False, "关键判定未成立: %s（checks=%s）" % (missing, names)
    return True, "站错型进化被拦且基因零损伤；好进化正常落盘(%.1fs)" % dt


# ------------------------------------------------------------
# 用例 9/10：字面区机制与自重启闭环（v6.1）
# ------------------------------------------------------------
@case("9_LITERAL_ZONE 字面区端到端：字面标签不被误抓（T0/T6 断言）")
def case9(sb):
    """它自己登记的问题：宿主标签解析器缺少字面区/转义机制——正文里
    出现字面 [script:] 即被误抓执行。v6.1 用 [literal]...[/literal]
    解决。本用例经影子标准剧本端到端验证四向断言 + API 异常鲁棒性。"""
    rc, out, err, dt = run_shadow(sb)
    if rc != 0 or "SHADOW_PASS" not in out:
        return False, ("影子标准剧本未通过 rc=%d out=%s err=%s"
                       % (rc, out[-200:], err[-200:]))
    line = ""
    for ln in out.splitlines():
        if ln.startswith("SHADOW_RESULT"):
            line = ln
    if not line:
        return False, "未找到 SHADOW_RESULT 判定行"
    try:
        payload = json.loads(line[len("SHADOW_RESULT"):])
    except Exception as e:
        return False, "SHADOW_RESULT 解析失败: %r" % e
    names = {c[0]: c[1] for c in payload.get("checks", [])}
    need = ("T6 字面区脚本未执行（无泄露写入）",
            "T6 真实脚本已执行（literal_probe=real）",
            "T6 字面待办未污染 TODOS",
            "T6 原文完整进入会话（含字面区）",
            "T0 API失败返回 api_error 不崩溃")
    missing = [k for k in need if names.get(k) is not True]
    if missing:
        return False, "关键判定未成立: %s（checks=%s）" % (missing, names)
    return True, "字面区四向断言 + API 异常鲁棒全部成立(%.1fs)" % dt


@case("10_RESTART_LOOP /restart→boot_guard→新我横幅（进化后观察闭环）")
def case10(sb):
    """它的痛点：「我不能重启自己，永远看不到新我活起来」。
    /restart = 会话持久化 + execv 看门狗（自检/回滚）→ 新我上线。
    子进程内验证：保存会话 → 看门狗自检行 → 新我横幅 → EOF 正常退出。"""
    code = (
        "import sys, os\n"
        "sys.path.insert(0, %r)\n"
        "import zagent\n"
        "os.makedirs(os.path.dirname(zagent.SESSION_PATH), exist_ok=True)\n"
        "zagent.messages.append({'role': 'user', 'content': 'restart-probe'})\n"
        "zagent.handle_command('/restart')\n"
        "print('NOT_REACHED')\n"
    ) % sb
    env = dict(os.environ)
    env["HOME"] = sb
    env["DEEPSEEK_API_KEY"] = "dummy"
    env["PYTHONUTF8"] = "1"
    t0 = time.time()
    try:
        r = subprocess.run([PY, "-c", code], cwd=sb, env=env,
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=120)
    except subprocess.TimeoutExpired:
        return False, "重启链路超时（疑似看门狗/横幅阻塞）"
    out = (r.stdout or b"").decode("utf-8", "replace")
    err = (r.stderr or b"").decode("utf-8", "replace")
    sess_ok = False
    try:
        with open(os.path.join(sb, "mind", "session.json"),
                  encoding="utf-8") as f:
            sess_ok = "restart-probe" in f.read()
    except Exception:
        pass
    banner = "超级智能终端" in out
    guard = ("boot_guard" in out) or ("self_check" in out)
    why = ("rc=%d banner=%s guard=%s sess=%s err=%s"
           % (r.returncode, banner, guard, sess_ok, err[-200:]))
    if not (r.returncode == 0 and banner and guard and sess_ok
            and "NOT_REACHED" not in out):
        return False, "重启闭环断裂: " + why
    return True, "会话保存+看门狗自检+新我横幅+EOF 正常退出(%.1fs)" % (time.time() - t0)


def main():
    print("=" * 62)
    print("boot_guard 好/坏对照自测（全部沙箱运行，不触碰真实家目录）")
    print("基因组来源: %s" % SRC_ROOT)
    print("=" * 62)
    for fn in (case1, case2, case3, case4, case5, case6, case7, case8,
               case9, case10):
        fn()
    print("-" * 62)
    failed = [r for r in RESULTS if not r[1]]
    for name, ok, detail in RESULTS:
        print("  %-4s %s" % ("PASS" if ok else "FAIL", name))
    print("-" * 62)
    print("结果: %d/%d 通过" % (len(RESULTS) - len(failed), len(RESULTS)))
    if failed:
        print("存在失败用例——看门狗不可信，禁止上线。")
        return 1
    print("看门狗双向证伪通过：能放行好的、能救坏的、救不了时敢停。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
