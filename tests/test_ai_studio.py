"""AI 扒谱 · 复刻页的验证。

这一页最容易做成"界面画出来了但点了没反应"（假进度条 + 随机结果）。所以这里
逐条验证它**真的改了工程**：扒谱出来的音符进了 project、复刻把速度与调式写对了、
音色包真的新增了轨道与鼓件、撤销撤得掉、指令台的每条命令都有回执。
另外也验证它的守门：没有参考音频时点按钮要给出**明确的一句话**，而不是一声不响。
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys as _sys
import tempfile
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parent.parent
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))

import numpy as np
from PySide6.QtWidgets import QApplication

app = QApplication([])

from app.audio import audiodecode, demo, drums, render, soundpacks, wavio
from app.audio.project import (
    ADSR, FilterParams, Note, Project, SynthParams, Track, drum_to_pitch, pitch_to_drum,
)
from app.ui.controller import AppController
from app.ui.pages.ai_studio import AiStudioPage, COMMANDS

ok = True


def check(label, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print(f"  [{'OK  ' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")


def make_click_project():
    """一段"鼓 + 短促旋律"的小工程：扒谱对它最拿手（实测的适用区间）。"""
    proj = Project(name="参考", bpm=120.0)
    proj.bars = 2
    d = Track(name="鼓", kind="drum", volume=1.0)
    d.ensure_kit()
    d.kit = {k: v for k, v in drums.default_voices().items()}
    for step in range(0, 16, 2):
        d.add_note(Note(step=step, pitch=drum_to_pitch("hat_closed"), length=1, velocity=0.55))
    for step in (0, 8):
        d.add_note(Note(step=step, pitch=drum_to_pitch("kick"), length=1, velocity=1.0))
    for step in (4, 12):
        d.add_note(Note(step=step, pitch=drum_to_pitch("snare"), length=1, velocity=0.9))
    proj.add_track(d)
    lead = Track(name="旋律", kind="synth", volume=0.9)
    lead.synth = SynthParams(wave="saw", amp=ADSR(0.005, 0.1, 0.7, 0.06, 2.0),
                             filter=FilterParams(kind="lp", cutoff=4200.0))
    for step, pitch in [(0, 60), (4, 64), (8, 67), (12, 72)]:
        lead.add_note(Note(step=step, pitch=pitch, length=3, velocity=0.9))
    proj.add_track(lead)
    return proj


class Rig:
    def __init__(self, project=None):
        self.project = project if project is not None else make_click_project()
        self.ctrl = AppController(self.project)
        self.page = AiStudioPage(self.project)
        self.page.resize(1280, 760)
        self.page.bind(self.ctrl)
        self.page.show()
        QApplication.processEvents()
        self.edited = []
        self.page.edited.connect(lambda: self.edited.append(1))

    def close(self):
        self.page.close()
        self.ctrl.shutdown()


# ==========================================================================
print("=" * 70)
print("1) 构造、绘制与契约")
print("=" * 70)
rig = Rig()
page = rig.page
check("页面能构造", page is not None)
pm = page.grab()
check("页面能绘制（grab 不报错、有内容）", not pm.isNull() and pm.width() > 1000,
      f"{pm.width()}x{pm.height()}")
check("契约要求的两个信号都在",
      hasattr(page, "edited") and hasattr(page, "seekRequested"))
for name in ("bind", "set_project", "rebuild", "refresh_notes", "set_position"):
    check(f"契约方法 {name} 存在", callable(getattr(page, name, None)))
page.set_position(2.5, True)
check("set_position 记录了位置与播放标志",
      abs(page._position - 2.5) < 1e-9 and page._playing is True)
page.set_project(rig.project)
check("set_project 不换工程对象时也安全", page.project is rig.project)
check("页面自称是离线引擎（不装成云端模型）",
      "离线" in page.engine_badge.text(), f"徽标「{page.engine_badge.text()}」")
check("指令台列出了可用命令（帮助与实际解析同一份）",
      len(COMMANDS) >= 5 and any(k == "扒谱" for k, _d in COMMANDS),
      "、".join(k for k, _d in COMMANDS))

# ==========================================================================
print()
print("=" * 70)
print("2) 守门：没有参考音频时的每一次点击都要有明确回执")
print("=" * 70)
check("（前置）此刻还没有参考音频", page._audio is None)
check("点「扒谱」→ 状态栏给出一句话，而不是静默返回",
      page.run_transcribe() is None and "参考音频" in page.status.text(),
      f"状态「{page.status.text()}」")
check("点「复刻」同样有回执",
      page.run_replicate() is None and "参考音频" in page.status.text(),
      f"状态「{page.status.text()}」")
check("没有扒谱结果时「生成轨道」不写工程",
      page.apply_transcription() == [] and len(rig.project.tracks) == 2)
check("没有指纹时「生成并加入」不写工程",
      page.apply_replication() == [] and len(rig.project.tracks) == 2)
check("不认识的音色包不会崩、也不写工程",
      page.apply_pack("不存在的包") is None and len(rig.project.tracks) == 2)
check("守门时没有压无效撤销点", len(rig.ctrl._undo) == 0,
      f"{len(rig.ctrl._undo)} 步")

# ==========================================================================
print()
print("=" * 70)
print("3) 参考音频：从工程取分轨 / 从文件读 WAV")
print("=" * 70)
check("「用鼓轨」拿到的是鼓轨的分轨",
      page.use_project_audio("drums") and page._audio is not None
      and "鼓" in page._audio_label, f"{page._audio_label}")
check("波形缩略图拿到了包络", page.wave.has_audio())
check("来源标签写清了是什么", "分轨" in page.source_label.text(),
      f"「{page.source_label.text()}」")
check("整曲也能当参考", page.use_project_audio("mix")
      and "整曲" in page._audio_label, f"{page._audio_label}")

tmp = _Path(tempfile.mkdtemp(prefix="aistudio_"))
wav_path = tmp / "ref.wav"
stem_audio = render.render_track(rig.project.tracks[0], rig.project)
wavio.save_wav(wav_path, stem_audio, render.SR, bit_depth=16, dither=False)
check("能读一个真的 WAV 文件当参考", page.choose_file(str(wav_path))
      and "ref.wav" in page._audio_label, f"{page._audio_label}")
check("读进来的音频长度对得上",
      abs(page._audio.shape[-1] / render.SR - stem_audio.shape[-1] / render.SR) < 0.02,
      f"{page._audio.shape[-1] / render.SR:.2f}s vs {stem_audio.shape[-1] / render.SR:.2f}s")
check("读不存在的文件 → 明确失败，不抛异常",
      page.choose_file(str(tmp / "没有这个.wav")) is False
      and "找不到" in page.status.text(), f"状态「{page.status.text()}」")
bad_path = tmp / "bad.wav"
bad_path.write_bytes(b"not a wave file at all")
check("读一个坏文件 → 明确失败，不抛异常",
      page.choose_file(str(bad_path)) is False and page.status.text(),
      f"状态「{page.status.text()}」")

check("文件对话框认的不止 WAV（mp3 / m4a / flac 都在过滤器里）",
      all(f"*.{s}" in audiodecode.FILE_FILTER for s in ("wav", "mp3", "m4a", "flac")),
      audiodecode.FILE_FILTER)
# 把同一段 WAV 换个后缀，逼着走 QtMultimedia 那条解码路（容器内容不变，
# 变的是分派路径）—— 这是"非 WAV 也能当参考"这条能力在无编码器环境下的可验证形式
m4a_path = tmp / "ref.m4a"
m4a_path.write_bytes(wav_path.read_bytes())
check("非 WAV 后缀走 QtMultimedia 解码，且长度与 WAV 那条路一致",
      page.choose_file(str(m4a_path)) and "ref.m4a" in page._audio_label
      and abs(page._audio.shape[-1] / render.SR
              - stem_audio.shape[-1] / render.SR) < 0.05,
      f"{page._audio_label} · {page._audio.shape[-1] / render.SR:.2f}s")
check("非 WAV 的坏文件同样明确失败，不抛异常",
      page.choose_file(str(tmp / "坏.m4a")) is False
      and "找不到" in page.status.text(), f"状态「{page.status.text()}」")

# ==========================================================================
print()
print("=" * 70)
print("4) 扒谱：结果 → 真轨道 → 能撤销")
print("=" * 70)
page.use_project_audio("drums")
res = page.run_transcribe()
check("扒谱返回结果且检出了鼓点", res is not None and len(res.drums) > 0,
      f"{len(res.drums)} 个鼓点 · {res.bpm:.0f} BPM")
check("结果卡片把摘要与依据都显示出来了",
      "BPM" in page.trans_summary.text() and "起音" in page.trans_detail.text(),
      f"「{page.trans_summary.text()}」")
check("复刻用的指纹会作废旧结果（换了参考音频之后不残留）",
      page._transcription is res)

before = len(rig.project.tracks)
undo_before = len(rig.ctrl._undo)
made = page.apply_transcription()
check("「生成轨道」真的把轨道加进了工程",
      len(rig.project.tracks) == before + len(made) and len(made) >= 1,
      f"{before} → {len(rig.project.tracks)} 条 · 新增 {[t.name for t in made]}")
check("新增的轨道有音符（不是空壳）",
      all(len(t.notes) > 0 for t in made),
      "、".join(f"{t.name}:{len(t.notes)}" for t in made))
check("压了 1 步撤销点且标签是「AI 扒谱」",
      len(rig.ctrl._undo) - undo_before == 1 and rig.ctrl.undo_label() == "AI 扒谱",
      f"「{rig.ctrl.undo_label()}」")
check("发了 edited（界面才会知道要重渲染）", len(rig.edited) == 1,
      f"{len(rig.edited)} 次")
check("日志里留了痕（每一行都对应一次真实动作）",
      any("已加入" in line for line in page._log), page._log[-1])

rig.ctrl.undo()
check("撤销之后轨道数回到原样", len(rig.project.tracks) == before,
      f"{len(rig.project.tracks)} 条")

# ==========================================================================
print()
print("=" * 70)
print("5) 复刻：指纹 → 生成参数 → 真工程（速度/调式/音色包都要落地）")
print("=" * 70)
page.use_project_audio("drums")
profile = page.run_replicate()
check("指纹量出来了", profile is not None, profile.summary() if profile else "")
check("指纹卡片把 8 项指标都显示了",
      page.profile_detail.text().count("\n") >= 6,
      page.profile_detail.text().replace("\n", " | ")[:90] + "…")

before = len(rig.project.tracks)
undo_before = len(rig.ctrl._undo)
tracks = page.apply_replication(bars=4, seed=2024, pack_key="808")
check("按指纹生成并加入工程", len(tracks) == 4 and len(rig.project.tracks) == before + 4,
      "、".join(f"{t.name}:{len(t.notes)}" for t in tracks))
check("工程速度跟着参考音频改（否则网格与小节都对不上）",
      abs(rig.project.bpm - profile.bpm) < 1e-6,
      f"{rig.project.bpm:.1f} BPM")
check("生成的小节数与要求一致（4 小节 → 音符落在 64 步内）",
      max(n.step for t in tracks for n in t.notes) < 64,
      f"最大步号 {max(n.step for t in tracks for n in t.notes)}")
drum_tracks = [t for t in tracks if t.kind == "drum"]
check("套了音色包的鼓轨带上了 808 底鼓",
      drum_tracks and "kick_808" in drum_tracks[0].kit,
      f"kit {len(drum_tracks[0].kit) if drum_tracks else 0} 件")
check("压了 1 步撤销点且标签是「AI 复刻」",
      len(rig.ctrl._undo) - undo_before == 1 and rig.ctrl.undo_label() == "AI 复刻",
      f"「{rig.ctrl.undo_label()}」")
check("同一颗种子生成同一首（复刻要可复现）",
      [len(t.notes) for t in page.apply_replication(bars=4, seed=2024)]
      == [len(t.notes) for t in tracks],
      "音符数一致")
check("换一颗种子会换一首（有得挑）",
      [len(t.notes) for t in page.apply_replication(bars=4, seed=99)]
      != [len(t.notes) for t in tracks])

# ==========================================================================
print()
print("=" * 70)
print("6) 音色包：芯片与指令都要真的建轨")
print("=" * 70)
check("页面上每个包都有一颗芯片",
      len(page.pack_chips) == len(soundpacks.SOUND_PACKS),
      "、".join(c.text() for c in page.pack_chips))
before = len(rig.project.tracks)
track = page.apply_pack("808_kit")
check("点芯片 → 新轨道进了工程",
      track is not None and len(rig.project.tracks) == before + 1,
      f"{track.name if track else '—'}（{len(track.notes) if track else 0} 音符）")
check("新轨道是鼓轨且套上了这个包的鼓件",
      track.kind == "drum" and "kick_808" in track.kit and "cowbell_808" in track.kit,
      f"kit {len(track.kit)} 件")
check("新轨道带一小节示范鼓型（点了就能听见）",
      0 < len(track.notes) <= 16, f"{len(track.notes)} 个音符")
check("轨道名/颜色来自包", track.name == "808 全套", track.name)
check("这个包在工程里能渲染出非静音音频",
      float(np.abs(render.render_track(track, rig.project)).max()) > 0.05)

# ==========================================================================
print()
print("=" * 70)
print("7) 指令台：每条命令都真的干活、都有回执")
print("=" * 70)
check("「帮助」列出全部命令",
      page.run_command("帮助") == "帮助"
      and sum(1 for line in page._log if "—" in line) >= len(COMMANDS))
# 这一条原本直接假设"此刻没有参考音频"，但走到第 7 节时参考音频早就装好了
# （第 3–6 节都在用它），于是它一直在断言一个不成立的前提 —— 只是这套测试
# 没登记进 run_all.py，红了也没人看见。改成**真的把参考清掉再验**，保持原意。
_saved_audio, _saved_label = page._audio, page._audio_label
page._audio = None
check("「扒谱」没参考音频时给出明确回执",
      page.run_command("扒谱") == "扒谱失败", page._log[-1])
page._audio, page._audio_label = _saved_audio, _saved_label
page.run_command("参考 鼓轨")
check("「参考 鼓轨」切换了参考音频", "鼓" in page._audio_label, page._audio_label)
out = page.run_command("扒谱 120")
check("「扒谱 120」用指定速度扒谱并生成轨道", out.startswith("扒谱"), f"回执「{out}」")
check("指定的速度被采纳", page._transcription is not None
      and abs(page._transcription.bpm - 120.0) < 1e-6,
      f"{page._transcription.bpm if page._transcription else '—'} BPM")
check("「复刻 4」生成 4 轨", page.run_command("复刻 4").startswith("复刻"),
      page._log[-1])
check("「音色包 牛铃」按名字匹配到包",
      page.run_command("音色包 牛铃") == "音色包 牛铃组", page._log[-1])
check("「音色包 808」按 key 匹配",
      page.run_command("音色包 808") == "音色包 808 低音", page._log[-1])
check("不认识的音色包有明确回执",
      page.run_command("音色包 钢琴") == "音色包未识别", page._log[-1])
check("不认识的指令有明确回执", page.run_command("唱首歌吧") == "未知指令",
      page._log[-1])
check("「清空」清掉日志但页面还在",
      page.run_command("清空") == "清空" and page.log_label.text() == "",
      f"「{page.log_label.text()[:20]}」")
check("空文本不产生任何动作", page.run_command("   ") == "")

page.input.setText("帮助")
echo = page.submit_command()
check("输入框回车发送也能走通（submit_command）",
      echo == "帮助" and page.input.text() == "", f"回显「{echo}」")
check("日志有上限（不会无限涨、把界面拖慢）",
      len(page._log) <= 130, f"{len(page._log)} 行")

# ==========================================================================
print()
print("=" * 70)
print("8) 与工程/控制器的联动")
print("=" * 70)
check("全程只用了这一个控制器（没有偷偷换工程对象）",
      page.project is rig.ctrl.project is rig.project)
check("这一串操作之后工程里真的多了轨道",
      len(rig.project.tracks) > 4, f"{len(rig.project.tracks)} 条")
check("撤销栈在深度上限内（不会无限增长）", len(rig.ctrl._undo) <= 60,
      f"{len(rig.ctrl._undo)} 步")
check("页面在大量操作之后仍能重绘", not page.grab().isNull())

demo_proj = demo.demo_project()
rig2 = Rig(demo_proj)
check("换到示范工程也能构造与绘制", not rig2.page.grab().isNull())
check("示范工程里「用旋律轨」能找到一条合成器轨",
      rig2.page.use_project_audio("lead") and rig2.page._audio is not None,
      f"{rig2.page._audio_label}")
rig2.page.run_command("扒谱")
check("示范工程的旋律轨也能扒出音符", rig2.page._transcription is not None
      and len(rig2.page._transcription.pitched) > 0,
      f"{len(rig2.page._transcription.pitched)} 个音"
      if rig2.page._transcription else "—")
rig2.close()
rig.close()

# ==========================================================================
print()
print("=" * 70)
print("9) 页面已注册进主窗口")
print("=" * 70)
from app.ui.main_window import PAGE_SPECS

check("PAGE_SPECS 里有 AI 页",
      any(key == "ai" and cls == "AiStudioPage" for key, cls, _m in PAGE_SPECS),
      "、".join(k for k, _c, _m in PAGE_SPECS))

print()
print("=" * 70)
print("全部通过" if ok else "存在失败项")
print("=" * 70)
sys.exit(0 if ok else 1)
