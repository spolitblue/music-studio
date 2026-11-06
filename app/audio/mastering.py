"""母带 —— 只做三件事：**把频谱摆平、把电平找准、把峰值看住**。

为什么要单独一个模块
--------------------
"混音过于猛烈、听着难受"这件事，一半来自编曲（音符太满、鼓太硬），
另一半来自母带：本工程的母带限幅器长期在做 30 dB 以上的增益衰减
（实测示范曲 +30.8 dBFS 进、-1 dBFS 出），于是所有作品都被压成
"从头到尾一个劲"，峰均比只剩 ~10 dB（真人编曲的 OST 是 17.6 dB）。

这里给的是另一套做法，三步，每一步都能量：

1. **频谱摆平**：拿参考音频的长时平均谱当目标，把成品按**频段**拉过去
   （只动 ±6 dB，线性相位、分块 overlap-add，不做任何"味道"处理）。
   实测成品在高频段比参考多出好几个 dB —— 那就是"刺"的来源。
2. **电平找准**：按目标峰值整体缩放，让限幅器**只做峰值保险**，
   而不是像现在这样当总线的电平设定。
3. **峰值看住**：最后挂一级前视限幅（复用 ``render.limiter``），只吃最后 1~2 dB。

不做"响度战争"：母带的目标是**让参考与成品在同一个频谱-动态平面上可比**，
不是把 RMS 顶到最高。
"""

from __future__ import annotations

import numpy as np

from app.audio import dsp, render

SR = dsp.SR

#: 频段划分（Hz）。低频段窄、高频段宽 —— 后者才是"刺"待的地方。
BANDS: tuple[tuple[float, float], ...] = (
    (20.0, 100.0), (100.0, 300.0), (300.0, 1000.0), (1000.0, 3000.0),
    (3000.0, 8000.0), (8000.0, 16000.0),
)
BAND_NAMES = ("20-100", "100-300", "300-1k", "1k-3k", "3k-8k", "8k-16k")


# ==========================================================================
# 测量
# ==========================================================================


