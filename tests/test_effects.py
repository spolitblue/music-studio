"""效果器链编辑页的验证 —— 界面、数据、排序、参数、试听、撤销。

跑法（工程根目录）：
    .venv\\Scripts\\python.exe -u tests\\test_effects.py
"""

import math
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# --- 项目根路径引导（本文件位于子目录）---
import sys as _sys
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parent.parent
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))
# --- 引导结束 ---

import numpy as np
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

app = QApplication([])

from app.audio import demo
from app.audio import effects as fx
from app.audio import render
from app.audio.project import Effect, Note, Project, Track
from app.ui.controller import AppController
from app.ui.pages.effects import (
    AUDITION_DEBOUNCE_MS, AUDITION_SECONDS, AUDITION_TAIL, EFFECT_ORDER, EffectRow,
    EffectsPage, format_value, log_to_pos, log_to_value, normalize_value,
    play_audition_clip, read_param, render_audition_clip, specs_of,
)

ok = True
_count = [0]


def check(label, cond, detail=""):
    global ok
    _count[0] += 1
    if not cond:
        ok = False
    print(f"  [{'OK  ' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")


def click(widget, x=None, y=None):
    """给控件发一个真实的鼠标按下事件（走 Qt 的事件分发，而不是直接调槽）。"""
    if x is None:
        x = widget.width() / 2.0
    if y is None:
        y = widget.height() / 2.0
    ev = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(x, y), QPointF(x, y),
                     Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier)
    app.sendEvent(widget, ev)
    return ev


def move(widget, x, y):
    ev = QMouseEvent(QEvent.Type.MouseMove, QPointF(x, y), QPointF(x, y),
                     Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier)
    app.sendEvent(widget, ev)


# ==========================================================================
# 测试台
# ==========================================================================

project = demo.demo_project()
ctrl = AppController(project)
page = EffectsPage(project)
page.bind(ctrl)
# 编排者（main_window）会做这件事：撤销会把整棵轨道树换成新对象，必须重建界面
ctrl.projectChanged.connect(page.rebuild)
page.resize(1240, 800)
page.show()


def T(i):
    """按序号取**当前**工程里的轨道。

    绝不缓存 Track / Effect 引用：撤销走的是 ``project.__dict__.update(...)``，
    整棵树会被换成新对象，缓存下来的旧引用会静静地指向一份没人再读的数据
    —— 界面这边靠 track.id 重新解析，测试这边就得靠每次现取。
    """
    return project.tracks[i]


DRUM, BASS, PAD, LEAD, ARP = 0, 1, 2, 3, 4

print("=" * 70)
print("1) 页面构造与绘制")
print("=" * 70)
pix = page.grab()
check("页面能构造并且能绘制", not pix.isNull(), f"{pix.width()}×{pix.height()}")
check("左列列出了全部轨道", len(page._picks) == len(project.tracks),
      f"{len(page._picks)} 行 / {len(project.tracks)} 轨")
check("底部列出了六种效果",
      len(page._add_tiles) == 6 and set(page._add_tiles) == set(EFFECT_ORDER),
      "、".join(fx.EFFECT_LABELS[k] for k in page._add_tiles))
check("默认停在第一条轨道", page.current_track() is T(DRUM),
      page.current_track().name if page.current_track() else "无")
check("鼓组没有效果 → 链是空的且不崩", page.rows == [] and not T(DRUM).effects)
check("效果图标名取自 effects.EFFECT_ICONS",
      {k: fx.EFFECT_ICONS[k] for k in EFFECT_ORDER} ==
      {k: fx.EFFECT_ICONS[k] for k in page._add_tiles})

print()
print("=" * 70)
print("2) 左列选轨道（真实点击）")
print("=" * 70)
click(page._picks[LEAD])
check("点轨道行切到了主旋律", page.current_track() is T(LEAD),
      page.current_track().name if page.current_track() else "无")
check("右侧显示该轨已有的效果", len(page.rows) == len(T(LEAD).effects) == 1,
      f"{len(page.rows)} 行")
check("已有效果被正确认出来",
      page.rows[0].effect.kind == "distortion" and page.rows[0].effect is T(LEAD).effects[0])
click(page._picks[DRUM])
check("切回鼓组后链又空了", page.rows == [] and page.current_track() is T(DRUM))

print()
print("=" * 70)
print("3) 添加效果（点底部格子，走真实鼠标事件）")
print("=" * 70)
seen = []
page.edited.connect(lambda: seen.append(1))
n0 = len(T(DRUM).effects)
click(page._add_tiles["reverb"])
check("混响加进了 track.effects", len(T(DRUM).effects) == n0 + 1,
      f"{n0} → {len(T(DRUM).effects)}")
added = T(DRUM).effects[-1]
check("加在链尾", added is T(DRUM).effects[-1] and added.kind == "reverb")
check("参数取自 Effect.reverb() 工厂默认值",
      added.params == Effect.reverb().params, str(added.params))
check("界面同步长出这一行", len(page.rows) == 1 and page.rows[0].effect is added)
check("新加的效果自动展开", page.rows[0].expanded)
check("改动发出了 edited 信号", len(seen) == 1, f"{len(seen)} 次")

click(page._add_tiles["filter"])
click(page._add_tiles["delay"])
check("六种都能加（这里连加三种）",
      [e.kind for e in T(DRUM).effects] == ["reverb", "filter", "delay"],
      " → ".join(e.kind for e in T(DRUM).effects))
check("界面行数与链长一致", len(page.rows) == 3)
check("每次改动都发了 edited", len(seen) == 3, f"{len(seen)} 次")

