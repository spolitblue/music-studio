"""减法合成器 —— 把一个音符渲染成一段立体声音频。

信号链（经典减法合成，顺序很重要）：

    振荡器(unison 堆叠, 带限波表)
        → 频率滑音 / 音高包络      ← 决定"起音有没有咬合感"
        → 幅度包络 ADSR            ← 先做幅度，再做滤波，符合模拟电路的实际顺序
        → 谐振低通 + 滤波包络      ← 决定音色的明暗与"哇"的走向
        → unison 立体声展开
        → 力度

设计取舍
--------
* **返回 (2, n) 立体声**：unison 展开和声像在音符内部就要定好，不能留给总线。
* **滤波分两条路径**：截止频率恒定时走 FFT 卷积（快几十倍），有包络扫频时才走
  逐样本 SVF。默认 ``env_amount = 0`` 走快路径，所以大多数音色渲染是毫秒级。
* **相位连续性**：同一音符的滑音用相位累加，不重置相位，避免滑音处的咔嗒声。
"""

from __future__ import annotations

import numpy as np

from app.audio import dsp
from app.audio.project import SynthParams

SR = dsp.SR


def _freq_curve(
    n: int,
    sr: int,
    target_hz: float,
    from_hz: float | None,
    glide: float,
    pitch_env_semis: float,
    pitch_env_time: float,
) -> np.ndarray:
    """逐样本瞬时频率 = 滑音 + 音高包络。

    两者都作用在"音分域"再转频率，才符合听感（频率的对数感知）。
    """
    base = np.full(n, float(target_hz), dtype=np.float64)

    if from_hz and glide > 1e-4 and abs(from_hz - target_hz) > 1e-6:
        g = min(1.0, glide * sr)
        k = int(g)
        if k > 1:
            # 指数滑音：音分域线性 ⇒ 频率域指数，听感上匀速
            ramp = np.linspace(0.0, 1.0, k)
            base[:k] = from_hz * (target_hz / from_hz) ** ramp

    if abs(pitch_env_semis) > 1e-4 and pitch_env_time > 1e-5:
        t = np.arange(n, dtype=np.float64) / sr
        decay = np.exp(-t / max(1e-4, pitch_env_time / 3.0))
        base = base * (2.0 ** (pitch_env_semis * decay / 12.0))

    return base


def render_synth_note(
    sp: SynthParams,
    pitch: int,
    velocity: float = 0.85,
    duration: float = 0.5,
    sr: int = SR,
    glide_from: int | None = None,
) -> np.ndarray:
    """渲染一个音符，返回 ``(2, n)`` 的立体声 buffer（含 release 尾巴）。"""
    amp = sp.amp
    n = int(max(1, round((duration + amp.release + 0.04) * sr)))
    if n < 2:
        return np.zeros((2, 2), dtype=np.float32)

    vy = float(np.clip(velocity, 0.0, 1.0))
    pitches = sp.detuned_pitches(pitch)
    voices = max(1, len(pitches))

    # ---- 幅度包络 ------------------------------------------------------
    env = dsp.adsr(n, sr, amp.attack, amp.decay, amp.sustain, amp.release, amp.curve)

    if sp.wave == "noise":
        # 噪声没有音高：直接生成噪声再走滤波，包络照旧
        rng = np.random.default_rng(0xC0FFEE)
        mono = rng.standard_normal(n).astype(np.float32)
        mono *= env
        left = right = mono
    else:
        # ---- 逐 unison 渲染 ------------------------------------------
        left = np.zeros(n, dtype=np.float32)
        right = np.zeros(n, dtype=np.float32)
        spread = float(np.clip(sp.spread, 0.0, 1.0))

        for i, p in enumerate(pitches):
            target = dsp.midi_to_hz(p)
            from_hz = dsp.midi_to_hz(glide_from + sp.octave * 12 + sp.semitone) if glide_from is not None else None
            freq = _freq_curve(
                n, sr, target, from_hz, sp.glide, sp.pitch_env_amount, sp.pitch_env_time
            )
            v = dsp.render_osc(sp.wave, freq, sr) * env

            # unison 在立体声场里对称铺开；单振荡器时居中
            if voices == 1:
                gain_l = gain_r = 1.0
            else:
                offset = (i / (voices - 1)) * 2.0 - 1.0     # -1..1
                pan = offset * spread
                gain_l, gain_r = dsp.equal_power_pan(pan)
            left += v * gain_l
            right += v * gain_r

        norm = 1.0 / max(1.0, voices ** 0.6)   # unison 堆叠会加总，压回来
        left *= norm
        right *= norm

    # ---- 滤波 ----------------------------------------------------------
    flt = sp.filter
    lo_hz, hi_hz = flt.cutoff_range(pitch)
    q = max(0.5, float(flt.resonance))

    if flt.env_amount > 1e-4 and abs(lo_hz - hi_hz) > 1.0:
        fenv = dsp.adsr(
            n, sr, flt.env.attack, flt.env.decay, flt.env.sustain, flt.env.release, flt.env.curve
        )
        cutoff = lo_hz + (hi_hz - lo_hz) * fenv
        left = dsp.resonant_lowpass(left, cutoff, q, sr)
        right = dsp.resonant_lowpass(right, cutoff, q, sr)
    else:
        left = dsp.resonant_lowpass(left, hi_hz, q, sr)
        right = dsp.resonant_lowpass(right, hi_hz, q, sr)

    # ---- 收尾 ----------------------------------------------------------
    out = np.stack([left, right]).astype(np.float32) * (vy * sp.volume)
    # 尾部淡出，杜绝 buffer 边界爆音
    return dsp.fade(out, fade_in=0, fade_out=min(96, n // 8))


__all__ = ["render_synth_note", "SR"]
