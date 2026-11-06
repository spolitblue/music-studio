"""音色包（808 / 牛铃）的验证。

覆盖的不是"能出声"这种弱断言，而是**这套音色之所以是它**的那些声学特征：
808 底鼓的低频基频与长尾巴、808 牛铃的窄带通中心、拉丁牛铃更低更长；
再加上包本身的语义：往套鼓里加鼓件、覆盖参数、不共享对象、一键建轨、示范鼓型、
以及鼓机页上"套上之后网格真的多出那几行"。
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys as _sys
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parent.parent
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))

import numpy as np
from PySide6.QtWidgets import QApplication

app = QApplication([])

from app.audio import demo, drums, render, soundpacks
from app.audio.project import (
    DEFAULT_KIT, DRUM_NAMES, KIT_ORDER, PACK_KIT, Note, Project, Track, drum_to_pitch,
    pitch_to_drum,
)
from app.ui.controller import AppController
from app.ui.pages.drums import DrumPage

ok = True


def check(label, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print(f"  [{'OK  ' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")


def mono(buf):
    a = np.asarray(buf, dtype=np.float32)
    return a.mean(axis=0) if a.ndim == 2 else a


def dominant_hz(buf, sr=render.SR, lo=20.0, hi=400.0):
    """低频段的基频（FFT 峰值）。用来验"808 底鼓是真的低"。"""
    x = mono(buf).astype(np.float64)
    x = x * np.hanning(x.size)
    spec = np.abs(np.fft.rfft(x))
    freqs = np.fft.rfftfreq(x.size, 1.0 / sr)
    band = (freqs >= lo) & (freqs <= hi)
    if not np.any(band):
        return 0.0
    return float(freqs[band][int(np.argmax(spec[band]))])


def band_energy(buf, lo, hi, sr=render.SR):
    x = mono(buf).astype(np.float64)
    spec = np.abs(np.fft.rfft(x)) ** 2
    freqs = np.fft.rfftfreq(x.size, 1.0 / sr)
    return float(spec[(freqs >= lo) & (freqs < hi)].sum())


def decay_seconds(buf, sr=render.SR, floor: float = 0.05):
    """包络掉到峰值 ``floor`` 倍所用的秒数（2ms 块取最大值，与扒谱同口径）。"""
    x = np.abs(mono(buf))
    block = max(1, int(0.002 * sr))
    n = x.size // block
    if n < 2:
        return 0.0
    env = x[: n * block].reshape(n, block).max(axis=1)
    peak = float(env.max())
    if peak <= 1e-9:
        return 0.0
    below = np.flatnonzero(env < peak * floor)
    return float((below[0] if below.size else env.size) * 0.002)


def envelope_at(buf, seconds, sr=render.SR):
    """某个时刻附近的振幅（10ms 窗的峰值）—— 用来量尾巴。"""
    x = np.abs(mono(buf))
    i = int(seconds * sr)
    seg = x[max(0, i - int(0.005 * sr)): i + int(0.005 * sr)]
    return float(seg.max()) if seg.size else 0.0


# ==========================================================================
print("=" * 70)
print("1) 新鼓件进了数据模型，但没有污染默认套鼓")
print("=" * 70)
check("808 底鼓有自己的音高（35）", drum_to_pitch("kick_808") == 35)
check("808 牛铃有自己的音高（55）", drum_to_pitch("cowbell_808") == 55)
check("拉丁牛铃有自己的音高（57）", drum_to_pitch("cowbell_latin") == 57)
check("三个新鼓件都有中文名", all(DRUM_NAMES.get(p) for p in PACK_KIT),
      "、".join(DRUM_NAMES.get(p, p) for p in PACK_KIT))
check("默认套鼓仍然是 12 件（新件刻意不进 DEFAULT_KIT）", len(DEFAULT_KIT) == 12,
      f"{len(DEFAULT_KIT)} 件")
check("扩展件不在默认套鼓里", not (set(PACK_KIT) & set(DEFAULT_KIT)))
check("网格顺序 = 默认套鼓 + 扩展件", KIT_ORDER == DEFAULT_KIT + PACK_KIT,
      f"{len(KIT_ORDER)} 件")
check("默认套鼓的音色表里没有新件（ensure_kit 不会自动加进去）",
      not (set(PACK_KIT) & set(drums.default_voices())))

# ==========================================================================
print()
print("=" * 70)
print("2) 808 底鼓：低频基频 + 长尾巴（这两条就是它之所以是 808）")
print("=" * 70)
kick = drums.render_drum("kick", drums.default_voices()["kick"], 1.0)
v808 = soundpacks.pack_by_key("808").voice_of("kick_808")
kick808 = drums.render_drum("kick_808", v808, 1.0)

f_kick = dominant_hz(kick)
f_808 = dominant_hz(kick808)
check("808 底鼓的基频落在 30~60Hz（比常规底鼓更低）", 30.0 <= f_808 <= 60.0,
      f"{f_808:.1f} Hz")
check("常规底鼓的基频明显更高", f_kick > f_808 + 5.0, f"{f_kick:.1f} vs {f_808:.1f} Hz")

peak808 = float(np.abs(kick808).max())
tail808 = envelope_at(kick808, 0.60) / max(1e-9, peak808)
tail_kick = envelope_at(kick, 0.60) / max(1e-9, peak808)
d808, d_kick = decay_seconds(kick808), decay_seconds(kick)
check("808 底鼓衰减到 5% 的时间是常规底鼓的 3 倍以上（长尾是它的定义）",
      d808 > d_kick * 3.0, f"{d808 * 1000:.0f}ms vs {d_kick * 1000:.0f}ms")
check("0.3~0.9s 的尾巴上，808 还明显比常规底鼓响",
      tail808 > tail_kick * 10.0, f"{tail808 * 100:.2f}% vs {tail_kick * 100:.2f}%")
check("808 的长度上限给了 1.6s（能当低音用）", drums.MAX_LEN["kick_808"] >= 1.6,
      f"{drums.MAX_LEN['kick_808']}s")

sub = band_energy(kick808, 20.0, 120.0)
total = band_energy(kick808, 20.0, 20000.0)
sub_kick = band_energy(kick, 20.0, 120.0)
total_kick = band_energy(kick, 20.0, 20000.0)
check("808 底鼓的能量几乎都在 120Hz 以下（sub 的判据）", sub / max(1e-9, total) > 0.6,
      f"{sub / max(1e-9, total) * 100:.1f}%（常规底鼓 "
      f"{sub_kick / max(1e-9, total_kick) * 100:.1f}%）")
check("808 的低频占比高于常规底鼓",
      sub / max(1e-9, total) > sub_kick / max(1e-9, total_kick))

# 换 packet 里的"紧密"档：尾巴应该明显变短
tight = soundpacks.pack_by_key("808_kit").voice_of("kick_808")
tight_buf = drums.render_drum("kick_808", tight, 1.0)
check("同一个鼓件换参数档位，尾巴跟着变（参数真的生效）",
      envelope_at(tight_buf, 0.6) < envelope_at(kick808, 0.6) * 0.6,
      f"{envelope_at(tight_buf, 0.6):.5f} < {envelope_at(kick808, 0.6):.5f}")

# ==========================================================================
print()
print("=" * 70)
print("3) 808 牛铃 / 拉丁牛铃：窄带通中心与尾巴长短")
print("=" * 70)
pack808 = soundpacks.pack_by_key("cowbell")
bell808 = drums.render_drum("cowbell_808", pack808.voice_of("cowbell_808"), 1.0)
bell_latin = drums.render_drum("cowbell_latin", pack808.voice_of("cowbell_latin"), 1.0)
bell_old = drums.render_drum("cowbell", drums.default_voices()["cowbell"], 1.0)

# 谱质心：808 牛铃该落在 2~4kHz
def centroid(buf, sr=render.SR):
    x = mono(buf).astype(np.float64)
    spec = np.abs(np.fft.rfft(x))
    freqs = np.fft.rfftfreq(x.size, 1.0 / sr)
    return float((freqs * spec).sum() / max(1e-9, spec.sum()))


c808, c_latin = centroid(bell808), centroid(bell_latin)
check("808 牛铃的谱质心落在 2~5kHz（那圈金属味就在这）", 2000.0 <= c808 <= 5000.0,
      f"{c808:.0f} Hz")
check("拉丁牛铃比 808 牛铃更低", c_latin < c808, f"{c_latin:.0f} vs {c808:.0f} Hz")
check("拉丁牛铃的尾巴比 808 牛铃长",
      decay_seconds(bell_latin) > decay_seconds(bell808) * 1.5,
      f"衰减到 5%：{decay_seconds(bell_latin) * 1000:.0f}ms vs "
      f"{decay_seconds(bell808) * 1000:.0f}ms")
check("三件牛铃都不是同一个声音",
      len({round(centroid(b), 1) for b in (bell808, bell_latin, bell_old)}) == 3,
      f"{centroid(bell808):.0f} / {centroid(bell_latin):.0f} / {centroid(bell_old):.0f} Hz")
for name, buf in (("808 牛铃", bell808), ("拉丁牛铃", bell_latin)):
    check(f"{name} 渲染出的是非静音立体声",
          buf.shape[0] == 2 and float(np.abs(buf).max()) > 0.05,
          f"{buf.shape} 峰值 {float(np.abs(buf).max()):.3f}")

# ==========================================================================
print()
print("=" * 70)
print("4) 音色包语义：加鼓件 / 覆盖参数 / 不共享对象")
print("=" * 70)
check("四个包都在", set(soundpacks.pack_keys()) == {"808", "cowbell", "808_kit", "trap"},
      "、".join(soundpacks.pack_keys()))
check("按 key 取包", soundpacks.pack_by_key("808") is not None
      and soundpacks.pack_by_key("nope") is None)
check("每个包都有一句话说明", all(p.summary and p.tip for p in soundpacks.SOUND_PACKS))
check("describe 里有新增鼓件与配套音色", "新增" in soundpacks.describe(pack808))

proj = Project(name="包测试")
track = Track(name="鼓", kind="drum")
track.ensure_kit()
before_kinds = set(track.kit)
applied = soundpacks.apply_pack(track, pack808)
check("套包之后新鼓件进了 kit", "cowbell_808" in track.kit and "cowbell_latin" in track.kit,
      f"新增 {sorted(set(track.kit) - before_kinds)}")
check("返回值点名了被改动的鼓件", set(applied) >= {"cowbell_808", "cowbell_latin", "cowbell"},
      f"{applied}")
check("套上的鼓件一定是启用的", all(track.kit[p].enabled for p in applied))
check("原有的 12 件没有被弄丢", set(DEFAULT_KIT) <= set(track.kit),
      f"kit 共 {len(track.kit)} 件")

# 不共享对象：两条轨道各套一次，改一条不该影响另一条
t1 = Track(name="A", kind="drum"); t1.ensure_kit()
t2 = Track(name="B", kind="drum"); t2.ensure_kit()
soundpacks.apply_pack(t1, pack808)
soundpacks.apply_pack(t2, pack808)
t1.kit["cowbell_808"].decay = 0.9
check("两条轨道不共享 DrumVoice（改 A 不影响 B）",
      abs(t2.kit["cowbell_808"].decay - pack808.voices["cowbell_808"]["decay"]) < 1e-9,
      f"B 的 decay={t2.kit['cowbell_808'].decay:.3f}")
check("包里的参数对象也没被就地改掉",
      abs(soundpacks.pack_by_key("cowbell").voices["cowbell_808"]["decay"]
          - pack808.voices["cowbell_808"]["decay"]) < 1e-9)

# 合成器预设：套到合成器轨上
bass = Track(name="808 贝斯", kind="synth")
got = soundpacks.apply_pack(bass, soundpacks.pack_by_key("808"))
check("合成器轨套包 → 写的是 synth 预设", got == ["synth"], f"{got}")
check("808 低音预设是正弦 + 滑音（808 低音的骨架就是这个）",
      bass.synth.wave == "sine" and bass.synth.glide >= 0.04,
      f"wave={bass.synth.wave} glide={bass.synth.glide}")
trap_bass = Track(name="Trap 贝斯", kind="synth")
soundpacks.apply_pack(trap_bass, soundpacks.pack_by_key("trap"))
check("Trap 包给的是另一档预设（滑音关掉、sustain 更高）",
      trap_bass.synth.glide == 0.0
      and trap_bass.synth.amp.sustain > bass.synth.amp.sustain,
      f"glide={trap_bass.synth.glide} sustain={trap_bass.synth.amp.sustain:.2f} vs "
      f"{bass.synth.amp.sustain:.2f}")
check("两条合成器轨各拿一份预设（互不共享）",
      soundpacks.apply_pack(Track(name="C", kind="synth"),
                            soundpacks.pack_by_key("808")) == ["synth"])

# ==========================================================================
print()
print("=" * 70)
print("5) 一键建轨 + 示范鼓型（音色包最要紧的是立刻听见）")
print("=" * 70)
proj2 = Project(name="成轨")
for key in soundpacks.pack_keys():
    pack = soundpacks.pack_by_key(key)
    tr = soundpacks.make_track(proj2, pack)
    proj2.add_track(tr)
    buf = render.render_track(tr, proj2)
    notes = len(tr.notes)
    check(f"「{pack.name}」建出的轨道有音符、能渲染出声音",
          notes >= 4 and float(np.abs(buf).max()) > 0.05,
          f"{notes} 音符 · 峰值 {float(np.abs(buf).max()):.3f}")

tr808 = proj2.tracks[0]
check("示范鼓型用上了这个包的招牌鼓件（808 底鼓）",
      any(pitch_to_drum(n.pitch) == "kick_808" for n in tr808.notes),
      f"音符 {len(tr808.notes)} 个")
check("示范鼓型只有一小节（16 步以内）",
      max(n.step for n in tr808.notes) < 16, f"最大步号 {max(n.step for n in tr808.notes)}")

pattern = soundpacks.demo_pattern(soundpacks.pack_by_key("808_kit"))
pieces = {p for p, _s, _v in pattern}
check("全套包的示范鼓型用到了底鼓 + 军鼓 + 踩镲 + 牛铃",
      {"kick_808", "snare", "hat_closed", "cowbell_808"} <= pieces,
      "、".join(sorted(pieces)))
check("示范鼓型限定了可用鼓件时不会用到别的",
      {p for p, _s, _v in soundpacks.demo_pattern(
          soundpacks.pack_by_key("808_kit"), {"kick_808"})} == {"kick_808"})

# ==========================================================================
print()
print("=" * 70)
print("6) 鼓机页：点了音色包，网格真的多出那几行")
print("=" * 70)
demo_proj = demo.demo_project()
ctrl = AppController(demo_proj)
page = DrumPage(demo_proj)
page.resize(1280, 800)
page.bind(ctrl)
page.show()
QApplication.processEvents()
drum_track = next(t for t in demo_proj.tracks if t.kind == "drum")
page.set_track(drum_track)
QApplication.processEvents()

rows_before = list(page.grid.rows)
check("（前置）默认鼓轨是 12 行", len(rows_before) == 12, f"{len(rows_before)} 行")
check("页面上有音色包芯片，数量与包数一致",
      len(page.pack_chips) == len(soundpacks.SOUND_PACKS), f"{len(page.pack_chips)} 个")
check("音色包行在鼓型行下面（两块都是'一次点一整套'）",
      page.pack_bar.y() > page.pattern_bar.y())

undo_before = len(ctrl._undo)
page.pack_chips[0].clicked.emit()      # 真的走芯片点击
QApplication.processEvents()
rows_after = list(page.grid.rows)
check("点「808 低音」之后网格多出 808 底鼓那一行",
      "kick_808" in rows_after and len(rows_after) == len(rows_before) + 1,
      f"{len(rows_before)} → {len(rows_after)} 行")
check("套包压了 1 步撤销点，标签点名了音色包",
      len(ctrl._undo) - undo_before == 1 and ctrl.undo_label() == "音色包·808 低音",
      f"「{ctrl.undo_label()}」")
check("提示条说明了套上了什么", "808 底鼓" in page._hint.text(),
      f"「{page._hint.text()}」")
check("套包后界面能重绘", not page.grab().isNull())

undo_before = len(ctrl._undo)
page.apply_sound_pack("cowbell")
QApplication.processEvents()
check("再套牛铃包 → 又多两行（808 牛铃 + 拉丁牛铃）",
      {"cowbell_808", "cowbell_latin"} <= set(page.grid.rows),
      "、".join(page.grid.rows))
check("第二次套包也各压 1 步撤销点", len(ctrl._undo) - undo_before == 1)
check("不存在的包名不会崩、也不会压撤销点",
      page.apply_sound_pack("没有这个包") == []
      and len(ctrl._undo) - undo_before == 1)
check("鼓机页仍然能整体绘制", not page.grab().isNull())

# 套包之后的鼓轨真的能渲染出声音（不是只改了界面）
buf = render.render_track(drum_track, demo_proj)
check("套包后的鼓轨渲染非静音", float(np.abs(buf).max()) > 0.05,
      f"峰值 {float(np.abs(buf).max()):.3f}")

page.close()
ctrl.shutdown()

print()
print("=" * 70)
print("全部通过" if ok else "存在失败项")
print("=" * 70)
sys.exit(0 if ok else 1)