print()
print("=" * 70)
print("4) 上移 / 下移真的改了 track.effects 的顺序")
print("=" * 70)
delay_eff = T(DRUM).effects[2]
page.rows[2].btn_up.click()
page.rows[1].btn_up.click()
check("连点两次上移，delay 到了第 1 位",
      [e.kind for e in T(DRUM).effects] == ["delay", "reverb", "filter"],
      " → ".join(e.kind for e in T(DRUM).effects))
check("是同一个对象被搬动，不是被重建", T(DRUM).effects[0] is delay_eff)
check("界面行顺序跟着模型走",
      [r.effect.kind for r in page.rows] == [e.kind for e in T(DRUM).effects])

page.rows[0].btn_down.click()
check("下移把它挪回去",
      [e.kind for e in T(DRUM).effects] == ["reverb", "delay", "filter"],
      " → ".join(e.kind for e in T(DRUM).effects))
page.rows[0].btn_down.click()
check("再下移继续往后走",
      [e.kind for e in T(DRUM).effects] == ["delay", "reverb", "filter"],
      " → ".join(e.kind for e in T(DRUM).effects))

before = [e.kind for e in T(DRUM).effects]
page.rows[0].btn_up.click()
page.rows[2].btn_down.click()
check("首行上移 / 末行下移被挡住，顺序不变",
      [e.kind for e in T(DRUM).effects] == before,
      " → ".join(e.kind for e in T(DRUM).effects))
check("被挡住时也不多发 edited", len(seen) == 7, f"{len(seen)} 次（7 次有效移动）")

print()
print("=" * 70)
print("5) 删除")
print("=" * 70)
victim = T(DRUM).effects[1]
page.rows[1].btn_delete.click()
check("删掉的是点中的那一个", all(e is not victim for e in T(DRUM).effects))
check("链长减一", len(T(DRUM).effects) == 2,
      " → ".join(e.kind for e in T(DRUM).effects))
check("界面同步", len(page.rows) == 2)

# 两个参数完全相同的效果：Effect 是 dataclass，== 是逐字段比较，
# 用 list.remove / in 会删错人 —— 页面必须按对象身份删
twin_a, twin_b = Effect.reverb(), Effect.reverb()
check("这两个效果确实 ==（所以不能按值删）", twin_a == twin_b and twin_a is not twin_b)
T(DRUM).effects = [twin_a, twin_b]
page.rebuild()
page.rows[1].btn_delete.click()
check("参数相同的两个效果，删的是点中的那个",
      len(T(DRUM).effects) == 1 and T(DRUM).effects[0] is twin_a)
T(DRUM).effects = [Effect.reverb(), Effect.delay()]
page.rebuild()

print()
print("=" * 70)
print("6) 旁通开关改了 Effect.enabled")
print("=" * 70)
eff = T(DRUM).effects[0]
check("默认是启用的", eff.enabled is True)
page.rows[0].btn_toggle.click()
check("点一下变旁通", eff.enabled is False)
check("旁通后行上有提示", "旁通" in page.rows[0].badge.text(), page.rows[0].badge.text())
page.rows[0].btn_toggle.click()
check("再点一下恢复启用", eff.enabled is True and page.rows[0].badge.text() == "",
      f"徽章「{page.rows[0].badge.text()}」")
check("引擎真的会跳过被旁通的效果",
      np.allclose(fx.apply_effect(np.ones((2, 64), dtype=np.float32),
                                  Effect("reverb", False, {}), 120.0, 4),
                  np.ones((2, 64), dtype=np.float32)))

print()
print("=" * 70)
print("7) 参数编辑：滑块 / 对数刻度 / 分段 / 开关")
print("=" * 70)
T(DRUM).effects = [Effect.filter()]          # 滤波器同时有 对数参数 + 三选一
page.rebuild()
page.rows[0].btn_icon.click()                # 点图标展开
check("点图标展开参数区", page.rows[0].expanded)
editor = page.rows[0].editor
feff = T(DRUM).effects[0]
check("按类型生成了正确数量的控件",
      set(editor.sliders) == {"cutoff", "resonance"} and set(editor.segments) == {"kind"},
      f"滑块={sorted(editor.sliders)} 分段={sorted(editor.segments)}")

slider = editor.sliders["cutoff"]
slider.set_value(0.0, notify=True)
lo_hz = feff.params["cutoff"]
slider.set_value(1.0, notify=True)
hi_hz = feff.params["cutoff"]
slider.set_value(0.5, notify=True)
mid_hz = feff.params["cutoff"]
check("滑块改动写进了 effect.params", "cutoff" in feff.params and "resonance" in feff.params)
check("对数刻度：中点是几何中点而不是算术中点",
      abs(mid_hz - (40.0 * 18000.0) ** 0.5) < 1.0,
      f"中点 {mid_hz:.1f}Hz（几何中点 {(40 * 18000) ** 0.5:.1f}）")
check("两端正好落在 40Hz / 18000Hz",
      abs(lo_hz - 40.0) < 0.2 and abs(hi_hz - 18000.0) < 1.0,
      f"{lo_hz:.2f}Hz / {hi_hz:.1f}Hz")
check("低频段的行程被拉开（这才是对数刻度的意义）",
      log_to_pos(200, 40, 18000) > 0.17,
      f"200Hz 落在滑块 {log_to_pos(200, 40, 18000):.1%} 处，线性刻度只有 0.9%")
check("读数带单位",
      ("Hz" in editor.readouts["cutoff"].text()) or ("kHz" in editor.readouts["cutoff"].text()),
      editor.readouts["cutoff"].text())

