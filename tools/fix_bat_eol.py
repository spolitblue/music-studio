"""把项目里的 .bat / .cmd 统一转成 CRLF 换行。

**为什么需要这个工具**

Windows 批处理解析器**要求 CRLF**。用 LF 换行的 .bat 会被 `cmd.exe` 从任意位置
劈开，一个 851 字节的脚本能变成十几条 "'orlevel' 不是内部或外部命令" 之类的
报错，双击后窗口一闪就没 —— 用户看到的只是"这个文件打不开"。

而大多数现代编辑器/write 工具默认写 LF，所以这个坑很容易悄悄复发。
本工具用作维护命令，同时 :mod:`tests.test_design` 会在每次跑测试时断言所有
批处理文件都是 CRLF，从根上防止回归。

用法::

    python tools/fix_bat_eol.py          # 就地转换
    python tools/fix_bat_eol.py --check  # 只检查，不修改（有问题返回 1）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUFFIXES = {".bat", ".cmd"}


def bat_files() -> list[Path]:
    out = []
    for p in ROOT.rglob("*"):
        if p.is_file() and p.suffix.lower() in SUFFIXES and ".venv" not in p.parts:
            out.append(p)
    return sorted(out)


def is_crlf(path: Path) -> bool:
    raw = path.read_bytes()
    if not raw:
        return True
    # 有 CR 但不配对，或者有 LF 却没有 CR —— 都算不合格
    lone_lf = raw.replace(b"\r\n", b"").count(b"\n")
    return lone_lf == 0


def convert(path: Path) -> bool:
    raw = path.read_bytes()
    fixed = raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    if fixed == raw:
        return False
    path.write_bytes(fixed)
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="批处理文件换行符规范化")
    ap.add_argument("--check", action="store_true", help="只检查，不修改")
    args = ap.parse_args(argv)

    files = bat_files()
    if not files:
        print("没找到 .bat / .cmd 文件")
        return 0

    bad = [p for p in files if not is_crlf(p)]
    if args.check:
        for p in files:
            mark = "OK  " if is_crlf(p) else "LF !"
            print(f"  [{mark}] {p.relative_to(ROOT)}")
        if bad:
            print(f"\n{len(bad)} 个批处理文件不是 CRLF —— cmd.exe 会解析失败。")
            print("修复： python tools/fix_bat_eol.py")
            return 1
        print(f"\n全部 {len(files)} 个批处理文件都是 CRLF。")
        return 0

    changed = 0
    for p in bad:
        if convert(p):
            print(f"  已转成 CRLF: {p.relative_to(ROOT)}")
            changed += 1
    print(f"完成：检查 {len(files)} 个，修改 {changed} 个。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
