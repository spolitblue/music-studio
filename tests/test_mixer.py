"""混音台页面的验证。

覆盖：构造与绘制、推子/旋钮真的改了工程数据、静音独奏的字段与视觉同步、
母带四项参数、拖动一整笔只占一步撤销、参考电平分析走真实渲染路径、
母带实时电平的回落、空工程不崩。

追加（缺陷修复补的用例）：
* 静音/独奏**必须压撤销点**，Ctrl+Z 能撤掉，且会把重做栈清空；
* 「清除静音与独奏」的撤销点压在清零**之前**，撤销能把那组状态原样恢复；
* 试听片段真的是「最响的连续 2.5 秒」，不是「最热 0.25s 块居中」；
* 渲染失败必须有可见提示，不允许静默降级成「这条轨道没声音」。
"""

import os
import sys
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys as _sys
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parent.parent
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))

import time

import numpy as np
from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtGui import QImage, QMouseEvent
from PySide6.QtWidgets import QApplication

app = QApplication([])

from app.audio import demo, render
from app.audio.player import PlayerState
from app.audio.project import Note, Project, Track
from app.ui.controller import AppController
from app.ui.pages.mixer import (
    AUDITION_SECONDS, FADER_MAX, METER_FLOOR_DB, MixerPage, VerticalFader,
    VerticalMeter, _audition_excerpt,
)

ok = True