editor.segments["kind"].set_current_index(1)
check("分段控件改了 kind（lp→hp）", feff.params["kind"] == "hp", str(feff.params["kind"]))
editor.segments["kind"].set_current_index(2)
check("再选一个（hp→bp）", feff.params["kind"] == "bp", str(feff.params["kind"]))
check("徽章跟着显示当前类型", "BP" in page.rows[0].badge.text(), page.rows[0].badge.text())

# 开关参数
T(DRUM).effects = [Effect.delay()]
page.rebuild()
page.rows[0].btn_chevron.click()
editor = page.rows[0].editor
edelay = T(DRUM).effects[0]
check("布尔参数生成了开关芯片", "ping_pong" in editor.switches)
check("开关初始读数正确", editor.switches["ping_pong"].text() == "旁通",
      editor.switches["ping_pong"].text())
click(editor.switches["ping_pong"])            # 真鼠标点击：芯片先自翻转，再发 clicked
check("开关写进了 params", edelay.params["ping_pong"] is True,
      repr(edelay.params["ping_pong"]))
check("开关读数跟着变", editor.switches["ping_pong"].text() == "开启",
      editor.switches["ping_pong"].text())

editor.sliders["feedback"].set_value(99.0, notify=True)
check("超上限的值被钳到 0.9", abs(edelay.params["feedback"] - 0.9) < 1e-9,
      str(edelay.params["feedback"]))
editor.sliders["feedback"].set_value(-5.0, notify=True)
check("超下限的值被钳到 0", abs(edelay.params["feedback"] - 0.0) < 1e-9,
      str(edelay.params["feedback"]))
check("参数值没有浮点噪声（拖完不会存进 0.30000000000000004）",
      all(not (isinstance(v, float) and len(repr(v)) > 9) for v in edelay.params.values()),
      str(edelay.params))

# 整数参数
T(DRUM).effects = [Effect.bitcrush()]
page.rebuild()
page.rows[0].btn_chevron.click()
ebit = page.rows[0].editor
ed_bc = T(DRUM).effects[0]
ebit.sliders["bits"].set_value(5.0, notify=True)
check("位深写成 int 而不是 float",
      ed_bc.params["bits"] == 5 and isinstance(ed_bc.params["bits"], int),
      repr(ed_bc.params["bits"]))
check("整数读数不带小数点", format_value(specs_of("bitcrush")[0], 5) == "5 bit",
      format_value(specs_of("bitcrush")[0], 5))
check("频率读数会自动换单位",
      format_value(specs_of("filter")[1], 480.0) == "480 Hz"
      and format_value(specs_of("filter")[1], 12000.0) == "12.00 kHz",
      f"{format_value(specs_of('filter')[1], 480.0)} / "
      f"{format_value(specs_of('filter')[1], 12000.0)}")

print()
print("=" * 70)
print("8) 对数刻度映射往返（滑块值 → Hz → 滑块值）")
print("=" * 70)
worst_hz = 0.0
for hz in (40.0, 55.0, 100.0, 440.0, 1000.0, 4400.0, 12000.0, 18000.0):
    pos = log_to_pos(hz, 40.0, 18000.0)
    back = log_to_value(pos, 40.0, 18000.0)
    worst_hz = max(worst_hz, abs(back - hz) / hz)
check("Hz → 滑块 → Hz（40~18000Hz 八个取样点）", worst_hz < 1e-9,
      f"最大相对误差 {worst_hz:.2e}")

worst_pos = 0.0
for pos in (0.0, 0.125, 0.37, 0.5, 0.8, 1.0):
    back = log_to_pos(log_to_value(pos, 40.0, 18000.0), 40.0, 18000.0)
    worst_pos = max(worst_pos, abs(back - pos))
check("滑块 → Hz → 滑块（六个取样点）", worst_pos < 1e-9,
      f"最大绝对误差 {worst_pos:.2e}")

for kind in EFFECT_ORDER:
    bad = []
    for spec in specs_of(kind):
        if spec.kind != "log":
            continue
        for frac in (0.0, 0.3, 0.7, 1.0):
            v = log_to_value(frac, spec.lo, spec.hi)
            if abs(log_to_pos(v, spec.lo, spec.hi) - frac) > 1e-9:
                bad.append(f"{spec.key}@{frac}")
    n_log = sum(1 for s in specs_of(kind) if s.kind == "log")
    check(f"{fx.EFFECT_LABELS[kind]} 的 {n_log} 个对数参数都能往返", not bad, "、".join(bad))

check("越界输入被钳住而不是抛异常",
      log_to_pos(1.0, 40.0, 18000.0) == 0.0
      and log_to_pos(99999.0, 40.0, 18000.0) == 1.0
      and log_to_value(-3.0, 40.0, 18000.0) == 40.0
      and log_to_value(7.0, 40.0, 18000.0) == 18000.0)

print()
print("=" * 70)
print("9) 试听：渲染带头效果链的一小段")
print("=" * 70)
page.select_track(T(LEAD))
clip = render_audition_clip(project, T(LEAD))
check("渲染出立体声片段，长度 = AUDITION_SECONDS + AUDITION_TAIL",
      clip.shape[0] == 2
      and clip.shape[1] == int((AUDITION_SECONDS + AUDITION_TAIL) * render.SR),
      f"{clip.shape} @ {render.SR}Hz")
check("带效果链（失真）的片段是非静音的", float(np.max(np.abs(clip))) > 0.01,
      f"峰值 {float(np.max(np.abs(clip))):.4f}")
check("没有 NaN，也没有爆表",
      bool(np.isfinite(clip).all()) and float(np.max(np.abs(clip))) <= 1.0)

