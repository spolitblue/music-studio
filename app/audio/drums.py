"""鼓机 —— 全部由代码合成，不依赖任何采样文件。

为什么不用采样？因为一个"能真正使用"的软件不该要求用户先去下载音色包；
而且合成鼓可以做到**每个音符独立调音、调衰减、调音色**，可编辑性远超固定采样。

各鼓件的合成手法（都是经典做法）
----------------------------------
* **底鼓**：正弦 + 极快音高下坠（120→45Hz 约 40ms）+ 瞬态点击，再轻微饱和得到"拳感"。
* **军鼓**：两个共振正弦（鼓腔音）× 短衰减 + 带通噪声（响弦）长一点。
* **拍手**：3~4 个间隔约 10ms 的噪声脉冲 + 尾音，模仿多人拍手的时间差。
* **踩镲**：高通噪声 + 一组非谐波方波（金属质感）。开镲只改衰减。
* **嗵鼓**：与底鼓同构，但初始频率更高、下坠更缓。
* **吊镲/叮叮镲**：多个非谐比方波叠加 + 高通，衰减很长。
* **边击**：极短带通噪声 + 高频正弦瞬态。
* **牛铃**：两个方波（约 540 / 800Hz）经带通。
* **808 底鼓**：正弦 + 慢音高下坠 + 长尾巴（可到 1.6s）。尾巴里几乎没有谐波，
  所以它同时是"底鼓"和"低音"——808 bass 就是把它按音高弹成长音符。
* **808 牛铃 / 拉丁牛铃**：同样是双方波，区别在带通中心（2650Hz vs 1750Hz）与
  尾巴长度；808 那件的辨识度几乎全在那圈带通上。

``tone`` / ``snap`` / ``decay`` / ``tune`` 四个参数能覆盖绝大多数风格走向，
808 与牛铃那几件也一样（见 :mod:`app.audio.soundpacks` 里的成套预设）。
"""

from __future__ import annotations

import numpy as np

from app.audio import dsp
from app.audio.project import DrumVoice

SR = dsp.SR

#: 每个鼓件的最大渲染长度（秒）—— 防止超长衰减把整个工程拖慢
MAX_LEN = {
    "kick": 1.2, "snare": 0.8, "clap": 0.8, "rim": 0.25,
    "hat_closed": 0.35, "hat_pedal": 0.25, "hat_open": 1.6,
    "tom_low": 1.0, "tom_mid": 0.9, "tom_high": 0.8,
    "crash": 3.0, "ride": 2.5, "cowbell": 0.8,
    # 808 底鼓可以长得像贝斯音：上限给到 1.6s，再长就该用合成器声部了
    "kick_808": 1.6, "cowbell_808": 0.6, "cowbell_latin": 0.9,
}

#: 各鼓件的基础音高（Hz）
BASE_FREQ = {
    "kick": 52.0, "snare": 190.0, "clap": 0.0, "rim": 420.0,
    "hat_closed": 0.0, "hat_pedal": 0.0, "hat_open": 0.0,
    "tom_low": 110.0, "tom_mid": 150.0, "tom_high": 205.0,
    "crash": 0.0, "ride": 0.0, "cowbell": 540.0,
    # 808 底鼓比常规底鼓更低（E1 附近），这是它能当低音用的前提
    "kick_808": 41.0, "cowbell_808": 540.0, "cowbell_latin": 440.0,
}


def _pitch_sweep(n: int, sr: int, f_start: float, f_end: float, time_const: float) -> np.ndarray:
    """指数频率下坠：``f(t) = f_end + (f_start − f_end)·e^(−t/τ)``。"""
    t = np.arange(n, dtype=np.float64) / sr
    f = f_end + (f_start - f_end) * np.exp(-t / max(1e-4, time_const))
    return np.cumsum(f) / sr          # 相位


def _sine_from_phase(phase: np.ndarray) -> np.ndarray:
    return np.sin(2.0 * np.pi * phase).astype(np.float32)


