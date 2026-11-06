"""编曲助手（AI 生成）的验证 —— 重点不是"界面画出来了"，而是"生成的东西对不对"。

最有价值的一条是**逐音检查音阶归属**：生成式功能最容易出的问题就是
"听起来像那么回事，其实跑调了"，而那是可以严格证明的。
所以下面所有关于音高的断言都**不用被测模块的函数**，而是在测试里
重新抄一份调式音程表、重新推一遍和弦音 —— 否则就是拿实现验证实现。
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys as _sys
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parent.parent
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractButton, QApplication

app = QApplication([])

import numpy as np

from app.audio import demo, render
from app.audio.player import HAVE_SOUNDDEVICE
from app.audio.project import DRUM_MAP, Project, pitch_to_drum
from app.theme import accents
from app.ui.controller import AppController
from app.ui.pages.composer import CUSTOM, MODES, PARTS, PRESETS, ComposerPage

ok = True


def check(label, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print(f"  [{'OK  ' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")


# --------------------------------------------------------------------------
# 测试侧独立重抄的理论表（故意不从被测模块导入）
# --------------------------------------------------------------------------

MODE_IV = {
    "major": (0, 2, 4, 5, 7, 9, 11),
    "minor": (0, 2, 3, 5, 7, 8, 10),
    "harmonic_minor": (0, 2, 3, 5, 7, 8, 11),
    "pentatonic": (0, 3, 5, 7, 10),
    "dorian": (0, 2, 3, 5, 7, 9, 10),
    "mixolydian": (0, 2, 4, 5, 7, 9, 10),
    "blues": (0, 3, 5, 6, 7, 10),
}


def scale_pcs(key_pc, mode):
    return {(key_pc + iv) % 12 for iv in MODE_IV[mode]}


def degree_tone_pcs(key_pc, mode, degree):
    """级数 → 三和弦的三个音高类（按"音阶内叠三度"重新推一遍）。"""
    iv = MODE_IV[mode]
    n = len(iv)
    out = []
    for k in range(3):
        i = (degree - 1) + 2 * k
        octave, idx = divmod(i, n)
        out.append((key_pc + iv[idx] + 12 * octave) % 12)
    return out


def pcs_of(notes):
    return {n.pitch % 12 for n in notes}


def bar_of(note, steps_per_bar):
    return note.step // steps_per_bar


def steps_of(track, piece, bar, spb):
    """某一小节里某个鼓件落在哪些步上。"""
    return {n.step % spb for n in track.notes
            if bar_of(n, spb) == bar and pitch_to_drum(n.pitch) == piece}


# --------------------------------------------------------------------------
# 测试台
# --------------------------------------------------------------------------


class Rig:
    def __init__(self):
        self.project = demo.demo_project()          # 真实的 5 轨示范工程
        self.page = ComposerPage(self.project)
        self.page.resize(1180, 720)
        self.page.show()
        self.ctrl = AppController(self.project)
        self.page.bind(self.ctrl)
        # 主窗口的接线是 projectChanged → _sync_project_meta() → _sync_pages() →
        # page.rebuild()，而且撤销是**就地替换工程内容**（不换对象）。这里用一行
        # 等价的连接代替：不接上就测不到"撤销之后页面认不认得出被还回来的轨道"，
        # 而那正是缺陷① 藏身的地方。
        self.ctrl.projectChanged.connect(self.page.rebuild)

    def setup_basic(self, bars=4, seed=20240607):
        """一组固定参数：Am–F–C–G / A 自然小调 / 4 小节。"""
        self.page.set_preset("amfcg")
        self.page.set_bars(bars)
        self.page.set_bpm(96)
        self.page.set_density(0.5)
        self.page.set_parts({k: True for k, *_ in PARTS})
        self.page.set_seed(seed)

    def tracks(self):
        return self.page.generated_tracks()

    def by_name(self, name):
        for t in self.tracks():
            if t.name == name:
                return t
        return None

    def pitched(self):
        return [n for t in self.tracks() if t.kind != "drum" for n in t.notes]

    def signature(self):
        return {t.name: tuple(sorted((n.step, n.pitch) for n in t.notes))
                for t in self.tracks()}

    def close(self):
        self.ctrl.shutdown()


rig = Rig()
page, proj, ctrl = rig.page, rig.project, rig.ctrl
SPB = proj.steps_per_bar
BASE_TRACKS = len(proj.tracks)
BASE_NAMES = [t.name for t in proj.tracks]

print("=" * 70)
print("1) 页面构造与绘制")
print("=" * 70)
check("页面能构造", page is not None)
pix = page.grab()
check("离屏绘制不报错且有内容", pix.width() > 200 and pix.height() > 200,
      f"grab {pix.width()}x{pix.height()}")
check("参数控件齐全（主音 12 / 调式 ≥6 / 走向 ≥4 / 声部 4）",
      len(page.key_chips) == 12 and len(page.mode_chips) >= 6
      and len(page.preset_chips) >= 4 and len(page.part_chips) == 4,
      f"{len(page.key_chips)}/{len(page.mode_chips)}/"
      f"{len(page.preset_chips)}/{len(page.part_chips)}")
check("调式表覆盖题目要求的六种",
      {"major", "minor", "harmonic_minor", "pentatonic", "dorian", "mixolydian"}
      <= {m.key for m in MODES} and set(MODE_IV) == {m.key for m in MODES},
      " / ".join(m.label for m in MODES))
check("四个预设走向都在（另加自定义入口）",
      {"pop4536251", "canon", "amfcg", "blues12"} <= {p.key for p in PRESETS},
      " / ".join(p.label for p in PRESETS))
check("走向预设的级数都在 1~7 内、长度 ≥4",
      all(all(1 <= d <= 7 for d in p.degrees) and len(p.degrees) >= 4 for p in PRESETS))
check("自定义模式可用", isinstance(CUSTOM, str) and page.seg.count() == 2)
check("初始参数取自工程（不擅自改 bpm/小节）",
      abs(page.current_params().bpm - proj.bpm) < 1e-6,
      f"bpm={page.current_params().bpm}")

print()
print("=" * 70)
print("2) 生成真的往工程里加了轨道与音符")
print("=" * 70)
rig.setup_basic(bars=4, seed=424242)
p = page.current_params()
check("预设 Am–F–C–G 把主音/调式带过来了", (p.key_pc, p.mode) == (9, "minor"),
      f"key={p.key_pc} mode={p.mode}")
check("预设级数为 1 6 3 7", p.degrees == (1, 6, 3, 7), f"{p.degrees}")

before_notes = sum(len(t.notes) for t in proj.tracks)
ok_gen = page.generate()
after = rig.tracks()
check("生成返回成功", ok_gen is True)
check("新增了 4 条轨道", len(proj.tracks) == BASE_TRACKS + 4,
      f"{BASE_TRACKS} → {len(proj.tracks)}")
check("原有轨道一根没动",
      [t.name for t in proj.tracks[:BASE_TRACKS]] == BASE_NAMES,
      " / ".join(t.name for t in proj.tracks[:BASE_TRACKS]))
check("新轨道都叫「AI xx」",
      {t.name for t in after} == {"AI 和声", "AI 贝斯", "AI 鼓组", "AI 旋律"},
      " / ".join(sorted(t.name for t in after)))
check("生成确实加了音符",
      sum(len(t.notes) for t in proj.tracks) > before_notes,
      f"{before_notes} → {sum(len(t.notes) for t in proj.tracks)}")
check("每条生成轨道都有音符", all(t.notes for t in after),
      " · ".join(f"{t.name} {len(t.notes)}" for t in after))
check("鼓轨是 kind='drum' 且带套鼓",
      rig.by_name("AI 鼓组").kind == "drum" and len(rig.by_name("AI 鼓组").kit) >= 12)
check("合成器轨是 kind='synth' 且带 SynthParams",
      all(t.kind == "synth" and t.synth is not None
          for t in after if t.kind != "drum"))
check("每轨都有图标、颜色与音量",
      all(t.icon and t.color.startswith("#") and t.volume > 0 for t in after),
      " · ".join(f"{t.name}:{t.icon}/{t.color}" for t in after))
palette_colors = {c.name() for c in accents.track_palette()}
check("颜色取自 accents.track_palette()", all(t.color in palette_colors for t in after))
check("四条轨颜色互不相同（相邻轨道不撞色）",
      len({t.color for t in after}) == len(after))
check("每条轨道在每个小节都有音符",
      all({bar_of(n, SPB) for n in t.notes} == {0, 1, 2, 3} for t in after),
      " · ".join(f"{t.name}{sorted({bar_of(n, SPB) for n in t.notes})}" for t in after))
check("生成的音符都在工程时间轴内",
      all(0 <= n.step < proj.total_steps for t in after for n in t.notes),
      f"total_steps={proj.total_steps}")
check("速度与小节数被写进工程",
      abs(proj.bpm - 96.0) < 1e-6 and proj.bars >= 4,
      f"bpm={proj.bpm} bars={proj.bars}")

print()
print("=" * 70)
print("3) 所有音高都落在所选调式的音阶内（逐音检查）")
print("=" * 70)
KEY, MODE = p.key_pc, p.mode
SCALE = scale_pcs(KEY, MODE)
pitched = rig.pitched()
bad = [n for n in pitched if n.pitch % 12 not in SCALE]
check(f"{KEY} {MODE} 音阶内共 {len(pitched)} 个音，全部在音阶里",
      bool(pitched) and not bad,
      f"调外音 {len(bad)} 个" + (f"：{[(n.step, n.pitch) for n in bad[:5]]}" if bad else ""))
check("A 自然小调的预期音高类与手推一致", SCALE == {9, 11, 0, 2, 4, 5, 7},
      f"{sorted(SCALE)}")

for track in after:
    if track.kind == "drum":
        continue
    off = [n for n in track.notes if n.pitch % 12 not in SCALE]
    check(f"「{track.name}」逐音都在音阶内", not off,
          f"{len(track.notes)} 个音" + (f"，越界 {len(off)}" if off else ""))

# 边界：极端参数下也不许跑调
page.set_density(1.0)
page.set_bars(2)
page.generate()
hi_dense = rig.pitched()
check("密度 1.0 / 2 小节时仍然全部在音阶内",
      bool(hi_dense) and all(n.pitch % 12 in SCALE for n in hi_dense),
      f"{len(hi_dense)} 个音")
page.set_density(0.0)
page.generate()
lo_dense = rig.pitched()
check("密度 0.0 时仍然全部在音阶内",
      bool(lo_dense) and all(n.pitch % 12 in SCALE for n in lo_dense),
      f"{len(lo_dense)} 个音")
check("复杂度真的影响音符密度", len(hi_dense) > len(lo_dense),
      f"密度 1.0 → {len(hi_dense)} 个音；密度 0.0 → {len(lo_dense)} 个音")

print()
print("=" * 70)
print("4) 是「和弦音」而不是随机音")
print("=" * 70)
rig.setup_basic(bars=4, seed=99)
page.set_density(0.6)
page.generate()
bass, pad, lead = rig.by_name("AI 贝斯"), rig.by_name("AI 和声"), rig.by_name("AI 旋律")
degrees = page.current_params().degrees
expected = {b: degree_tone_pcs(KEY, MODE, degrees[b % len(degrees)]) for b in range(4)}
print("  逐小节和弦（测试侧手推音高类）：",
      " | ".join(f"第{b + 1}小节 {sorted(expected[b])}" for b in range(4)))
print("  预览面板显示的走向：", page.preview_text().splitlines()[0])

bass_bad = [n for n in bass.notes
            if n.pitch % 12 not in {expected[bar_of(n, SPB)][0],
                                    expected[bar_of(n, SPB)][2]}]
check("贝斯只走根音/五度（都是和弦音）", not bass_bad,
      f"{len(bass.notes)} 个音，越界 {len(bass_bad)}")
root_hits = sum(1 for n in bass.notes
                if n.pitch % 12 == expected[bar_of(n, SPB)][0])
check("贝斯以根音为主（≥50%）", root_hits >= 0.5 * len(bass.notes),
      f"{root_hits}/{len(bass.notes)} = {root_hits / len(bass.notes):.0%}")

pad_bad = [n for n in pad.notes if n.pitch % 12 not in expected[bar_of(n, SPB)]]
check("铺底只弹当前小节的和弦音", not pad_bad,
      f"{len(pad.notes)} 个音，越界 {len(pad_bad)}")

pad_by_bar = {b: sorted(n.pitch for n in pad.notes if bar_of(n, SPB) == b) for b in range(4)}
moves = []
for b in range(1, 4):
    moves += [abs(a - c) for a, c in zip(pad_by_bar[b - 1][:3], pad_by_bar[b][:3])]
check("铺底做了声部进行（相邻和弦各声部移动 ≤7 个半音）",
      bool(moves) and max(moves) <= 7, f"最大移动 {max(moves) if moves else '-'} 半音")

strong = [n for n in lead.notes if n.step % (SPB // 2) == 0]
strong_chord = [n for n in strong if n.pitch % 12 in expected[bar_of(n, SPB)]]
check("旋律强拍上有相当比例是当前和弦音（和声咬合）",
      bool(strong) and len(strong_chord) >= 0.4 * len(strong),
      f"{len(strong_chord)}/{len(strong)} = {len(strong_chord) / max(1, len(strong)):.0%}")

print()
print("=" * 70)
print("5) 鼓型符合风格")
print("=" * 70)
drum = rig.by_name("AI 鼓组")
used_pieces = sorted({pitch_to_drum(n.pitch) for n in drum.notes})
check("鼓件音高全部是合法套鼓件", all(int(n.pitch) in DRUM_MAP for n in drum.notes),
      f"{len(used_pieces)} 种：{'/'.join(used_pieces)}")
kicks = {b: steps_of(drum, "kick", b, SPB) for b in range(4)}
snares = {b: steps_of(drum, "snare", b, SPB) for b in range(4)}
hats = {b: (steps_of(drum, "hat_closed", b, SPB) | steps_of(drum, "hat_open", b, SPB)
            | steps_of(drum, "hat_pedal", b, SPB)) for b in range(4)}
print("  底鼓：", {b: sorted(v) for b, v in kicks.items()})
print("  军鼓：", {b: sorted(v) for b, v in snares.items()})
print("  踩镲：", {b: sorted(v) for b, v in hats.items()})
EIGHTHS = {0, 2, 4, 6, 8, 10, 12, 14}
check("底鼓落在 1、3 拍（步 0 与 8）",
      all(0 in kicks[b] and 8 in kicks[b] for b in range(4)))
check("军鼓落在 2、4 拍（步 4 与 12）—— 反拍",
      all(4 in snares[b] and 12 in snares[b] for b in range(4)))
check("踩镲按八分音符铺（每小节至少覆盖 6/8 个偶数步）",
      all(len(hats[b] & EIGHTHS) >= 6 for b in range(4)),
      " · ".join(f"第{b + 1}小节 {len(hats[b] & EIGHTHS)}/8" for b in range(4)))
check("鼓点全部落在 16 分网格上（不会出现半格）",
      all(isinstance(n.step, int) and n.step >= 0 for n in drum.notes))
toms = sum(1 for n in drum.notes
           if bar_of(n, SPB) == 3 and pitch_to_drum(n.pitch).startswith("tom"))
check("最后一小节有过门（嗵鼓）", toms >= 3, f"{toms} 个嗵鼓")

print()
print("=" * 70)
print("6) 旋律不是随机音（马尔可夫：级进为主 + 跳进受限）")
print("=" * 70)
lead_pitches = [n.pitch for n in sorted(lead.notes, key=lambda n: n.step)]
ivs = [abs(b - a) for a, b in zip(lead_pitches, lead_pitches[1:])]
stepwise = sum(1 for d in ivs if d <= 4)
check("相邻音多为级进（≤4 个半音占比 ≥50%）",
      bool(ivs) and stepwise / len(ivs) >= 0.5,
      f"{stepwise}/{len(ivs)} = {stepwise / max(1, len(ivs)):.0%}")
check("旋律用到的音高类不超过调式音级数（没有半音乱入）",
      len(pcs_of(lead.notes)) <= len(MODE_IV[MODE]),
      f"{len(pcs_of(lead.notes))} 个音高类 ≤ {len(MODE_IV[MODE])}")
check("旋律不是单音重复（用了 ≥3 个不同音高）",
      len(set(lead_pitches)) >= 3, f"{len(set(lead_pitches))} 个不同音高")
check("旋律音域不超过一个半八度",
      max(lead_pitches) - min(lead_pitches) <= 19,
      f"{min(lead_pitches)}~{max(lead_pitches)}")

print()
print("=" * 70)
print("7) 不同调式 → 不同音高集合")
print("=" * 70)
rig.setup_basic(bars=4, seed=7)
page.set_key(0)
page.set_mode("major")
page.generate()
major_pcs = pcs_of(rig.pitched())
page.set_mode("minor")
page.generate()
minor_pcs = pcs_of(rig.pitched())
check("C 自然大调用到的音高类都落在大调音阶内",
      bool(major_pcs) and major_pcs <= scale_pcs(0, "major"), f"{sorted(major_pcs)}")
check("C 自然小调用到的音高类都落在小调音阶内",
      bool(minor_pcs) and minor_pcs <= scale_pcs(0, "minor"), f"{sorted(minor_pcs)}")
check("两种调式得到的音高集合不同", major_pcs != minor_pcs,
      f"差集 {sorted(major_pcs ^ minor_pcs)}")

page.set_key(2)               # 换主音 D
page.set_mode("major")
page.generate()
d_major_pcs = pcs_of(rig.pitched())
check("换主音后音高集合随之平移", d_major_pcs != major_pcs,
      f"差集 {sorted(d_major_pcs ^ major_pcs)}")

page.set_mode("pentatonic")
page.generate()
pent = pcs_of(rig.pitched())
check("五声音阶下只用 5 个音高类（且都在音阶内）",
      pent <= scale_pcs(2, "pentatonic") and len(pent) <= 5,
      f"{sorted(pent)}")

print()
print("=" * 70)
print("8) 可重复但有变化")
print("=" * 70)
rig.setup_basic(bars=4, seed=None)
page.generate()
sig1 = rig.signature()
page.generate()
sig2 = rig.signature()
diff = sum(1 for k in sig1 if sig1[k] != sig2.get(k))
check("同参数连点两次结果不同（默认每次随机）", diff > 0,
      f"{diff}/{len(sig1)} 条轨不同")
check("连点两次不会堆叠轨道", len(proj.tracks) == BASE_TRACKS + 4,
      f"{len(proj.tracks)} 条")
check("两次都符合同一风格（都在音阶内）",
      all(n.pitch % 12 in SCALE for n in rig.pitched()))

page.set_seed(20240607)
page.generate()
sig3 = rig.signature()
page.set_seed(20240607)
page.generate()
check("填了固定种子 → 结果可复现", rig.signature() == sig3)
page.set_seed(20240608)
page.generate()
check("换一个种子 → 结果不同", rig.signature() != sig3)

print()
print("=" * 70)
print("9) 撤销 / 重做 / 清空")
print("=" * 70)
page.clear_generated()                       # 先回到"只有原有轨道"，便于计数
n_before = len(proj.tracks)
u_before = len(ctrl._undo)
page.set_seed(1)
page.generate()
check("生成前压了撤销点（push_undo('AI 生成')）",
      len(ctrl._undo) == u_before + 1 and ctrl.undo_label() == "AI 生成",
      f"标签「{ctrl.undo_label()}」")
check("生成加了 4 条轨道", len(proj.tracks) == n_before + 4,
      f"{n_before} → {len(proj.tracks)}")
ctrl.undo()
check("撤销后生成的轨道消失、原有轨道还在",
      len(proj.tracks) == n_before
      and [t.name for t in proj.tracks] == BASE_NAMES,
      f"{len(proj.tracks)} 条：{' / '.join(t.name for t in proj.tracks)}")
# 这条以前是**掩盖性**断言：它通过的原因是"页面把归属全忘了"，而不是"工程里
# 真的没有 AI 轨" —— 页面忘光了恰好也让这里为空。现在归属是从工程数据里扫出来的，
# 为空是因为工程里确实一条都没有；"撤销后还能重新认领"由 15) 单独验证。
check("撤销后扫工程找不到 AI 轨（没有幽灵引用）", rig.tracks() == [],
      "判据是工程数据本身，不是页面手里那份 id 列表")
ctrl.redo()
check("重做后生成结果回来", len(proj.tracks) == n_before + 4,
      f"{len(proj.tracks)} 条")
check("重做后仍能按标记找回生成轨道", len(rig.tracks()) == 4,
      f"{len(rig.tracks())} 条")

n_before_clear = len(proj.tracks)
check("清空返回成功", page.clear_generated() is True)
check("清空后生成的轨道立刻消失", len(proj.tracks) == BASE_TRACKS,
      f"{n_before_clear} → {len(proj.tracks)}")
check("清空后一条 AI 轨道都不剩",
      not any(t.name.startswith("AI ") for t in proj.tracks))
check("清空后预览回到未生成状态", "尚未生成" in page.preview_text())
check("清空也进了撤销栈", ctrl.undo_label() == "清空 AI 生成",
      f"标签「{ctrl.undo_label()}」")
ctrl.undo()
check("撤销清空后轨道回来了", len(proj.tracks) == n_before_clear,
      f"{len(proj.tracks)} 条")
check("没有生成内容时清空是空操作（第二次返回 False）",
      page.clear_generated() is True and page.clear_generated() is False)
check("清空两次之后工程干净", len(proj.tracks) == BASE_TRACKS,
      f"{len(proj.tracks)} 条")

print()
print("=" * 70)
print("10) 边界与拒绝")
print("=" * 70)
rig.setup_basic(bars=4, seed=5)
page.set_parts({"drums": False, "bass": False, "pad": False, "lead": False})
check("一个声部都不勾 → 拒绝生成", page.generate() is False)
check("拒绝后工程没被改动", len(proj.tracks) == BASE_TRACKS)
page.set_parts({"drums": True, "bass": False, "pad": False, "lead": False})
page.generate()
check("只勾鼓 → 只生成一条鼓轨",
      [t.kind for t in rig.tracks()] == ["drum"] and rig.tracks()[0].name == "AI 鼓组")
page.set_parts({"drums": False, "bass": False, "pad": False, "lead": True})
page.generate()
check("只勾旋律 → 只生成一条旋律轨",
      [t.name for t in rig.tracks()] == ["AI 旋律"])
page.set_parts({k: True for k, *_ in PARTS})

page.set_custom_degrees([1, 5, 6, 4])
check("自定义 4 个和弦可以生成", page.generate() is True,
      f"级数 {page.current_params().degrees}")
page.set_custom_degrees([1, 5, 6, 4, 3, 2, 5, 1])
check("自定义 8 个和弦可以生成", page.generate() is True,
      f"级数 {page.current_params().degrees}")
page.set_custom_degrees([1, 5, 6])
check("自定义 3 个和弦 → 拒绝生成", page.generate() is False)
page.set_custom_degrees([])
check("自定义 0 个和弦 → 拒绝生成", page.generate() is False)
page.set_custom_degrees([1, 5, 6, 4])
page.generate()
check("自定义模式下音符仍然全在音阶内",
      all(n.pitch % 12 in scale_pcs(page.current_params().key_pc,
                                    page.current_params().mode)
          for n in rig.pitched()))
page.append_degree(2)
check("点级数芯片是追加而不是覆盖", len(page.current_params().degrees) == 5,
      f"{page.current_params().degrees}")
for _ in range(6):
    page.append_degree(4)
check("自定义最多 8 个（再点也不涨）",
      len(page.current_params().degrees) == 8,
      f"{len(page.current_params().degrees)} 个")
page._pop_degree()
check("退格能删掉最后一级", len(page.current_params().degrees) == 7,
      f"{page.current_params().degrees}")

page.set_bars(1)
page.generate()
check("1 小节也能生成，且音符不越界",
      bool(rig.tracks()) and all(n.step < SPB for t in rig.tracks() for n in t.notes),
      f"{sum(len(t.notes) for t in rig.tracks())} 个音符")
page.set_bars(16)
page.generate()
check("16 小节能生成，四个声部每小节都有音",
      proj.bars == 16 and all(len({bar_of(n, SPB) for n in t.notes}) == 16
                              for t in rig.tracks()),
      f"bars={proj.bars}")
page.set_bars(99)
check("小节数被钳到 16", page.current_params().bars == 16)
page.set_bpm(300)
check("速度上限钳到 180", page.current_params().bpm == 180.0)
page.set_bpm(20)
check("速度下限钳到 60", page.current_params().bpm == 60.0)

empty = Project(bars=4)
empty_page = ComposerPage(empty)
empty_page.set_seed(3)
empty_page.set_bars(4)
check("空工程（0 条轨道）也能生成",
      empty_page.generate() is True and len(empty.tracks) == 4,
      f"{len(empty.tracks)} 条")
check("空工程生成的内容同样在音阶内",
      all(n.pitch % 12 in scale_pcs(empty_page.current_params().key_pc,
                                    empty_page.current_params().mode)
          for t in empty.tracks if t.kind != "drum" for n in t.notes))

print()
print("=" * 70)
print("11) 预览内容")
print("=" * 70)
rig.setup_basic(bars=4, seed=11)
page.set_key(0)                              # C 大调，便于人读和弦名
page.set_mode("major")
page.set_custom_degrees([1, 5, 6, 4])        # C 大调下 = C–G–Am–F
page.generate()
text = page.preview_text()
print(text)
check("预览含和弦进行", "走向" in text and "C → G → Am → F" in text)
check("预览含调式音阶音名", "音阶" in text and "C D E F G A B" in text)
check("预览含每轨音符数",
      all(t.name in text and f"{len(t.notes)} 音符" in text for t in rig.tracks()))
check("预览含随机种子（可复现）", "种子" in text)

print()
print("=" * 70)
print("12) 渲染与试听")
print("=" * 70)
rig.setup_basic(bars=4, seed=2024)
page.generate()
audio, stats = page.render_generated()
check("生成内容能单独渲染出音频", audio is not None and audio.shape[1] > 0,
      f"{audio.shape if audio is not None else None}")
check("单独渲染的音频没有 NaN / Inf", bool(np.isfinite(audio).all()))
check("单独渲染不是静音", float(np.abs(audio).max()) > 0.01,
      f"峰值 {float(np.abs(audio).max()):.4f}")
check("单独渲染不削波", stats.peak <= 1.0, f"峰值 {stats.peak_db:+.2f} dBFS")

full, fstats = render.render_project(proj)
check("整首工程（原有轨道 + 生成轨道）能渲染", full.shape[1] > 0, f"{full.shape}")
check("整首渲染无 NaN", bool(np.isfinite(full).all()))
check("整首渲染不削波", fstats.peak <= 1.0, f"峰值 {fstats.peak_db:+.2f} dBFS")
check("整首渲染不是静音", fstats.peak > 0.01, f"峰值 {fstats.peak_db:+.2f} dBFS")
check("渲染统计里包含了新增的轨道数", fstats.tracks >= 9, f"{fstats.tracks} 轨")

played = page.audition_generated()
if HAVE_SOUNDDEVICE:
    check("试听按钮把音频交给了播放器", played is True)
else:
    check("没有音频设备时试听优雅失败（不抛异常）", played is False,
          "本机 sounddevice 不可用")
cleared = page.clear_generated()
check("清空后试听返回 False（没有内容可听）",
      cleared is True and page.audition_generated() is False)
page.generate()                              # 恢复生成状态给后面用

print()
print("=" * 70)
print("13) 页面契约接口")
print("=" * 70)
check("有 edited / notesEdited / seekRequested 信号",
      hasattr(page, "edited") and hasattr(page, "notesEdited")
      and hasattr(page, "seekRequested"))
fired = []
page.edited.connect(lambda: fired.append("edited"))
page.notesEdited.connect(lambda: fired.append("notesEdited"))
page.set_seed(77)
page.generate()
check("生成会发 edited 通知编排者重渲染", "edited" in fired and "notesEdited" in fired,
      f"{fired}")

page.grab()                                  # 先画一次，芯片命中区才会被算出来
hits = page.result_view._hits
check("预览里的和弦芯片有可点区域", len(hits) >= 4, f"{len(hits)} 个")
seen = []
page.seekRequested.connect(lambda s: seen.append(s))
chip_rect, chip_bar = hits[1]
QMouseEvent_ = QMouseEvent
page.result_view.mousePressEvent(QMouseEvent_(
    QEvent.Type.MouseButtonPress, chip_rect.center(), chip_rect.center(),
    Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
    Qt.KeyboardModifier.NoModifier))
per_bar = SPB * proj.seconds_per_step
check("点第 2 个和弦芯片会请求跳到那一小节",
      len(seen) == 1 and abs(seen[0] - chip_bar * per_bar) < 1e-6,
      f"小节 {chip_bar + 1} → {seen}")

page.set_position(per_bar * 2, True)
check("set_position 能算出当前小节并高亮", page.result_view.play_bar == 2,
      f"play_bar={page.result_view.play_bar}")
page.refresh_notes()
page.rebuild()
page.set_project(proj)
check("refresh_notes / rebuild / set_project 都不报错且状态一致",
      len(rig.tracks()) == 4, f"{len(rig.tracks())} 条生成轨道")

print()
print("=" * 70)
print("14) 反复生成后整首仍可渲染（回归）")
print("=" * 70)
page.set_bars(4)
for seed in (11, 22, 33):
    page.set_seed(seed)
    page.generate()
final, fs = render.render_project(proj)
check("连续三次重生成后仍无 NaN", bool(np.isfinite(final).all()))
check("连续三次重生成后仍不削波", fs.peak <= 1.0, f"{fs.peak_db:+.2f} dBFS")
check("连续三次重生成没有堆叠轨道", len(proj.tracks) == BASE_TRACKS + 4,
      f"{len(proj.tracks)} 条")
check("重生成的参数一致时风格一致（仍全在音阶内）",
      all(n.pitch % 12 in SCALE for t in rig.tracks() if t.kind != "drum"
          for n in t.notes))

print()
print("=" * 70)
print("15) 生成 → 生成 → 撤销 → 再生成：不堆叠、不留孤儿（缺陷① 回归）")
print("=" * 70)
# 复核者实测的复现序列。撤销会把整棵轨道树从 JSON 重建，页面手里记的 id 会集体
# 失效；如果归属是"拿 id 去工程里查"而不是"扫工程数据"，页面在撤销之后就再也认不出
# 那批轨道，于是 generate() 里的 _remove_generated() 一个都删不掉，新一批直接叠上去。
page.clear_generated()                        # 先回到"只剩用户原有轨道"
clean = len(proj.tracks)
clean_names = [t.name for t in proj.tracks]
check("基线干净（工程里一条 AI 轨都没有）",
      clean == BASE_TRACKS and not any(t.name.startswith("AI ") for t in proj.tracks),
      f"{clean} 条")

page.set_seed(1)
page.generate()
first_batch = len(proj.tracks)
page.set_seed(2)
page.generate()
check("连点两次生成是替换而不是叠加",
      first_batch == clean + 4 and len(proj.tracks) == clean + 4,
      f"{clean} → {first_batch} → {len(proj.tracks)}")

ctrl.undo()                                   # 撤掉第二次生成，工程回到第一批
check("撤销后工程回到第一批（4 条 AI 轨）",
      len(proj.tracks) == clean + 4, f"{len(proj.tracks)} 条")
check("撤销后页面能重新认领被还回来的那批（缺陷①的核心）",
      len(rig.tracks()) == 4, f"{len(rig.tracks())} 条")

# "打开工程 / 重启软件"：新页面手里没有任何历史记录，归属只能从工程数据里读出来。
# 这也顺带验证了标记真的随工程 JSON 往返（from_dict(to_dict()) 之后仍然认得）。
snap = Project.from_dict(proj.to_dict())
twin = ComposerPage(snap)
check("换一个页面看同一份工程数据，同样认得出这 4 条 AI 轨",
      len(twin.generated_tracks()) == 4, f"{len(twin.generated_tracks())} 条")

page.set_seed(3)
page.generate()                               # ← 缺陷① 就是在这一步把两批叠起来
ai_now = [t.name for t in proj.tracks if t.name.startswith("AI ")]
check("撤销后再生成：工程轨道数没有堆叠", len(proj.tracks) == clean + 4,
      f"{len(proj.tracks)} 条（应 {clean + 4}）")
check("撤销后再生成：AI 轨仍然只有一批 4 条", len(ai_now) == 4,
      f"{len(ai_now)} 条：{' / '.join(ai_now)}")
check("原有轨道一根没动", [t.name for t in proj.tracks[:clean]] == clean_names)

check("清空本次生成成功", page.clear_generated() is True)
leftovers = [t.name for t in proj.tracks if t.name.startswith("AI ")]
check("清空后没有清不掉的孤儿 AI 轨",
      len(proj.tracks) == BASE_TRACKS and not leftovers,
      f"{len(proj.tracks)} 条"
      + (f"，残留 {' / '.join(leftovers)}" if leftovers else ""))
check("清空后页面也不再认得任何生成轨道", rig.tracks() == [])

print()
print("=" * 70)
print("16) Ctrl+G 快捷键与标题徽标（缺陷②③）")
print("=" * 70)
sc = getattr(page, "shortcut_generate", None)
check("tooltip 承诺了 Ctrl+G，快捷键就真的注册了 Ctrl+G",
      sc is not None and sc.key().toString() == "Ctrl+G",
      "没注册" if sc is None else sc.key().toString())

# 只检查"注册了"是不够的：这一条真的按一次键，看工程有没有多出 4 条轨道
page.activateWindow()
QTest.qWait(30)
before_key = len(proj.tracks)
QTest.keyClick(page, Qt.Key.Key_G, Qt.KeyboardModifier.ControlModifier)
QTest.qWait(30)
check("按 Ctrl+G 真的会跑一次生成",
      len(proj.tracks) == before_key + 4,
      f"{before_key} → {len(proj.tracks)} 条")

badge = getattr(page, "_head_icon", None)
badge_deco = (
    badge is not None
    and badge.focusPolicy() == Qt.FocusPolicy.NoFocus
    and badge.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
)
check("标题条上的机器人徽标不是按钮（没有「点了没反应」的假控件）",
      badge is not None and not isinstance(badge, QAbstractButton),
      type(badge).__name__ if badge is not None else "缺失")
check("徽标纯装饰：不抢键盘焦点、鼠标事件穿透", badge_deco,
      f"focusPolicy={badge.focusPolicy().name if badge is not None else '-'}"
      f" mouseTransparent="
      f"{badge.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents) if badge is not None else '-'}")

page.clear_generated()
rig.close()

print()
print("=" * 70)
print("全部通过" if ok else "存在失败项")
print("=" * 70)
sys.exit(0 if ok else 1)