with_fx = render_audition_clip(project, T(LEAD))
for e in T(LEAD).effects:
    e.enabled = False
bypassed = render_audition_clip(project, T(LEAD))
for e in T(LEAD).effects:
    e.enabled = True
check("旁通前后渲染结果不同（效果真的串在信号路径上）",
      not np.allclose(with_fx, bypassed, atol=1e-6),
      f"最大差异 {float(np.max(np.abs(with_fx - bypassed))):.4f}")
check("恢复启用后与最初完全一致", np.allclose(with_fx, render_audition_clip(project, T(LEAD)),
                                              atol=1e-6))

T(DRUM).effects = []
page.rebuild()
clip_drum = render_audition_clip(project, T(DRUM))
check("空效果链照样渲染出非静音音频", float(np.max(np.abs(clip_drum))) > 0.01,
      f"峰值 {float(np.max(np.abs(clip_drum))):.4f}")

empty = Project(name="空工程", bpm=120.0, bars=2)
empty_track = Track(name="空轨", kind="synth")
empty.add_track(empty_track)
empty_clip = render_audition_clip(empty, empty_track)
check("没有音符的轨渲染成静音而不是报错",
      empty_clip.shape[0] == 2 and float(np.max(np.abs(empty_clip))) == 0.0,
      f"{empty_clip.shape}")
check("静音片段不会去占用试听流", play_audition_clip(empty, empty_track) is False)

result = page.audition_now()
check("手动试听不抛异常且返回布尔", isinstance(result, bool), f"返回 {result}")

print()
print("=" * 70)
print("10) 防抖（拖旋钮不能每帧重渲染）")
print("=" * 70)
check(f"防抖间隔是 {AUDITION_DEBOUNCE_MS}ms", AUDITION_DEBOUNCE_MS == 350)
check("默认开着自动试听", page._auto_audition is True and page.chip_auto.isChecked())

calls = []
saved = page._run_audition


def _spy():
    calls.append(1)
    page._audition_timer.stop()
    return True


page._run_audition = _spy
page._audition_timer.timeout.disconnect()
page._audition_timer.timeout.connect(_spy)

page.select_track(T(LEAD))
if not page.rows[0].expanded:
    page.rows[0].btn_chevron.click()
eslider = page.rows[0].editor.sliders.get("drive")
check("失真轨的参数区里有 drive 滑块", eslider is not None)
for i in range(12):                      # 模拟一次拖动：12 次 valueChanged
    eslider.set_value(0.15 + i * 0.06, notify=True)
check("改动后排上了试听（计时器在等）", page.audition_pending)
check("连续 12 次改动一次都没渲染", calls == [], f"{len(calls)} 次")
page.audition_now()
check("停手后 flush 才渲染，且只渲染一次", calls == [1], f"{len(calls)} 次")

page._run_audition = saved
page._audition_timer.timeout.disconnect()
page._audition_timer.timeout.connect(page._run_audition)
page.chip_auto.setChecked(False)
check("关掉自动试听", page._auto_audition is False)
eslider.set_value(0.9, notify=True)
check("关掉之后不再排试听", not page.audition_pending)
page.chip_auto.setChecked(True)

print()
print("=" * 70)
print("11) 撤销：改动前先 push_undo，撤销能整链回滚")
print("=" * 70)
page.select_track(T(PAD))
before_kinds = [e.kind for e in T(PAD).effects]
check("起始状态：铺底轨上有一个高通滤波", before_kinds == ["filter"], " → ".join(before_kinds))

n_undo = len(ctrl._undo)
page.add_effect("chorus")
check("加效果之前压了撤销点", len(ctrl._undo) == n_undo + 1, f"{n_undo} → {len(ctrl._undo)}")
check("撤销点的标签指明了操作", ctrl.undo_label() == "添加合唱", ctrl.undo_label())
check("链上确实多了一个", [e.kind for e in T(PAD).effects] == ["filter", "chorus"],
      " → ".join(e.kind for e in T(PAD).effects))

stale_pad = T(PAD)
ctrl.undo()
check("撤销把整棵轨道树换成了新对象", T(PAD) is not stale_pad)
after = [e.kind for e in T(PAD).effects]
check("撤销后效果链回到原样", after == before_kinds, " → ".join(after))
check("界面跟着重建（没有指着废弃对象）",
      [r.effect.kind for r in page.rows] == after and page.current_track() is T(PAD),
      " → ".join(r.effect.kind for r in page.rows))

# 一次拖动 = 一个撤销点
page.select_track(T(LEAD))
if not page.rows[0].expanded:
    page.rows[0].btn_chevron.click()
drag_slider = page.rows[0].editor.sliders.get("drive")
check("失真轨参数区里有 drive", drag_slider is not None)
n_undo = len(ctrl._undo)
drive_before = page.rows[0].effect.params["drive"]
click(drag_slider, drag_slider.width() - 1.0, drag_slider.height() / 2.0)   # 按下
for i in range(8):                                                          # 拖动
    move(drag_slider, drag_slider.width() * (0.2 + 0.08 * i), drag_slider.height() / 2.0)
check("一次拖动（按下 + 8 次移动）只占一步撤销",
      len(ctrl._undo) == n_undo + 1, f"{len(ctrl._undo) - n_undo} 步")
check("拖动过程中参数确实在变", page.rows[0].effect.params["drive"] != drive_before,
      f"{drive_before} → {page.rows[0].effect.params['drive']}")

