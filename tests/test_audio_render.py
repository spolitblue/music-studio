"""端到端渲染验证：Demo 工程 → 立体声 → WAV / MIDI。

同时检查音频健康度（爆音、NaN、直流偏置、立体声相关性），
这是"能不能真正用"的硬指标。
"""

import json
import time
from pathlib import Path

import numpy as np

# --- 项目根路径引导（本文件位于子目录，保证从任何 cwd 都能 import app.*）---
import sys as _sys
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parent.parent
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))
# --- 引导结束 ---

from app.audio import dsp, render, wavio
from app.audio.demo import demo_project, empty_project

ok = True


def check(label, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print(f"  [{'OK  ' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")


OUT = Path("_out")
OUT.mkdir(exist_ok=True)

print("=" * 70)
print("1) 构建示范工程")
print("=" * 70)
p = demo_project()
total_notes = sum(len(t.notes) for t in p.tracks)
print(f"  名称    : {p.name}")
print(f"  速度    : {p.bpm} BPM, {p.bars} 小节, 每小节 {p.steps_per_bar} 步")
print(f"  时长    : {p.duration_seconds():.2f} 秒")
print(f"  轨道    : {len(p.tracks)} 条, 共 {total_notes} 个音符")
for t in p.tracks:
    print(f"            [{t.kind:5s}] {t.name:6s} {len(t.notes):4d} 音符  "
          f"音色={t.synth.wave if t.kind == 'synth' else 'drum':8s} 图标={t.icon}")

check("有轨道", len(p.tracks) >= 4)
check("有音符", total_notes >= 100)

print()
print("=" * 70)
print("2) 渲染")
print("=" * 70)
t0 = time.perf_counter()
steps = []


def prog(frac, label):
    steps.append((frac, label))


audio, stats = render.render_project(p, progress=prog)
wall = time.perf_counter() - t0

dur = audio.shape[1] / stats.sample_rate
print(f"  输出    : {audio.shape[0]} 声道 × {audio.shape[1]} 样本 "
      f"= {dur:.2f} 秒 @ {stats.sample_rate} Hz")
print(f"  渲染耗时: {stats.seconds:.2f} 秒（实时倍速 {dur / max(1e-6, stats.seconds):.1f}×）")
print(f"  峰值    : {stats.peak_db:+.2f} dBFS     有效值: {stats.rms_db:+.2f} dBFS")
print(f"  限幅    : {'已介入' if stats.limited else '未介入'}")

check("渲染无 NaN/Inf", bool(np.isfinite(audio).all()))
check("无削波（峰值 <= -0.05dBFS）", stats.peak <= dsp.db_to_gain(-0.05),
      f"{stats.peak_db:+.2f} dBFS")
check("响度合理（峰值 > -12dBFS）", stats.peak_db > -12.0, f"{stats.peak_db:+.2f} dBFS")
check("有效值合理（-30 ~ -6 dBFS）", -30.0 < stats.rms_db < -6.0, f"{stats.rms_db:+.2f} dBFS")
check("渲染快于实时", stats.seconds < dur, f"{stats.seconds:.2f}s vs {dur:.2f}s")

print()
print("=" * 70)
print("3) 音频健康度")
print("=" * 70)
dc = float(np.abs(audio.mean(axis=1)).max())
check("直流偏置可忽略", dc < 2e-4, f"{dc:.2e}")

# 立体声相关度本身不是指标 —— 宽立体声（乒乓延迟、unison 展开）本就该解相关，
# 甚至略负。真正要保证的是**单声道兼容性**：折叠成单声道时不能掉太多电平。
l, r = audio[0], audio[1]
corr = float(np.corrcoef(l, r)[0, 1]) if l.std() > 1e-9 and r.std() > 1e-9 else 1.0
mono = (l + r) * 0.5
mono_loss_db = dsp.gain_to_db(dsp.rms(mono)) - dsp.gain_to_db(dsp.rms(audio))
# 解相关的立体声折叠成单声道天然损失 3dB；理论值 = 10·log10((1+c)/2)
expected_loss = 10.0 * np.log10(max(1e-6, (1.0 + corr) / 2.0))
check("立体声相关度在合理区间", -0.6 < corr < 0.99, f"{corr:.3f}（宽立体声本来就该偏低）")
check("单声道折叠损失符合理论", abs(mono_loss_db - expected_loss) < 0.3,
      f"实测 {mono_loss_db:+.2f} dB / 理论 {expected_loss:+.2f} dB")
check("单声道兼容性良好", mono_loss_db > -4.5, f"{mono_loss_db:+.2f} dB")

# 每个声道都要有内容
check("左右声道都有信号", float(np.abs(l).max()) > 0.05 and float(np.abs(r).max()) > 0.05,
      f"L={np.abs(l).max():.3f} R={np.abs(r).max():.3f}")

# 分段电平：不应有整段静音（说明某个编曲段落没出声）
seg = dur / 8
levels = [float(np.sqrt(np.mean(audio[:, int(i * seg * stats.sample_rate):
                                       int((i + 1) * seg * stats.sample_rate)] ** 2)))
          for i in range(8)]
check("全程无静音段", min(levels) > 1e-3,
      " ".join(f"{dsp.gain_to_db(v):+.0f}" for v in levels) + " dB")

# 起始与结束应已淡入淡出
check("开始无爆音", float(np.abs(audio[:, 0]).max()) < 0.02, f"{np.abs(audio[:, 0]).max():.4f}")
check("结束无爆音", float(np.abs(audio[:, -1]).max()) < 0.02, f"{np.abs(audio[:, -1]).max():.4f}")

print()
print("=" * 70)
print("4) 导出 WAV / MIDI / 工程")
print("=" * 70)
for bits in (16, 24, 32):
    f = wavio.save_wav(OUT / f"demo_{bits}bit.wav", audio, stats.sample_rate, bits)
    back, sr = wavio.load_wav(f)
    n = min(back.shape[1], audio.shape[1])
    # 16bit 往返误差应约等于量化噪声（~1/32768）
    err = float(np.max(np.abs(back[:, :n] - audio[:, :n])))
    limit = {16: 2e-4, 24: 1e-6, 32: 1e-6}[bits]
    check(f"{bits} bit 往返", back.shape[0] == 2 and sr == stats.sample_rate and err < limit,
          f"{f.stat().st_size / 1024:.0f} KB, 最大误差 {err:.2e}")

mid = wavio.save_midi(OUT / "demo.mid", p)
head = mid.read_bytes()[:4]
check("MIDI 文件头正确", head == b"MThd", f"{mid.stat().st_size} 字节")

proj_path = p.save(OUT / "demo.musicproj.json")
loaded = type(p).load(proj_path)
check("工程往返：轨道数一致", len(loaded.tracks) == len(p.tracks))
check("工程往返：音符数一致",
      sum(len(t.notes) for t in loaded.tracks) == total_notes)
check("工程往返：音色参数一致",
      loaded.tracks[1].synth.wave == p.tracks[1].synth.wave
      and abs(loaded.tracks[1].synth.filter.cutoff - p.tracks[1].synth.filter.cutoff) < 1e-6)
check("工程往返：鼓件参数一致",
      abs(loaded.tracks[0].kit["kick"].decay - p.tracks[0].kit["kick"].decay) < 1e-9)

print()
print("=" * 70)
print("5) 改动响应（改一个音就重渲染）")
print("=" * 70)
p2 = demo_project()
p2.tracks = [t for t in p2.tracks if t.kind == "drum"]
t0 = time.perf_counter()
a2, s2 = render.render_project(p2)
dt = time.perf_counter() - t0
check("纯鼓组渲染够快", dt < 3.0, f"{dt:.2f} 秒, 峰值 {s2.peak_db:+.2f} dBFS")

e = empty_project()
t0 = time.perf_counter()
a3, s3 = render.render_project(e)
dt = time.perf_counter() - t0
check("空工程不崩", a3.shape[0] == 2 and bool(np.isfinite(a3).all()), f"{dt:.3f} 秒")

print()
print(f"  产出文件在 {OUT.resolve()}")
for f in sorted(OUT.iterdir()):
    print(f"    {f.name:28s} {f.stat().st_size / 1024:9.1f} KB")

print()
print("=" * 70)
print("全部通过" if ok else "存在失败项")
print("=" * 70)
raise SystemExit(0 if ok else 1)