def _long_term_spectrum(mono: np.ndarray, sr: int = SR, *,
                        block: int = 1 << 15) -> np.ndarray:
    """长时平均幅度谱（Welch：分块 + Hann 窗 + 平均）。"""
    mono = np.asarray(mono, dtype=np.float64).ravel()
    if mono.size < block:
        mono = np.pad(mono, (0, block - mono.size))
    win = np.hanning(block)
    acc = np.zeros(block // 2 + 1, dtype=np.float64)
    count = 0
    step = block // 2
    for start in range(0, mono.size - block + 1, step):
        seg = mono[start:start + block] * win
        acc += np.abs(np.fft.rfft(seg))
        count += 1
    if count == 0:
        acc = np.abs(np.fft.rfft(mono[:block] * win))
        count = 1
    return acc / count


def band_levels(audio, sr: int = SR, *,
                bands: tuple[tuple[float, float], ...] = BANDS) -> list[float]:
    """各频段的电平（dBFS）—— 在**时域**上滤出来再量 RMS。

    为什么不用 FFT 分箱求和（第一版的做法）：箱数随频段宽度线性增长，
    8k-16k 段有 8000 个箱、20-100 段只有 80 个 —— 求和出来的"能量占比"
    实际是**带宽加权**的，不是听感。实测它把参考音频算成"74% 能量在 100Hz 以下"，
    而那显然不成立。时域滤波后量 RMS 没有这个坑，读数还直接是 dBFS，
    可以跟任何地方的响度对上。
    """
    mono = audio.mean(axis=0) if np.ndim(audio) == 2 else np.asarray(audio)
    mono = np.asarray(mono, dtype=np.float32)
    nyq = sr / 2.0
    out = []
    for lo, hi in bands:
        hi = min(hi, nyq - 100.0)
        if hi <= lo * 1.05:
            out.append(-120.0)
            continue
        band = dsp.bandpass(mono, lo, hi, sr)
        out.append(float(dsp.gain_to_db(max(dsp.rms(band), 1e-9))))
    return out


def harshness(audio, sr: int = SR) -> dict:
    """"听着难受"的可量部分：高频电平、谱质心、峰均比、瞬态密度。"""
    stereo = np.asarray(audio, dtype=np.float32)
    if stereo.ndim == 1:
        stereo = np.stack([stereo, stereo])
    mono = stereo.mean(axis=0)
    levels = band_levels(stereo, sr)
    peak = float(np.max(np.abs(mono))) or 1e-12
    rms = float(np.sqrt(np.mean(mono ** 2))) or 1e-12
    spec = _long_term_spectrum(mono, sr)
    freqs = np.fft.rfftfreq((spec.size - 1) * 2, 1.0 / sr)
    centroid = float((spec * freqs).sum() / max(spec.sum(), 1e-20))
    # 瞬态密度：1 秒窗里 8ms 峰均比超过 3 的窗口比例（"一下一下有多密"）
    n = (mono.size // sr) * sr
    transients = 0.0
    if n > sr:
        frames = mono[:n].reshape(-1, sr)
        picks = list(frames[::max(1, frames.shape[0] // 60)])[:60]
        k = max(2, sr // 125)
        acc = []
        for f in picks:
            sub = f[: f.size // k * k].reshape(-1, k)
            p = np.max(np.abs(sub), axis=1)
            r = np.sqrt(np.mean(sub ** 2, axis=1)) + 1e-9
            acc.append(float(np.mean(p / r > 3.0)))
        transients = float(np.mean(acc)) if acc else 0.0
    return {
        "centroid_hz": centroid,
        "band_levels_db": levels,
        "hf_level_db": float(10 * np.log10(10 ** (levels[4] / 10) + 10 ** (levels[5] / 10))),
        "crest_db": float(dsp.gain_to_db(peak / rms)),
        "transient_share": transients,
    }


def balance_table(ref_levels: list[float], mix_levels: list[float]) -> str:
    rows = ["频段       参考       成品         差"]
    for name, a, b in zip(BAND_NAMES, ref_levels, mix_levels):
        rows.append(f"{name:<9}{a:7.1f} dBFS{b:7.1f} dBFS{b - a:+7.1f} dB")
    return "\n".join(rows)


# ==========================================================================
# 频谱摆平
# ==========================================================================


def _band_curve(sr: int, ref_levels: list[float], ref_rms_db: float,
                mix_levels: list[float], mix_rms_db: float,
                max_db: float, points: int = 2048
                ) -> tuple[np.ndarray, np.ndarray, list[float]]:
    """算出"把成品的**频谱形状**拉到参考"的增益曲线。

    关键：比的是**相对**电平（各自减去自己的总 RMS），不是绝对 dBFS。
    第一版拿绝对电平去比 —— 而混音在进母带前是 +14 dBFS 的原始总线，
    参考是 -23 dBFS 的正常成品，于是每一段的差值都是 -35 dB 上下，
    全被钳到 -10 dB，曲线成了一条平线：**摆平器一直在空转**，
    还在事后被"归一化到目标峰值"这一步正好抵消掉（实测开/关完全一样）。
    形状归形状、电平归电平 —— 电平那一半由 target_peak_db 负责。
    """
    ref_rel = [v - ref_rms_db for v in ref_levels]
    mix_rel = [v - mix_rms_db for v in mix_levels]
    want = [float(np.clip(r - m, -max_db, max_db)) for r, m in zip(ref_rel, mix_rel)]
    centers = [float(np.sqrt(lo * hi)) for lo, hi in BANDS]
    freqs = np.geomspace(20.0, min(20000.0, sr / 2.0), points)
    # 两端锚到 0 dB：极低频与极高频不该被"摆平"动作整体抬高/压低
    xs = np.array([10.0] + centers + [sr / 2.0])
    ys = np.array([0.0] + want + [0.0])
    curve = np.interp(np.log(freqs), np.log(xs), ys)
    return freqs, curve, want


def match_spectrum(stereo, sr: int = SR, *, reference=None,
                   max_db: float = 6.0, block: int = 1 << 18
                   ) -> tuple[np.ndarray, dict]:
    """把成品的频段电平拉到参考音频的样子（线性相位、分块 overlap-add）。

    ``reference`` 是参考音频（任意形状）；不给就原样返回。
    """
    data = np.asarray(stereo, dtype=np.float32)
    if data.ndim == 1:
        data = np.stack([data, data])
    if reference is None:
        return data, {"applied": False, "reason": "没有参考"}
    ref = np.asarray(reference, dtype=np.float32)
    ref_mono = ref.mean(axis=0) if ref.ndim == 2 else ref

    mix_levels = band_levels(data, sr)
    ref_levels = band_levels(ref_mono, sr)
    mono = data.mean(axis=0)
    mix_rms_db = float(dsp.gain_to_db(max(dsp.rms(mono), 1e-9)))
    ref_rms_db = float(dsp.gain_to_db(max(dsp.rms(ref_mono), 1e-9)))
    freqs, curve, want = _band_curve(sr, ref_levels, ref_rms_db,
                                     mix_levels, mix_rms_db, max_db)
    gain = 10.0 ** (curve / 20.0)

    bins = np.fft.rfftfreq(block, 1.0 / sr)
    bin_gain = np.interp(bins, freqs, gain).astype(np.float32)

    n = data.shape[1]
    out = np.zeros_like(data)
    win = np.hanning(block).astype(np.float32)
    step = block // 2
    norm = np.zeros(n, dtype=np.float32)
    for start in range(0, max(1, n - block + step), step):
        w = win[: min(block, n - start)]
        norm[start:start + w.size] += (w * w)[: w.size]
    norm = np.maximum(norm, 1e-6)

    for ch in range(data.shape[0]):
        for start in range(0, max(1, n - block + step), step):
            seg = data[ch, start:start + block]
            take = min(block, n - start)
            if seg.size < block:
                seg = np.pad(seg, (0, block - seg.size))
            spec = np.fft.rfft(seg * win)
            fixed = np.fft.irfft(spec * bin_gain, n=block).astype(np.float32) * win
            out[ch, start:start + take] += fixed[:take]      # 尾巴只累加有效长度
    out /= norm
    return out, {"applied": True, "want_db": want,
                 "ref_levels_db": ref_levels, "mix_levels_db": mix_levels,
                 "ref_rms_db": ref_rms_db, "mix_rms_db": mix_rms_db}


# ==========================================================================
# 成品母带
# ==========================================================================


def gentle_master(stereo, sr: int = SR, *, reference=None, max_tilt_db: float = 6.0,
                  tilt_passes: int = 2, target_peak_db: float = -2.0,
                  limiter_db: float = -1.0, fade_in: float = 0.05,
                  fade_out: float = 2.5, before: dict | None = None
                  ) -> tuple[np.ndarray, dict]:
    """三步母带：摆频谱 → 定电平 → 看峰值。返回 (音频, 报告)。

    ``tilt_passes``：曲线是**平滑**的（6 个频段的修正量在频率上线性插值），
    而相邻频段常常要求互相冲突的修正（例如 300-1k 要 -8、1k-3k 要 -9、3k-8k 要 +1），
    一遍下来只能改到七八成。所以量一遍、改一遍、再量一遍补残差 ——
    实测两遍之后残差从 ~4 dB 收敛到 ~1 dB。摆平是线性相位操作，叠两遍没有相位代价。
    """
    data = np.asarray(stereo, dtype=np.float32)
    if data.ndim == 1:
        data = np.stack([data, data])
    report: dict = {"before": before or harshness(data, sr)}
    report["passes"] = []

    # 1) 频谱摆平（可多遍）
    total_want = [0.0] * len(BANDS)
    tilt: dict = {"applied": False}
    for _ in range(max(1, int(tilt_passes))):
        data, tilt = match_spectrum(data, sr, reference=reference, max_db=max_tilt_db)
        if not tilt.get("applied"):
            break
        total_want = [a + b for a, b in zip(total_want, tilt["want_db"])]
        report["passes"].append({"want_db": tilt["want_db"]})
    report["tilt"] = tilt
    report["total_want_db"] = total_want

    # 2) 直流阻断 + 电平找准
    data = np.stack([dsp.biquad_static(data[0], "hp", 18.0, 0.707, sr),
                     dsp.biquad_static(data[1], "hp", 18.0, 0.707, sr)])
    pre_peak = float(np.max(np.abs(data))) or 1e-9
    target = float(dsp.db_to_gain(target_peak_db))
    gain = target / pre_peak
    data = data * gain
    report["pre_limiter_peak_db"] = float(dsp.gain_to_db(pre_peak))
    report["gain_db"] = float(dsp.gain_to_db(gain))

    # 3) 峰值保险（只吃最后 1 dB 上下）
    post_peak = float(np.max(np.abs(data))) or 1e-9
    if dsp.gain_to_db(post_peak) > limiter_db:
        data = render.limiter(data, threshold_db=limiter_db, release=0.12, sr=sr)
    data = dsp.fade(data,
                    fade_in=int(max(1, fade_in * sr)),
                    fade_out=int(max(1, fade_out * sr)))
    data = np.clip(data, -1.0, 1.0).astype(np.float32)
    report["after"] = harshness(data, sr)
    return data, report


__all__ = ["BANDS", "BAND_NAMES", "band_levels", "balance_table", "harshness",
           "match_spectrum", "gentle_master"]