page.select_track(T(ARP))
page.add_effect("bitcrush")
n_undo = len(ctrl._undo)
page.rows[0].btn_toggle.click()
check("旁通也压撤销点", len(ctrl._undo) == n_undo + 1, ctrl.undo_label())
n_undo = len(ctrl._undo)
page.rows[0].btn_delete.click()
check("删除也压撤销点", len(ctrl._undo) == n_undo + 1, ctrl.undo_label())
n_undo = len(ctrl._undo)
page.add_effect("chorus")
page.add_effect("filter")
page.rows[1].btn_up.click()
check("排序也压撤销点", len(ctrl._undo) == n_undo + 3,
      f"{len(ctrl._undo) - n_undo} 步 / {ctrl.undo_label()}")
while ctrl.can_undo:                     # 清干净，别把状态带进后面的用例
    ctrl.undo()

print()
print("=" * 70)
print("12) 边界：没有轨道 / 未知效果 / 缺参数键 / 契约接口")
print("=" * 70)
bare = Project(name="空", bpm=120.0, bars=4)
bare_page = EffectsPage(bare)
bare_page.resize(900, 600)
check("空工程也能构造与绘制", not bare_page.grab().isNull())
check("空工程没有当前轨道", bare_page.current_track() is None)
check("空工程点添加不崩也不写数据",
      bare_page.add_effect("reverb") is None and bare.tracks == [])
check("没有轨道时六个格子被禁用",
      all(not t.isEnabled() for t in bare_page._add_tiles.values()))
bare_page.set_project(demo.demo_project())
check("换工程后能正常起来",
      bare_page.current_track() is not None and len(bare_page._picks) == 5)

weird = Track(name="怪轨", kind="synth")
weird.effects = [Effect("granular", True, {})]     # 模拟新版本存下来的未知效果
odd = Project(name="怪", bpm=120.0, bars=2)
odd.add_track(weird)
odd_page = EffectsPage(odd)
odd_page.resize(900, 600)
check("未知效果类型不会让页面崩", not odd_page.grab().isNull() and len(odd_page.rows) == 1)
check("未知类型退回原始键名", odd_page.rows[0].name.text() == "granular",
      odd_page.rows[0].name.text())
odd_page.rows[0].btn_chevron.click()
check("未知类型给出说明而不是空白参数区",
      len(odd_page.rows[0].editor.sliders) == 0 and len(odd_page.rows[0].editor.segments) == 0)

partial = Track(name="缺键", kind="synth")
partial.effects = [Effect("reverb", True, {"size": 0.8})]     # 其余四个键都没有
part_proj = Project(name="缺", bpm=120.0, bars=2)
part_proj.add_track(partial)
part_page = EffectsPage(part_proj)
part_page.resize(900, 600)
part_page.rows[0].btn_chevron.click()
ped = part_page.rows[0].editor
check("缺键时读的是工厂默认值（size 用的是工程里的 0.8）",
      abs(float(read_param(partial.effects[0], specs_of("reverb")[0])) - 0.8) < 1e-9)
check("缺键的参数区照样建得起来", len(ped.sliders) == 5, f"{len(ped.sliders)} 个滑块")
ped.sliders["mix"].set_value(0.42, notify=True)
check("在原本缺失的键上改动也能写进去",
      abs(partial.effects[0].params["mix"] - 0.42) < 1e-9, str(partial.effects[0].params))
check("缺键 + 空轨也能渲染（静音但不报错）",
      float(np.max(np.abs(render_audition_clip(part_proj, partial)))) == 0.0)

# 契约要求的三个接口都要能用
part_page.refresh_notes()
part_page.set_position(12.5, True)
check("set_position 记下位置并显示",
      abs(part_page._position - 12.5) < 1e-9 and "12.50" in part_page._pos_label.text(),
      part_page._pos_label.text())
part_page.set_position(0.0, False)
part_page.rebuild()
check("rebuild / refresh_notes / set_position 都不抛异常", True)
check("seekRequested 信号存在（契约接口，本页不使用）",
      hasattr(part_page, "seekRequested") and hasattr(part_page, "edited"))

print()
print("=" * 70)
print("13) 六种效果都能加、都能调、串起来能渲染")
print("=" * 70)
solo = Project(name="单轨", bpm=120.0, bars=2)
solo_track = Track(name="测试轨", kind="synth")
for i in range(16):
    solo_track.add_note(Note(step=i, pitch=60 + (i % 5), length=1, velocity=0.85))
solo.add_track(solo_track)
solo_page = EffectsPage(solo)
solo_page.resize(1100, 700)
solo_page.show()
for kind in EFFECT_ORDER:
    e = solo_page.add_effect(kind)
    check(f"能添加「{fx.EFFECT_LABELS[kind]}」", e is not None and e.kind == kind)
check("六种效果都在链上", [e.kind for e in solo_track.effects] == list(EFFECT_ORDER),
      " → ".join(e.kind for e in solo_track.effects))
check("链长对应六行界面", len(solo_page.rows) == 6)

full_clip = render_audition_clip(solo, solo_track)
check("六种效果串在一起仍然渲染出非静音", float(np.max(np.abs(full_clip))) > 0.005,
      f"峰值 {float(np.max(np.abs(full_clip))):.4f}")
check("试听有安全增益兜底（六连链原始峰值 17.4 倍满刻度，会被等比例压回 ≤0.95）",
      float(np.max(np.abs(full_clip))) <= 0.95 + 1e-6,
      f"峰值 {float(np.max(np.abs(full_clip))):.4f}")
check("六连链没有产生 NaN", bool(np.isfinite(full_clip).all()))
check("安全增益是等比缩放，不是削波（波形形状不变）",
      float(np.max(np.abs(full_clip))) < 1.0)

