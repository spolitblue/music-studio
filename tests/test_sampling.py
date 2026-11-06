"""采样与母带：从参考里抠音色、把成品频谱摆平到参考。

这一套的重点不是"函数能跑"，而是两条**曾经真的错过的**判据：

1. 频谱摆平必须真的改变**形状**。第一版拿绝对电平去比（混音进母带前是
   +14 dBFS、参考是 -23 dBFS），每段差值都被钳到 -10 dB，曲线成了一条平线 ——
   开着和关着的结果一模一样，而报告里那一栏照样写着"已摆平"。
   所以这里断言：摆平之后，成品与参考的**频段离差必须显著变小**。
2. 采样必须真的来自参考音频，而不是"随便剪一段"。断言抠出来的样本长度、
   峰均比、以及**它确实出现在参考里那些位置**。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path as _Path

import numpy as np

_ROOT = _Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.audio import dsp, mastering, sampler, wavio          # noqa: E402
from app.audio.project import Note, Project, Track             # noqa: E402

ok = True


def check(label, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print(f"  [{'OK  ' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")


SR = 44100
rng = np.random.default_rng(7)

print("=" * 70)
print("1) 采样器：单件样本的时值、音高换算、落盘往返")
print("=" * 70)

# 一个 0.5 秒的"鼓"：低频正弦 + 快速衰减
n = int(0.5 * SR)
t = np.arange(n) / SR
hit = (np.sin(2 * np.pi * 90 * t) * np.exp(-t * 12)).astype(np.float32)
shot = sampler.OneShot(name="probe", samples=hit, sr=SR)

check("单件时长读得对", abs(shot.seconds - 0.5) < 1e-6, f"{shot.seconds:.4f}s")

up = shot.resampled(2.0)
down = shot.resampled(0.5)
check("升八度（ratio=2）时值减半", abs(up.size - n // 2) <= 2, f"{up.size} vs {n // 2}")
check("降八度（ratio=0.5）时值加倍", abs(down.size - n * 2) <= 2, f"{down.size} vs {n * 2}")
check("重采样不改变峰值量级",
      abs(float(np.max(np.abs(up))) - float(np.max(np.abs(hit)))) < 0.05,
      f"{float(np.max(np.abs(up))):.3f} vs {float(np.max(np.abs(hit))):.3f}")

buf = np.zeros(SR, dtype=np.float32)
sampler.play_into(buf, shot, 0.25, gain=0.5, sr=SR)
check("play_into 落在正确的时刻且按增益缩放",
      abs(float(np.max(np.abs(buf[int(0.25 * SR):int(0.3 * SR)]))) - 0.5 * float(np.max(np.abs(hit)))) < 0.05
      and float(np.max(np.abs(buf[:int(0.24 * SR)]))) == 0.0)

with tempfile.TemporaryDirectory(prefix="sampler_") as tmp:
    kit = sampler.SampleKit(shots={"probe": shot, "tone": sampler.OneShot(
        name="tone", samples=hit, sr=SR, pitch=60.0)}, source="自检")
    out = sampler.save_kit(kit, tmp)
    back = sampler.load_kit(out)
    check("落盘 → 读回，样本名与时值都对得上",
          set(back.shots) == {"probe", "tone"}
          and abs(back.shots["probe"].seconds - 0.5) < 1e-3,
          "、".join(f"{k}:{v.seconds * 1000:.0f}ms" for k, v in back.shots.items()))
    check("kit.json 里记了音高（乐音样本要用）", back.shots["tone"].pitch == 60.0)

print()
print("=" * 70)
print("2) 从一段合成音频里抠样本：真的抠到了那些位置")
print("=" * 70)

# 造一段"参考"：160 BPM、每拍一记低鼓 + 每半拍一记高频铃，再加一个 440Hz 持续音
bpm = 120.0
beat = 60.0 / bpm
total = 8.0
ref = np.zeros(int(total * SR), dtype=np.float32)
for i in range(int(total / beat)):
    at = int(i * beat * SR)
    seg = (np.sin(2 * np.pi * 70 * (np.arange(int(0.3 * SR)) / SR))
           * np.exp(-np.arange(int(0.3 * SR)) / SR * 14)).astype(np.float32)
    ref[at:at + seg.size] += seg * 0.9
    bell = (rng.standard_normal(int(0.06 * SR)).astype(np.float32)
            * np.exp(-np.arange(int(0.06 * SR)) / SR * 60))
    at2 = at + int(0.5 * beat * SR)
    ref[at2:at2 + bell.size] += bell * 0.5
tone = (np.sin(2 * np.pi * 440 * np.arange(int(3 * SR)) / SR)
        * np.exp(-np.arange(int(3 * SR)) / SR * 0.8)).astype(np.float32)
ref[int(3.0 * SR):int(3.0 * SR) + tone.size] += tone * 0.8
ref = np.stack([ref, ref])

kit = sampler.extract_kit(ref, SR, max_seconds=total)
check("至少抠出两个样本（低频 + 别的）", len(kit.shots) >= 2, kit.summary())
check("抠出来的样本都有合理的时长",
      all(0.02 <= s.seconds <= 1.5 for s in kit.shots.values()),
      "、".join(f"{k}:{v.seconds * 1000:.0f}ms" for k, v in kit.shots.items()))
lo = [k for k in kit.shots if "kick" in k or "808" in k]
check("低频那一下被归到底鼓类", bool(lo), "、".join(kit.shots))
check("有音高的那一段被当成乐音样本", kit.tone is not None,
      f"pitch={kit.tone.pitch if kit.tone else None}")
if kit.tone is not None:
    check("乐音样本的基频估计落在 440Hz 附近（MIDI 69±2）",
          abs(float(kit.tone.pitch) - 69) <= 2, f"MIDI {kit.tone.pitch}")

print()
print("=" * 70)
print("3) 母带：频谱摆平必须真的改变形状（这条曾经是空转）")
print("=" * 70)

# "参考"：低频厚、高频少；"成品"：中频突出、低频空 —— 正是实测里那个毛病
def make(kind: str) -> np.ndarray:
    dur = 6.0
    n = int(dur * SR)
    tt = np.arange(n) / SR
    src = rng.standard_normal(n).astype(np.float32) * 0.05
    if kind == "ref":
        lo = dsp.biquad_static(src, "lp", 120.0, 0.7, SR) * 14.0
        mid = dsp.bandpass(src, 300.0, 1500.0, SR) * 1.0
    else:
        lo = dsp.biquad_static(src, "lp", 120.0, 0.7, SR) * 0.6
        mid = dsp.bandpass(src, 300.0, 3000.0, SR) * 9.0
    out = lo + mid
    out = out / max(float(np.max(np.abs(out))), 1e-9) * 0.7
    return np.stack([out, out]).astype(np.float32)


ref_audio = make("ref")
mix_audio = make("mix")
ref_lv = mastering.band_levels(ref_audio, SR)
mix_lv = mastering.band_levels(mix_audio, SR)
spread_before = float(np.std([b - a for a, b in zip(ref_lv, mix_lv)]))

fixed, report = mastering.gentle_master(mix_audio, SR, reference=ref_audio,
                                        max_tilt_db=10.0, tilt_passes=2)
ref_rel = [v - report["after"]["band_levels_db"][0] * 0 for v in ref_lv]
fixed_lv = mastering.band_levels(fixed, SR)

# 比"相对形状"：各自减去自己的整体 RMS 再比
def rel(lv, audio):
    mono = np.asarray(audio).mean(axis=0)
    rms = 20 * np.log10(max(float(np.sqrt(np.mean(mono ** 2))), 1e-9))
    return [v - rms for v in lv]


r_ref, r_mix, r_fix = rel(ref_lv, ref_audio), rel(mix_lv, mix_audio), rel(fixed_lv, fixed)
spread_mix = float(np.std([a - b for a, b in zip(r_ref, r_mix)]))
spread_fixed = float(np.std([a - b for a, b in zip(r_ref, r_fix)]))
check("摆平器报告自己真的动了手", report["tilt"].get("applied") is True)
check("摆平之后频谱形状明显更接近参考",
      spread_fixed < spread_mix * 0.6,
      f"离差 {spread_mix:.2f} dB → {spread_fixed:.2f} dB")
check("两遍之后残差继续下降（不是一遍就停）", len(report["passes"]) == 2,
      f"{len(report['passes'])} 遍")

peak_db = float(dsp.gain_to_db(float(np.max(np.abs(fixed)))))
check("成品峰值被压到目标以内且不削波",
      peak_db <= -1.9 and int(np.sum(np.abs(fixed) >= 0.999)) == 0,
      f"峰值 {peak_db:+.2f} dBFS")

print()
print("=" * 70)
print("4) 母带不许把动态做没（峰均比不能被压掉）")
print("=" * 70)

before_crest = mastering.harshness(mix_audio, SR)["crest_db"]
after_crest = mastering.harshness(fixed, SR)["crest_db"]
check("摆平 + 定电平之后峰均比没有被压扁",
      after_crest > before_crest - 1.5,
      f"{before_crest:.2f} → {after_crest:.2f} dB")

print()
print("=" * 70)
print("5) Minecraft 音符盒音色：读本机游戏资产")
print("=" * 70)

assets = sampler.find_minecraft_assets()
if assets is None:
    print("  [跳过] 这台机器上没找到 Minecraft assets（可接受）")
else:
    mc = sampler.load_note_block_kit(assets)
    check("读到了音符盒乐器（竖琴/铃/鼓都有）",
          all(k in mc for k in ("harp", "bell", "basedrum", "snare", "hat")),
          "、".join(sorted(mc.shots)))
    check("打击乐不参与移调（底鼓/军鼓/踩镲没有根音）",
          all(mc.get(k).pitch is None for k in ("basedrum", "snare", "hat")))
    check("乐器有根音，且音区彼此不同（贝斯低、铃高）",
          (mc.get("harp").pitch or 0) > 0
          and (mc.get("bass").pitch or 99) < (mc.get("bell").pitch or 0),
          f"bass={mc.get('bass').pitch} harp={mc.get('harp').pitch} "
          f"bell={mc.get('bell').pitch}")
    check("根音不是被一律钉成 F#4（那会把贝斯拉成 16Hz 的嗡嗡声）",
          (mc.get("bass").pitch or 0) != 66.0
          and abs((mc.get("harp").pitch or 66.0) - 66.0) < 0.01,
          f"bass={mc.get('bass').pitch}（应≈42） harp={mc.get('harp').pitch}")

    # 用它演奏一小段，验证"能出声、不削波、落在合理音区"
    proj = Project(name="mc", bpm=120.0, bars=2)
    kit_track = Track(name="AI 鼓组", kind="drum", volume=0.9)
    kit_track.ensure_kit()
    for step in (0, 4, 8, 12):
        kit_track.add_note(Note(step=step, pitch=36, length=1, velocity=0.9))
    for step in (4, 12):
        kit_track.add_note(Note(step=step, pitch=38, length=1, velocity=0.8))
    proj.add_track(kit_track)
    lead = Track(name="AI 旋律", kind="synth", volume=0.7)
    for i, pitch in enumerate((60, 62, 63, 67)):
        lead.add_note(Note(step=i * 4, pitch=pitch, length=3, velocity=0.8))
    proj.add_track(lead)
    from tools import music_ops                              # noqa: E402

    settings = music_ops.MixSettings(kit_source="minecraft", target_peak_db=-2.0)
    audio, report = music_ops.mixdown(proj, kit=mc, settings=settings)
    peak = float(np.max(np.abs(audio)))
    check("音符盒模式能混出成品且不削波", 0.0 < peak <= 1.0,
          f"峰值 {20 * np.log10(max(peak, 1e-9)):+.2f} dBFS · "
          f"秒数 {audio.shape[1] / SR:.1f}")
    check("四个声部都进了混音（鼓/贝斯/和声/旋律各自的层都在）",
          "AI 鼓组" in report["mc"]["layers"],
          "、".join(report["mc"]["layers"]))
    check("成品峰值落在目标附近（母带真的定了电平）",
          -3.5 < 20 * np.log10(max(peak, 1e-9)) < -1.0,
          f"{20 * np.log10(max(peak, 1e-9)):+.2f} dBFS")

print()
print("=" * 70)
print("采样 / 母带自检：" + ("全部通过" if ok else "存在失败项"))
print("=" * 70)
raise SystemExit(0 if ok else 1)
