# -*- coding: utf-8 -*-
"""zcore.util - pure helpers extracted from zagent.py (P1). No module-level state."""
import ast
import os
import re
import signal
import shutil
import tempfile
import subprocess
def atomic_write(path, text):
    """同目录临时文件 + fsync + os.replace 原子替换。
    中途被杀也不会在主文件留下半截内容。"""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".rain-", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(path):
            try:
                os.chmod(tmp, os.stat(path).st_mode & 0o777)
            except Exception:
                pass
        os.replace(tmp, path)          # 原子操作：要么全旧，要么全新
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def unwrap_code(text):
    """只剥离「完整」Markdown 围栏（首行 ``` 或 ```python/```py 且末行 ```），
    绝不删除任何 Python 标识符；非围栏/未闭合围栏一律原样返回。
    v4.3 修复：围栏判定必须是三个反引号——退化版（空字符串）因
    strip() 后末行不可能为空白而恒 False，围栏永远不剥。
    v5.9 修复：反引号一律用 chr(96) 构造。历史事故：反引号在宿主
    转义链路被吞，判定退化成空字符串（本函数曾整个失效）。chr(96)
    从根上免疫该类转义事故，判据不再是可被吞的字面量。"""
    fence = chr(96) * 3
    text = text.strip()
    lines = text.splitlines()
    if len(lines) < 2:
        return text
    first = lines[0].strip()
    if not first.startswith(fence):
        return text
    lang = first[len(fence):].strip().lower()
    if lang not in ("", "python", "py"):
        return text
    if lines[-1].strip() != fence:
        return text                      # 未闭合围栏不剥，防误伤代码
    return "\n".join(lines[1:-1]).strip()


def check_global_positions(source: str):
    """护栏：AST 检查所有 global 声明位于函数体第一条语句。
    返回不合规函数名列表（空 = 通过）。这条护栏正是 v1 踩坑
    经验的代码化——教训写进基因，机器代代执行。"""
    problems = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if not any(isinstance(n, ast.Global) for n in ast.walk(node)):
            continue
        body = list(node.body)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]          # docstring 可以在 global 之前
        if not (body and isinstance(body[0], ast.Global)):
            problems.append(node.name)
    return problems


def extract_version(source: str):
    """从源码顶部提取 VERSION 数字；找不到返回 None。"""
    m = re.search(r'^VERSION\s*=\s*([0-9]+(?:\.[0-9]+)?)', source, re.M)
    return float(m.group(1)) if m else None


# ------------------------------------------------------------
# v6.1 字面区（fenced literal zone）
# 历史事故：宿主对 AI 回复做全文标签扫描，正文里出现 [script:] 等
# 字面形态即被误抓执行 → SyntaxError 频发，AI 只能「不写字面方括号」
# 自我阉割。字面区让区内一切字符免于解析——这是表达能力的归还，
# 不是可选装饰。
# ------------------------------------------------------------
LITERAL_OPEN = "[literal]"
LITERAL_CLOSE = "[/literal]"
_LITERAL_PAIR_RE = re.compile(r"\[literal\](.*?)\[/literal\]", re.S)


def mask_literal(text):
    """字面区屏蔽：[literal]...[/literal] 整段（含标记）等长替换为空格。

    返回 (masked, zones, unclosed_start)：
      - masked：与原文等长的屏蔽文本（字面区为空格），供一切标签解析；
        等长性保证 masked 与原文坐标一一对应，可按区间还原。
      - zones：成对字面区 [(start, end, content)]，区间含标记、content
        为区内原文（不含标记）。
      - unclosed_start：存在未闭合 [literal]（有开无合）时为其起点位置，
        否则 -1。安全侧取舍：从开标记到文本末尾一律按字面处理——
        宁可整段不解析，绝不让区内标签形态被误抓执行。
    嵌套不做特殊处理：非贪婪配到第一个闭合标记。"""
    zones = []

    def _blank(m):
        zones.append((m.start(), m.end(), m.group(1)))
        return " " * len(m.group(0))

    masked = _LITERAL_PAIR_RE.sub(_blank, text)
    unclosed = masked.find(LITERAL_OPEN)
    if unclosed >= 0:
        masked = masked[:unclosed] + " " * (len(text) - unclosed)
    zones.sort(key=lambda z: z[0])
    return masked, zones, unclosed


def _hide_artifact(p):
    """对照组操作：临时移开产物，返回备份路径。"""
    backup = p + ".rain_ctrl_hidden"
    os.rename(p, backup)
    return backup


def _restore_artifact(p, backup):
    """恢复产物（若检查命令在对照期创建了同名垃圾，先清除）。"""
    if os.path.exists(p):
        if os.path.isdir(backup):
            shutil.rmtree(p)
        else:
            os.unlink(p)
    os.rename(backup, p)


def _without_scripts(text, blocks):
    """把脚本块整段替换为占位符（倒序替换保住下标）。
    控制标签只从脚本之外的文本解析。"""
    for start, end, _code, _desc in reversed(blocks):
        text = text[:start] + "\n[脚本已提取]\n" + text[end:]
    return text


def safe_child_env():
    """子进程环境：剥离 API 密钥，强制 UTF-8。"""
    env = os.environ.copy()
    env.pop("DEEPSEEK_API_KEY", None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _kill_process_tree(proc):
    """终止整个进程组（POSIX），子进程拉起的孙进程一并回收。"""
    try:
        if os.name == "posix" and hasattr(os, "killpg"):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:
            pass


def show_exec_result(code, rc, out, err):
    print("\n" + "═" * 50)
    print("【脚本执行区】")
    print("─" * 50)
    print(code)
    print("─" * 50)
    print("退出码: %d" % rc)
    print("标准输出:\n%s" % (out or "(空)"))
    print("标准错误:\n%s" % (err or "(空)"))
    print("═" * 50)


def apply_patch_tag(text, base_dir=None):
    # 解析 patch 标签，返回 (目标路径, 补丁内容) 列表
    import os as _os
    _lb = chr(91)
    _rb = chr(93)
    _open = _lb + 'patch:'
    _close = _lb + '/patch' + _rb
    if base_dir is None:
        base_dir = _os.path.expanduser('~')
    results = []
    idx = 0
    while True:
        s = text.find(_open, idx)
        if s < 0:
            break
        e = text.find(_rb, s)
        if e < 0:
            break
        target = text[s + len(_open):e].strip()
        c = text.find(_close, e)
        if c < 0:
            break
        body = text[e + 1:c]
        if not _os.path.isabs(target):
            target = _os.path.join(base_dir, target)
        results.append((target, body))
        idx = c + len(_close)
    return results


def auto_notify(title, content):
    """任务完成自动通知 + 震动。环境变量 RAIN_AUTO_NOTIFY=0 可禁用。"""
    if os.environ.get("RAIN_AUTO_NOTIFY", "1") == "0":
        return
    try:
        subprocess.run(
            ["termux-notification", "--title", str(title), "--content", str(content), "--sound"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=10,
        )
        subprocess.run(
            ["termux-vibrate", "-d", "200"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=5,
        )
    except Exception:
        pass

