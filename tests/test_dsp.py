"""DSP 层验证：正确性 + 性能。"""

import time

import numpy as np

# --- 项目根路径引导（本文件位于子目录，保证从任何 cwd 都能 import app.*）---
import sys as _sys
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parent.parent
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))
# --- 引导结束 ---

from app.audio import dsp

ok = True


def check(label, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print(f"  [{'OK  ' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")


print("=" * 68)
print("1) 带限振荡器 —— 是否真的不混叠")
print("=" * 68)

SR = 44100


N_FFT = 16384


def interharmonic_db(x, f, sr=SR, n=N_FFT, window=None):
    """测"非谐波能量占比"（dB）—— 混叠的正确度量。

    混叠不是产生奈奎斯特以上的频率（采样信号里根本不存在），而是**折叠回带内**，
    在 k·f 之外的频点上凭空造出能量。所以要看的是：除了各次谐波，还有多少能量。

    **必须用相干采样**：频率取 sr/n 的整数倍时，各次谐波恰好落在 bin 上，
    矩形窗下无泄漏；否则窗函数旁瓣会把 −50dB 左右的本底糊上来，
    把真正干净的信号误判成有混叠（这个坑踩过一次）。
    """
    x = x[:n]
    spec = np.abs(np.fft.rfft(x if window is None else x * window)) ** 2
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    df = sr / n

    mask = np.zeros(freqs.size, dtype=bool)
    k = 1
    while k * f < sr * 0.5:
        c = k * f
        mask[max(0, int(round(c / df)) - 2): int(round(c / df)) + 3] = True
        k += 1
    mask[freqs < 30.0] = True

    harm = float(spec[mask].sum())
    inter = float(spec[~mask].sum())
    return 10 * np.log10(max(1e-30, inter) / max(1e-30, harm + inter))


def coherent_freq(midi, n=N_FFT, sr=SR):
    """取最接近该 MIDI 音高的、能整除 FFT 的频率。"""
    target = dsp.midi_to_hz(midi)
    m = max(1, int(round(target * n / sr)))
    return m * sr / n


print("  相干采样（频率对齐 FFT bin，无窗泄漏）—— 非谐波能量，越低越干净")
for wave in ("saw", "square", "triangle"):
    for midi in (33, 60, 84, 100):
        f = coherent_freq(midi)
        x = dsp.render_osc(wave, np.full(N_FFT, f), SR)
        db = interharmonic_db(x, f)
        legit = int((SR * 0.5) // f)
        check(f"{wave:9s} MIDI {midi:3d} ({f:8.1f}Hz, {legit:3d} 谐波)",
              db < -80.0, f"非谐波能量 {db:7.1f} dB")

print()
print("  参照 A：理想加法合成（数学上完全带限，应接近数值底噪）")
for wave in ("saw", "square"):
    f = coherent_freq(100)
    t = np.arange(N_FFT) / SR
    h = int((SR * 0.5) // f)
    if wave == "saw":
        k = np.arange(1, h + 1, dtype=np.float64)
        ideal = (np.sin(2 * np.pi * np.outer(t, k) * f) / k).sum(axis=1)
    else:
        k = np.arange(1, h + 1, 2, dtype=np.float64)
        ideal = (np.sin(2 * np.pi * np.outer(t, k) * f) / k).sum(axis=1)
    ideal /= np.abs(ideal).max()
    print(f"         {wave:12s} MIDI 100: 非谐波能量 {interharmonic_db(ideal.astype(np.float32), f):7.1f} dB")

print()
print("  参照 B：未做带限的 naive 波形（真实混叠长什么样）")
for wave in ("naive saw", "naive square"):
    for midi in (84, 100):
        f = coherent_freq(midi)
        t = np.arange(N_FFT) / SR
        if "saw" in wave:
            naive = 2.0 * ((t * f) % 1.0) - 1.0
        else:
            naive = np.sign(np.sin(2 * np.pi * f * t))
        print(f"         {wave:12s} MIDI {midi:3d}: 非谐波能量 "
              f"{interharmonic_db(naive.astype(np.float32), f):7.1f} dB")

print()
print("=" * 68)
print("2) 包络")
print("=" * 68)
env = dsp.adsr(SR, SR, 0.01, 0.1, 0.6, 0.2)
check("ADSR 峰值 = 1", abs(float(env.max()) - 1.0) < 0.02, f"{env.max():.4f}")
check("ADSR 从 0 起", float(env[0]) < 0.01, f"{env[0]:.4f}")
check("ADSR 落到 0", float(env[-1]) < 0.01, f"{env[-1]:.4f}")
sus_idx = int(0.4 * SR)
check("ADSR 延音段 ≈ sustain", abs(float(env[sus_idx]) - 0.6) < 0.05, f"{env[sus_idx]:.4f}")

penv = dsp.percussive_env(int(0.5 * SR), SR, 0.001, 0.2)
check("打击乐包络单调衰减", float(penv[100]) > float(penv[-1]), f"{penv[100]:.3f} → {penv[-1]:.4f}")

print()
print("=" * 68)
print("3) 滤波器")
print("=" * 68)
n = SR
t = np.arange(n) / SR
sig = (np.sin(2 * np.pi * 100 * t) + np.sin(2 * np.pi * 8000 * t)).astype(np.float32)

lp = dsp.lowpass_static(sig, 500.0, 0.707, SR)
spec = np.abs(np.fft.rfft(lp)) / n
freqs = np.fft.rfftfreq(n, 1.0 / SR)
i100 = int(np.argmin(np.abs(freqs - 100)))
i8k = int(np.argmin(np.abs(freqs - 8000)))
check("低通保留 100Hz", spec[i100] > 0.3, f"幅度 {spec[i100]:.3f}")
check("低通滤掉 8kHz", spec[i8k] < 0.02, f"幅度 {spec[i8k]:.5f}")
check("滤波后无 NaN/Inf", bool(np.isfinite(lp).all()))

# 时变扫频必须稳定（不能自激）
sweep = np.geomspace(200.0, 12000.0, n)
tv = dsp.resonant_lowpass(sig, sweep, q=6.0, sr=SR)
check("时变共振扫频稳定", bool(np.isfinite(tv).all()) and dsp.peak(tv) < 12.0,
      f"峰值 {dsp.peak(tv):.3f}")

print()
print("=" * 68)
print("4) 卷积与非线性")
print("=" * 68)
a = np.random.default_rng(1).standard_normal(1000).astype(np.float32)
b = np.random.default_rng(2).standard_normal(64).astype(np.float32)
fast = dsp.fft_convolve(a, b)
ref = np.convolve(a.astype(np.float64), b.astype(np.float64))[:1000]
err = float(np.max(np.abs(fast - ref)))
check("FFT 卷积 == 直接卷积", err < 1e-3, f"最大误差 {err:.2e}")

sc = dsp.soft_clip(np.linspace(-3, 3, 2001).astype(np.float32), drive=2.0)
check("软削波有界且单调", float(np.abs(sc).max()) <= 1.001 and bool(np.all(np.diff(sc) >= -1e-6)),
      f"最大 {np.abs(sc).max():.4f}")

print()
print("=" * 68)
print("5) 性能（离线渲染预算）")
print("=" * 68)
N = SR * 8  # 8 秒单声道

t0 = time.perf_counter()
dsp.render_osc("saw", np.full(N, 220.0), SR)
t_osc = time.perf_counter() - t0

t0 = time.perf_counter()
dsp.lowpass_static(np.random.default_rng(3).standard_normal(N).astype(np.float32), 2000.0, 0.707, SR)
t_lp = time.perf_counter() - t0

t0 = time.perf_counter()
dsp.resonant_lowpass(np.random.default_rng(4).standard_normal(N).astype(np.float32),
                     np.geomspace(300, 6000, N), 4.0, SR)
t_svf = time.perf_counter() - t0

t0 = time.perf_counter()
dsp.fft_convolve(np.random.default_rng(5).standard_normal(N).astype(np.float32),
                 np.random.default_rng(6).standard_normal(SR * 2).astype(np.float32))
t_rev = time.perf_counter() - t0

print(f"  振荡器 8s        : {t_osc * 1000:7.1f} ms")
print(f"  静态低通 8s      : {t_lp * 1000:7.1f} ms")
print(f"  时变 SVF 8s      : {t_svf * 1000:7.1f} ms  ({t_svf / N * 1e9:.0f} ns/样本)")
print(f"  2s IR 卷积 8s    : {t_rev * 1000:7.1f} ms")

check("振荡器够快", t_osc < 0.5)
check("静态滤波够快", t_lp < 0.5)
check("时变 SVF 可接受", t_svf < 4.0)
check("长 IR 卷积够快", t_rev < 1.0)

print()
print("=" * 68)
print("全部通过" if ok else "存在失败项")
print("=" * 68)
raise SystemExit(0 if ok else 1)