for row in solo_page.rows:
    row.btn_chevron.click()
    label = fx.EFFECT_LABELS[row.effect.kind]
    actual = (len(row.editor.sliders) + len(row.editor.segments)
              + len(row.editor.switches))
    expected = len(specs_of(row.effect.kind))
    check(f"「{label}」参数区控件数与声明表一致", actual == expected,
          f"{actual} / {expected}"
          f"（滑块{len(row.editor.sliders)} 分段{len(row.editor.segments)}"
          f" 开关{len(row.editor.switches)}）")

check("手风琴：同时只展开一个",
      sum(1 for r in solo_page.rows if r.expanded) == 1,
      f"{sum(1 for r in solo_page.rows if r.expanded)} 个展开")

# ==========================================================================
# 以下六节是复核者实测复现的缺陷补的回归用例
# ==========================================================================


def editor_for(effect):
    """拿一个镶在效果行里的参数编辑器（不挂到任何页面上）。"""
    return EffectRow(effect, 0, 1).editor


def implied(spec, slider):
    """滑块此刻**指向**的参数值（与 _make_slider 的映射互逆）。"""
    return (log_to_value(slider.value(), spec.lo, spec.hi)
            if spec.kind == "log" else slider.value())


def page_with(effect):
    """把一个效果装进单轨工程，返回 (页面, 那条轨)。"""
    tr = Track(name="坏值轨", kind="synth")
    tr.effects = [effect]
    pr = Project(name="坏值工程", bpm=120.0, bars=2)
    pr.add_track(tr)
    pg = EffectsPage(pr)
    pg.resize(900, 600)
    return pg, tr


print()
print("=" * 70)
print("14) 缺陷①：params 里的坏值不能让页面构造直接抛异常")
print("=" * 70)
# 复核者实测：read_param 只在**键不存在**时回落默认值；键存在而值是 None /
# 非法字符串时原样返回，于是 float(raw) 在 ParamEditor._make_slider 里抛
# TypeError / ValueError —— 表现是"打开这个工程页面就崩"。缺键本来就不崩，
# 两条路径必须同样安全。最后三条（NaN / inf）是修这一处时顺手发现的同类坏值。
BAD_VALUES = [
    ("bitcrush", {"mix": None}),
    ("bitcrush", {"bits": "abc"}),
    ("filter", {"cutoff": None}),
    ("delay", {"time_beats": "x"}),
    ("bitcrush", {"bits": None}),
    ("distortion", {"drive": None}),
    ("chorus", {"rate": None}),
    ("reverb", {}),                       # 缺键：原本就不崩，作为对照
    ("bitcrush", {"bits": 4.6}),          # 只是越精度，不是坏值
    ("filter", {"kind": None}),
    ("delay", {"ping_pong": None}),
    ("filter", {"cutoff": float("nan")}),
    ("chorus", {"rate": float("inf")}),
]
for kind, params in BAD_VALUES:
    label = f"{kind} {params!r}"
    try:
        bp, btr = page_with(Effect(kind, True, dict(params)))
        bp.rows[0].btn_chevron.click()          # 参数区也要真建出来
        drawn, err = not bp.grab().isNull() and bool(bp.rows[0].editor.readouts), ""
    except Exception as exc:
        drawn, err = False, f"{type(exc).__name__}: {exc}"
    check(f"{label} 能构造、能展开、能绘制", drawn, err)

pg, tr = page_with(Effect("reverb", True, {"mix": None}))
mix_spec = next(s for s in specs_of("reverb") if s.key == "mix")
check("值为 None 时像缺键一样回落工厂默认值",
      abs(float(read_param(tr.effects[0], mix_spec))
          - float(Effect.reverb().params["mix"])) < 1e-9,
      f"读到 {read_param(tr.effects[0], mix_spec)!r}，"
      f"工厂默认 {Effect.reverb().params['mix']!r}")
pg.rows[0].btn_chevron.click()
pg.rows[0].editor.sliders["mix"].set_value(0.42, notify=True)
check("坏值所在的参数照样能拖滑块改回来",
      abs(float(tr.effects[0].params["mix"]) - 0.42) < 1e-9,
      str(tr.effects[0].params))

pg, tr = page_with(Effect("filter", True, {"cutoff": float("nan")}))
cut_spec = specs_of("filter")[1]
check("NaN 也被换成合法值（否则钳位失效、滑块位置变 NaN）",
      math.isfinite(float(read_param(tr.effects[0], cut_spec)))
      and math.isfinite(float(pg.rows[0].editor.sliders["cutoff"].value())),
      f"读到 {read_param(tr.effects[0], cut_spec)!r} / "
      f"滑块 {pg.rows[0].editor.sliders['cutoff'].value()!r}")

print()
print("=" * 70)
print("15) 缺陷③⑥：读数与滑块必须描述同一个值（钳位 / 取整只做一次）")
print("=" * 70)
# 复核者实测：cutoff=25（下界 40）时滑块位置 0.000（=40Hz）而读数写「25 Hz」；
# bits=4.6 时滑块停在 4.6 而读数已是「5 bit」—— 同一个参数两种说法。
COHERENCE_CASES = [
    ("filter", {"cutoff": 25.0}),        # 比下界 40 还小
    ("filter", {"cutoff": 99000.0}),     # 比上界 18000 还大
    ("bitcrush", {"bits": 4.6}),         # 整数参数
    ("bitcrush", {"downsample": 0.2}),
    ("delay", {"feedback": 3.0}),
    ("reverb", {}),                      # 默认值也要对得上
    ("distortion", {}),
    ("chorus", {}),
]
for kind, params in COHERENCE_CASES:
    ed = editor_for(Effect(kind, True, dict(params)))
    bad = []
    for spec in specs_of(kind):
        if spec.kind in ("enum", "bool") or spec.key not in ed.sliders:
            continue
        value = implied(spec, ed.sliders[spec.key])
        shown = ed.readouts[spec.key].text()
        if shown != format_value(spec, value):
            bad.append(f"{spec.key}: 滑块指着 {value:g} / 读数「{shown}」")
    check(f"{kind} {params or '默认'} 的每个参数都自洽", not bad, "；".join(bad))

