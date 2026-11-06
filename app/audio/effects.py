"""效果器 —— 混响 / 延迟 / 失真 / 滤波 / 合唱 / 降位。

统一的处理范式：**能表达成线性时不变系统的，一律转成冲激响应后用 FFT 卷积**。
混响、延迟、静态滤波都是这类，数学上与逐样本递推完全等价，但快一到两个数量级，
因此可以放心用长尾混响而不拖慢渲染。

非线性的（失真、降位）和时变的（合唱的调制延迟）没法卷积，各用各的办法：
合唱靠"可变延迟读取 + 线性插值"整体向量化，没有 Python 循环。
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np

from app.audio import dsp
from app.audio.project import Effect

SR = dsp.SR


# ==========================================================================
# 混响
# ==========================================================================


@lru_cache(maxsize=32)
def reverb_ir(
    size: float,
    damping: float,
    predelay: float,
    sr: int = SR,
    seed: int = 20260101,
) -> tuple[np.ndarray, np.ndarray]:
    """程序生成一条混响冲激响应（返回左/右两条）。

    做法是把白噪声按频段拆开，**每个频段给不同的衰减时间** —— 这是混响最关键的
    听感特征：真实空间的低频混响尾巴远长于高频。只有单一衰减的噪声尾听上去
    像"沙沙声"而不是"空间"。

    * ``size``     0..1 → 衰减时间 0.25s ~ 3.8s
    * ``damping``  0..1 → 高频相对低频的额外衰减量
    * ``predelay`` 直达声与混响之间的间隔（秒），影响"空间大小"的直觉判断
    """
    size = float(np.clip(size, 0.0, 1.0))
    damping = float(np.clip(damping, 0.0, 1.0))
    predelay = max(0.0, float(predelay))

    decay = 0.25 + size * 3.55
    pre_n = int(predelay * sr)
    length = int(decay * sr) + pre_n + 64
    rng = np.random.default_rng(seed)

    #: (中心频率, 相对带宽) —— 四个频段足够塑造出"频率相关衰减"
    bands = ((110.0, 1.6), (620.0, 2.2), (2600.0, 2.2), (8500.0, 2.6))
    #: 频段越高，damping 造成的额外衰减越强
    damp_weight = (0.0, 0.22, 0.55, 0.85)

    left = np.zeros(length, dtype=np.float64)
    right = np.zeros(length, dtype=np.float64)

    t = np.arange(length, dtype=np.float64) / sr
    for (center, bw), w in zip(bands, damp_weight):
        band_decay = max(0.05, decay * (1.0 - damping * w))
        env = np.exp(-6.907755 * np.maximum(0.0, t - predelay) / band_decay)
        env[:pre_n] = 0.0

        for ch, acc in ((0, left), (1, right)):
            noise = rng.standard_normal(length)
            lo = max(30.0, center / bw)
            hi = min(sr * 0.45, center * bw)
            band = dsp.bandpass(noise.astype(np.float32), lo, hi, sr)
            acc += band * env

    # 早期反射：几个离散抽头 —— 让空间有"墙"而不是一团雾
    er_offsets = (0.011, 0.019, 0.028, 0.041, 0.057)
    er_gains = (0.62, -0.44, 0.36, -0.28, 0.20)
    for off, g in zip(er_offsets, er_gains):
        idx = pre_n + int(off * sr)
        if idx < length:
            left[idx] += g
            right[idx + 3] += g * 0.94

    # 整段做一次低频塑形，去掉噪声尾里的"嘶嘶"
    left = dsp.biquad_static(left.astype(np.float32), "lp", 11000.0, 0.7, sr).astype(np.float64)
    right = dsp.biquad_static(right.astype(np.float32), "lp", 11000.0, 0.7, sr).astype(np.float64)

    # 归一化到统一能量，这样 size 变化不会带来响度跳变
    energy = float(np.sqrt(np.mean(left ** 2) + np.mean(right ** 2)))
    if energy > 1e-9:
        scale = 0.55 / energy
        left *= scale
        right *= scale

    return left.astype(np.float32), right.astype(np.float32)


def apply_reverb(stereo: np.ndarray, params: dict, sr: int = SR) -> np.ndarray:
    """对 (2, n) 施加混响。``mix`` 为 1.0 时表示"全湿"（用于发送总线）。"""
    if stereo.size == 0:
        return stereo
    size = float(params.get("size", 0.6))
    damping = float(params.get("damping", 0.5))
    mix = float(np.clip(params.get("mix", 0.25), 0.0, 1.0))
    predelay = float(params.get("predelay", 0.02))
    width = float(np.clip(params.get("width", 1.0), 0.0, 1.0))
    if mix <= 1e-4:
        return stereo

    ir_l, ir_r = reverb_ir(round(size, 3), round(damping, 3), round(predelay, 3), sr)
    left = dsp.fft_convolve(stereo[0], ir_l)
    right = dsp.fft_convolve(stereo[1], ir_r)

    # 宽度：>0 时把单声道成分挪向两侧
    if width < 0.999:
        mid = (left + right) * 0.5
        left = mid + (left - mid) * width
        right = mid + (right - mid) * width

    return np.stack([
        stereo[0] * (1.0 - mix) + left * mix,
        stereo[1] * (1.0 - mix) + right * mix,
    ]).astype(np.float32)


# ==========================================================================
# 延迟
# ==========================================================================


@lru_cache(maxsize=64)
def delay_ir(
    delay_samples: int,
    feedback: float,
    tone: float,
    ping_pong: bool,
    sr: int = SR,
    max_seconds: float = 6.0,
) -> tuple[np.ndarray, np.ndarray]:
    """生成延迟的冲激响应（左右各一条）。

    每一轮回声都比上一轮更暗 —— 真实延迟（磁带回声 / 模拟延时）就是这个行为，
    只做等幅衰减会显得很"数字"。
    """
    d = max(1, int(delay_samples))
    fb = float(np.clip(feedback, 0.0, 0.95))
    length = int(min(max_seconds * sr, d * (1 + int(np.log(1e-4) / np.log(max(1e-4, fb))) if fb > 1e-3 else 1)))
    length = max(length, d + 16)

    ir_l = np.zeros(length, dtype=np.float32)
    ir_r = np.zeros(length, dtype=np.float32)

    repeats = int(np.ceil(length / d))
    for k in range(1, repeats + 1):
        idx = k * d
        if idx >= length:
            break
        gain = fb ** k
        # 每多一轮，低通再作用一次 —— 高频逐轮流失
        tap_l = np.zeros(64, dtype=np.float32)
        tap_r = np.zeros(64, dtype=np.float32)
        if ping_pong:
            # 乒乓：左右轮流承接
            if k % 2 == 1:
                tap_l[0] = gain
                tap_r[0] = gain * 0.35
            else:
                tap_r[0] = gain
                tap_l[0] = gain * 0.35
        else:
            tap_l[0] = gain
            tap_r[0] = gain * 0.92
        # 按轮次累积低通：用累计截断频率近似
        cutoff = max(400.0, float(tone) * (0.78 ** (k - 1)))
        tap_l = dsp.biquad_static(tap_l, "lp", cutoff, 0.7, sr)
        tap_r = dsp.biquad_static(tap_r, "lp", cutoff, 0.7, sr)
        ir_l[idx:idx + 64] += tap_l[: max(0, min(64, length - idx))]
        ir_r[idx:idx + 64] += tap_r[: max(0, min(64, length - idx))]

    ir_l[0] = 0.0   # 干声由调用方保留，冲激响应里不含
    ir_r[0] = 0.0
    return ir_l, ir_r


def apply_delay(stereo: np.ndarray, params: dict, bpm: float = 120.0,
                steps_per_beat: int = 4, sr: int = SR) -> np.ndarray:
    """对 (2, n) 施加延迟。``time_beats`` 以拍为单位，因此变速时延迟自动跟随。"""
    if stereo.size == 0:
        return stereo
    mix = float(np.clip(params.get("mix", 0.22), 0.0, 1.0))
    if mix <= 1e-4:
        return stereo

    time_beats = float(params.get("time_beats", 0.375))
    seconds = time_beats * 60.0 / max(20.0, float(bpm))
    delay_samples = max(16, int(round(seconds * sr)))
    feedback = float(np.clip(params.get("feedback", 0.35), 0.0, 0.95))
    tone = float(params.get("tone", 5000.0))
    ping_pong = bool(params.get("ping_pong", False))

    ir_l, ir_r = delay_ir(delay_samples, round(feedback, 4), round(tone, 1), ping_pong, sr)
    wet_l = dsp.fft_convolve(stereo[0], ir_l)
    wet_r = dsp.fft_convolve(stereo[1], ir_r)
    return np.stack([
        stereo[0] + wet_l * mix,
        stereo[1] + wet_r * mix,
    ]).astype(np.float32)


# ==========================================================================
# 失真
# ==========================================================================


def apply_distortion(stereo: np.ndarray, params: dict, sr: int = SR) -> np.ndarray:
    """波形整形失真。削波前的增益补偿很关键 —— 否则一开失真就只是变大。"""
    if stereo.size == 0:
        return stereo
    drive = max(0.01, float(params.get("drive", 3.0)))
    tone = float(params.get("tone", 4500.0))
    mix = float(np.clip(params.get("mix", 0.6), 0.0, 1.0))
    mode = str(params.get("mode", "soft"))

    out = []
    for ch in range(2):
        x = stereo[ch]
        before = dsp.rms(x)
        if mode == "hard":
            wet = dsp.hard_clip(x * drive, 1.0)
        elif mode == "tube":
            wet = dsp.asym_saturate(x, drive, 0.18)
        else:
            wet = dsp.soft_clip(x, drive)
        # 削波后掉亮度：用低通把刺耳的高次谐波收一点
        wet = dsp.biquad_static(wet, "lp", tone, 0.7, sr)
        # 响度补偿：让 mix 旋钮改变的是音色而不是音量
        after = dsp.rms(wet)
        if after > 1e-6 and before > 1e-6:
            wet = wet * (before / after) ** 0.85
        out.append(x * (1.0 - mix) + wet * mix)

    return np.stack(out).astype(np.float32)


# ==========================================================================
# 静态滤波
# ==========================================================================


def apply_filter(stereo: np.ndarray, params: dict, sr: int = SR) -> np.ndarray:
    if stereo.size == 0:
        return stereo
    kind = str(params.get("kind", "lp"))
    cutoff = float(params.get("cutoff", 8000.0))
    q = float(params.get("resonance", 0.7))
    return np.stack([
        dsp.biquad_static(stereo[0], kind, cutoff, q, sr),
        dsp.biquad_static(stereo[1], kind, cutoff, q, sr),
    ]).astype(np.float32)


# ==========================================================================
# 合唱
# ==========================================================================


def apply_chorus(stereo: np.ndarray, params: dict, sr: int = SR) -> np.ndarray:
    """调制延迟合唱 —— 整体向量化，无 Python 循环。

    关键是**两条通道的 LFO 相位错开**，否则只是把声音变厚而没有空间感。
    """
    if stereo.size == 0:
        return stereo
    rate = max(0.01, float(params.get("rate", 0.8)))
    depth = float(np.clip(params.get("depth", 0.4), 0.0, 1.0))
    mix = float(np.clip(params.get("mix", 0.35), 0.0, 1.0))
    spread = float(np.clip(params.get("spread", 0.6), 0.0, 1.0))
    if mix <= 1e-4:
        return stereo

    n = stereo.shape[1]
    t = np.arange(n, dtype=np.float64) / sr
    base_ms = 12.0 + depth * 10.0
    swing_ms = 3.0 + depth * 9.0

    src = np.arange(n, dtype=np.float64)
    out = []
    for ch in range(2):
        phase = 0.0 if ch == 0 else np.pi * spread
        lfo = np.sin(2.0 * np.pi * rate * t + phase)
        # 第二层 LFO 让调制不呈完美周期，听感更接近真实合唱
        lfo += 0.35 * np.sin(2.0 * np.pi * rate * 1.63 * t + phase * 1.7)
        delay = (base_ms + swing_ms * lfo) * 1e-3 * sr
        pos = np.clip(src - delay, 0.0, n - 1)
        wet = np.interp(pos, src, stereo[ch]).astype(np.float32)
        out.append(stereo[ch] * (1.0 - mix) + wet * mix)

    return np.stack(out).astype(np.float32)


# ==========================================================================
# 降位
# ==========================================================================


def apply_bitcrush(stereo: np.ndarray, params: dict, sr: int = SR) -> np.ndarray:
    """量化降位 + 采样保持降采样（lofi 味道）。"""
    if stereo.size == 0:
        return stereo
    bits = int(np.clip(params.get("bits", 8), 2, 16))
    down = int(np.clip(params.get("downsample", 2), 1, 32))
    mix = float(np.clip(params.get("mix", 1.0), 0.0, 1.0))

    levels = 2 ** bits
    out = []
    for ch in range(2):
        x = stereo[ch]
        y = np.round(x * (levels / 2.0)) / (levels / 2.0)
        if down > 1:
            # 采样保持：每 down 个样本取一次，其余重复前值
            idx = (np.arange(y.size) // down) * down
            y = y[np.clip(idx, 0, y.size - 1)]
        out.append(x * (1.0 - mix) + y * mix)
    return np.stack(out).astype(np.float32)


# ==========================================================================
# 派发
# ==========================================================================

HANDLERS = {
    "reverb": apply_reverb,
    "delay": apply_delay,
    "distortion": apply_distortion,
    "filter": apply_filter,
    "chorus": apply_chorus,
    "bitcrush": apply_bitcrush,
}

EFFECT_LABELS = {
    "reverb": "混响",
    "delay": "延迟",
    "distortion": "失真",
    "filter": "滤波",
    "chorus": "合唱",
    "bitcrush": "降位",
}

EFFECT_ICONS = {
    "reverb": "crystal_ball",
    "delay": "loop",
    "distortion": "zap",
    "filter": "level_slider",
    "chorus": "sparkles",
    "bitcrush": "abacus",
}


def apply_effect(stereo: np.ndarray, effect: Effect, bpm: float = 120.0,
                 steps_per_beat: int = 4, sr: int = SR) -> np.ndarray:
    """按类型派发。未知类型原样返回 —— 保证旧工程遇到新效果器不会崩。"""
    if not effect.enabled:
        return stereo
    handler = HANDLERS.get(effect.kind)
    if handler is None:
        return stereo
    if effect.kind in ("delay",):
        return handler(stereo, effect.params, bpm, steps_per_beat, sr)
    return handler(stereo, effect.params, sr)


def apply_chain(stereo: np.ndarray, effects: list[Effect], bpm: float = 120.0,
                steps_per_beat: int = 4, sr: int = SR) -> np.ndarray:
    y = stereo
    for fx in effects:
        y = apply_effect(y, fx, bpm, steps_per_beat, sr)
    return y


__all__ = [
    "reverb_ir", "apply_reverb", "delay_ir", "apply_delay",
    "apply_distortion", "apply_filter", "apply_chorus", "apply_bitcrush",
    "apply_effect", "apply_chain", "HANDLERS", "EFFECT_LABELS", "EFFECT_ICONS",
]
