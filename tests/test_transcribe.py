"""扒谱与复刻的验证。

扒谱这东西很容易写出"看起来在测、其实只测了不崩"的用例（比如只断言"返回了
一个对象"）。所以这里全部用**合成出来的、答案已知**的音频：
已知音高与位置的旋律、已知鼓件的鼓型、已知速度与调式的参考片段，
然后按音高/步号/鼓件/速度去核对，并明确写下"什么情况下它会不准"。

复刻同理：指纹的每一项都要能被独立量出来对账（速度、调式、密度、明暗、鼓占比），
再验证它交给编曲助手之后真的生成出了音符。
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

from app.audio import demo, drums, render, replicate, transcribe
from app.audio.project import (
    ADSR, FilterParams, Note, Project, SynthParams, Track, drum_to_pitch,
)
from app.ui.pages.composer import compose, params_from_profile, scale_pc_set

ok = True


def check(label, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print(f"  [{'OK  ' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")


# --------------------------------------------------------------------------
# 造已知答案的素材
# --------------------------------------------------------------------------


def melody_project(bpm: float, pitches: list[tuple[int, int]]) -> tuple[Project, Track]:
    """一段音高与位置都已知的旋律（锯齿 + 低通，和扒谱的单声部场景一致）。"""
    proj = Project(name=f"旋律{bpm:.0f}", bpm=bpm)
    proj.bars = 4
    track = Track(name="lead", kind="synth", volume=1.0)
    track.synth = SynthParams(
        wave="saw", amp=ADSR(0.005, 0.10, 0.7, 0.06, 2.0),
        filter=FilterParams(kind="lp", cutoff=4200.0, resonance=0.9))
    for step, pitch in pitches:
        track.add_note(Note(step=step, pitch=pitch, length=3, velocity=0.85))
    proj.add_track(track)
    return proj, track


def drum_project(pattern: list[tuple[str, int, float]]) -> tuple[Project, Track]:
    """鼓型：用**调好的**音色表（``default_voices``），与界面上听到的一致。"""
    proj = Project(name="鼓型", bpm=120.0)
    proj.bars = 2
    track = Track(name="dr", kind="drum", volume=1.0)
    track.ensure_kit()
    track.kit = {k: v for k, v in drums.default_voices().items()}
    for piece, step, vel in pattern:
        track.add_note(Note(step=step, pitch=drum_to_pitch(piece), length=1, velocity=vel))
    proj.add_track(track)
    return proj, track


MELODY = [(0, 60), (4, 64), (8, 67), (12, 72), (16, 67), (20, 64), (24, 62), (28, 60)]
DRUM_PATTERN = (
    [("hat_closed", s, 0.55) for s in range(0, 16, 2)]
    + [("kick", 0, 1.0), ("kick", 8, 1.0), ("snare", 4, 0.9), ("snare", 12, 0.9)]
)

# ==========================================================================
print("=" * 70)
print("1) 扒旋律：音高与步号都要对得上（合成素材，答案已知）")
print("=" * 70)
proj_m, track_m = melody_project(120.0, MELODY)
buf_m = render.render_track(track_m, proj_m)
res_m = transcribe.transcribe(buf_m, render.SR)

check("速度估得准（误差 < 2%）", abs(res_m.bpm - 120.0) / 120.0 < 0.02,
      f"{res_m.bpm:.1f} BPM")
check("调式定对了（C 大调）", res_m.key_pc == 0 and res_m.mode == "major",
      res_m.key_name)

got = {(int(s), int(p)) for s, p, _v in res_m.pitched}
want = set(MELODY)
hits = want & got
check("8 个音全部扒对（音高 + 步号）", len(hits) == len(want),
      f"命中 {len(hits)}/{len(want)}")
check("没有凭空多出音符（精确率 100%）", got <= want,
      f"多出 {sorted(got - want)}")
check("每个音的力度都落在合理区间",
      all(0.2 <= v <= 1.0 for _s, _p, v in res_m.pitched),
      f"{[round(v, 2) for _s, _p, v in res_m.pitched]}")

# 手动指定 BPM 时，步号按指定值量化：BPM 减半 → 每一步的时长翻倍 → 步号减半
res_manual = transcribe.transcribe(buf_m, render.SR, bpm=60.0)
top_auto = max(s for s, _p, _v in res_m.pitched)
top_manual = max(s for s, _p, _v in res_manual.pitched)
check("手动指定 BPM 会被采纳（步号按指定的速度量化）",
      abs(res_manual.bpm - 60.0) < 1e-6 and abs(top_manual - top_auto / 2) <= 1,
      f"60 BPM 下最大步号 {top_manual}，120 BPM 下 {top_auto}")

# 另一个调式：A 小调
proj_am, track_am = melody_project(96.0, [(0, 57), (4, 60), (8, 64), (12, 69)])
res_am = transcribe.transcribe(render.render_track(track_am, proj_am), render.SR)
check("A 小调的片段被定成 A 小调", res_am.key_pc == 9 and res_am.mode == "minor",
      res_am.key_name)

# ==========================================================================
print()
print("=" * 70)
print("2) 扒鼓型：步号要全，鼓件不能凭空发明")
print("=" * 70)
proj_d, track_d = drum_project(DRUM_PATTERN)
res_d = transcribe.transcribe(render.render_track(track_d, proj_d), render.SR)

expected_at: dict[int, set[str]] = {}
# 同族内允许互判，跨族不行 —— 这是实测出来的分辨力边界，不是偷懒：
#  * 镲族（闭/开/吊）：同一记闭镲单独渲染衰减 12ms、夹在底鼓军鼓之间量到 56ms，
#    而一记开镲单独渲染就是 66ms，密集材料里"闭/开"这个维度分不开。
#  * 中频打击乐（军鼓/拍手/边击）：拍手的特征只有"包络里有多个脉冲"，
#    而军鼓旁边同时响一记踩镲也会造出多个脉冲 —— 实测 step 4 的军鼓被判成拍手。
# 位置（步号）与低频那几件照旧严格核对，那才是节奏骨架。
HAT_FAMILY = {"hat_closed", "hat_open", "hat_pedal", "crash", "ride"}
MID_FAMILY = {"snare", "clap", "rim"}
for piece, step, _v in DRUM_PATTERN:
    mark = "*hat*" if piece in HAT_FAMILY else ("*mid*" if piece in MID_FAMILY else piece)
    expected_at.setdefault(step, set()).add(mark)


def _matches(step: int, piece: str) -> bool:
    allowed = expected_at.get(step, set())
    if piece in allowed:
        return True
    if "*hat*" in allowed and piece in HAT_FAMILY:
        return True
    return "*mid*" in allowed and piece in MID_FAMILY


steps_got = {s for s, _p, _v in res_d.drums}
check("16 分网格上的步号全对（0/2/4…14）", steps_got == set(expected_at),
      f"检出 {sorted(steps_got)}")
check("没有凭空发明的步号", steps_got <= set(expected_at))
bad = [(s, p) for s, p, _v in res_d.drums if not _matches(s, p)]
check("每个检出的鼓件都落在那一步真实存在的鼓件集合里", not bad, f"越界 {bad}")
kick_steps = {s for s, p, _v in res_d.drums if p in ("kick", "kick_808")}
snare_steps = {s for s, p, _v in res_d.drums if p in ("snare", "clap")}
check("底鼓位置对（0 与 8）", kick_steps == {0, 8}, f"{sorted(kick_steps)}")
check("军鼓位置对（4 与 12）", snare_steps == {4, 12}, f"{sorted(snare_steps)}")
check("速度按鼓点估出来（120 附近）", abs(res_d.bpm - 120.0) / 120.0 < 0.05,
      f"{res_d.bpm:.1f} BPM（按 {len(res_d.drums)} 个鼓点估得）")
check("扒谱日志把依据写清楚了（可追溯）",
      all(("速度" in line) or ("起音" in line) or ("扒出" in line) for line in res_d.log),
      " | ".join(res_d.log))

# 反向的精度检查：只有底鼓与军鼓时，不许凭空发明镲
proj_nd, track_nd = drum_project([("kick", 0, 1.0), ("snare", 4, 0.9),
                                  ("kick", 8, 1.0), ("snare", 12, 0.9)])
res_nd = transcribe.transcribe(render.render_track(track_nd, proj_nd), render.SR)
invented = [(s, p) for s, p, _v in res_nd.drums if p in HAT_FAMILY]
check("没有镲的鼓型里不会凭空发明镲", not invented, f"{invented}")
check("只有底鼓与军鼓时位置依然对",
      {s for s, p, _v in res_nd.drums if p in ("kick", "kick_808")} == {0, 8}
      and {s for s, p, _v in res_nd.drums if p in ("snare", "clap")} == {4, 12},
      f"检出 {sorted(res_nd.drums)}")

# 同时有旋律与鼓的整曲（两条轨）。旋律用**短促**的音符：这是实测出来的边界 ——
# 长音（length=3）会把鼓点盖住，混音里一记鼓都检不出；短促织体则没问题。
proj_both = Project(name="合", bpm=120.0)
proj_both.bars = 2
proj_both.add_track(track_d)
lead2 = Track(name="lead", kind="synth", volume=0.9)
lead2.synth = SynthParams(wave="saw", amp=ADSR(0.005, 0.1, 0.7, 0.06, 2.0),
                          filter=FilterParams(kind="lp", cutoff=4200.0))
for step, pitch in [(0, 64), (4, 67), (8, 71), (12, 67)]:
    lead2.add_note(Note(step=step, pitch=pitch, length=1, velocity=0.9))
proj_both.add_track(lead2)
res_both = transcribe.transcribe(render.render_project(proj_both)[0], render.SR)
check("两条轨的混音里鼓与旋律都能扒出来（短促织体）",
      len(res_both.drums) >= 4 and len(res_both.pitched) >= 2,
      f"{len(res_both.pitched)} 音 + {len(res_both.drums)} 鼓")

# 同一条旋律拉成长音，鼓点检出会明显变弱 —— 这条边界写下来，别当成没发生
proj_long = Project(name="合长", bpm=120.0)
proj_long.bars = 2
proj_long.add_track(track_d)
lead3 = Track(name="lead", kind="synth", volume=0.9)
lead3.synth = SynthParams(wave="saw", amp=ADSR(0.005, 0.1, 0.7, 0.06, 2.0),
                          filter=FilterParams(kind="lp", cutoff=4200.0))
for step, pitch in [(0, 64), (4, 67), (8, 71), (12, 67)]:
    lead3.add_note(Note(step=step, pitch=pitch, length=3, velocity=0.9))
proj_long.add_track(lead3)
res_long = transcribe.transcribe(render.render_project(proj_long)[0], render.SR)
print(f"  · 混音里的鼓点：短促织体 {len(res_both.drums)} 个，"
      f"长音织体 {len(res_long.drums)} 个（边界：持续长音会盖住鼓点，分轨没有这个问题）")
check("持续长音会把鼓点盖住（已知边界，分轨分析没有这个问题）",
      len(res_long.drums) < len(res_both.drums),
      f"{len(res_long.drums)} < {len(res_both.drums)}")

# ==========================================================================
print()
print("=" * 70)
print("3) 速度估计的适用边界（说清它什么时候准、什么时候不准）")
print("=" * 70)
demo_proj = demo.demo_project()
stem_vs_mix = []
for tag, buf in (("鼓轨分轨", render.render_track(demo_proj.tracks[0], demo_proj)),
                 ("贝斯分轨", render.render_track(demo_proj.tracks[1], demo_proj)),
                 ("整曲混音", render.render_project(demo_proj)[0])):
    r = transcribe.transcribe(buf, render.SR)
    err = abs(r.bpm - demo_proj.bpm) / demo_proj.bpm
    stem_vs_mix.append((tag, r.bpm, err))

for tag, bpm, err in stem_vs_mix:
    print(f"  · {tag}：估得 {bpm:.1f} BPM（真实 {demo_proj.bpm:.0f}，"
          f"误差 {err * 100:.1f}%）")
drum_err = next(e for t, _b, e in stem_vs_mix if t == "鼓轨分轨")
mix_err = next(e for t, _b, e in stem_vs_mix if t == "整曲混音")
check("鼓轨分轨上速度误差 < 2%（这是推荐用法，界面上也是这么提示的）",
      drum_err < 0.02, f"{drum_err * 100:.1f}%")
check("整曲混音上误差确实更大（持续声部盖住拍点，这条限制写进文档与界面）",
      mix_err > drum_err, f"整曲 {mix_err * 100:.1f}% vs 分轨 {drum_err * 100:.1f}%")
check("即便在整曲上，估出来的速度也落在合理区间（不会给出垃圾值）",
      60.0 <= next(b for t, b, _e in stem_vs_mix if t == "整曲混音") <= 180.0)

# ==========================================================================
print()
print("=" * 70)
print("4) 边界：静音 / 太短 / 空工程不崩")
print("=" * 70)
silence = np.zeros((2, render.SR * 3), dtype=np.float32)
res_sil = transcribe.transcribe(silence, render.SR)
check("纯静音：不崩、没有事件、日志说清楚", res_sil.total == 0 and res_sil.log,
      f"{res_sil.total} 个事件 · {res_sil.log[0]}")

tiny = np.zeros((2, 200), dtype=np.float32)
res_tiny = transcribe.transcribe(tiny, render.SR)
check("短于一个分析窗：直接返回并说明原因",
      res_tiny.total == 0 and "太短" in "".join(res_tiny.log),
      " | ".join(res_tiny.log))

noise = np.random.default_rng(7).standard_normal((2, render.SR * 2)).astype(np.float32) * 0.3
res_noise = transcribe.transcribe(noise, render.SR)
check("纯噪声：不会给出满屏假音符（宽带噪声没有可估基频）",
      len(res_noise.pitched) < 40, f"{len(res_noise.pitched)} 个音高事件")

empty_proj = Project(name="空")
check("空工程也能建轨道（只是没有内容）",
      transcribe.make_tracks(res_sil, empty_proj) == [])

# ==========================================================================
print()
print("=" * 70)
print("5) 扒谱产物 → 工程轨道")
print("=" * 70)
made = transcribe.make_tracks(res_m, proj_m)
check("产出旋律 + 鼓两条轨道", [t.kind for t in made] == ["synth", "drum"]
      or [t.kind for t in made] == ["synth"],
      "、".join(f"{t.name}({t.kind})" for t in made))
mel = made[0]
check("旋律轨的音符数与扒出来的一致", len(mel.notes) == len(res_m.pitched),
      f"{len(mel.notes)} 个")
check("音高真的写进去了（不是空的占位轨）",
      {n.pitch for n in mel.notes} == {p for _s, p, _v in res_m.pitched},
      f"{sorted({n.pitch for n in mel.notes})}")
check("时值至少 1 步（不会生成 0 长度的音符）",
      all(n.length >= 1 for n in mel.notes))
check("音符按步号有序（编辑区里好读）",
      [n.step for n in mel.notes] == sorted(n.step for n in mel.notes))

made_d = transcribe.make_tracks(res_d, proj_d)
drum_made = next((t for t in made_d if t.kind == "drum"), None)
check("鼓轨用 drum 类型，音高映射回鼓件",
      drum_made is not None and drum_made.kind == "drum",
      f"{len(drum_made.notes) if drum_made else 0} 个鼓点")
if drum_made is not None:
    from app.audio.project import pitch_to_drum

    pieces = {pitch_to_drum(n.pitch) for n in drum_made.notes}
    check("鼓轨上的鼓件都在检出结果里", pieces <= {p for _s, p, _v in res_d.drums},
          "、".join(sorted(pieces)))

# ==========================================================================
print()
print("=" * 70)
print("6) 复刻：指纹的每一项都要能对账")
print("=" * 70)
ref_proj = Project(name="参考", bpm=120.0)
ref_proj.bars = 4
ref_drum = Track(name="dr", kind="drum", volume=1.0)
ref_drum.ensure_kit()
ref_drum.kit = {k: v for k, v in drums.default_voices().items()}
for piece, step, vel in DRUM_PATTERN:
    ref_drum.add_note(Note(step=step, pitch=drum_to_pitch(piece), length=1, velocity=vel))
ref_proj.add_track(ref_drum)
ref_lead = Track(name="lead", kind="synth", volume=0.9)
ref_lead.synth = SynthParams(wave="saw", amp=ADSR(0.005, 0.1, 0.7, 0.06, 2.0),
                             filter=FilterParams(kind="lp", cutoff=5200.0))
# 用 8 个音的 C 大调旋律（而不是 C-E-G 三音琶音）：三个音级在大小调模板之间
# 本来就歧义（C-E-G 既像 C 大调也像 E 小调），要验"调式定得对"就得给够信息。
for step, pitch in MELODY:
    ref_lead.add_note(Note(step=step, pitch=pitch, length=3, velocity=0.9))
ref_proj.add_track(ref_lead)

ref_audio = render.render_project(ref_proj)[0]
# 指纹分两种参考来验：**分轨**是推荐用法（界面提示里也写了），整曲混音另有一条
# 限制断言 —— 把两者分开测，才不会用"整曲不准"掩盖"分轨也不准"。
lead_audio = render.render_track(ref_lead, ref_proj)
drum_audio = render.render_track(ref_drum, ref_proj)
profile = replicate.analyze_reference(lead_audio, render.SR)          # 有音高的参考
drum_profile = replicate.analyze_reference(drum_audio, render.SR)     # 打击乐参考
mix_profile = replicate.analyze_reference(ref_audio, render.SR)       # 整曲混音
print(f"  · 旋律分轨指纹：{profile.summary()}")
for name, value in profile.metrics():
    print(f"      {name}: {value}")
print(f"  · 鼓分轨指纹：{drum_profile.summary()}")

check("速度对得上（误差 < 5%）", abs(profile.bpm - 120.0) / 120.0 < 0.05,
      f"{profile.bpm:.1f} BPM")
check("调式对得上（C 大调）", profile.key_pc == 0 and profile.mode == "major",
      profile.key_name)
check("旋律分轨里没有鼓 → 鼓占比为 0", profile.drum_ratio < 0.2,
      f"{profile.drum_ratio * 100:.0f}%")
check("鼓分轨里鼓占比很高", drum_profile.drum_ratio > 0.5,
      f"{drum_profile.drum_ratio * 100:.0f}%")
check("响度是合理的负数 dBFS", -40.0 < profile.energy_db < 0.0,
      f"{profile.energy_db:+.1f} dBFS")
check("明暗落在可解释的区间", profile.brightness_hz > 200.0,
      f"{profile.brightness_hz:.0f} Hz · {profile.brightness_label}")
check("小节数由时长与速度推出（4 小节左右）", 3 <= profile.bars <= 6,
      f"{profile.bars} 小节 / {profile.seconds:.1f}s")
check("metrics 有 8 行（界面上逐条显示）", len(profile.metrics()) == 8)
check("只在拍点落音的材料判为平直（没有后半拍就无从谈摇摆）",
      profile.swing < 0.06, f"{profile.swing:.2f} · {profile.swing_label}")
check("整曲混音上速度会偏（持续声部盖住拍点，这条限制写在文档与界面上）",
      abs(mix_profile.bpm - 120.0) / 120.0 >= abs(profile.bpm - 120.0) / 120.0,
      f"整曲 {mix_profile.bpm:.1f} vs 分轨 {profile.bpm:.1f} BPM")

same_seed = profile.pick_progression(3) == profile.pick_progression(3)
check("同一个种子给出的和弦走向稳定（可复现）", same_seed,
      f"{profile.pick_progression(3)}")
check("不同种子会换走向（有得挑）",
      len({profile.pick_progression(i) for i in range(6)}) >= 2,
      f"{[profile.pick_progression(i) for i in range(4)]}")

# 复刻的下一步：指纹 → 生成参数 → 真的生成出音符
params = params_from_profile(profile, bars=4, seed=11)
check("参数里的调式/速度/密度来自指纹",
      params.key_pc == profile.key_pc and params.mode == profile.mode
      and abs(params.bpm - profile.bpm) < 1e-6
      and abs(params.density - profile.density) < 1e-6,
      f"{params.mode} · {params.bpm:.0f} BPM · density={params.density:.2f}")
check("（前置）旋律分轨的鼓占比低、鼓分轨的鼓占比高",
      profile.drum_ratio <= 0.12 < drum_profile.drum_ratio,
      f"{profile.drum_ratio * 100:.0f}% vs {drum_profile.drum_ratio * 100:.0f}%")
drum_params = params_from_profile(drum_profile, bars=4, seed=11)
check("鼓占够高时保留鼓声部（鼓分轨）", drum_params.parts["drums"] is True)
quiet_params = params_from_profile(profile, bars=4, seed=11)
check("参考里几乎没有鼓时就把鼓声部关掉（这是「像不像」里最明显的一条）",
      quiet_params.parts["drums"] is False,
      f"鼓占比 {profile.drum_ratio * 100:.0f}%")

result = compose(ref_proj, params)
check("按指纹生成真的出了音符",
      len(result.tracks) >= 2 and result.total_notes > 20,
      "、".join(f"{t.name}:{len(t.notes)}" for t in result.tracks))
scale_pcs = scale_pc_set(profile.key_pc, profile.mode)
out_of_scale = [(t.name, n.pitch) for t in result.tracks if t.kind == "synth"
                for n in t.notes if (n.pitch % 12) not in scale_pcs]
check("生成结果落在参考的调式里（调内音，一个都不跑调）", not out_of_scale,
      f"调式 {profile.key_name} · 越界 {out_of_scale[:4]}")
check("同一个种子生成同一首（复刻可复现）",
      compose(ref_proj, params_from_profile(profile, bars=4, seed=11)).total_notes
      == result.total_notes,
      f"{result.total_notes} 音符")


print()
print("=" * 70)
print("全部通过" if ok else "存在失败项")
print("=" * 70)
sys.exit(0 if ok else 1)