check("整数参数越界/带小数时：滑块与模型都落在取整后的整数上",
      editor_for(Effect("bitcrush", True, {"bits": 4.6})).sliders["bits"].value() == 5.0
      and isinstance(read_param(Effect("bitcrush", True, {"bits": 4.6}),
                                specs_of("bitcrush")[0]), int),
      f"滑块 {editor_for(Effect('bitcrush', True, {'bits': 4.6})).sliders['bits'].value()}")

# 缺陷⑥（写入侧）：整数参数写进模型时取整，滑块也必须跟着停在整数上
eff_bits = Effect.bitcrush(bits=5)
ed_bits = editor_for(eff_bits)
ed_bits.sliders["bits"].set_value(5.37, notify=True)
check("拖动整数参数：模型存 5，滑块也停在 5.0（不能停在 5.37）",
      eff_bits.params["bits"] == 5 and ed_bits.sliders["bits"].value() == 5.0,
      f"模型 {eff_bits.params['bits']!r} / 滑块 {ed_bits.sliders['bits'].value()!r}")
check("整数参数的读数与滑块一致", ed_bits.readouts["bits"].text() == "5 bit",
      ed_bits.readouts["bits"].text())

eff_mix = Effect.delay()
ed_mix = editor_for(eff_mix)
ed_mix.sliders["feedback"].set_value(0.42, notify=True)
check("非整数参数不会被误吸附（0.42 就是 0.42）",
      abs(ed_mix.sliders["feedback"].value() - 0.42) < 1e-9
      and abs(float(eff_mix.params["feedback"]) - 0.42) < 1e-9,
      f"滑块 {ed_mix.sliders['feedback'].value()!r} / 模型 {eff_mix.params['feedback']!r}")

print()
print("=" * 70)
print("16) 缺陷②：徽章显示中文名，而不是引擎里的英文原始值")
print("=" * 70)
for mode, want in (("soft", "软削波"), ("hard", "硬削波"), ("tube", "电子管")):
    row = EffectRow(Effect.distortion(mode=mode), 0, 1)
    check(f"失真 mode={mode!r} 的徽章是「{want}」", row.badge.text() == want, row.badge.text())
check("滤波器徽章仍是国际通用缩写 LP / HP / BP",
      [EffectRow(Effect.filter(kind=k), 0, 1).badge.text()
       for k in ("lp", "hp", "bp")] == ["LP", "HP", "BP"],
      str([EffectRow(Effect.filter(kind=k), 0, 1).badge.text() for k in ("lp", "hp", "bp")]))
check("引擎里的原始值没被改成中文（只是不往界面上贴）",
      Effect.distortion(mode="tube").params["mode"] == "tube"
      and Effect.filter(kind="hp").params["kind"] == "hp")
badge_eff = Effect.distortion(mode="soft")
badge_row = EffectRow(badge_eff, 0, 1)
badge_eff.params["mode"] = "hard"
badge_row.reload()
check("参数变了徽章跟着变（走同一条显示路径）", badge_row.badge.text() == "硬削波",
      badge_row.badge.text())
check("旁通时徽章仍然带上旁通提示",
      EffectRow(Effect("distortion", False, {"mode": "tube"}), 0, 1).badge.text()
      == "电子管 · 已旁通",
      EffectRow(Effect("distortion", False, {"mode": "tube"}), 0, 1).badge.text())

print()
print("=" * 70)
print("17) 缺陷④：试听缓冲真把混响/延迟的尾巴交给播放流")
print("=" * 70)
n_content = int(AUDITION_SECONDS * render.SR)
n_full = int((AUDITION_SECONDS + AUDITION_TAIL) * render.SR)
clip_tail = render_audition_clip(project, T(LEAD))
check("返回长度 = (AUDITION_SECONDS + AUDITION_TAIL) × 采样率",
      clip_tail.shape[1] == n_full,
      f"{clip_tail.shape}（只算内容段应是 {n_content}）")
check("tail=0 时正好只返回内容段（尾巴是参数，不是写死的）",
      render_audition_clip(project, T(LEAD), tail=0.0).shape[1] == n_content,
      f"{render_audition_clip(project, T(LEAD), tail=0.0).shape}")

# 尾巴里必须真有东西：一个短音符 + 大混响/大反馈延迟，内容段之后还得有余韵
tail_track = Track(name="余韵", kind="synth")
tail_track.add_note(Note(step=0, pitch=60, length=1, velocity=0.9))
tail_track.effects = [Effect.reverb(size=0.9, damping=0.3, mix=0.9),
                      Effect.delay(time_beats=0.5, feedback=0.6, mix=0.5)]
tail_proj = Project(name="余韵", bpm=120.0, bars=2)
tail_proj.add_track(tail_track)
tail_clip = render_audition_clip(tail_proj, tail_track)
# 尾巴被切掉时这里是**空**切片：让断言干净地 FAIL，而不是在 np.max 上抛异常
# （测试崩掉会把后面的用例一起带走，反而看不清是哪一条断言挂的）
tail = tail_clip[:, n_content:] if tail_clip.shape[1] > n_content else tail_clip[:, :0]
tail_peak = float(np.max(np.abs(tail))) if tail.size else 0.0
content_peak = float(np.max(np.abs(tail_clip[:, :n_content])))
check("内容段之后仍有信号 —— 那正是被原实现切掉的余韵",
      tail_peak > 1e-3, f"尾部峰值 {tail_peak:.5f}")
