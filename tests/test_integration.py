"""跨页面集成测试 —— 验证「整个应用合起来是成立的」。

与各模块自己的测试不同，这个文件**只走契约接口**（`set_project / rebuild /
refresh_notes / set_position`、`edited / seekRequested` 信号），不碰任何页面的内部字段。
所以它不会因为某个模块重构而假失败，但能抓住**模块之间**才会出的问题：

* 7 个页面能否共存、切换、一起响应工程变化
* 撤销/重做跨页面是否一致（一个页面改的东西，另一个页面要跟着变）
* 在多个页面里改完之后，**整曲还能不能渲染**（这是最终判据）
* 工程被整个换掉时，所有页面是否都换到了新引用
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

from app.audio import demo, render
from app.audio.project import Project, Track
from app.ui.main_window import PAGE_SPECS, MainWindow

ok = True


def check(label, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print(f"  [{'OK  ' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")


print("=" * 72)
print("1) 所有页面共存")
print("=" * 72)
win = MainWindow(demo.demo_project())
win.resize(1400, 860)
win.show()
app.processEvents()

check("没有页面加载失败", not win.page_errors, str(win.page_errors or "无"))
for key, class_name, _mod in PAGE_SPECS:
    page = win.pages.get(key)
    check(f"{key} 是真页面", page is not None and type(page).__name__ == class_name,
          type(page).__name__ if page else "缺失")

print()
print("=" * 72)
print("2) 契约接口齐备（编排者依赖这些，缺一个就会静默失效）")
print("=" * 72)
for key, class_name, _mod in PAGE_SPECS:
    page = win.pages[key]
    for attr in ("rebuild", "refresh_notes", "set_position"):
        check(f"{key}.{attr} 存在", callable(getattr(page, attr, None)))
    for sig in ("edited", "seekRequested"):
        s = getattr(page, sig, None)
        check(f"{key}.{sig} 是信号", s is not None and hasattr(s, "connect"))

print()
print("=" * 72)
print("3) 逐页切换 + 绘制 + 位置转发")
print("=" * 72)
st = win.controller.player.snapshot()
for key, _cn, _m in PAGE_SPECS:
    try:
        win._navigate(key)
        win.repaint()
        app.processEvents()
        win._on_transport_changed(st)
        check(f"{key} 切换/绘制/位置转发", True)
    except Exception as exc:
        check(f"{key} 切换/绘制/位置转发", False, f"{type(exc).__name__}: {exc}")

print()
print("=" * 72)
print("4) 工程变化广播到所有页面")
print("=" * 72)
before = sum(len(t.notes) for t in win.project.tracks)
try:
    for _ in range(3):
        win.controller.projectChanged.emit()
        app.processEvents()
    check("反复广播 projectChanged 不崩", True)
except Exception as exc:
    check("反复广播 projectChanged 不崩", False, str(exc))

# refresh_notes 也要能安全地打在所有页面上（渲染完成时会调）
try:
    win._on_render_finished(win.controller.stats)
    check("_render_finished 广播 refresh_notes 不崩", True)
except Exception as exc:
    check("_render_finished 广播 refresh_notes 不崩", False, str(exc))

print()
print("=" * 72)
print("5) 撤销/重做跨页面一致")
print("=" * 72)
# 直接改工程模拟"某个页面做的编辑"，然后走主窗口的撤销
win.controller.push_undo("集成测试")
win.project.tracks[0].add_note(__import__("app.audio.project", fromlist=["Note"]).Note(step=1, pitch=36))
after = sum(len(t.notes) for t in win.project.tracks)
win._undo()
app.processEvents()
restored = sum(len(t.notes) for t in win.project.tracks)
check("编辑后音符增加", after == before + 1, f"{before} → {after}")
check("撤销后还原", restored == before, f"{restored}")
check("撤销后页面仍能绘制", all(
    (win._navigate(k), win.repaint(), app.processEvents(), True)[-1] for k, _c, _m in PAGE_SPECS))
win._redo()
app.processEvents()
check("重做后恢复编辑", sum(len(t.notes) for t in win.project.tracks) == after)
win._undo()
app.processEvents()

print()
print("=" * 72)
print("6) 换工程时所有页面都要换到新引用")
print("=" * 72)
# 这是最容易出错的一条：撤销是「就地替换 dict 内容」，打开工程是「换对象」。
# 页面若还指着旧对象，用户后续编辑会写进一个没人读的工程里。
new_proj = demo.empty_project()
new_proj.name = "集成测试用空工程"
win.controller.set_project(new_proj)
app.processEvents()
stale = []
for key, _cn, _m in PAGE_SPECS:
    page = win.pages[key]
    p = getattr(page, "project", None)
    if p is not None and p is not new_proj:
        stale.append(key)
check("没有页面还指着旧工程", not stale, f"仍指向旧对象的：{stale}" if stale else "全部已换")

print()
print("=" * 72)
print("7) 多页面改动之后，整曲仍能渲染（最终判据）")
print("=" * 72)
proj = demo.demo_project()
win.controller.set_project(proj)
app.processEvents()
# 在几条不同轨道上各改一点，模拟用户逛了一圈
proj.tracks[0].add_note(__import__("app.audio.project", fromlist=["Note"]).Note(step=2, pitch=36))
proj.tracks[1].volume = 0.5
proj.tracks[1].pan = -0.4
proj.tracks[2].mute = True
from app.audio.project import Effect

proj.tracks[3].effects.append(Effect.distortion(drive=2.0, mix=0.3))
proj.master.volume = 0.85
audio, stats = render.render_project(proj)
check("渲染无 NaN/Inf", bool(np.isfinite(audio).all()))
check("渲染不削波", stats.peak <= 1.0, f"峰值 {stats.peak_db:+.2f} dBFS")
check("渲染非静音", float(np.abs(audio).max()) > 0.01,
      f"峰值 {float(np.abs(audio).max()):.3f}")
check("总时长合理", audio.shape[1] / stats.sample_rate > 5.0,
      f"{audio.shape[1] / stats.sample_rate:.1f} 秒")

print()
print("=" * 72)
print("8) 页面构造失败时的降级路径")
print("=" * 72)
# 契约允许某个模块坏掉时只降级那一页，而不是整个程序起不来。
# 但注意：Qt 的 fail-fast（半构造的 QWidget + 在跑的 QThread）Python 拦不住 ——
# 所以这里只能验证「Python 层异常」这条路径。
from app.ui.main_window import load_page_class

cls, err = load_page_class("app.ui.pages.__nonexistent__", "NopePage")
check("导入失败返回 (None, 原因) 而不是抛异常", cls is None and bool(err), err[:60])
cls2, err2 = load_page_class("app.ui.pages.drums", "NoSuchClass")
check("类名不存在也走同一条降级路径", cls2 is None and bool(err2), err2[:60])

win.controller.shutdown()

print()
print("=" * 72)
print("全部通过" if ok else "存在失败项")
print("=" * 72)
sys.exit(0 if ok else 1)
