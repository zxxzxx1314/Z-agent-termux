#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""zcore.boot_guard v2 —— 启动看门狗（自检 + 快照 + 失败自动回滚）

解决的两个根本问题（来自自我评估第 1、2 条）：
  1. 盲改无法自测的系统：「编译通过 + 局部导入」证明不了「下次启动能跑起来」，
     看门狗在每次启动前做三层自检，把「能不能启动」变成启动时的客观判定。
  2. 自我手术没有救援：若某次进化改坏了启动路径，Agent 自己起不来、
     备份是死的。看门狗是外置守护者，主文件怎么坏它都不受牵连，
     能自动回滚到上一个验证过的全基因组快照。

设计原则（经验教训代码化）：
  - 守护者必须外置：本文件只用标准库，绝不 import zagent / zcore，
    主体坏到什么程度都不影响救援能力。
  - 全基因组视角：自检/快照/回滚的对象是 zagent.py + zcore/*.py 的整体，
    任何一块坏了都能整体恢复（单文件快照救不了 zcore 被改坏的场景）。
  - 三层自检：AST 语法 → 关键符号存在 → 子进程隔离导入 + 关键函数冒烟。
  - 自检必须双向证伪：--check 探针对好文件 PASS / 坏文件 FAIL，
    证明它在真判断，不是恒真。
  - 快照自带清单 + sha256：回滚前先验证快照本身完好，绝不拿坏快照覆盖。

启动流程（启动器应指向本文件，而不是直接指向 zagent.py）：
  python zcore/boot_guard.py
    → 自检通过   ：快照全基因组到 last_good/，放行 execv 启动 zagent.py
    → 自检失败   ：验证 last_good 快照 → 完好则整体回滚 → 重新自检 → 放行
    → 快照也坏 / 无快照：FATAL 终止，绝不运行一个坏掉的躯体（退回人工救援）

诚实覆盖边界：
  能挡：语法错误、关键函数缺失、导入即崩（含 zcore 依赖坏）。
  挡不住：导入正常但运行到特定路径才崩的逻辑错误——那需要运行时
  看门狗（P3 verify 子系统方向），本文件不冒充万能自救。

环境开关：
  ZAGENT_HOME   指定宿主根目录（默认 = zcore 的上一级，部署时即 ~）
  ZAGENT_LAUNCH 自检通过后是否真的 execv 启动主体（"0" = 只走到放行，
                供自测与人工检查使用，默认启动）
  --check       只自检不快照不启动，退出码 0=PASS / 1=FAIL（双向证伪探针）

退出码：0 正常放行/探针通过；1 探针 FAIL；
       2 FATAL：快照存在但也坏；3 FATAL：无快照可回滚。
"""
import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

PY = sys.executable
CHECK_TIMEOUT = 40          # 子进程冒烟超时（秒）
DEEP_TIMEOUT = 300          # --deep 影子沙箱超时（秒）
KEEP_ARCHIVES = 10          # 时间戳归档保留份数

# 关键符号分两层（P1 抽离后 unwrap_code/extract_version 由 zcore.util 提供，
# 不再定义于主文件——检查层若不区分会误杀好文件，自测用例 6 已证明）：
#   REQUIRED_MAIN  ：必须「定义」在 zagent.py 内的结构骨架（AST 检查）
#   REQUIRED_SMOKE ：全基因组组合后必须「可用」的符号（导入冒烟 hasattr）
# 它们是「这个躯体还是 Z-Agent」的最低判据，不是全部健康判据。
REQUIRED_MAIN = (
    "main", "call_api", "apply_evolution", "verify_contract_once",
    "is_trivial_check", "extract_scripts", "build_system_prompt",
)
REQUIRED_SMOKE = REQUIRED_MAIN + ("unwrap_code", "extract_version")

# 看门狗自身、其自测与影子沙箱设施都不属于「被守护的基因组」：
# 救援者不能是病人——这些文件坏了不该触发基因回滚，也不阻塞启动。
_INFRA_FILES = {"boot_guard.py", "boot_guard_selftest.py",
                "shadow_test.py", "shadow_child.py"}


def root():
    """宿主根目录：ZAGENT_HOME 优先，否则取 zcore 的上一级（部署时 = ~）。"""
    env = os.environ.get("ZAGENT_HOME", "").strip()
    if env:
        return os.path.abspath(env)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _p(rel):
    return os.path.join(root(), rel)


LOG_PATH = lambda: _p("work/boot_guard.log")          # noqa: E731
SNAP_DIR = lambda: _p("work/boot_guard/last_good")    # noqa: E731
ARCHIVE_DIR = lambda: _p("work/boot_guard/archive")   # noqa: E731


def _log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        os.makedirs(os.path.dirname(LOG_PATH()), exist_ok=True)
        with open(LOG_PATH(), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        print("[boot_guard] " + line, flush=True)
    except Exception:
        pass


# ------------------------------------------------------------
# 基因组清单：zagent.py + zcore/*.py（剔除看门狗自身与缓存）
# ------------------------------------------------------------
def genome_files(rt=None):
    rt = rt or root()
    files = []
    main_py = os.path.join(rt, "zagent.py")
    if os.path.exists(main_py):
        files.append(("zagent.py", main_py))
    zc = os.path.join(rt, "zcore")
    if os.path.isdir(zc):
        for name in sorted(os.listdir(zc)):
            if not name.endswith(".py"):
                continue
            if name in _INFRA_FILES or name.startswith("last_good"):
                continue
            files.append(("zcore/" + name, os.path.join(zc, name)))
    return files


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------
# 第 1 层：AST 语法 + 关键符号；第 3 层：子进程隔离导入冒烟
# ------------------------------------------------------------
def self_check(main_path, rt=None):
    """对单个主文件做完整自检（连带 zcore 导入）。返回 (ok, msg)。"""
    rt = rt or root()
    if not main_path or not os.path.exists(main_path):
        return False, "not_found"
    try:
        src = open(main_path, encoding="utf-8").read()
    except Exception as e:
        return False, "read_fail:" + repr(e)

    # --- 第 1 层：语法 ---
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return False, "syntax:L%s:%s" % (e.lineno, e.msg)

    # --- 第 2 层：关键符号存在 ---
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    missing = [n for n in REQUIRED_MAIN if n not in names]
    if missing:
        return False, "missing_func:" + ",".join(missing)

    # --- 第 3 层：子进程隔离导入 + 关键函数冒烟（不触发 main）---
    smoke = (
        "import importlib.util, sys, types\n"
        "try:\n"
        "    import requests\n"
        "except Exception:\n"
        "    sys.modules['requests'] = types.ModuleType('requests')\n"
        "sys.path.insert(0, %(root)r)\n"
        "spec = importlib.util.spec_from_file_location('zag_smoke', %(main)r)\n"
        "m = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(m)\n"          # __name__ != __main__，main() 不会执行
        "for _n in %(req)r:\n"
        "    assert hasattr(m, _n), 'missing:' + _n\n"
        "assert m.unwrap_code('print(1)') == 'print(1)', 'unwrap 误伤代码'\n"
        "_bt = chr(96)*3\n"
        "assert m.unwrap_code(_bt + 'python\\nimport os\\n' + _bt) == 'import os', '围栏剥离失效'\n"
        "assert m.unwrap_code('python\\nimport os\\n') == 'python\\nimport os', '非围栏被误剥'\n"
        "assert m.extract_version('VERSION = 9.9') == 9.9, '版本提取失效'\n"
        "_bad, _ = m.is_trivial_check(\"print('x')\")\n"
        "assert _bad, 'trivial gate 失效'\n"
        "_cl = m.parse_accept_blocks('[accept:]\\n产物: /tmp/x\\n"
        "检查: open(\"/tmp/x\")\\n标准: s\\n[/accept]')\n"
        "assert _cl and _cl[0]['artifacts'] == ['/tmp/x'], '合同解析失效'\n"
        "_mk, _zs, _uc = m.mask_literal('a[literal]x[script:]y[/script][/literal]b')\n"
        "assert '[script:]' not in _mk and '[literal]' not in _mk, '字面区屏蔽失效'\n"
        "assert len(_mk) == 41 and _zs[0][2] == 'x[script:]y[/script]', '等长/内容失配'\n"
        "assert _uc < 0, '误报未闭合'\n"
        "print('SMOKE_OK')\n"
    ) % {"root": rt, "main": main_path, "req": list(REQUIRED_SMOKE)}
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        r = subprocess.run(
            [PY, "-c", smoke], env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=CHECK_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, "import_timeout(%ds)" % CHECK_TIMEOUT
    except Exception as e:
        return False, "import_exc:" + repr(e)
    if r.returncode != 0:
        err = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        tail = err[-1] if err else "no_stderr"
        return False, "import_fail:" + tail[:200]
    if b"SMOKE_OK" not in (r.stdout or b""):
        return False, "smoke_no_marker"
    return True, "ok"


def genome_check(rt=None):
    """全基因组自检：每个文件可读 + 语法过 + 主文件导入冒烟连带 zcore。"""
    rt = rt or root()
    files = genome_files(rt)
    if not files:
        return False, "genome_empty(缺 zagent.py)"
    for rel, path in files:
        if not os.path.exists(path):
            return False, "missing:" + rel
        try:
            ast.parse(open(path, encoding="utf-8").read())
        except SyntaxError as e:
            return False, "syntax:%s:L%s:%s" % (rel, e.lineno, e.msg)
        except Exception as e:
            return False, "read_fail:%s:%s" % (rel, repr(e))
    return self_check(os.path.join(rt, "zagent.py"), rt)


# ------------------------------------------------------------
# 快照：清单 + sha256；校验；安装（回滚）
# ------------------------------------------------------------
def snapshot_genome():
    """把当前基因组快照到 last_good/（先写临时目录再原子换名），
    同时留时间戳归档。任一失败只告警，不阻断启动。"""
    files = genome_files()
    if not files:
        _log("snapshot_fail: genome_empty")
        return False
    tmp = SNAP_DIR() + ".tmp"
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        manifest = {"created": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "files": []}
        for rel, path in files:
            dst = os.path.join(tmp, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(path, dst)
            manifest["files"].append({"path": rel, "sha256": _sha256(path)})
        with open(os.path.join(tmp, "manifest.json"), "w",
                  encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1)
        if os.path.exists(SNAP_DIR()):
            shutil.rmtree(SNAP_DIR())
        os.makedirs(os.path.dirname(SNAP_DIR()), exist_ok=True)
        os.rename(tmp, SNAP_DIR())
    except Exception as e:
        _log("snapshot_fail " + repr(e))
        shutil.rmtree(tmp, ignore_errors=True)
        return False
    # 时间戳归档（保留最近 KEEP_ARCHIVES 份）
    try:
        os.makedirs(ARCHIVE_DIR(), exist_ok=True)
        arc = os.path.join(
            ARCHIVE_DIR(), "genome_%s" % time.strftime("%Y%m%d_%H%M%S"))
        shutil.copytree(SNAP_DIR(), arc)
        olds = sorted(d for d in os.listdir(ARCHIVE_DIR())
                      if d.startswith("genome_"))
        for d in olds[:-KEEP_ARCHIVES]:
            shutil.rmtree(os.path.join(ARCHIVE_DIR(), d), ignore_errors=True)
    except Exception as e:
        _log("archive_fail(不影响救援) " + repr(e))
    _log("snapshot_ok (%d files)" % len(files))
    return True


def snapshot_valid():
    """验证 last_good 快照：清单可读、哈希全对、且快照整体能通过自检。
    在临时目录里组装验证，绝不先污染真实文件。"""
    snap = SNAP_DIR()
    mf = os.path.join(snap, "manifest.json")
    if not os.path.exists(mf):
        return False, "no_manifest"
    try:
        manifest = json.load(open(mf, encoding="utf-8"))
    except Exception as e:
        return False, "manifest_corrupt:" + repr(e)
    tmp = SNAP_DIR() + ".verify"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        for item in manifest.get("files", []):
            src = os.path.join(snap, item["path"].replace("/", os.sep))
            if not os.path.exists(src):
                return False, "snap_missing:" + item["path"]
            if _sha256(src) != item["sha256"]:
                return False, "snap_hash_mismatch:" + item["path"]
            dst = os.path.join(tmp, item["path"].replace("/", os.sep))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
        ok, msg = genome_check(tmp)
        if not ok:
            return False, "snap_self_check_fail:" + msg
        return True, tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _atomic_install(rel, src):
    dst = _p(rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".boot_tmp"
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def rollback():
    """把验证过的快照整体装回宿主目录。返回 (ok, msg)。"""
    ok, info = snapshot_valid()
    if not ok:
        _log("rollback_abort: " + info)
        return False, info
    snap = SNAP_DIR()
    try:
        manifest = json.load(
            open(os.path.join(snap, "manifest.json"), encoding="utf-8"))
        for item in manifest["files"]:
            _atomic_install(item["path"],
                            os.path.join(snap, item["path"].replace("/", os.sep)))
    except Exception as e:
        _log("rollback_fail " + repr(e))
        return False, repr(e)
    ok2, msg2 = genome_check()
    if not ok2:
        _log("rollback_verify_fail " + msg2)
        return False, msg2
    _log("rollback_ok (全基因组已恢复并通过自检)")
    return True, "ok"


# ------------------------------------------------------------
# 启动编排
# ------------------------------------------------------------
def launch():
    if os.environ.get("ZAGENT_LAUNCH", "") == "0":
        _log("launch_skipped (ZAGENT_LAUNCH=0)")
        print("LAUNCH_SKIPPED", flush=True)
        return
    main_py = _p("zagent.py")
    _log("launch " + main_py)
    os.chdir(root())
    os.execv(PY, [PY, main_py])


def run():
    ok, msg = genome_check()
    _log("self_check -> %s (%s)" % (ok, msg))
    if ok:
        snapshot_genome()
        launch()
        return 0
    _log("BROKEN_GENOME detected: " + msg)
    if os.path.exists(SNAP_DIR()):
        rok, rmsg = rollback()
        if rok:
            _log("rescue_complete，放行重启")
            launch()
            return 0
        _log("FATAL: 快照损坏，无法救援 (%s)；绝不运行坏躯体，退出待人工。" % rmsg)
        return 2
    _log("FATAL: 无快照可回滚；绝不运行坏躯体，退出待人工。")
    return 3


def deep_shadow():
    """--deep 深度体检：在静态三层之上叠加影子沙箱运行时验证。
    假 LLM + 隔离 HOME 跑一轮真实会话剧本，专拦「站错型」缺陷
    （静态全绿、运行即崩）。返回 (ok, 摘要)。"""
    shadow_py = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "shadow_test.py")
    if not os.path.exists(shadow_py):
        return False, "影子沙箱缺失（zguard/shadow_test.py）"
    try:
        r = subprocess.run(
            [PY, shadow_py], cwd=root(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=DEEP_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, "影子体检超时（>%ds）" % DEEP_TIMEOUT
    out = (r.stdout or b"").decode("utf-8", "replace")
    line = ""
    for ln in out.splitlines():
        if ln.startswith(("SHADOW_PASS", "SHADOW_FAIL")):
            line = ln[:300]
    ok = r.returncode == 0 and "SHADOW_PASS" in out
    return ok, line or ("rc=%d" % r.returncode)


def main():
    if "--check" in sys.argv:
        ok, msg = genome_check()
        print(("PASS" if ok else "FAIL") + " " + msg, flush=True)
        return 0 if ok else 1
    if "--deep" in sys.argv:
        ok, msg = genome_check()
        _log("deep_check static -> %s (%s)" % (ok, msg))
        if not ok:
            print("DEEP_FAIL static " + msg, flush=True)
            return 1
        sok, smsg = deep_shadow()
        _log("deep_check shadow -> %s (%s)" % (sok, smsg))
        print(("DEEP_PASS " if sok else "DEEP_FAIL ") + smsg, flush=True)
        return 0 if sok else 1
    return run()


if __name__ == "__main__":
    sys.exit(main())
