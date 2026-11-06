"""DSP 基元 —— 音频引擎的地基。

全部基于 numpy 的**离线渲染**思路：一次性算完整段音频，而不是逐块实时计算。
好处是可以放心用卷积、FFT、理想滤波器这些"作弊"手段，音质和速度都远好于
逐样本的实时实现。

三条关键设计
------------
1. **带限波表**：锯齿/方波用加法合成预生成"每倍频程一张表"的波形表（mip-map），
   按基频选用谐波数不超奈奎斯特的那一张。既没有混叠（naive 波形在高音区会
   发出刺耳的金属声），又比每音符现场做加法合成快几十倍。
2. **IIR 走 FFT**：滤波器/延迟/混响都是线性时不变系统，可以先求冲激响应，
   再用 FFT 卷积。数学上与原 IIR 完全等价，但把 O(N·阶数) 的串联递推
   变成了 O(N log N)。
3. **控制率与音频率分离**：时变滤波器（有包络的扫频）的系数每 32 个样本
   更新一次并线性插值到逐样本。听感无损，省掉每样本一次三角函数。
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

SR = 44100              # 默认采样率
#: 波表长度。4096 时线性插值残差在低音区约 −92dB，8192 后降到 −110dB 以下 ——
#: 代价只是约 2MB 常驻表数据，换掉一个刚好卡在 16bit 量化底噪上的瑕疵，值得。
TABLE_SIZE = 8192
MAX_HARMONICS = 512     # 波表最高谐波数
CONTROL_BLOCK = 32      # 时变参数的控制率块长


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


def db_to_gain(db: float) -> float:
    return float(10.0 ** (db / 20.0))


def gain_to_db(g: float) -> float:
    return float(20.0 * np.log10(max(1e-9, g)))


def midi_to_hz(pitch: float) -> float:
    """MIDI 音高 → 频率。A4(69) = 440Hz。"""
    return float(440.0 * (2.0 ** ((pitch - 69.0) / 12.0)))


def hz_to_midi(hz: float) -> float:
    return float(69.0 + 12.0 * np.log2(max(1e-6, hz) / 440.0))


NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def pitch_name(pitch: int, with_octave: bool = True) -> str:
    p = int(round(pitch))
    name = NOTE_NAMES[p % 12]
    return f"{name}{p // 12 - 1}" if with_octave else name


def equal_power_pan(pan: float) -> tuple[float, float]:
    """等功率声像。``pan`` ∈ [-1, 1]，返回 (左增益, 右增益)。"""
    p = max(-1.0, min(1.0, float(pan)))
    angle = (p + 1.0) * 0.25 * np.pi      # 0..π/2
    return float(np.cos(angle)), float(np.sin(angle))


def crossfade_edges(x: np.ndarray, fade: int = 64) -> np.ndarray:
    """给信号首尾加极短淡入淡出，消除拼接爆音。"""
    n = len(x)
    if n < 2 * fade or fade <= 0:
        return x
    y = x.copy()
    ramp = np.linspace(0.0, 1.0, fade, dtype=y.dtype)
    y[:fade] *= ramp
    y[-fade:] *= ramp[::-1]
    return y


def fft_convolve(x: np.ndarray, ir: np.ndarray) -> np.ndarray:
    """FFT 卷积，返回与 ``x`` 等长的结果（丢弃拖尾）。

    比 ``np.convolve`` 快一个数量级；冲激响应长（混响）时优势更明显。
    """
    if ir.size == 0 or x.size == 0:
        return x
    if ir.size == 1:
        return x * ir[0]
    n = x.size + ir.size - 1
    nfft = 1 << int(np.ceil(np.log2(max(2, n))))
    X = np.fft.rfft(x, nfft)
    H = np.fft.rfft(ir, nfft)
    y = np.fft.irfft(X * H, nfft)[: x.size]
    return y.astype(np.float32, copy=False)


# --------------------------------------------------------------------------
# 波表（带限振荡器）
# --------------------------------------------------------------------------


def _additive(wave: str, harmonics: int, size: int = TABLE_SIZE) -> np.ndarray:
    """用加法合成生成一个周期的波形，谐波数受 ``harmonics`` 限制。"""
    t = np.arange(size, dtype=np.float64) / size
    out = np.zeros(size, dtype=np.float64)

    if wave == "sine":
        out = np.sin(2.0 * np.pi * t)

    elif wave in ("saw", "sawtooth"):
        k = np.arange(1, harmonics + 1, dtype=np.float64)
        # Σ sin(2πkt)/k  —— 幅度按 1/k 滚降
        out = (np.sin(2.0 * np.pi * np.outer(t, k)) / k).sum(axis=1)

    elif wave in ("square", "pulse"):
        k = np.arange(1, harmonics + 1, 2, dtype=np.float64)
        if k.size:
            out = (np.sin(2.0 * np.pi * np.outer(t, k)) / k).sum(axis=1)

    elif wave in ("triangle", "tri"):
        k = np.arange(1, harmonics + 1, 2, dtype=np.float64)
        if k.size:
            signs = np.where(((k - 1) / 2) % 2 == 0, 1.0, -1.0)
            out = ((signs / (k ** 2)) * np.sin(2.0 * np.pi * np.outer(t, k))).sum(axis=1)

    elif wave == "organ":
        # 风琴：基频 + 若干八度泛音，用于 pad / 和弦垫
        for mult, amp in ((1, 1.0), (2, 0.5), (3, 0.25), (4, 0.18), (6, 0.10), (8, 0.07)):
            if mult <= harmonics:
                out += amp * np.sin(2.0 * np.pi * mult * t)

    elif wave == "noise":
        rng = np.random.default_rng(7)
        out = rng.standard_normal(size)

    else:
        out = np.sin(2.0 * np.pi * t)

    peak = float(np.max(np.abs(out))) if out.size else 1.0
    if peak > 1e-9:
        out /= peak
    return out.astype(np.float32)


@lru_cache(maxsize=64)
def wavetable(wave: str, harmonics: int) -> np.ndarray:
    """按"波形 + 谐波上限"缓存一张 wavetable。"""
    h = max(1, min(MAX_HARMONICS, int(harmonics)))
    if wave == "sine" or wave == "noise":
        h = 1
    return _additive(wave, h)


@lru_cache(maxsize=16)
def _mip_levels(wave: str) -> tuple[tuple[int, np.ndarray], ...]:
    """一倍频程一档的波表组：谐波数 1,2,4,...,MAX。"""
    levels = []
    h = 1
    while h <= MAX_HARMONICS:
        levels.append((h, wavetable(wave, h)))
        h *= 2
    return tuple(levels)


def pick_table(wave: str, freq: float, sr: int = SR) -> np.ndarray:
    """选谐波数不超过奈奎斯特的那张表 —— 这是"不混叠"的关键。"""
    max_h = int((sr * 0.5) / max(20.0, abs(freq)))
    levels = _mip_levels(wave)
    chosen = levels[0][1]
    for h, table in levels:
        if h <= max_h:
            chosen = table
        else:
            break
    return chosen


def render_osc(
    wave: str,
    freq: np.ndarray,
    sr: int = SR,
    phase0: float = 0.0,
) -> np.ndarray:
    """用相位累加 + 波表插值渲染一段振荡器。

    ``freq`` 是逐样本的瞬时频率数组（可以是常数、滑音、或带音高包络）。
    """
    n = freq.size
    if n == 0:
        return np.zeros(0, dtype=np.float32)

    if wave == "noise":
        rng = np.random.default_rng(12345)
        return rng.standard_normal(n).astype(np.float32)

    # 相位用双精度累加，避免长音符上 float32 累积误差导致音高漂移
    phase = phase0 + np.cumsum(freq.astype(np.float64)) / float(sr)
    frac = phase - np.floor(phase)          # 0..1

    # 分段选表：整段共用一个基频档次（音符内频率变化通常不超过一个八度）
    table = pick_table(wave, float(np.mean(np.abs(freq))), sr)
    size = table.size
    idx = frac * size
    i0 = idx.astype(np.int64) % size
    i1 = (i0 + 1) % size
    w = (idx - np.floor(idx)).astype(np.float32)
    return (table[i0] * (1.0 - w) + table[i1] * w).astype(np.float32)


# --------------------------------------------------------------------------
# 包络
# --------------------------------------------------------------------------


def adsr(
    n: int,
    sr: int,
    attack: float,
    decay: float,
    sustain: float,
    release: float,
    curve: float = 2.0,
) -> np.ndarray:
    """采样级 ADSR 包络。

    ``curve`` > 1 让拐点呈指数形（更像模拟合成器）；= 1 为线性。
    总长 = n；若 n 超过 "a+d+r" 则中段保持 sustain。
    """
    a = max(0, int(attack * sr))
    d = max(0, int(decay * sr))
    r = max(0, int(release * sr))
    sus = float(np.clip(sustain, 0.0, 1.0))

    env = np.zeros(n, dtype=np.float32)
    pos = 0

    if a > 0:
        seg = min(a, n)
        env[:seg] = np.linspace(0.0, 1.0, seg, dtype=np.float32) ** (1.0 / max(1e-3, curve))
        pos = seg
    else:
        env[:1] = 1.0
        pos = 1

    if pos < n and d > 0:
        seg = min(d, n - pos)
        ramp = np.linspace(1.0, sus, seg, dtype=np.float32)
        # 指数形衰减：先降得快，尾巴拖长
        ramp = sus + (1.0 - sus) * (1.0 - np.linspace(0.0, 1.0, seg, dtype=np.float32)) ** curve
        env[pos:pos + seg] = ramp
        pos += seg

    if pos < n:
        body = n - pos
        if r > 0 and body > r:
            hold = body - r
            env[pos:pos + hold] = sus
            pos += hold
            env[pos:pos + r] = sus * (1.0 - np.linspace(0.0, 1.0, r, dtype=np.float32)) ** curve
        else:
            # 音符还没到 release 就结束了：整体再压一段尾巴
            env[pos:] = sus * (1.0 - np.linspace(0.0, 1.0, body, dtype=np.float32)) ** curve

    return env


def percussive_env(n: int, sr: int, attack: float, decay: float, curve: float = 3.0) -> np.ndarray:
    """打击乐包络：瞬时起音 + 指数衰减（无延音段）。"""
    a = max(1, int(attack * sr))
    env = np.zeros(n, dtype=np.float32)
    if a < n:
        env[:a] = np.linspace(0.0, 1.0, a, dtype=np.float32)
        tail = n - a
        env[a:] = np.exp(-np.linspace(0.0, curve * 6.0, tail, dtype=np.float32))
    else:
        env[:] = np.linspace(0.0, 1.0, n, dtype=np.float32)
    return env


# --------------------------------------------------------------------------
# 滤波器
# --------------------------------------------------------------------------


def svf_coeffs(cutoff: np.ndarray, q: float, sr: int = SR) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """TPT/ZDF 状态变量滤波器的三组系数（逐样本数组）。

    Zavalishin 的拓扑保持变换：即使扫频很猛也不会自激炸掉。
    """
    fc = np.clip(cutoff, 20.0, sr * 0.46875)
    g = np.tan(np.pi * fc / sr).astype(np.float64)
    k = 1.0 / max(1e-3, float(q))
    a1 = 1.0 / (1.0 + g * (g + k))
    a2 = g * a1
    a3 = g * a2
    return a1, a2, a3


def svf_run(
    x: np.ndarray,
    a1: np.ndarray,
    a2: np.ndarray,
    a3: np.ndarray,
    mode: str = "lp",
    k: float = 1.4142135623730951,
) -> np.ndarray:
    """逐样本跑状态变量滤波器。

    ``a1/a2/a3`` 可以是逐样本数组或标量；``k`` 是阻尼（= 1/Q，通常扫描中不变）。

    三个输出（Zavalishin 的 TPT 形式）::

        low  = v2
        band = v1
        high = x − k·v1 − v2
    """
    n = x.size
    out = np.empty(n, dtype=np.float32)
    ic1 = 0.0
    ic2 = 0.0

    xs = x.astype(np.float64, copy=False)
    a1a = np.broadcast_to(np.asarray(a1, dtype=np.float64), (n,))
    a2a = np.broadcast_to(np.asarray(a2, dtype=np.float64), (n,))
    a3a = np.broadcast_to(np.asarray(a3, dtype=np.float64), (n,))
    kk = float(k)

    for i in range(n):
        _a1 = a1a[i]
        _a2 = a2a[i]
        _a3 = a3a[i]
        v3 = xs[i] - ic2
        v1 = _a1 * ic1 + _a2 * v3
        v2 = ic2 + _a2 * ic1 + _a3 * v3
        ic1 = 2.0 * v1 - ic1
        ic2 = 2.0 * v2 - ic2
        if mode == "lp":
            out[i] = v2
        elif mode == "bp":
            out[i] = v1
        elif mode == "hp":
            out[i] = xs[i] - kk * v1 - v2
        elif mode == "notch":
            out[i] = xs[i] - kk * v1
        else:
            out[i] = v2
    return out


def biquad_lowpass_coeffs(cutoff: float, q: float, sr: int = SR):
    """RBJ 双二阶低通系数 (b0,b1,b2,a1,a2)。"""
    w0 = 2.0 * np.pi * float(np.clip(cutoff, 10.0, sr * 0.46875)) / sr
    cw = np.cos(w0)
    sw = np.sin(w0)
    alpha = sw / (2.0 * max(0.05, q))
    b0 = (1.0 - cw) / 2.0
    b1 = 1.0 - cw
    b2 = (1.0 - cw) / 2.0
    a0 = 1.0 + alpha
    a1 = -2.0 * cw
    a2 = 1.0 - alpha
    return b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0


def biquad_coeffs(kind: str, cutoff: float, q: float = 0.707, sr: int = SR):
    """RBJ Cookbook 双二阶系数，返回 ``(b0, b1, b2, a1, a2)``（已归一化 a0）。

    支持 lp / hp / bp / notch / peak —— 做鼓机与音色整形够用了。
    """
    fc = float(np.clip(cutoff, 10.0, sr * 0.46875))
    w0 = 2.0 * np.pi * fc / sr
    cw = float(np.cos(w0))
    sw = float(np.sin(w0))
    qq = max(0.05, float(q))
    alpha = sw / (2.0 * qq)

    if kind == "lp":
        b0, b1, b2 = (1.0 - cw) / 2.0, 1.0 - cw, (1.0 - cw) / 2.0
    elif kind == "hp":
        b0, b1, b2 = (1.0 + cw) / 2.0, -(1.0 + cw), (1.0 + cw) / 2.0
    elif kind == "bp":            # 峰值增益归一化
        b0, b1, b2 = qq * alpha, 0.0, -qq * alpha
    elif kind == "notch":
        b0, b1, b2 = 1.0, -2.0 * cw, 1.0
    elif kind == "peak":
        A = 10.0 ** (qq / 40.0)   # 复用 q 作为增益(dB)入口
        b0, b1, b2 = 1.0 + alpha * A, -2.0 * cw, 1.0 - alpha * A
        a0 = 1.0 + alpha / A
        a1 = -2.0 * cw
        a2 = 1.0 - alpha / A
        return b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0
    else:
        b0, b1, b2 = (1.0 - cw) / 2.0, 1.0 - cw, (1.0 - cw) / 2.0

    a0 = 1.0 + alpha
    a1 = -2.0 * cw
    a2 = 1.0 - alpha
    return b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0


def biquad_static(
    x: np.ndarray, kind: str, cutoff: float, q: float = 0.707, sr: int = SR
) -> np.ndarray:
    """静态双二阶滤波 —— 走冲激响应 + FFT 卷积，与逐样本递推等价但快得多。"""
    if x.size == 0:
        return x
    b0, b1, b2, a1, a2 = biquad_coeffs(kind, cutoff, q, sr)
    h = iir_impulse_response((b0, b1, b2), (a1, a2), min(32768, max(256, x.size)))
    return fft_convolve(x, h)


def bandpass(x: np.ndarray, low: float, high: float, sr: int = SR) -> np.ndarray:
    """带通 = 低通 + 高通串联。用于给噪声"塑形"成军鼓/踩镲。"""
    y = biquad_static(x, "hp", float(low), 0.707, sr)
    return biquad_static(y, "lp", float(high), 0.707, sr)


def iir_impulse_response(b: tuple[float, float, float], a: tuple[float, float], length: int) -> np.ndarray:
    """用递推求双二阶段的冲激响应（供 FFT 卷积使用）。"""
    b0, b1, b2 = b
    a1, a2 = a
    h = np.zeros(length, dtype=np.float64)
    x0, x1, x2 = 1.0, 0.0, 0.0
    y1, y2 = 0.0, 0.0
    for i in range(length):
        y0 = b0 * x0 + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        h[i] = y0
        x2, x1, x0 = x1, x0, 0.0
        y2, y1 = y1, y0
        if i > 64 and abs(y0) < 1e-7:
            break
    return h.astype(np.float32)


def lowpass_static(x: np.ndarray, cutoff: float, q: float = 0.707, sr: int = SR) -> np.ndarray:
    """静态低通 —— 走 FFT 卷积，比逐样本递推快得多且结果等价。"""
    b = biquad_lowpass_coeffs(cutoff, q, sr)
    h = iir_impulse_response((b[0], b[1], b[2]), (b[3], b[4]), min(16384, max(256, x.size)))
    return fft_convolve(x, h)


def resonant_lowpass(
    x: np.ndarray,
    cutoff: np.ndarray | float,
    q: float = 0.707,
    sr: int = SR,
) -> np.ndarray:
    """共振低通。

    * 截止频率**恒定** → 走 FFT 卷积（双二阶冲激响应），快且结果等价。
    * 截止频率**时变**（滤波包络扫频）→ 只能走逐样本 SVF，因为系统不再时不变。
    """
    if x.size == 0:
        return x

    if np.isscalar(cutoff):
        fc = float(cutoff)
        if fc >= sr * 0.48:
            return x  # 完全打开，不必滤波
        return lowpass_static(x, fc, q, sr)

    n = x.size
    ctrl = np.asarray(cutoff, dtype=np.float64).ravel()
    if ctrl.size != n:
        ctrl = np.interp(np.linspace(0.0, 1.0, n), np.linspace(0.0, 1.0, ctrl.size), ctrl)

    # 若整段的截止频率几乎不变，就没必要走慢路径
    if float(ctrl.max() - ctrl.min()) < max(20.0, float(ctrl.mean()) * 0.02):
        fc = float(ctrl.mean())
        if fc >= sr * 0.48:
            return x
        return lowpass_static(x, fc, q, sr)

    # 控制率降采样 → 音频率线性插值
    ctrl_pts = ctrl[::CONTROL_BLOCK]
    if ctrl_pts.size < 2:
        ctrl_pts = np.array([ctrl[0], ctrl[-1]], dtype=np.float64)
    per_sample = np.interp(
        np.linspace(0.0, 1.0, n),
        np.linspace(0.0, 1.0, ctrl_pts.size),
        ctrl_pts,
    )
    a1, a2, a3 = svf_coeffs(per_sample, q, sr)
    return svf_run(x, a1, a2, a3, "lp", k=1.0 / max(1e-3, float(q)))


# --------------------------------------------------------------------------
# 非线性
# --------------------------------------------------------------------------


def soft_clip(x: np.ndarray, drive: float = 1.0) -> np.ndarray:
    """tanh 软削波 —— 过载的首选，不会产生方波那样的刺耳高频。"""
    d = max(1e-3, float(drive))
    return np.tanh(x * d).astype(np.float32)


def hard_clip(x: np.ndarray, threshold: float = 1.0) -> np.ndarray:
    return np.clip(x, -threshold, threshold).astype(np.float32)


def asym_saturate(x: np.ndarray, drive: float = 1.0, bias: float = 0.15) -> np.ndarray:
    """非对称饱和 —— 产生偶次谐波，听感更"电子管"。"""
    d = max(1e-3, float(drive))
    y = np.tanh((x + bias) * d) - np.tanh(bias * d)
    peak = float(np.max(np.abs(y))) or 1.0
    return (y / peak).astype(np.float32)


def one_pole_lowpass(x: np.ndarray, cutoff: float, sr: int = SR) -> np.ndarray:
    """一阶低通（走 FFT 卷积版），用于音色明暗调节。"""
    fc = float(np.clip(cutoff, 20.0, sr * 0.49))
    alpha = np.exp(-2.0 * np.pi * fc / sr)
    # 一阶 IIR 的冲激响应有解析解：(1-α)·α^n
    length = int(min(x.size, max(64, np.log(1e-6) / np.log(max(1e-6, alpha)))))
    n = np.arange(length, dtype=np.float64)
    h = (1.0 - alpha) * (alpha ** n)
    return fft_convolve(x, h.astype(np.float32))


# --------------------------------------------------------------------------
# 归一化 / 分析
# --------------------------------------------------------------------------


def peak(x: np.ndarray) -> float:
    return float(np.max(np.abs(x))) if x.size else 0.0


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x ** 2))) if x.size else 0.0


def normalize(x: np.ndarray, target: float = 0.98) -> np.ndarray:
    p = peak(x)
    if p < 1e-9:
        return x
    return (x * (target / p)).astype(np.float32)


def fade(x: np.ndarray, fade_in: int = 0, fade_out: int = 0) -> np.ndarray:
    """首尾淡入淡出。

    一律沿**最后一个轴**（时间轴）操作并对声道广播 —— 立体声输入是 (2, n)，
    若按第 0 轴切会把声道当成时间来淡出，形状对不上直接抛错。
    """
    y = x
    n = y.shape[-1] if y.ndim else 0
    if n == 0:
        return y

    if fade_in > 0 and fade_in <= n:
        if y is x:
            y = y.copy()
        y[..., :fade_in] *= np.linspace(0.0, 1.0, fade_in, dtype=y.dtype)

    if fade_out > 0 and fade_out <= n:
        if y is x:
            y = y.copy()
        y[..., -fade_out:] *= np.linspace(1.0, 0.0, fade_out, dtype=y.dtype)

    return y


__all__ = [
    "SR", "TABLE_SIZE", "MAX_HARMONICS", "CONTROL_BLOCK", "NOTE_NAMES",
    "db_to_gain", "gain_to_db", "midi_to_hz", "hz_to_midi", "pitch_name",
    "equal_power_pan", "crossfade_edges", "fft_convolve",
    "wavetable", "pick_table", "render_osc",
    "adsr", "percussive_env",
    "svf_coeffs", "svf_run", "biquad_lowpass_coeffs", "biquad_coeffs", "biquad_static",
    "bandpass", "iir_impulse_response",
    "lowpass_static", "resonant_lowpass", "one_pole_lowpass",
    "soft_clip", "hard_clip", "asym_saturate",
    "peak", "rms", "normalize", "fade",
]
