#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""zcore.shadow_test —— 影子沙箱编排器（父进程端）

职责：为一次影子验证搭建一次性沙箱（被测 zagent.py + live zcore），
在隔离子进程中运行 zcore/shadow_child.py 驱动剧本，回收判定。

这是 Z-Agent 自评第 4 条「致命缺失：改完就知道对不对」的落地：
  - 进化闸门用它（zagent.shadow_gate）：候选基因落盘前必须先在
    影子沙箱里完整活过一轮真实会话——挡「站错型」逻辑错误；
  - boot_guard --deep 用它：深度体检（静态三层 + 影子运行时）；
  - 对照自测用它：证明「静态全绿但运行即崩」的基因组必被拦下。

调用方式（三处入口，语义一致）：
  python zcore/shadow_test.py [--genome ROOT]
      —— 影子体检 ROOT（默认：本文件所在项目的根）
  python zcore/shadow_test.py --candidate 候选.py --live-root ROOT
      —— 用 ROOT 的 zcore + 候选主文件组沙箱（进化闸门走这条）
  可选：--scenario standard|gate  --json  --timeout 秒

输出：末行 SHADOW_PASS/SHADOW_FAIL + 失败项摘要；退出码 0/1。
纯标准库、无网络、不触碰真实家目录（沙箱即弃，KEEP=1 可保留排障）。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

PY = sys.executable
SELF_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROOT = os.path.dirname(SELF_DIR)
CHILD = os.path.join(SELF_DIR, "shadow_child.py")
DEFAULT_TIMEOUT = 300


def build_sandbox(live_root, candidate=None):
    """一次性基因组沙箱：live zcore + 被测 zagent.py。返回沙箱路径。"""
    sandbox = tempfile.mkdtemp(prefix="zshadow_")
    _zg = os.path.join(live_root, "zguard")
    if os.path.isdir(_zg):
        os.makedirs(os.path.join(sandbox, "zguard"), exist_ok=True)
        for _n in sorted(os.listdir(_zg)):
            _f = os.path.join(_zg, _n)
            if os.path.isfile(_f) and _n.endswith(".py"):
                shutil.copy2(_f, os.path.join(sandbox, "zguard", _n))
    src_zcore = os.path.join(live_root, "zcore")
    dst_zcore = os.path.join(sandbox, "zcore")
    if not os.path.isdir(src_zcore):
        shutil.rmtree(sandbox, ignore_errors=True)
        raise RuntimeError("live zcore 不存在: %s" % src_zcore)
    shutil.copytree(src_zcore, dst_zcore,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    src_main = candidate or os.path.join(live_root, "zagent.py")
    with open(src_main, "r", encoding="utf-8", errors="strict") as f:
        text = f.read()                 # 读 strict：源码必须是合法 UTF-8
    with open(os.path.join(sandbox, "zagent.py"), "w", encoding="utf-8") as f:
        f.write(text)
    return sandbox


def run_shadow(live_root, candidate=None, scenario="standard",
               timeout=DEFAULT_TIMEOUT):
    """返回 (ok, payload, detail_line)。payload 为影子子进程的 JSON 判定。"""
    sandbox = None
    try:
        sandbox = build_sandbox(live_root, candidate)
        env = dict(os.environ)
        env.pop("DEEPSEEK_API_KEY", None)       # 影子会话不允许带真 Key
        env["RAIN_AUTO_NOTIFY"] = "0"
        try:
            r = subprocess.run(
                [PY, CHILD, sandbox, scenario],
                cwd=sandbox, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=timeout,
            )
            out = (r.stdout or b"").decode("utf-8", "replace")
            err = (r.stderr or b"").decode("utf-8", "replace")
            rc = r.returncode
        except subprocess.TimeoutExpired:
            return False, None, "影子沙箱超时（>%ds）——候选疑似死循环/阻塞" % timeout
        payload = None
        for line in out.splitlines():
            if line.startswith("SHADOW_RESULT "):
                try:
                    payload = json.loads(line[len("SHADOW_RESULT "):])
                except Exception:
                    payload = None
        if payload is None:
            tail = (out + "\n" + err).strip()[-600:]
            return False, None, ("影子子进程未产出判定（rc=%d）: %s"
                                 % (rc, tail))
        ok = bool(payload.get("ok")) and rc == 0
        fails = [c for c in payload.get("checks", []) if not c[1]]
        n_err = len(payload.get("errors", []))
        detail = "%d 项断言，%d 失败，%d 崩溃" % (
            len(payload.get("checks", [])), len(fails), n_err)
        if fails:
            detail += "；首批失败: " + "；".join(
                "%s(%s)" % (c[0], c[2]) for c in fails[:3])
        if n_err:
            detail += "；崩溃尾部: " + payload["errors"][-1][-200:]
        return ok, payload, detail
    finally:
        if sandbox and os.environ.get("ZAGENT_SHADOW_KEEP") != "1":
            shutil.rmtree(sandbox, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="Z-Agent 影子沙箱编排器")
    ap.add_argument("--genome", default=None,
                    help="被测基因组根目录（默认：项目根）")
    ap.add_argument("--candidate", default=None,
                    help="候选主文件（进化闸门用；配合 --live-root）")
    ap.add_argument("--live-root", default=None,
                    help="提供 zcore 依赖的 live 根目录")
    ap.add_argument("--scenario", default="standard",
                    choices=["standard", "gate"])
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    ap.add_argument("--json", action="store_true",
                    help="输出完整 JSON 判定")
    args = ap.parse_args()

    live_root = os.path.abspath(args.live_root or args.genome or DEFAULT_ROOT)
    if args.candidate and not args.live_root:
        ap.error("--candidate 必须与 --live-root 同时使用")
    if not os.path.isfile(os.path.join(live_root, "zagent.py")):
        print("SHADOW_FAIL live 根目录缺少 zagent.py: %s" % live_root)
        return 1
    try:
        ok, payload, detail = run_shadow(
            live_root, args.candidate, args.scenario, args.timeout)
    except Exception as e:
        print("SHADOW_FAIL 沙箱构建失败: %r" % e)
        return 1
    if payload is not None:
        # 单行回显机器判定（供进化闸门与对照自测解析）
        print("SHADOW_RESULT " + json.dumps(payload, ensure_ascii=False))
    if args.json and payload is not None:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    print(("SHADOW_PASS " if ok else "SHADOW_FAIL ")
          + "(%s @ %s) " % (args.scenario, live_root) + detail)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