check("尾巴的音量是内容段的同一量级（不是残留的数值噪声）",
      tail_peak > 0.005 * content_peak,
      f"尾部峰值 {tail_peak:.5f} / 内容段峰值 {content_peak:.4f}")
check("片段**末尾**仍有可听的余韵（不是刚过切点就没声了）",
      float(np.max(np.abs(tail_clip[:, -int(0.3 * render.SR):]))) > 1e-4,
      f"最后 0.3s 峰值 "
      f"{float(np.max(np.abs(tail_clip[:, -int(0.3 * render.SR):]))):.6f}")

print()
print("=" * 70)
print("18) bind() 自接 projectChanged：宿主漏接时不留指向废弃对象的旧行")
print("=" * 70)
wire_proj = Project(name="接线", bpm=120.0, bars=4)
wire_track = Track(name="接线轨", kind="synth")
for i in range(8):
    wire_track.add_note(Note(step=i, pitch=60 + i, length=1, velocity=0.8))
wire_proj.add_track(wire_track)
wire_ctrl = AppController(wire_proj)
wired = EffectsPage(wire_proj)
wired.bind(wire_ctrl)          # 关键：**不**手工 connect(projectChanged, rebuild)
wired.resize(1000, 700)
wired.show()
wired.add_effect("reverb")
check("加效果后界面一行、模型一个", len(wired.rows) == 1 and len(wire_track.effects) == 1)

rebuilds = []
_orig_rebuild = wired.rebuild


def _counting_rebuild():
    rebuilds.append(1)
    _orig_rebuild()


wired.rebuild = _counting_rebuild
wired.bind(wire_ctrl)                    # 重复 bind：不能把同一个槽接两次
wire_ctrl.projectChanged.emit()
check("重复 bind 不会让 projectChanged 接两次", len(rebuilds) == 1, f"{len(rebuilds)} 次 rebuild")
wired.rebuild = _orig_rebuild

# add_effect 内部已经在**改动前**压了一个撤销点（"添加混响"），所以这里直接撤
# 就能回到"0 个效果"。不再手动 push_undo —— 那压进去的是"已经有了混响"的快照，
# 撤一次只会原地不动（写这条用例时先踩了一次）。
wire_ctrl.undo()
check("撤销后模型里 0 个效果", len(wire_proj.tracks[0].effects) == 0,
      f"{len(wire_proj.tracks[0].effects)} 个")
check("界面同步清空 —— 没有留下指向已丢弃 Effect 的旧行",
      wired.rows == [], f"{len(wired.rows)} 行")
check("界面上每一行都还指着模型里真实存在的效果",
      all(any(r.effect is e for e in wire_proj.tracks[0].effects) for r in wired.rows))

other_proj = Project(name="另一个工程", bpm=100.0, bars=1)
other_proj.add_track(Track(name="另一轨", kind="synth"))
wire_ctrl.set_project(other_proj)
check("控制器换了工程对象时本页跟着换引用（否则本页还在读旧工程）",
      wired.project is other_proj and wired.current_track() is other_proj.tracks[0],
      f"{wired.project.name} / "
      f"{wired.current_track().name if wired.current_track() else '无'}")
wire_ctrl.shutdown()

print()
print("=" * 70)
print("19) 缺陷⑤：被兜住的异常必须留痕（撤销点丢失不能无声无息）")
print("=" * 70)
err_proj = Project(name="留痕", bpm=120.0, bars=1)
err_proj.add_track(Track(name="留痕轨", kind="synth"))
err_page = EffectsPage(err_proj)


class _DeadUndoCtrl:
    """撤销栈坏掉的控制器：push_undo 抛异常，statusMessage 连 emit 都没有。"""

    statusMessage = object()          # 故意的：没有 .emit

    def __init__(self, project):
        self.project = project

    def push_undo(self, label):
        raise RuntimeError("撤销栈写不进去")


class _DeadStatusCtrl:
    """状态栏坏掉，但撤销栈是好的。"""

    statusMessage = object()

    def __init__(self, project):
        self.project = project

    def push_undo(self, label):
        return None


err_page.bind(_DeadUndoCtrl(err_proj))
err_page._status_label.setText("")
err_page._commit("添加混响")
check("撤销点压不进栈时，状态区给出提示",
      "撤销点压入失败" in err_page._status_label.text(), err_page._status_label.text())
check("异常被记进 error_log（日志里查得到）",
      any("撤销点压入失败" in m for m in getattr(err_page, "error_log", ())),
      str(getattr(err_page, "error_log", "<没有 error_log>")))

err_page.bind(_DeadStatusCtrl(err_proj))
err_page._status_label.setText("")
err_page._status("参数已更新")
check("状态栏发不出去时，标签上仍然看得见这句话",
      err_page._status_label.text() == "参数已更新", err_page._status_label.text())
check("状态栏故障本身也留痕，而且不会把前一条覆盖掉",
      "状态栏消息发送失败" in getattr(err_page, "last_error", "")
      and any("撤销点压入失败" in m for m in getattr(err_page, "error_log", ())),
      str(getattr(err_page, "error_log", "<没有 error_log>")))

print()
print("=" * 70)
print(f"{_count[0]} 项检查 —— " + ("全部通过" if ok else "存在失败项"))
print("=" * 70)

ctrl.shutdown()
sys.exit(0 if ok else 1)