def check(label, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print(f"  [{'OK  ' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")


# --------------------------------------------------------------------------
# 鼠标事件工具（可以单独指定全局坐标 —— 旋钮是按全局位移算值的）
# --------------------------------------------------------------------------


def _ev(kind, x, y, btn, buttons, mod=Qt.KeyboardModifier.NoModifier, gy=None):
    gy = y if gy is None else gy
    return QMouseEvent(kind, QPointF(x, y), QPointF(x, gy), btn, buttons, mod)


def press(w, x, y, mod=Qt.KeyboardModifier.NoModifier, gy=None):
    w.mousePressEvent(_ev(QMouseEvent.Type.MouseButtonPress, x, y,
                          Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, mod, gy))


def drag(w, x, y, mod=Qt.KeyboardModifier.NoModifier, gy=None):
    w.mouseMoveEvent(_ev(QMouseEvent.Type.MouseMove, x, y,
                         Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, mod, gy))


def release(w, x, y, gy=None):
    w.mouseReleaseEvent(_ev(QMouseEvent.Type.MouseButtonRelease, x, y,
                            Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier, gy))


def click(w, x, y, gy=None):
    press(w, x, y, gy=gy)
    release(w, x, y, gy=gy)


def double_click(w, x, y):
    w.mouseDoubleClickEvent(_ev(QMouseEvent.Type.MouseButtonDblClick, x, y,
                                Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton))


def knob_drag(knob, delta_px):
    """按 GlassKnob 的行程（140px 走满量程）拖动旋钮，返回新值。"""
    gy0 = 400.0
    press(knob, knob.width() / 2, knob.height() / 2, gy=gy0)
    drag(knob, knob.width() / 2, knob.height() / 2, gy=gy0 - delta_px)
    release(knob, knob.width() / 2, knob.height() / 2, gy=gy0 - delta_px)
    return knob.value()


def alpha_sum(widget):
    """控件渲染结果的透明通道总和 —— 用来判断"开"和"关"画出来是不是同一个东西。"""
    img = widget.grab().toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    ptr = img.constBits()
    arr = np.frombuffer(ptr, dtype=np.uint8,
                        count=img.width() * img.height() * 4).reshape(img.height(),
                                                                      img.width(), 4)
    return int(arr[..., 3].sum())


class Rig:
    def __init__(self, project=None):
        self.project = project if project is not None else demo.demo_project()
        self.ctrl = AppController(self.project)
        self.page = MixerPage(self.project)
        self.page.resize(1320, 780)
        self.page.bind(self.ctrl)
        self.page.show()
        QApplication.processEvents()

    def close(self):
        self.page.close()
        self.ctrl.shutdown()


# ==========================================================================
print("=" * 70)
print("1) 构造与绘制")
print("=" * 70)
rig = Rig()
page = rig.page
proj = rig.project

check("通道条数量 = 轨道数 + 母带", len(page._strips) == len(proj.tracks),
      f"{len(page._strips)} 条 + 母带")
check("母带条在布局最右", page.row.indexOf(page._master) == page.row.count() - 1,
      f"index={page.row.indexOf(page._master)} / count={page.row.count()}")
check("母带条前有分隔线",
      page.row.itemAt(page.row.count() - 2).widget() is page._divider)
check("通道条顺序与轨道一致",
      all(page.row.itemAt(i).widget() is page._strips[i] for i in range(len(page._strips))))
check("通道条按轨道色上色",
      page._strips[0]._color.name().lower() == proj.tracks[0].color.lower(),
      f"{page._strips[0]._color.name()} vs {proj.tracks[0].color}")

pm = page.grab()
check("页面能绘制（grab 不报错）", not pm.isNull() and pm.width() > 1000,
      f"{pm.width()}x{pm.height()}")
check("通道条能单独绘制", not page._strips[0].grab().isNull())
check("母带条能单独绘制", not page._master.grab().isNull())

f = page._strips[0].fader
check("竖向推子拿到了可用的高度", f.height() >= 120, f"{f.width()}x{f.height()}")
check("推子是竖的（高 > 宽）", f.height() > f.width() * 2,
      f"{f.width()}x{f.height()}")
check("推子槽在控件内部",
      f._track_rect().top() >= 0 and f._track_rect().bottom() <= f.height(),
      f"{f._track_rect()}")
check("刻度参考线覆盖整个量程",
      len(f._ticks) >= 3 and f._ticks[0] == 0.0 and f._ticks[-1] == FADER_MAX,
      f"{f._ticks}")

# ==========================================================================
print()
print("=" * 70)
print("2) 推子拖动真的改了 track.volume")
print("=" * 70)
strip = page._strips[0]
track = strip.track
fader = strip.fader

edited_hits = []
page.edited.connect(lambda: edited_hits.append(1))

target = 1.2
y = fader._y_for_value(target)
press(fader, fader.width() / 2, y)
check("按下即到位（点哪跳哪）", abs(track.volume - target) < 1e-6,
      f"推子 {fader.value():.4f} / 轨道 {track.volume:.4f}")
drag(fader, fader.width() / 2, fader._track_rect().top() - 40)
check("拖到顶被钳在 1.5", abs(track.volume - FADER_MAX) < 1e-6, f"{track.volume:.4f}")
drag(fader, fader.width() / 2, fader._track_rect().bottom() + 40)
check("拖到底 = 0.0", abs(track.volume) < 1e-6, f"{track.volume:.4f}")
release(fader, fader.width() / 2, fader._track_rect().bottom() + 40)
check("界面控件与数据模型一致", abs(fader.value() - track.volume) < 1e-9,
      f"{fader.value():.4f} / {track.volume:.4f}")
check("改动发出了 edited 信号", len(edited_hits) > 0, f"{len(edited_hits)} 次")

# Shift 精细拖动：不跳变，只做相对微调
track.volume = 1.0
fader.set_value(1.0)
press(fader, fader.width() / 2, fader._y_for_value(0.2),
      Qt.KeyboardModifier.ShiftModifier)
check("Shift 拖动不跳变", abs(track.volume - 1.0) < 1e-9, f"{track.volume:.4f}")
drag(fader, fader.width() / 2, fader._y_for_value(0.2) - 20,
     Qt.KeyboardModifier.ShiftModifier)
delta = track.volume - 1.0
check("Shift 上拖是正向微调", 0 < delta < 0.15, f"+{delta:.4f}")
release(fader, fader.width() / 2, fader._y_for_value(0.2) - 20)

double_click(fader, fader.width() / 2, fader._y_for_value(0.4))
check("双击推子回 1.0", abs(track.volume - 1.0) < 1e-9, f"{track.volume:.4f}")

# ==========================================================================
print()
print("=" * 70)
print("3) 声像旋钮改了 track.pan")
print("=" * 70)
track.pan = 0.0
strip.pan.set_value(0.0)
v = knob_drag(strip.pan, 35)          # 35px = 半量程（140px 走满 -1~1）
check("上拖使声像右移", abs(v - 0.5) < 0.02, f"旋钮 {v:+.3f}")
check("数据模型同步", abs(track.pan - v) < 1e-9, f"track.pan={track.pan:+.3f}")
v = knob_drag(strip.pan, -140)
check("下拖使声像左移到 -1（钳制）", abs(track.pan + 1.0) < 1e-6,
      f"track.pan={track.pan:+.3f}")
double_click(strip.pan, strip.pan.width() / 2, strip.pan.height() / 2)
check("双击声像旋钮回中", abs(track.pan) < 1e-9, f"track.pan={track.pan:+.3f}")

# ==========================================================================
print()
print("=" * 70)
print("4) 混响 / 延迟发送量")
print("=" * 70)
track.send_reverb = 0.0
track.send_delay = 0.0
strip.send_reverb.set_value(0.0)
strip.send_delay.set_value(0.0)
v = knob_drag(strip.send_reverb, 140)
check("混响发送可调到 1.0", abs(v - 1.0) < 1e-6 and abs(track.send_reverb - 1.0) < 1e-9,
      f"{track.send_reverb:.3f}")
v = knob_drag(strip.send_reverb, -140)
check("混响发送下限 0.0", abs(track.send_reverb) < 1e-6, f"{track.send_reverb:.3f}")
v = knob_drag(strip.send_delay, 42)   # 42/140 = 0.3
check("延迟发送可调（约 0.3）", 0.25 < track.send_delay < 0.35, f"{track.send_delay:.3f}")
double_click(strip.send_delay, strip.send_delay.width() / 2,
             strip.send_delay.height() / 2)
check("双击发送旋钮归零", abs(track.send_delay) < 1e-9, f"{track.send_delay:.3f}")

# ==========================================================================
print()
print("=" * 70)
print("5) 静音 / 独奏：字段 + 视觉状态")
print("=" * 70)
before_off = alpha_sum(strip.mute_dot)
track.mute = False
strip.mute_dot.set_on(False)
click(strip.mute_dot, 10, 10)
check("点一下打开静音", track.mute is True, f"mute={track.mute}")
check("开关状态与字段同步", strip.mute_dot.is_on() is True)
after_on = alpha_sum(strip.mute_dot)
check("开/关的绘制结果不同（不是同一个样子）", after_on != before_off,
      f"alpha 和 {before_off} → {after_on}")
check("打开时填充更实（像素更多）", after_on > before_off)

track.mute = False
page.refresh_notes()
check("外部改数据后 refresh_notes 能把界面拉回同步",
      strip.mute_dot.is_on() is False)

click(strip.solo_dot, 10, 10)
check("点一下打开独奏", track.solo is True and strip.solo_dot.is_on() is True)
check("独奏语义：有独奏时其它轨不可闻",
      proj.any_solo() and not proj.is_audible(page._strips[1].track))

# 电平参考值必须先测出来，静音轨的表才会落到底
page.analyze_all_now()
page.refresh_notes()
other = page._strips[1]
check("被独奏挤掉的通道电平表落到表底",
      other.meter.peak_db() <= METER_FLOOR_DB + 1e-9,
      f"{other.meter.peak_db():.1f} dB")
check("不可闻通道的表被压暗", other.meter._dimmed is True)

track.solo = False
strip.solo_dot.set_on(False)
page.refresh_notes()
check("取消独奏后其它通道的表恢复", other.meter.peak_db() > METER_FLOOR_DB,
      f"{other.meter.peak_db():+.1f} dB")
check("恢复后不再压暗", other.meter._dimmed is False)

# ==========================================================================
print()
print("=" * 70)
print("6) 母带条：总线音量 / 限幅阈值 / 混响空间 / 延迟反馈")
print("=" * 70)
master = page._master
mf = master.fader
press(mf, mf.width() / 2, mf._y_for_value(0.6))
release(mf, mf.width() / 2, mf._y_for_value(0.6))
check("总线推子改了 master.volume", abs(proj.master.volume - 0.6) < 1e-6,
      f"{proj.master.volume:.3f}")

lim = master.limiter
press(lim, lim.width() / 2, lim._y_for_value(-6.0))
release(lim, lim.width() / 2, lim._y_for_value(-6.0))
check("限幅阈值可调到 -6 dB", abs(proj.master.limiter_threshold + 6.0) < 1e-6,
      f"{proj.master.limiter_threshold:+.2f} dB")
check("电平表上的阈值标记跟着动",
      abs((master.meter.marker_db() or 0) + 6.0) < 1e-6,
      f"marker={master.meter.marker_db()}")
drag_lim = lim._y_for_value(-40.0)
press(lim, lim.width() / 2, drag_lim)
release(lim, lim.width() / 2, drag_lim)
check("限幅阈值下限钳在 -12 dB", abs(proj.master.limiter_threshold + 12.0) < 1e-6,
      f"{proj.master.limiter_threshold:+.2f} dB")
press(lim, lim.width() / 2, lim._track_rect().top() - 30)
release(lim, lim.width() / 2, lim._track_rect().top() - 30)
check("限幅阈值上限钳在 0 dB", abs(proj.master.limiter_threshold) < 1e-6,
      f"{proj.master.limiter_threshold:+.2f} dB")

proj.master.reverb.params["size"] = 0.0
master.reverb.set_value(0.0)
knob_drag(master.reverb, 140)
check("混响空间改到 params['size']",
      abs(float(proj.master.reverb.params["size"]) - 1.0) < 1e-6,
      f"{float(proj.master.reverb.params['size']):.3f}")
proj.master.delay.params["feedback"] = 0.0
master.delay.set_value(0.0)
knob_drag(master.delay, 63)           # 63/140*(0.9-0) ≈ 0.405
fb = float(proj.master.delay.params["feedback"])
check("延迟反馈改到 params['feedback'] 且在 0~0.9 内", 0.35 < fb < 0.45,
      f"{fb:.3f}")
knob_drag(master.delay, 400)
check("延迟反馈上限钳在 0.9", abs(float(proj.master.delay.params["feedback"]) - 0.9) < 1e-6,
      f"{float(proj.master.delay.params['feedback']):.3f}")

# ==========================================================================
print()
print("=" * 70)
print("7) 一整笔拖动只占一步撤销")
print("=" * 70)
u0 = len(rig.ctrl._undo)
fader.set_value(track.volume)
y0 = fader._y_for_value(track.volume)
press(fader, fader.width() / 2, y0)
for i in range(1, 11):
    drag(fader, fader.width() / 2, y0 - i * 6)
release(fader, fader.width() / 2, y0 - 60)
pushed = len(rig.ctrl._undo) - u0
check("拖一整笔（按下 + 10 次移动）只压 1 步", pushed == 1, f"压了 {pushed} 步")
check("拖动确实改了音量", abs(track.volume - 1.0) > 0.1, f"{track.volume:.3f}")

u1 = len(rig.ctrl._undo)
press(fader, fader.width() / 2, fader._y_for_value(0.9))
release(fader, fader.width() / 2, fader._y_for_value(0.9))
check("第二笔拖动再加 1 步", len(rig.ctrl._undo) - u1 == 1,
      f"压了 {len(rig.ctrl._undo) - u1} 步")

u2 = len(rig.ctrl._undo)
press(fader, fader.width() / 2, fader._y_for_value(track.volume))
release(fader, fader.width() / 2, fader._y_for_value(track.volume))
check("按在手柄原处（值没变）不占撤销步", len(rig.ctrl._undo) == u2,
      f"压了 {len(rig.ctrl._undo) - u2} 步")

u3 = len(rig.ctrl._undo)
knob_drag(strip.pan, 70)
check("旋钮拖一整笔也只占 1 步", len(rig.ctrl._undo) - u3 == 1,
      f"压了 {len(rig.ctrl._undo) - u3} 步")
check("撤销标签是中文动作名", rig.ctrl.undo_label() == "调整声像",
      f"「{rig.ctrl.undo_label()}」")

# ==========================================================================
print()
print("=" * 70)
print("8) 撤销后界面与工程同步")
print("=" * 70)
strip.fader.set_value(0.8)
track.volume = 0.8
rig.ctrl.push_undo("调整音量")
strip.fader.set_value(1.3)
track.volume = 1.3
rig.ctrl.undo()
page.rebuild()
QApplication.processEvents()
check("撤销后控件跟着回到旧值",
      abs(page._strips[0].fader.value() - page._strips[0].track.volume) < 1e-9
      and abs(page._strips[0].track.volume - 0.8) < 1e-6,
      f"推子={page._strips[0].fader.value():.3f} 轨道={page._strips[0].track.volume:.3f}")

# ==========================================================================
print()
print("=" * 70)
print("9) 分轨参考电平（真实渲染路径）")
print("=" * 70)
import time
t0 = time.perf_counter()
peak, rms = page.analyze_track(proj.tracks[0])
t1 = time.perf_counter()
check("分析鼓轨得到非静音电平", peak > METER_FLOOR_DB + 1.0,
      f"峰值 {peak:+.1f} dB，RMS {rms:+.1f} dB，耗时 {t1 - t0:.2f}s")
check("峰值 >= RMS", peak >= rms - 1e-6, f"{peak:+.1f} >= {rms:+.1f}")

# 真正把这次分析的结果灌进缓存与界面（页面内部走的也是这条路）
page.analyze_all_now()
st = page._strips[0]
check("分析结果进了通道条的电平表",
      st.meter.peak_db() > METER_FLOOR_DB, f"{st.meter.peak_db():+.1f} dB")

v_before = st.meter.peak_db()
st.track.volume = 0.0
page._refresh_meter(st)
check("音量拉到 0，表落到表底（不重新渲染就算出来了）",
      st.meter.peak_db() <= METER_FLOOR_DB + 1e-9, f"{st.meter.peak_db():.1f} dB")
st.track.volume = 0.8
page._refresh_meter(st)
check("音量推回来，表跟着回来", st.meter.peak_db() > METER_FLOOR_DB,
      f"{v_before:+.1f} → {st.meter.peak_db():+.1f} dB")

empty = Track(name="空轨", kind="synth")
proj.add_track(empty)
page.rebuild()
page.analyze_all_now()
check("没有音符的轨道不崩、表读数为表底",
      page._levels[empty.id][0] <= METER_FLOOR_DB + 1e-9,
      f"{page._levels[empty.id][0]:.1f} dB")
page.analyze_all_now()
check("全部分析完成（队列已清空）", not page._queue)

# ==========================================================================
print()
print("=" * 70)
print("10) 试听片段（渲染路径产出的是真声音）")
print("=" * 70)
piece = page.excerpt(proj.tracks[0])
check("分析时顺手留下了试听片段", piece is not None and piece.size > 0,
      f"形状 {None if piece is None else piece.shape}")
if piece is not None:
    check("试听片段是立体声", piece.shape[0] == 2, f"{piece.shape[0]} 声道")
    check("试听片段非静音（真的是声音，不是空缓冲）",
          float(abs(piece).max()) > 0.001 and float(np.sqrt((piece ** 2).mean())) > 0.0005,
          f"峰值 {float(abs(piece).max()):.3f} / RMS {float(np.sqrt((piece**2).mean())):.4f}")
    check("片段长度在合理范围（0.5~3 秒）",
          0.5 < piece.shape[1] / 44100.0 <= 3.0,
          f"{piece.shape[1] / 44100.0:.2f}s")
    check("首尾做了淡入淡出（不会咔嚓）",
          abs(float(piece[0, 0])) < 1e-6 and abs(float(piece[0, -1])) < 1e-6,
          f"首 {piece[0, 0]:.6f} 尾 {piece[0, -1]:.6f}")

# 点试听：离屏下音频设备可能不可用，但**必须走到某条明确路径**并留下状态反馈。
# 旧版这里写的是 check(..., True)，永远不可能失败 —— 等于没测。
page._message("")                          # 先清空，确保看到的是这次留下的
page._audition_strip(page._strips[0])
aud_status = page._status.text()
dev_ok = page._controller.player.available
check("点试听留下了状态反馈，不是静默返回",
      bool(aud_status) and page.excerpt(page._strips[0].track) is not None,
      f"状态「{aud_status}」")
check("状态反馈与设备状态一致（成功则点名轨道，失败则说明原因）",
      aud_status == f"试听「{page._strips[0].track.name}」" if dev_ok
      else ("音频设备" in aud_status),
      f"设备可用={dev_ok} · 状态「{aud_status}」")

print()
print("=" * 70)
print("11) 母带实时电平（PlayerState 路径）")
print("=" * 70)
# 注意：第 9 节 rebuild 过页面，母带条是**新建的对象** —— 必须重新取，
# 拿旧的引用会测到一个已经脱离界面的死控件（这正是本节第一版失败的原因）。
master = page._master
mm = master.meter
page._on_transport(PlayerState(playing=True, peak_db=-6.0, rms_db=-14.0))
check("实时电平立即起（快起）", abs(mm.peak_db() + 6.0) < 1e-6
      and abs(mm.rms_db() + 14.0) < 1e-6,
      f"peak={mm.peak_db():+.1f} rms={mm.rms_db():+.1f}")
check("实时状态亮起指示灯", mm._live is True)

page._on_transport(PlayerState(playing=False, peak_db=-120.0, rms_db=-120.0))
first = mm.peak_db()
mm._tick()
second = mm.peak_db()
check("停播后是慢慢落，不是瞬间掉到底", second < first and second > METER_FLOOR_DB,
      f"{first:+.1f} → {second:+.1f} → 底 {METER_FLOOR_DB:.0f}")
for _ in range(60):
    mm._tick()
check("几次回落之后收敛到表底", abs(mm.peak_db() - METER_FLOOR_DB) < 1e-6,
      f"{mm.peak_db():.1f} dB")
check("峰值保持线不会低于当前电平，且跟着落下来",
      mm.hold_db() >= mm.peak_db() - 1e-6 and abs(mm.hold_db() - mm.peak_db()) < 0.5,
      f"hold={mm.hold_db():.1f} peak={mm.peak_db():.1f}")

page._on_render_finished(type("S", (), {"peak_db": -1.4, "rms_db": -9.8})())
check("渲染完成后母带表回落到整曲统计值",
      abs(mm.peak_db() + 1.4) < 1e-6, f"{mm.peak_db():+.1f} dB")

# ==========================================================================
print()
print("=" * 70)
print("12) 空工程 / 单轨工程不崩")
print("=" * 70)
bare = Project(name="空工程")
rig2 = Rig(bare)
check("空工程能构造页面", rig2.page._master is not None and len(rig2.page._strips) == 0)
check("空工程页面能绘制", not rig2.page.grab().isNull())
rig2.page.analyze_all_now()
check("空工程分析不崩、队列为空", not rig2.page._queue)
rig2.page.rebuild()
check("空工程 rebuild 不崩", len(rig2.page._strips) == 0)
bare.add_track(Track(name="唯一一轨", kind="drum", volume=0.9))
rig2.page.rebuild()
rig2.page.analyze_all_now()
check("加一轨后通道条出现", len(rig2.page._strips) == 1)
check("鼓轨（无音符）分析不崩", rig2.page._levels[bare.tracks[0].id][0] <= METER_FLOOR_DB + 1e-9)

# 契约接口：换整个工程 / 播放位置更新 / 两个约定信号
other_proj = demo.demo_project()
rig2.page.set_project(other_proj)
check("set_project 换工程后重建了通道条",
      len(rig2.page._strips) == len(other_proj.tracks) and rig2.page.project is other_proj,
      f"{len(rig2.page._strips)} 条")
check("换工程后仍能绘制", not rig2.page.grab().isNull())
rig2.page.set_position(2.5, True)
check("set_position 记录了位置与播放标志",
      abs(rig2.page._position - 2.5) < 1e-9 and rig2.page._playing is True,
      f"pos={rig2.page._position} playing={rig2.page._playing}")
check("契约要求的两个信号都在",
      hasattr(rig2.page, "edited") and hasattr(rig2.page, "seekRequested"))
# refresh_notes 的旧断言是 check(..., True)，同样是永远为真的凑数项。
# 换成"它真的把界面拉回了数据模型"—— 先把控件扭到别处，再叫它同步。
other_proj.master.volume = 0.42
rig2.page._master.fader.set_value(1.0)
rig2.page.refresh_notes()
check("refresh_notes 把母带推子拉回工程里的 0.42",
      abs(rig2.page._master.fader.value() - 0.42) < 1e-9,
      f"推子={rig2.page._master.fader.value():.3f} 工程={other_proj.master.volume:.3f}")
check("refresh_notes 顺带刷新了计数标签",
      f"{len(other_proj.tracks)} 条通道" in rig2.page._count.text(),
      f"「{rig2.page._count.text()}」")
rig2.close()

# 边界：极端参数
edge = demo.demo_project()
edge.tracks[0].volume = 99.0          # 超范围
edge.tracks[0].pan = -5.0
edge.master.limiter_threshold = -999.0
rig3 = Rig(edge)
check("超范围的音量被控件钳制", rig3.page._strips[0].fader.value() <= FADER_MAX,
      f"{rig3.page._strips[0].fader.value()}")
check("超范围的声像被钳制", rig3.page._strips[0].pan.value() >= -1.0,
      f"{rig3.page._strips[0].pan.value():+.2f}")
check("超范围的限幅阈值被钳制",
      rig3.page._master.limiter.value() >= -12.0,
      f"{rig3.page._master.limiter.value():+.2f}")
check("极端参数下仍能绘制", not rig3.page.grab().isNull())
rig3.close()

# ==========================================================================
print()
print("=" * 70)
print("13) 静音/独奏与「清除」按钮的撤销点")
print("=" * 70)
# 这一节就是复核者那段复现脚本的正式化：点开关 → 数撤销深度 → Ctrl+Z → 看字段。
# 第 5 节只看了字段与像素，第 7 节只看了推子/旋钮的粒度，中间这块空白正是
# 「点静音压不了撤销点」能一路漏过测试的原因。
rig4 = Rig()
page4 = rig4.page
ctrl4 = rig4.ctrl
proj4 = rig4.project

strip4 = page4._strips[0]
proj4.tracks[0].mute = False       # 从"没静音"开始，方便看这一步到底改没改
strip4.mute_dot.set_on(False)

u0 = len(ctrl4._undo)
click(strip4.mute_dot, 10, 10)
check("点静音压了 1 步撤销点", len(ctrl4._undo) == u0 + 1,
      f"_undo 深度 {u0} → {len(ctrl4._undo)}")
check("撤销标签是「静音」", ctrl4.undo_label() == "静音", f"「{ctrl4.undo_label()}」")
check("静音真的写进了工程", proj4.tracks[0].mute is True, f"mute={proj4.tracks[0].mute}")

page4._undo()                      # 走页面上的撤销按钮那条路（等价于 Ctrl+Z）
check("撤销之后静音字段被还原", proj4.tracks[0].mute is False,
      f"mute={proj4.tracks[0].mute}")
check("撤销之后界面上的静音圆点也跟着灭",
      page4._strips[0].mute_dot.is_on() is False,
      f"圆点={page4._strips[0].mute_dot.is_on()}")

# 独奏同理。顺带覆盖「push_undo 会清空重做栈」这个副作用 ——
# 上面那次 undo 刚把重做栈喂满，点独奏后它必须被清空，否则重做会跳回过期状态。
strip4 = page4._strips[0]          # 上一步 undo 触发了 rebuild，通道条是新对象
proj4.tracks[0].solo = False
strip4.solo_dot.set_on(False)
check("（前置）撤销之后重做栈非空", ctrl4.can_redo is True)
u1 = len(ctrl4._undo)
click(strip4.solo_dot, 10, 10)
check("点独奏压了 1 步撤销点", len(ctrl4._undo) == u1 + 1,
      f"_undo 深度 {u1} → {len(ctrl4._undo)}")
check("撤销标签是「独奏」", ctrl4.undo_label() == "独奏", f"「{ctrl4.undo_label()}」")
check("独奏真的写进了工程", proj4.tracks[0].solo is True, f"solo={proj4.tracks[0].solo}")
check("新的一笔编辑清空了重做栈（不会有过期重做）", ctrl4.can_redo is False)
page4._undo()
check("撤销之后独奏字段被还原", proj4.tracks[0].solo is False,
      f"solo={proj4.tracks[0].solo}")

# ---- 「清除静音与独奏」：撤销点必须压在清零之前 -------------------------
#
# 用复核者实测的那组状态：(静音,独奏) = [(T,F),(F,F),(F,F),(F,T),(F,F)]。
# 旧实现先清零再 push_undo，而 push_undo 是**当场**做 to_dict() 快照，
# 所以快照里已经是「清完」的样子 —— 撤销等于没撤。
pattern = [(True, False), (False, False), (False, False), (False, True), (False, False)]
for i, t in enumerate(proj4.tracks):
    t.mute, t.solo = pattern[i % len(pattern)]
page4.rebuild()
before = [(t.mute, t.solo) for t in proj4.tracks]
check("（前置）工程里确实有静音/独奏可清",
      any(t.mute or t.solo for t in proj4.tracks), f"{before}")

u2 = len(ctrl4._undo)
page4.btn_clear.click()            # 真的走工具条那颗按钮
check("清除后工程里不再有静音/独奏",
      not any(t.mute or t.solo for t in proj4.tracks),
      f"{[(t.mute, t.solo) for t in proj4.tracks]}")
check("清除压了 1 步撤销点", len(ctrl4._undo) - u2 == 1,
      f"压了 {len(ctrl4._undo) - u2} 步")
check("撤销标签是「清除静音与独奏」",
      ctrl4.undo_label() == "清除静音与独奏", f"「{ctrl4.undo_label()}」")

page4._undo()
after = [(t.mute, t.solo) for t in proj4.tracks]
check("撤销之后静音/独奏原样恢复", after == before,
      f"清除前 {before} → 撤销后 {after}")

u3 = len(ctrl4._undo)
for t in proj4.tracks:             # 先真的清干净，再点一次按钮
    t.mute = False
    t.solo = False
page4.rebuild()
page4.btn_clear.click()            # 已经没有静音/独奏了
check("没有可清除的东西时不压撤销点", len(ctrl4._undo) == u3,
      f"压了 {len(ctrl4._undo) - u3} 步")
check("并且给出提示", "没有" in page4._status.text(), f"状态「{page4._status.text()}」")
rig4.close()

# ==========================================================================
print()
print("=" * 70)
print("14) 试听片段 = 真正最响的连续 2.5 秒（不是「最热 0.25s 块居中」）")
print("=" * 70)


def best_window_energy(buf, n):
    """独立算一遍「长度为 n 的最响连续窗」的能量，当判据用。

    用前缀和，而不是复用 _audition_excerpt 里的任何东西 —— 判据和被测代码
    共用一套逻辑的话，两边一起错就永远测不出来了。
    """
    power = np.square(np.asarray(buf, dtype=np.float64)).sum(axis=0)
    csum = np.concatenate(([0.0], np.cumsum(power)))
    return float((csum[n:] - csum[:-n]).max())


def _energy(piece):
    return float(np.square(np.asarray(piece, dtype=np.float64)).sum())


n_ex = int(AUDITION_SECONDS * render.SR)

# 合成缓冲：12 秒静音里只有一段 2.5 秒的爆发，起点固定在 6.0s。
# 旧实现取「能量最高的 0.25s 块」再把 2.5s 窗居中，第一个最热块的中心在
# 6.125s，于是窗被推到 4.875~7.375s —— 只装进半个爆发，能量只剩 55%。
synth_buf = np.zeros((2, int(12.0 * render.SR)), dtype=np.float32)
burst_at = int(6.0 * render.SR)
tt = np.arange(n_ex) / float(render.SR)
tone = (0.4 * np.sin(2.0 * np.pi * 220.0 * tt)).astype(np.float32)
synth_buf[0, burst_at:burst_at + n_ex] = tone
synth_buf[1, burst_at:burst_at + n_ex] = tone
# 记号样本（远离 10ms 淡入区）：用来判断窗到底从哪个样本开始，比看能量更直白
synth_buf[:, burst_at + 1000:burst_at + 1005] = 1.0

piece_syn = _audition_excerpt(synth_buf)
gap_syn = 10.0 * np.log10(min(1.0, _energy(piece_syn)
                              / best_window_energy(synth_buf, n_ex)))
check("合成缓冲的片段长度就是 2.5 秒", piece_syn.shape[1] == n_ex,
      f"{piece_syn.shape}")
check("片段起点就落在爆发起点上（记号样本在片段里）",
      float(piece_syn[0, 1000:1005].max()) == 1.0,
      f"记号峰值 {float(piece_syn[0, 1000:1005].max()):.3f}")
check("片段能量达到真正最响的那 2.5 秒（差距 < 0.05 dB）", gap_syn > -0.05,
      f"{gap_syn:+.3f} dB（旧实现的块居中做法在这里是 -2.6 dB）")

# 真实轨道上也得成立：复核者实测旧实现给鼓轨选的窗比真正最响的窗响度低 1.25dB
trail = demo.demo_project()
buf_drum = render.render_track(trail.tracks[0], trail)
piece_drum = _audition_excerpt(buf_drum)
gap_drum = 10.0 * np.log10(min(1.0, _energy(piece_drum)
                              / best_window_energy(buf_drum, n_ex)))
check("真实鼓轨的片段也达到真正最响的 2.5 秒（差距 < 0.05 dB）", gap_drum > -0.05,
      f"{gap_drum:+.3f} dB · 峰值 {float(abs(piece_drum).max()):.3f}")

# ==========================================================================
print()
print("=" * 70)
print("15) 渲染失败必须看得见（不许静默降级成「这条轨道没声音」）")
print("=" * 70)
rig5 = Rig()
page5 = rig5.page
proj5 = rig5.project
real_render_track = render.render_track


def _boom(*_a, **_k):
    raise RuntimeError("模拟渲染失败")


render.render_track = _boom
try:
    page5._message("")
    peak5, rms5 = page5.analyze_track(proj5.tracks[0])
    status5 = page5._status.text()
    check("失败时电平退回表底（不假装有信号）",
          peak5 <= METER_FLOOR_DB + 1e-9 and rms5 <= METER_FLOOR_DB + 1e-9,
          f"peak={peak5:.1f} rms={rms5:.1f}")
    check("失败留下了可见提示（旧实现是静默返回）", "失败" in status5,
          f"状态「{status5}」")
    check("提示里点名了出问题的轨道", proj5.tracks[0].name in status5,
          f"状态「{status5}」")

    page5.analyze_all_now()            # 5 条轨道全部失败，队列走空
    check("失败记录被留下（供状态栏反复播报）",
          len(getattr(page5, "_failures", [])) >= 1,
          f"{len(getattr(page5, '_failures', []))} 条")
    check("失败提示不会被随后的状态刷新盖掉（旧实现里 _update_status 会盖掉）",
          "失败" in page5._status.text(), f"状态「{page5._status.text()}」")
    check("每条轨道都测过并退回了表底",
          len(page5._levels) == len(proj5.tracks)
          and all(page5._levels[t.id][0] <= METER_FLOOR_DB + 1e-9
                  for t in proj5.tracks),
          f"{len(page5._levels)}/{len(proj5.tracks)} 条")
finally:
    render.render_track = real_render_track

# 恢复之后重新测一次：故障消失，提示也该跟着消失（不能一直挂着旧错误）
page5._remeasure()
page5.analyze_all_now()
check("恢复渲染后重新测量，失败提示被清掉",
      not getattr(page5, "_failures", []) and "失败" not in page5._status.text(),
      f"状态「{page5._status.text()}」")
rig5.close()

# ==========================================================================
print()
print("=" * 70)
print("16) 通用后台作业设施（controller.submit_background）")
print("=" * 70)
# 这一节测的是**新的公共设施本身**：作业跑在后台、结果回 UI 线程、
# 取消与关闭都是粘性的。混音台（第 17 节）只是它的第一个使用者。
# 它是公共设施，所以先有断言、后有实现 —— 缺了它这些断言必须明确失败，
# 而不是让脚本在 AttributeError 上炸掉、把后面的量化证据一起带走。


def pump_until(cond, timeout=10.0, nap=0.004):
    """转事件循环直到 cond() 为真（或超时）。返回是否达成。

    离屏环境里没有"真"事件循环在转，队列信号必须靠 processEvents 才送达 ——
    这既是测试的驱动方式，也正好验证了"结果真的是通过事件循环回 UI 线程的"。
    """
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(nap)
    QApplication.processEvents()
    return bool(cond())


def pump_for(seconds=0.2, nap=0.004):
    """光转事件循环，不做判断。"""
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        QApplication.processEvents()
        time.sleep(nap)


main_thread = threading.current_thread()

if not callable(getattr(AppController, "submit_background", None)):
    check("控制器提供通用后台作业设施 submit_background", False,
          "旧实现没有这个入口 —— 第 16 节其余断言都依赖它")
else:
    bg = AppController(demo.demo_project())
    started: dict = {}
    arrived: dict = {}

    def _probe():
        started["thread"] = threading.current_thread()
        time.sleep(0.20)
        return "载荷"

    t_submit = time.perf_counter()
    job_bg = bg.submit_background(
        _probe, label="探测作业",
        on_done=lambda r: arrived.update(result=r, thread=threading.current_thread()),
    )
    t_return = time.perf_counter()
    check("提交立刻返回，不等作业跑完", t_return - t_submit < 0.10,
          f"{(t_return - t_submit) * 1000:.1f} ms（作业本身要 200ms）")
    check("作业句柄带标签、此刻未完成",
          job_bg.label == "探测作业" and job_bg.done is False,
          f"{job_bg!r}")

    pump_until(lambda: "thread" in started, 3.0)
    job_thread = started.get("thread")
    check("作业跑在后台线程上，不是 UI 线程",
          job_thread is not None and job_thread is not main_thread,
          f"作业线程 {job_thread.name if job_thread else '—'} / UI 线程 {main_thread.name}")
    check("作业在跑的时候 UI 线程照样能处理事件（提交后主线程没被占住）",
          pump_until(lambda: bool(arrived), 5.0), f"结果 {arrived.get('result')!r}")
    check("回调在 UI 线程里被调用（后台线程绝不碰控件）",
          arrived.get("thread") is main_thread,
          f"回调线程 {arrived.get('thread').name if arrived.get('thread') else '—'}")
    check("作业收工后不再计数", bg.background_active == 0,
          f"还剩 {bg.background_active} 个")

    # ---- 句柄的两种签名都要能用 ----------------------------------------
    # 这套"要不要递句柄"的判断是隐式的（看必填位置参数个数），一旦失灵，
    # 协作式取消会**静默**失效 —— 取消请求发得出去，作业里却收不到，没人报错。
    got_handle: dict = {}

    def _wants_handle(job):
        got_handle["handle"] = job
        return "带了句柄"

    handle_of = bg.submit_background(_wants_handle, label="要句柄的作业")
    pump_until(lambda: "handle" in got_handle, 3.0)
    check("作业函数声明了句柄参数就会收到句柄",
          got_handle.get("handle") is handle_of,
          f"{got_handle.get('handle')!r} / {handle_of!r}")
    zero_arg: dict = {}
    bg.submit_background(lambda: zero_arg.update(ok=True), label="不要句柄的作业")
    pump_until(lambda: bool(zero_arg), 3.0)
    check("不声明参数的老写法照样能用（fn 收 0 个参数）", zero_arg.get("ok") is True)

    # ---- 协作式取消：作业自己在检查点收工 ------------------------------
    coop = {"iter": 0}

    def _cooperative(job):
        for i in range(400):
            time.sleep(0.005)
            coop["iter"] = i
            if i == 5:
                job.cancel()             # 作业自己请求取消（模拟"用户改了工程"）
            job.raise_if_cancelled()
        coop["ran_to_end"] = True
        return "跑完了"

    bg.submit_background(_cooperative, label="可取消作业",
                         on_done=lambda r: coop.update(callback=r))
    pump_until(lambda: coop["iter"] >= 6, 5.0)
    pump_for(0.3)                        # 再转一会儿，等它彻底收工
    check("作业函数能在检查点主动收工（协作式取消）",
          coop.get("ran_to_end") is not True and coop["iter"] < 60,
          f"400 步的循环跑到第 {coop['iter']} 步就停了")
    check("被取消的作业不回调（取消不是失败，不该走 on_done/on_error）",
          "callback" not in coop, f"{sorted(coop)}")

    # ---- 取消是粘性的：已经算完、正在回程的结果也不许落地 ---------------
    late: list = []

    def _finishes_anyway():
        time.sleep(0.15)
        return "其实算好了"

    job_late = bg.submit_background(_finishes_anyway, on_done=lambda r: late.append(r))
    time.sleep(0.03)                     # 让它在后台跑起来
    job_late.cancel()                    # 结果还在回程路上就取消
    pump_until(lambda: job_late.done, 3.0)
    pump_for(0.2)
    check("取消是粘性的：算完的结果也不许落地", late == [] and job_late.cancelled,
          f"落地了 {late}")
    check("取消标志不会被任何后续路径复活（RenderWorker 的教训）",
          job_late.cancelled is True, f"{job_late!r}")

    # ---- 关闭：必须等所有后台作业收工 ----------------------------------
    inflight: dict = {}

    def _slow_tail():
        time.sleep(0.45)
        inflight["finished"] = True
        return 1

    bg.submit_background(_slow_tail, label="关窗时还在跑的作业")
    time.sleep(0.06)                     # 让它真的在跑，而不是还排在队里
    t0 = time.perf_counter()
    bg.shutdown()
    dt = time.perf_counter() - t0
    report = getattr(bg, "shutdown_report", {})
    check("shutdown 等到了在跑的后台作业收工", inflight.get("finished") is True)
    check("shutdown 报告线程池已排空", report.get("pool_drained") is True, f"{report}")
    check("shutdown 报告渲染线程也收工了",
          report.get("thread_running") is False, f"{report}")
    check("shutdown 的耗时覆盖了作业剩下的时间（说明它真的在等）",
          dt >= 0.25, f"{dt * 1000:.0f} ms（报告 {report.get('ms', 0):.0f} ms）")
    check("shutdown 耗时没有失控", dt < 3.0, f"{dt * 1000:.0f} ms")
    check("shutdown 之后后台作业计数归零", bg.background_active == 0)

    # ---- 关闭之后：粘性，绝不再接新作业 --------------------------------
    after: dict = {}
    job_after = bg.submit_background(lambda: after.update(ran=True),
                                     on_done=lambda r: after.update(cb=True))
    pump_for(0.3)
    check("关闭之后提交的作业根本不会被执行（粘性）", not after, f"{after}")
    check("关闭之后拿到的句柄是已取消的", job_after.cancelled is True, f"{job_after!r}")

# ==========================================================================
print()
print("=" * 70)
print("17) 首答：进入混音台不再冻结界面（本轮正文）")
print("=" * 70)
# 量的是"UI 线程单次被独占多久"：processEvents 里如果同步跑了 render_track，
# 那一次调用就会占住 0.2~2.6 秒（实测主旋律轨 2.61s）。两个探针：
#   1. 每次 processEvents 的耗时 → 单次阻塞上限；
#   2. 45ms 心跳定时器的实际间隔 → 事件循环有没有被饿死。
rig6 = Rig()
page6 = rig6.page
proj6 = rig6.project

page6.analyze_all_now()                  # 先把界面稳到"静止"，这一段允许慢
pump_for(0.05)

touched: set = set()
_orig_refresh = page6._refresh_meter


def _spy_refresh(strip):
    touched.add(threading.current_thread())   # 记录电平表是在哪个线程里被刷新的
    return _orig_refresh(strip)


page6._refresh_meter = _spy_refresh

beats: list = []
beat = QTimer()
beat.setInterval(45)
beat.timeout.connect(lambda: beats.append(time.perf_counter()))
beat.start()

page6._remeasure()                       # 工具条那颗"重新测量"按钮走的就是这条路
worst = 0.0
saw_jobs = 0
t0 = time.perf_counter()
while (page6._queue or getattr(page6, "_jobs", {})) and time.perf_counter() - t0 < 90:
    t_call = time.perf_counter()
    QApplication.processEvents()
    worst = max(worst, time.perf_counter() - t_call)
    saw_jobs = max(saw_jobs, len(getattr(page6, "_jobs", {})))
    time.sleep(0.002)
round_seconds = time.perf_counter() - t0
pump_for(0.05)
beat.stop()

gaps = [b - a for a, b in zip(beats, beats[1:])]
max_gap = max(gaps) if gaps else 0.0
print(f"  · 一轮测量总耗时 {round_seconds:.2f}s · UI 线程单次最长独占 {worst * 1000:.0f} ms"
      f" · 心跳最大间隔 {max_gap * 1000:.0f} ms（{len(beats)} 次心跳，设定 45 ms）")

check("这一轮真的测完了（不是靠「什么都没算」混过去的）",
      not page6._queue and len(page6._levels) == len(proj6.tracks),
      f"{len(page6._levels)}/{len(proj6.tracks)} 条")
lit = [s for s in page6._strips if s.meter.peak_db() > METER_FLOOR_DB]
check("后台结果落到了通道条的电平表上", len(lit) == len(page6._strips),
      f"{len(lit)}/{len(page6._strips)} 条亮着")
check("整轮是投给后台跑的（UI 线程上不留 render_track）", saw_jobs > 0,
      f"同时在跑的作业最多 {saw_jobs} 个")
check("UI 线程单次被独占的最长时间 < 300 ms", worst < 0.30,
      f"最长 {worst * 1000:.0f} ms（HANDOFF 记录：改前单轨最贵 2610 ms）")
check("心跳定时器（设 45 ms）实际最大间隔 < 450 ms", max_gap < 0.45,
      f"最长 {max_gap * 1000:.0f} ms（改前要等一整条轨道算完才轮到下一次心跳）")
check("电平表的刷新全部发生在 UI 线程", touched == {main_thread},
      f"见过这些线程：{[t.name for t in touched]}")
# 后台路径也走同一套"渲染一次既给电平、又留试听片段"，缺了它点图标就没声
excerpts = [page6.excerpt(t) for t in proj6.tracks]
check("后台路径也留下了试听片段（点图标立刻出声）",
      all(e is not None and e.size > 0 for e in excerpts),
      f"{sum(1 for e in excerpts if e is not None and e.size > 0)}/{len(excerpts)} 条有片段")
check("后台留下的片段是真声音，不是空缓冲",
      any(float(abs(e).max()) > 0.01 for e in excerpts if e is not None),
      f"最大峰值 {max((float(abs(e).max()) for e in excerpts if e is not None), default=0.0):.3f}")

# ==========================================================================
print()
print("=" * 70)
print("18) 过期结果不许覆盖新的（轮次代号 + 粘性取消）")
print("=" * 70)
# (a) 直接测轮次守卫：旧一轮的结果回来时，页面必须原样不理
tid6 = proj6.tracks[0].id
page6._levels.pop(tid6, None)
page6._queue = [tid6]
page6._round = 50
handler = getattr(page6, "_on_analysis_done", None)
if handler is None:
    check("过期轮次的结果被丢弃", False,
          "旧实现没有轮次守卫（_on_analysis_done 不存在）")
else:
    handler(49, tid6, (-1.0, -2.0, None, None))    # 第 49 轮的结果，已经是过去式
    check("过期轮次的结果被丢弃（没有写进 _levels）", tid6 not in page6._levels,
          f"_levels[{tid6}] = {page6._levels.get(tid6)}")
    check("过期结果也不会把队列里的条目划掉（新的一轮还得照跑）",
          tid6 in page6._queue, f"_queue={page6._queue}")
page6._queue = []
page6._round += 1

# (b) 端到端：一轮还没回来就重开了一轮 → 旧一轮算出来的东西必须被丢掉。
#     用一个"只有旧一轮才会给出"的记号电平（0.89 ≈ -1.0 dB）来分辨，
#     否则两条路径算出的真值一样，测不出到底是谁写的。
proj18 = Project(name="过期结果工程")
t18 = proj18.add_track(Track(name="唯一轨", kind="drum", volume=1.0))
t18.add_note(Note(step=0, pitch=36, length=1))
t18.add_note(Note(step=4, pitch=38, length=1))
rig18 = Rig(proj18)
page18 = rig18.page
page18.analyze_all_now()

real_render18 = render.render_track
gate18 = threading.Event()
stage = {"round": 0}


def _staged_render(track, project, sr=render.SR, note_callback=None):
    if stage["round"] == 0:              # 第一轮：卡在门口，直到测试放行
        gate18.wait(5.0)
        return np.full((2, 8192), 0.89, dtype=np.float32)   # 记号电平 ≈ -1.0 dB
    return real_render18(track, project, sr, note_callback)


render.render_track = _staged_render
try:
    page18._levels.clear()
    page18._excerpts.clear()
    page18._schedule_analysis()          # 第一轮（后台，被 gate 卡住）
    began = pump_until(lambda: bool(getattr(page18, "_jobs", {})), 3.0)
    check("（前置）第一轮的作业确实在后台跑着", began,
          f"在跑 {len(getattr(page18, '_jobs', {}))} 个")
    stage["round"] = 1                   # 从这里开始就是"新一轮"
    page18._schedule_analysis()          # 第二轮：作废第一轮
    finished = pump_until(lambda: not page18._queue, 30.0)
    fresh = page18._levels.get(t18.id)
    check("（前置）新一轮把真实读数算出来了",
          finished and fresh is not None and fresh[0] > METER_FLOOR_DB + 1.0,
          f"{fresh}")
    gate18.set()                         # 放行旧一轮：它算出的记号必须被丢掉
    pump_for(1.0)
    after = page18._levels.get(t18.id)
    check("放行后旧一轮的结果没有覆盖新一轮的读数", after == fresh,
          f"{fresh} → {after}（旧一轮的记号是 -1.0 dB）")
finally:
    gate18.set()
    render.render_track = real_render18
rig18.close()

# 第 17 节那个 rig 到这里才用得上（18(a) 借了它的页面），所以在这里关。
# 漏掉这一句的后果是实测过的：所有断言全绿，进程退出时却打印
# "QThread: Destroyed while thread '' is still running" 并以 0xC0000409 结束 ——
# 线程没等干净，Qt 直接 fail-fast，Python 拦不住。
rig6.close()

# ==========================================================================
print()
print("=" * 70)
print("19) 关窗：后台分析作业必须都收工（退出码的前提）")
print("=" * 70)
# "测试全绿"和"进程能正常退出"是两件事：线程没等干净时 Qt 会 fail-fast
# 把进程带走（退出码 0xC0000409），而那是**测试进程之外**的症状。
rig19 = Rig()
page19 = rig19.page
page19._remeasure()                      # 让后台开始测
pump_until(lambda: bool(getattr(page19, "_jobs", {})) or not page19._queue, 3.0)
pump_for(0.5)                            # 让它们真跑起来 —— 别在"刚提交、还没开工"时关窗
inflight19 = len(getattr(page19, "_jobs", {}))
check("（前置）关窗前页面确实还在忙",
      bool(page19._queue) or inflight19 > 0,
      f"队列 {len(page19._queue)} · 后台在跑 {inflight19}")
t0 = time.perf_counter()
rig19.ctrl.shutdown(wait_ms=6000)
dt = time.perf_counter() - t0
report19 = getattr(rig19.ctrl, "shutdown_report", {})
print(f"  · 关窗时后台还有 {inflight19} 条件业没回来 · shutdown 耗时 {dt * 1000:.0f} ms"
      f" · {report19}")
check("shutdown 等到线程池排空（没把后台线程扔在身后）",
      report19.get("pool_drained") is True, f"{report19}")
check("shutdown 报告渲染线程也不在跑了",
      report19.get("thread_running") is False, f"{report19}")
check("shutdown 耗时在 6 秒预算内", dt < 6.0, f"{dt * 1000:.0f} ms")
check("关窗后后台作业计数为 0",
      getattr(rig19.ctrl, "background_active", 0) == 0,
      f"{getattr(rig19.ctrl, 'background_active', 'N/A')}")
rig19.close()

# ==========================================================================
rig.close()

print()
print("=" * 70)
print("全部通过" if ok else "存在失败项")
print("=" * 70)
sys.exit(0 if ok else 1)
