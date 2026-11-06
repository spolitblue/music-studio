"""复刻命令行 —— 把一段参考音频变成"同一路数的另一首曲子"。

真正的流程在 :mod:`tools.music_ops`（MCP 服务器与实时控制台共用同一份），
这里只负责命令行参数与打印。

用法::

    .venv\\Scripts\\python tools\\replicate_cli.py 参考.mp3 --out _out\\demo
    .venv\\Scripts\\python tools\\replicate_cli.py 参考.mp3 --out _out\\demo --seeds 3
    .venv\\Scripts\\python tools\\replicate_cli.py 参考.mp3 --target-seconds 205

参考音频可以是 mp3 / m4a / flac（走 PySide6 自带的 QtMultimedia，不需要 ffmpeg）。

想**边跑边看**：先起常驻控制台，再把地址通过环境变量交给这条命令 ——
此时命令行进程会把每一步 POST 到控制台，网页上就能看到进度、波形与试听。

    .venv\\Scripts\\python tools\\music_mcp.py --console-only      # 另一个窗口
    $env:MUSIC_LIVE_URL = "http://127.0.0.1:8770"
    .venv\\Scripts\\python tools\\replicate_cli.py 参考.mp3

产物（写到 ``--out``）::

    replica_<seed>.wav / .mid / .musicproj.json    每个候选一套可继续编辑的工程
    report.txt                                     全部数值（也同步到控制台）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from tools import music_ops                              # noqa: E402
from tools.live_hub import hub                           # noqa: E402


def _parse_parts(text: str | None) -> dict:
    """``B=pling,A=bell`` → ``{"B": "pling", "A": "bell"}``。"""
    out: dict[str, str] = {}
    for item in (text or "").split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise SystemExit(f"--part-lead 要写成 部分=乐器，例如 B=pling（收到「{item}」）")
        part, instrument = item.split("=", 1)
        out[part.strip().upper()] = instrument.strip()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="按参考音频的指纹复刻一首曲子")
    ap.add_argument("reference", help="参考音频（wav / mp3 / m4a / flac）")
    ap.add_argument("--out", default="_out/replicate", help="输出目录")
    ap.add_argument("--bars", type=int, default=None, help="每段小节数（默认按指纹推）")
    ap.add_argument("--bpm", type=float, default=None, help="已知速度就直接给")
    ap.add_argument("--seeds", type=int, default=1, help="生成几个候选（1~8）")
    ap.add_argument("--seed", type=int, default=101, help="起始种子")
    ap.add_argument("--sections", type=int, default=1,
                    help="段落数：>1 时把多段接成一首长曲（每段换种子）")
    ap.add_argument("--target-seconds", type=float, default=None,
                    help="目标时长；给了就按它自动算段落数（上限 10 段）")
    ap.add_argument("--headroom-db", type=float, default=-3.5,
                    help="母带目标峰值（dBFS），默认 -3.5（减弱混音）；"
                         "给 none 则保持软件原样的母带链")
    ap.add_argument("--mix", choices=("gentle", "app"), default="gentle",
                    help="gentle（默认）：从参考采样 + 声部比例 + 频谱摆平的母带；"
                         "app：软件原本那条路（引擎整曲渲染 + 母带限幅）")
    ap.add_argument("--drums", choices=("sampled", "native", "off"), default="sampled",
                    help="鼓：sampled 用从参考抠出来的样本；native 用引擎鼓机")
    ap.add_argument("--lead", choices=("synth", "sampled"), default="synth",
                    help="主音：synth 用减法合成器；sampled 用参考里抠出来的音色")
    ap.add_argument("--kit-dir", default=None, help="复用已有的样本目录（含 kit.json）")
    ap.add_argument("--kit", choices=("reference", "minecraft"), default="reference",
                    help="音色来源：reference 从参考音频抠；minecraft 用本机 Minecraft 音符盒音色")
    ap.add_argument("--mc-lead", default=None,
                    help="音符盒模式下主音用哪件乐器（bell/chime/xylophone/iron_xylophone/"
                         "flute/pling/bit/harp…），默认 bell")
    ap.add_argument("--mc-kit-dir", default=None, help="复用已有的音符盒样本目录")
    ap.add_argument("--form", choices=("linear", "three-part"), default="linear",
                    help="曲式：linear 每段换走向；three-part = 三段式 A–B–A（A 再现）")
    ap.add_argument("--part-lead", default=None,
                    help="三段式里换成别的主音乐器，形如 B=pling（A 用 --mc-lead）")
    ap.add_argument("--no-soften", action="store_true",
                    help="不减弱混音（回到峰值 -2、力度 1.0、踩镲全留）")
    ap.add_argument("--no-console", action="store_true", help="不起/不连实时控制台")
    ap.add_argument("--name", default=None, help="工程名")
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    live = None
    if not args.no_console:
        live = hub(autostart=True)
        live.status("复刻中", running=True, note=str(out))
        print(f"实时控制台：{live.url}", flush=True)

    def say(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)
        if live is not None:
            live.log(text)

    try:
        headroom = None if str(args.headroom_db).lower() in ("none", "off", "no") \
            else float(args.headroom_db)
        t0 = time.perf_counter()
        result = music_ops.run_replicate(
            args.reference, out, live=live, bpm=args.bpm, bars=args.bars,
            sections=args.sections, target_seconds=args.target_seconds,
            seeds=args.seeds, seed=args.seed, headroom_db=headroom,
            name=args.name or "复刻", mix=args.mix, drums=args.drums, lead=args.lead,
            kit_dir=args.kit_dir, kit_source=args.kit, mc_lead=args.mc_lead,
            mc_kit_dir=args.mc_kit_dir, form=args.form,
            mc_lead_by_part=_parse_parts(args.part_lead),
            soften=not args.no_soften)
        say(f"\n用时合计 {time.perf_counter() - t0:.1f}s → {out}")
        for cand in result.candidates:
            say(f"  种子 {cand['seed']}：{cand['notes']} 音符 · "
                f"峰均比 {cand['health']['crest_db']:.1f} dB · {cand['path']}")
        (out / "report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (out / "fingerprint.json").write_text(json.dumps({
            "source": str(args.reference),
            "bpm": result.profile.bpm, "key": result.profile.key_name,
            "density": result.profile.density, "swing": result.profile.swing,
            "energy_db": result.profile.energy_db,
            "brightness_hz": result.profile.brightness_hz,
            "drum_ratio": result.profile.drum_ratio,
            "seconds": result.profile.seconds, "bars": result.profile.bars,
            "candidates": result.candidates,
        }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return 0
    finally:
        if live is not None:
            live.status("完成", running=False, note=str(out))


if __name__ == "__main__":
    raise SystemExit(main())