def _metal_cluster(n: int, sr: int, ratios: tuple[float, ...], base: float,
                   decay: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """一组非谐比方波 —— 金属打击乐的"叮"感来源。

    真金属体的振动模态是**非整数比**的；用整数比会听成乐音而不是镲。
    """
    t = np.arange(n, dtype=np.float64) / sr
    out = np.zeros(n, dtype=np.float64)
    for r in ratios:
        f = base * r
        if f >= sr * 0.45:
            continue
        ph = (f * t) % 1.0
        sq = np.where(ph < 0.5, 1.0, -1.0)
        out += sq * np.exp(-t / max(1e-4, 0.35 / max(0.2, r)))
    out *= decay
    return out.astype(np.float32)


def render_drum(
    piece: str,
    voice: DrumVoice,
    velocity: float = 0.85,
    sr: int = SR,
) -> np.ndarray:
    """渲染一个鼓件，返回 ``(2, n)`` 立体声。"""
    piece = piece if piece in MAX_LEN else "kick"
    vy = float(np.clip(velocity, 0.0, 1.0))
    tune_ratio = 2.0 ** (float(voice.tune) / 12.0)
    base = BASE_FREQ.get(piece, 60.0) * tune_ratio

    dur = float(np.clip(voice.decay, 0.01, MAX_LEN[piece]))
    n = int(max(64, dur * sr))
    rng = np.random.default_rng(abs(hash(piece)) & 0xFFFFFFFF)

    tone = float(np.clip(voice.tone, 0.0, 1.0))
    snap = float(np.clip(voice.snap, 0.0, 1.0))

    # 明亮度：tone=0 暗，tone=1 亮
    bright = 1200.0 * (10.0 ** (tone * 1.9))      # 1.2k ~ 95k（会被钳到奈奎斯特）

    if piece == "kick":
        ph = _pitch_sweep(n, sr, base * 3.2, base, 0.028 + 0.02 * (1 - snap))
        body = _sine_from_phase(ph)
        env = dsp.percussive_env(n, sr, 0.001, dur, curve=4.5)  # 快起音 + 指数衰减
        y = body * env
        # 瞬态点击：极短噪声，让底鼓在混音里"穿得出来"
        click = rng.standard_normal(n).astype(np.float32) * np.exp(
            -np.arange(n) / max(1.0, 0.0016 * sr)
        )
        y = y + click * (0.25 * snap)
        y = dsp.soft_clip(y, 1.6 + 1.4 * snap)      # 轻微饱和 = 拳感
        y = dsp.biquad_static(y, "lp", min(14000.0, 3000.0 + bright), 0.8, sr)

    elif piece == "snare":
        body = np.zeros(n, dtype=np.float32)
        for ratio, amp in ((1.0, 1.0), (1.78, 0.62), (2.44, 0.34)):
            f = base * ratio
            if f < sr * 0.45:
                t = np.arange(n, dtype=np.float64) / sr
                body += (amp * np.sin(2 * np.pi * f * t)).astype(np.float32)
        body_env = dsp.percussive_env(n, sr, 0.0008, min(0.18, dur * 0.5), curve=4.0)
        noise = rng.standard_normal(n).astype(np.float32)
        noise_env = dsp.percussive_env(n, sr, 0.0008, dur, curve=3.0)
        # 响弦噪声：带通放在 1.5k~9k，tone 决定重心
        lo = 900.0 + tone * 1400.0
        hi = min(16000.0, 6500.0 + bright)
        noise = dsp.bandpass(noise, lo, hi, sr)
        y = body * body_env * 0.6 + noise * noise_env * (0.75 + 0.35 * snap)

    elif piece == "clap":
        # 3~4 个间隔 ~10ms 的脉冲：多人拍手的时间差
        y = np.zeros(n, dtype=np.float32)
        spacing = int(0.0105 * sr)
        bursts = 3 + (1 if snap > 0.55 else 0)
        for b in range(bursts):
            off = b * spacing
            if off >= n:
                break
            seg = n - off
            bn = rng.standard_normal(seg).astype(np.float32)
            be = dsp.percussive_env(seg, sr, 0.0006, 0.012, curve=5.0)
            y[off:] += bn * be * (1.0 - 0.12 * b)
        tail = rng.standard_normal(n).astype(np.float32)
        te = dsp.percussive_env(n, sr, 0.001, dur, curve=3.0)
        y += tail * te * 0.35
        y = dsp.bandpass(y, 700.0 + tone * 500.0, min(14000.0, 4000.0 + bright), sr)

    elif piece in ("hat_closed", "hat_pedal", "hat_open"):
        noise = rng.standard_normal(n).astype(np.float32)
        # 踩镲的金属感来自非谐比方波簇
        ratios = (1.0, 1.342, 1.2312, 1.6532, 1.9523, 2.1523, 2.3213, 2.8102, 3.1021)
        metal = _metal_cluster(n, sr, ratios, max(280.0, base or 280.0),
                               dsp.percussive_env(n, sr, 0.0004, dur, curve=4.0), rng)
        env = dsp.percussive_env(n, sr, 0.0004, dur, curve=5.0)
        y = noise * 0.55 + metal * 0.5
        y = dsp.biquad_static(y, "hp", 5000.0 + tone * 3500.0, 0.8, sr)
        y = dsp.biquad_static(y, "lp", min(18000.0, 9000.0 + bright), 0.7, sr) * env

    elif piece in ("tom_low", "tom_mid", "tom_high"):
        ph = _pitch_sweep(n, sr, base * 2.0, base, 0.06 + 0.05 * (1 - snap))
        body = _sine_from_phase(ph)
        env = dsp.percussive_env(n, sr, 0.001, dur, curve=3.2)
        y = body * env
        y += rng.standard_normal(n).astype(np.float32) * env * 0.06 * snap
        y = dsp.soft_clip(y, 1.25)
        y = dsp.biquad_static(y, "lp", min(12000.0, 2500.0 + bright), 0.8, sr)

    elif piece == "rim":
        noise = rng.standard_normal(n).astype(np.float32)
        env = dsp.percussive_env(n, sr, 0.0002, max(0.02, dur * 0.4), curve=5.0)
        t = np.arange(n, dtype=np.float64) / sr
        ping = np.sin(2 * np.pi * (base * 4.0) * t).astype(np.float32)
        y = dsp.bandpass(noise, 1800.0 + tone * 1500.0, min(15000.0, 6000.0 + bright), sr) * env
        y += ping * dsp.percussive_env(n, sr, 0.0002, 0.02, curve=6.0) * 0.4

    elif piece in ("crash", "ride"):
        ratios = ((1.0, 1.41, 1.68, 2.07, 2.53, 2.94, 3.42, 4.11, 4.87, 5.63)
                  if piece == "crash"
                  else (1.0, 1.27, 1.81, 2.13, 2.71, 3.19, 3.77, 4.31))
        metal = _metal_cluster(n, sr, ratios, 320.0 + tone * 260.0,
                               dsp.percussive_env(n, sr, 0.0005, dur, curve=1.6), rng)
        noise = rng.standard_normal(n).astype(np.float32)
        nenv = dsp.percussive_env(n, sr, 0.0004, dur * 0.85, curve=2.2)
        y = metal * 0.62 + noise * nenv * (0.4 if piece == "crash" else 0.16)
        y = dsp.biquad_static(y, "hp", 2600.0 + tone * 2600.0, 0.7, sr)

    elif piece == "cowbell":
        t = np.arange(n, dtype=np.float64) / sr
        y = np.zeros(n, dtype=np.float32)
        for ratio, amp in ((1.0, 0.7), (1.48, 1.0)):    # 经典 540/800Hz 比例
            y += (amp * np.sign(np.sin(2 * np.pi * base * ratio * t))).astype(np.float32)
        y *= dsp.percussive_env(n, sr, 0.0005, dur, curve=4.0)
        y = dsp.bandpass(y, 400.0, min(12000.0, 2400.0 + bright), sr)

    elif piece == "kick_808":
        # TR-808 底鼓：正弦 + **慢一点**的音高下坠 + 很长的尾巴。
        # 与常规底鼓的区别全在这三点上：尾巴长（可到 1.6s）、几乎没有谐波、
        # 起始频率也不算高 —— 所以它既能当底鼓，也能按音高弹成低音线条
        # （808 bass 就是把这件鼓当低音吉他使，配上长音符即可）。
        ph = _pitch_sweep(n, sr, base * 4.5, base, 0.018 + 0.05 * (1.0 - snap))
        body = _sine_from_phase(ph)
        env = dsp.percussive_env(n, sr, 0.002, dur, curve=1.9)
        y = body * env
        # 点击放在最前面几毫秒：没有它，808 在小喇叭上会"听不见"，
        # 只剩一团低频糊在混音里。tone 控制这层点击的亮度。
        click = rng.standard_normal(n).astype(np.float32) * np.exp(
            -np.arange(n) / max(1.0, 0.0012 * sr)
        )
        y = y + click * (0.20 * snap)
        y = dsp.soft_clip(y, 1.3 + 0.6 * snap)
        y = dsp.biquad_static(y, "lp", min(9000.0, 1600.0 + bright), 0.8, sr)

    elif piece == "cowbell_808":
        # 808 牛铃：两个方波（540 / 800Hz，比例 1.48）经**窄带通**。
        # 那圈"当"的金属味就是带通中心给的 —— 中心取得低会变闷、取高会变镲，
        # 2650Hz 附近才是 808 那个辨识度极高的音色。
        t = np.arange(n, dtype=np.float64) / sr
        y = np.zeros(n, dtype=np.float32)
        for ratio, amp in ((1.0, 0.55), (1.48, 1.0)):
            y += (amp * np.sign(np.sin(2 * np.pi * base * ratio * t))).astype(np.float32)
        y *= dsp.percussive_env(n, sr, 0.0004, dur, curve=4.5)
        center = 2650.0 + tone * 900.0
        y = dsp.bandpass(y, center * 0.62, center * 1.55, sr)
        y = dsp.biquad_static(y, "hp", 320.0, 0.8, sr)      # 去掉方波里的低频隆隆

    elif piece == "cowbell_latin":
        # 拉丁牛铃（agogo 味）：同样的双方波，但整体更低、尾巴更长、带通更宽。
        # 它不是"点缀"而是节奏骨架 —— 所以听得清、又不抢戏。
        t = np.arange(n, dtype=np.float64) / sr
        y = np.zeros(n, dtype=np.float32)
        for ratio, amp in ((1.0, 0.8), (1.5, 1.0)):
            y += (amp * np.sign(np.sin(2 * np.pi * base * ratio * t))).astype(np.float32)
        y *= dsp.percussive_env(n, sr, 0.0005, dur, curve=3.0)
        center = 1750.0 + tone * 800.0
        y = dsp.bandpass(y, center * 0.5, center * 1.8, sr)
        y = dsp.biquad_static(y, "hp", 260.0, 0.8, sr)

    else:
        y = np.zeros(n, dtype=np.float32)

    # ---- 收尾：归一化 + 声像 -------------------------------------------
    y = dsp.fade(y, fade_in=0, fade_out=min(256, n // 6))
    peak = float(np.max(np.abs(y)))
    if peak > 1e-6:
        y = y / peak
    y = y * (vy * float(voice.level))

    gain_l, gain_r = dsp.equal_power_pan(voice.pan)
    # 轻微立体声展宽：低频保持单声道（避免低频相位问题），高频左右微差
    side = y * 0.04
    left = y * gain_l + side * gain_r
    right = y * gain_r - side * gain_l
    return np.stack([left, right]).astype(np.float32)


def default_voices() -> dict[str, DrumVoice]:
    """一套开箱可用的默认套鼓 —— 参数经过试听调过，直接能编曲。"""
    return {
        "kick": DrumVoice(level=1.0, tune=0.0, decay=0.34, tone=0.35, snap=0.7, pan=0.0),
        "snare": DrumVoice(level=0.82, tune=0.0, decay=0.22, tone=0.55, snap=0.65, pan=-0.05),
        "clap": DrumVoice(level=0.72, tune=0.0, decay=0.28, tone=0.5, snap=0.6, pan=0.20),
        "hat_closed": DrumVoice(level=0.5, tune=0.0, decay=0.055, tone=0.6, snap=0.5, pan=0.28),
        "hat_pedal": DrumVoice(level=0.42, tune=0.0, decay=0.04, tone=0.4, snap=0.4, pan=0.28),
        "hat_open": DrumVoice(level=0.44, tune=0.0, decay=0.42, tone=0.6, snap=0.45, pan=0.32),
        "tom_low": DrumVoice(level=0.7, tune=0.0, decay=0.42, tone=0.45, snap=0.5, pan=-0.35),
        "tom_mid": DrumVoice(level=0.68, tune=0.0, decay=0.36, tone=0.5, snap=0.5, pan=0.0),
        "tom_high": DrumVoice(level=0.66, tune=0.0, decay=0.30, tone=0.55, snap=0.5, pan=0.35),
        "rim": DrumVoice(level=0.6, tune=0.0, decay=0.12, tone=0.6, snap=0.7, pan=-0.2),
        "crash": DrumVoice(level=0.45, tune=0.0, decay=1.9, tone=0.6, snap=0.6, pan=-0.4),
        "ride": DrumVoice(level=0.4, tune=0.0, decay=1.4, tone=0.55, snap=0.4, pan=0.4),
        "cowbell": DrumVoice(level=0.4, tune=0.0, decay=0.28, tone=0.6, snap=0.5, pan=0.15),
    }


__all__ = ["render_drum", "default_voices", "MAX_LEN", "BASE_FREQ", "SR"]
