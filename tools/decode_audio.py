"""命令行封装：把任意音频（mp3 / m4a / flac / wav …）解码成工程能用的 WAV。

真正的解码在 :mod:`app.audio.audiodecode`（界面上的「参考音频」走同一条路），
这里只做"读进来 → 写出去"的文件操作，方便在命令行批量转换。

用法::

    .venv\\Scripts\\python tools\\decode_audio.py 输入.mp3 输出.wav
    .venv\\Scripts\\python tools\\decode_audio.py 输入.mp3 输出.wav --sr 22050 --mono

输出统一为 **32 位浮点 WAV**（不量化、不加抖动）：解码结果只是中间产物，
再量化一次只会在后续分析（起音/质心）里多一份噪声。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.audio import audiodecode, wavio          # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把音频解码成工程可用的 WAV")
    ap.add_argument("source", help="输入音频（mp3/wav/m4a/flac…）")
    ap.add_argument("dest", help="输出 WAV 路径")
    ap.add_argument("--sr", type=int, default=44100, help="目标采样率（默认 44100）")
    ap.add_argument("--mono", action="store_true", help="强制单声道")
    args = ap.parse_args(argv)

    audio, sr = audiodecode.decode(args.source, args.sr,
                                   channels=1 if args.mono else 2)
    out = wavio.save_wav(args.dest, audio, sr, bit_depth=32)
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    print(f"OK {out}")
    print(f"   采样率 {sr} Hz · 声道 {audio.shape[0]} · "
          f"时长 {audio.shape[1] / sr:.2f}s · 峰值 {peak:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
