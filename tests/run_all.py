"""测试总入口：依次跑完全部验证脚本。

用法::

    python tests/run_all.py            # 全部
    python tests/run_all.py dsp motion # 只跑指定项

每项都是独立子进程 —— 因为 UI 与 Qt 的崩溃（段错误/堆损坏）会带走整个进程，
只有隔离进程才能保证"一项崩了，其余项的结果依然可信"。
"""

from __future__ import annotations

import os
import subprocess
import sys as _sys

# 自己把输出重配成 UTF-8，不指望外部环境设 PYTHONIOENCODING。
# 否则在 GBK 控制台下 `python tests/run_all.py > out.txt` 会直接
# UnicodeEncodeError 崩掉（输出里有 ═ 与中文），而 run_tests.bat 恰好设了
# 那个变量，于是表现为「双击能跑、直接跑就崩」—— 最难查的那类环境依赖。
for _stream in (_sys.stdout, _sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
PY = sys.executable

SUITES: list[tuple[str, str, str]] = [
    ("design", "test_design.py", "设计系统 · 图标 · 玻璃绘制 · 平台能力"),
    ("motion", "test_motion.py", "弹簧动效（物理积分 / 中断继承速度 / 生命周期）"),
    ("liquid", "test_liquid.py", "液态玻璃（边缘折射真的吸取轮廓外画面 / 凝霜 / 性能）"),
    ("dsp", "test_dsp.py", "DSP 基元（带限振荡器 / 包络 / 滤波 / 卷积 / 性能）"),
    ("render", "test_audio_render.py", "端到端渲染 · 导出 · 工程往返"),
    ("seq", "test_sequencer.py", "音序器：泳道构建 / 坐标换算 / 增删改 / 小节收缩"),
    ("sequx", "test_sequencer_ux.py", "音序器 10 个易用性细节（试听/撤销/力度/时值/框选/跟随…）"),
    ("glass", "test_glass_neutral.py", "玻璃无色度 · 拖动时高频刷新且不重拍"),
    ("piano", "test_piano_roll.py", "钢琴卷帘：画/删/改音高/改时值/力度/量化"),
    ("drums", "test_drums.py", "鼓机：步进网格 / 鼓件参数 / 一键鼓型"),
    ("mixer", "test_mixer.py", "混音台：推子 / 声像 / 静音独奏 / 发送 / 电平表"),
    ("fx", "test_effects.py", "效果器链：增删排序 / 参数编辑 / 对数刻度 / 试听"),
    ("comp", "test_composer.py", "编曲助手：调式与和弦走向 / 生成 / 音阶内约束"),
    # 这两套先前**没登记在这里**，于是它们的红绿没人看得到：
    # tests/test_ai_studio.py 里 page.engine_badge.text() 一直是 AttributeError，
    # 而 run_all 照样报"全部通过"。测试不登记 = 测试不存在。
    ("ai", "test_ai_studio.py", "AI 扒谱·复刻页：参考音频（wav/mp3…）→ 音符 / 指纹 → 生成"),
    ("transcribe", "test_transcribe.py", "扒谱与复刻：起音检测 / 基频估计 / 鼓件粗分 / 指纹"),
    ("sampling", "test_sampling.py", "采样与母带：从参考抠音色 / 频谱摆平真的改形状"),
    ("integ", "test_integration.py", "跨页面集成：7 页共存 / 契约齐备 / 换工程换引用 / 端到端渲染"),
    ("ui", "test_ui_smoke.py", "界面构造与逐页绘制（offscreen）"),
]


def main(argv: list[str]) -> int:
    wanted = [a for a in argv[1:] if not a.startswith("-")]
    selected = [s for s in SUITES if not wanted or s[0] in wanted]
    if wanted and not selected:
        print(f"未知测试项：{wanted}")
        print("可选：" + ", ".join(s[0] for s in SUITES))
        return 2

    results: list[tuple[str, bool, float, str]] = []
    for key, filename, desc in selected:
        print()
        print("╔" + "═" * 74 + "╗")
        print(f"║ {desc[:72]:<72} ║")
        print("╚" + "═" * 74 + "╝")
        t0 = time.perf_counter()
        # 测试默认静音：试听用例会真把片段送进声卡，而 run_all 的输出要等
        # 该项跑完才打印 —— 表现为「屏幕没动静、声音却在响」。
        # 静音只影响是否真开音频流，缓冲渲染与断言强度完全不变。
        env = dict(os.environ)
        env.setdefault("MUSIC_STUDIO_SILENT_AUDIO", "1")
        # 子进程也要 UTF-8：run_tests.bat 里设了 PYTHONIOENCODING=utf-8，
        # 但直接 `python tests/run_all.py` 时没人设 —— 子进程在 GBK 控制台上
        # 打印「←」这类字符就会 UnicodeEncodeError 崩掉（实测 seq 就是这么挂的）。
        # 父进程自己 reconfigure 管不到子进程，所以必须显式传下去。
        env.setdefault("PYTHONIOENCODING", "utf-8")
        proc = subprocess.run(
            [PY, "-u", str(TESTS / filename)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        dt = time.perf_counter() - t0
        out = (proc.stdout or "")
        err = (proc.stderr or "").strip()
        print(out.rstrip())
        if err:
            print("--- stderr ---")
            print(err[-1500:])
        results.append((key, proc.returncode == 0, dt, err.splitlines()[-1] if err else ""))

    print()
    print("═" * 76)
    print("总结")
    print("═" * 76)
    ok = True
    for key, passed, dt, lasterr in results:
        mark = "通过" if passed else "失败"
        extra = "" if passed else f"   ← {lasterr[:60]}"
        print(f"  [{mark}] {key:<8} {dt:6.2f}s{extra}")
        ok = ok and passed
    print("═" * 76)
    print("全部通过" if ok else "存在失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
