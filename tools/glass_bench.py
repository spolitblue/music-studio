"""玻璃实时的量化基线 / 对比工具（改前改后跑同一个脚本）。

设计原则：**只测量，不改行为**。所有探针都是运行期 monkeypatch 包一层计时，
不往 app/ 里塞任何埋点代码 —— 否则测的就是"被观测过的系统"，两次跑不可比。

测量项
------
M1  面板清点：哪些控件走了液态路径（LIQUID=True），各自尺寸。
M2  首帧：窗口 show 之后到玻璃全部合成完毕的墙钟耗时，以及其中
    「液态合成 / 整窗预模糊 / 裁切 / 亮度分析」各占多少、调用了多少次。
M3  GUI 阻塞：一个 8ms 定时器的**实际间隔 vs 设定间隔**。
    这是"主线程被卡住"最直接的证据 —— 阻塞多久，定时器就迟到多久。
M4  拖动：模拟拖动窗口 N 步，测总耗时、每帧耗时、液态重算次数、帧率。
M5  背后是否有不透明内容：把面板的液态路径关掉后，
    窗口 grab 在该面板矩形内的**最大 alpha**。若背后是窗口透明底
    （DWM 亚克力透上来），最大 alpha 只等于面板自身材质的 alpha（很小）；
    若背后压着不透明内容，最大 alpha 会接近 255。
    这条用来判定一块面板属于 A 类（坐在窗口背景上）还是 B 类。

用法::

    .venv\\Scripts\\python.exe -u tools\\glass_bench.py --tag before
    .venv\\Scripts\\python.exe -u tools\\glass_bench.py --tag after --compare
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

OUT = _ROOT / "_out"


# --------------------------------------------------------------------------
# 探针
# --------------------------------------------------------------------------


class Probe:
    """把所有计时/计数集中在这里，避免散落的全局变量。"""

    def __init__(self) -> None:
        self.liquid_ms: list[float] = []       # 每次 liquid_fill 的耗时
        self.liquid_px: list[int] = []         # 每次 liquid_fill 的像素量
        self.blur_ms: list[float] = []         # 每次整窗预模糊
        self.crop_ms: list[float] = []         # 每次裁切
        self.lum_ms: list[float] = []          # 每次背景亮度分析
        self.paints: list[float] = []          # 每次窗口 paintEvent 的耗时
        self.glass_paint_ms: list[float] = []  # 每次 paint_glass 的耗时
        self.timer_gaps: list[float] = []      # 定时器实际间隔
        self.recording = False

    def reset(self) -> None:
        for name in ("liquid_ms", "liquid_px", "blur_ms", "crop_ms", "lum_ms",
                     "paints", "glass_paint_ms", "timer_gaps"):
            getattr(self, name).clear()

    # -- 汇总 ------------------------------------------------------------

    @property
    def liquid_total_ms(self) -> float:
        return sum(self.liquid_ms)

    @property
    def glass_total_ms(self) -> float:
        """玻璃相关同步计算的总和（首帧真正付掉的钱）。"""
        return (sum(self.liquid_ms) + sum(self.blur_ms)
                + sum(self.crop_ms) + sum(self.lum_ms))

    def worst_block_ms(self) -> float:
        """交互路径上最长的一次同步阻塞。

        取「单次液态合成」与「定时器最大迟到」的较大者 ——
        前者是界面卡住的直接原因，后者是卡住的后果，两者互为佐证。
        """
        a = max(self.liquid_ms) if self.liquid_ms else 0.0
        b = max(self.timer_gaps) if self.timer_gaps else 0.0
        return max(a, b)

    def summary(self) -> dict:
        def stat(xs: list[float]) -> dict:
            if not xs:
                return {"n": 0, "max": 0.0, "mean": 0.0, "sum": 0.0}
            return {"n": len(xs), "max": max(xs), "mean": statistics.fmean(xs),
                    "sum": sum(xs)}

        return {
            "liquid_calls": len(self.liquid_ms),
            "liquid_ms": stat(self.liquid_ms),
            "blur_ms": stat(self.blur_ms),
            "crop_ms": stat(self.crop_ms),
            "lum_ms": stat(self.lum_ms),
            "glass_total_ms": self.glass_total_ms,
            "paint_ms": stat(self.paints),
            "glass_paint_ms": stat(self.glass_paint_ms),
            "timer_gap_ms": stat(self.timer_gaps),
            "worst_block_ms": self.worst_block_ms(),
        }


PROBE = Probe()


def install_probes() -> None:
    """把 liquid_fill / AmbientSource 的方法包一层计时。"""
    from app.widgets import ambient as ambient_mod
    from app.widgets import liquid as liquid_mod

    orig_fill = liquid_mod.liquid_fill

    def timed_fill(*a, **kw):
        if not PROBE.recording:
            return orig_fill(*a, **kw)
        t0 = time.perf_counter()
        try:
            return orig_fill(*a, **kw)
        finally:
            PROBE.liquid_ms.append((time.perf_counter() - t0) * 1000.0)
            try:
                PROBE.liquid_px.append(int(a[1]) * int(a[2]))
            except Exception:
                pass

    liquid_mod.liquid_fill = timed_fill

    # paint_glass 是"玻璃的绘制成本"—— 实时档下它是唯一的玻璃开销，
    # 必须单独量出来，否则会跟"其它控件的绘制"混在一起说不清。
    orig_paint_glass = None
    try:
        from app.widgets import glass as glass_mod

        orig_paint_glass = glass_mod.paint_glass

        def timed_paint_glass(*a, **kw):
            if not PROBE.recording:
                return orig_paint_glass(*a, **kw)
            t0 = time.perf_counter()
            try:
                return orig_paint_glass(*a, **kw)
            finally:
                PROBE.glass_paint_ms.append((time.perf_counter() - t0) * 1000.0)

        glass_mod.paint_glass = timed_paint_glass
    except Exception:
        pass

    orig_blurred = ambient_mod.AmbientSource.blurred

    def timed_blurred(self, *a, **kw):
        if not PROBE.recording:
            return orig_blurred(self, *a, **kw)
        t0 = time.perf_counter()
        try:
            return orig_blurred(self, *a, **kw)
        finally:
            PROBE.blur_ms.append((time.perf_counter() - t0) * 1000.0)

    ambient_mod.AmbientSource.blurred = timed_blurred

    orig_crop = ambient_mod.AmbientSource.crop_with_margin

    def timed_crop(self, *a, **kw):
        if not PROBE.recording:
            return orig_crop(self, *a, **kw)
        t0 = time.perf_counter()
        try:
            return orig_crop(self, *a, **kw)
        finally:
            PROBE.crop_ms.append((time.perf_counter() - t0) * 1000.0)

    ambient_mod.AmbientSource.crop_with_margin = timed_crop

    orig_lum = ambient_mod.AmbientSource.backdrop_luminance

    def timed_lum(self, *a, **kw):
        if not PROBE.recording:
            return orig_lum(self, *a, **kw)
        t0 = time.perf_counter()
        try:
            return orig_lum(self, *a, **kw)
        finally:
            PROBE.lum_ms.append((time.perf_counter() - t0) * 1000.0)

    ambient_mod.AmbientSource.backdrop_luminance = timed_lum


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def collect_liquid_widgets(window):
    from PySide6.QtWidgets import QWidget

    out = []
    for w in window.findChildren(QWidget):
        if getattr(w, "LIQUID", False):
            out.append(w)
    return out


def _render_window_rgba(window):
    """把窗口画进一张**预先填成透明**的 pixmap。

    不能用 `window.grab()`：离屏平台下 grab 出来的 backing store 是不透明的，
    每个像素 alpha 都是 255，判据直接失效（第一版就踩了这个坑）。
    `QWidget.render()` 画进我们自己填过透明的 pixmap 才忠实反映
    "应用到底在面板位置画了多不透明的东西"。

    两个必须遵守的细节（都踩过，都会以 0xC0000005 访问违例的方式炸掉进程）：
    1. `constBits()` 之前必须把 QImage 绑到名字上 —— 写成
       `pix.toImage().convertToFormat(...).constBits()` 的话中间对象已被释放。
    2. 返回前必须 `.copy()` —— frombuffer 出来的数组不持有 QImage 的引用，
       函数返回后 QImage 析构，缓冲区悬空。

    另外：**必须画进 QImage 而不是 QPixmap**。离屏平台的 QPixmap 不保证有
    alpha 通道，`fill(transparent)` 之后 readback 的 alpha 依然是 255，
    于是"背后有没有内容"这个判据会全体失效（对照组也会显示 148）。
    """
    import numpy as np
    from PySide6.QtCore import QPoint, QRect, Qt
    from PySide6.QtGui import QImage

    dpr = window.devicePixelRatioF() or 1.0
    W, H = int(window.width() * dpr), int(window.height() * dpr)
    canvas = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
    canvas.setDevicePixelRatio(dpr)
    canvas.fill(Qt.GlobalColor.transparent)
    window.render(canvas, QPoint(0, 0), QRect(0, 0, window.width(), window.height()))

    img = canvas.convertToFormat(QImage.Format.Format_RGBA8888)
    arr = np.frombuffer(img.constBits(), dtype=np.uint8,
                        count=img.width() * img.height() * 4) \
            .reshape(img.height(), img.width(), 4).copy()
    return arr, dpr


def measure_backdrop(window, panels, app):
    """M5：每块面板**背后**有没有不透明内容（决定它是 A 类还是 B 类）。

    判据：把这块面板整块"抹掉"（挂一个 opacity=0 的 graphics effect ——
    几何完全不变，只是不再往画布上贡献任何像素），再渲染整窗，
    读它矩形内的 alpha。

    * alpha ≈ 0  → 背后什么都没画，桌面由 DWM 亚克力糊着透上来 ⇒ **A 类**
    * alpha 明显 > 0 → 背后压着应用自己的内容 ⇒ **B 类**，才需要真的去模糊

    第一版的做法是"把 LIQUID 关掉再看 alpha"，那是错的 ——
    面板自己画的图标/色点是**不透明**的，alpha_max 恒为 255，
    和"背后有什么"毫无关系（对照组也因此一直报警）。
    """
    import numpy as np
    from PySide6.QtWidgets import QGraphicsOpacityEffect

    results = []
    for w in panels:
        eff = None
        try:
            eff = QGraphicsOpacityEffect(w)
            eff.setOpacity(0.0)
            w.setGraphicsEffect(eff)
            app.processEvents()

            tl = w.mapTo(window, w.rect().topLeft())
            a, dpr = _render_window_rgba(window)
            H, W = a.shape[:2]
            x0 = max(0, int(tl.x() * dpr))
            y0 = max(0, int(tl.y() * dpr))
            x1 = min(W, int((tl.x() + w.width()) * dpr))
            y1 = min(H, int((tl.y() + w.height()) * dpr))
            sub = a[y0:y1, x0:x1, 3] if (y1 > y0 and x1 > x0) else a[0:1, 0:1, 3]
            results.append({
                "class": type(w).__name__,
                "size": [w.width(), w.height()],
                "alpha_max": int(sub.max()) if sub.size else -1,
                "alpha_mean": float(sub.mean()) if sub.size else -1.0,
            })
        except Exception as e:  # noqa: BLE001
            results.append({"class": type(w).__name__, "error": repr(e)})
        finally:
            try:
                w.setGraphicsEffect(None)
            except Exception:
                pass
    app.processEvents()

    # 对照组：窗口左上角 3x3（在任何面板与窗口轮廓之外，必须 ≈ 0）
    try:
        a, _ = _render_window_rgba(window)
        ctrl = a[:3, :3, 3]
        results.append({
            "class": "(对照:窗口左上角)",
            "size": [3, 3],
            "alpha_max": int(ctrl.max()),
            "alpha_mean": float(ctrl.mean()),
        })
    except Exception as e:  # noqa: BLE001
        results.append({"class": "(对照:窗口左上角)", "error": repr(e)})
    return results


def _pump(app, seconds: float) -> None:
    """跑事件循环 ``seconds`` 秒（让延迟任务、重绘、单发定时器都到位）。"""
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        app.processEvents()
        time.sleep(0.001)


def _wait_quiescent(app, quiet_ms: float = 1200.0, cap_s: float = 25.0) -> float:
    """等到 GUI 线程**连续** ``quiet_ms`` 毫秒不被阻塞为止。

    为什么必须等：应用启动时会发起一次整曲音频渲染，它跑在 RenderWorker 线程里，
    但进度信号是排队送到 **GUI 线程**处理的；渲染期间拖动窗口，量到的"卡顿"
    其实是音频渲染在报进度，与玻璃无关。

    第一版没有这一步，于是"拖动每步 210ms"被当成了玻璃的开销 ——
    cProfile 显示真凶是 dsp.py 的 svf_run/rfft，玻璃合成次数是 0。
    先让系统安静下来，量到的才是玻璃。

    返回等待耗时（秒）。
    """
    t0 = time.perf_counter()
    quiet_since = time.perf_counter()
    last = time.perf_counter()
    while time.perf_counter() - t0 < cap_s:
        app.processEvents()
        now = time.perf_counter()
        gap_ms = (now - last) * 1000.0
        last = now
        if gap_ms > 24.0:
            quiet_since = now          # 又被卡了一下，静默窗口重新计时
        elif (now - quiet_since) * 1000.0 >= quiet_ms:
            return time.perf_counter() - t0
        time.sleep(0.001)
    return time.perf_counter() - t0


def run(tag: str, glass_mode: str | None = None, throttle_ms: float = 0.0,
        no_glass_paint: bool = False) -> dict:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from app import theme
    from app.audio import demo
    from app.ui.chrome import FramelessWindow
    from app.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv)
    install_probes()

    # 决定性对照：把玻璃**绘制**整个抽掉（只留形状裁剪），
    # 看拖动每帧还剩多少 —— 用来回答"16ms 预算没达标，到底怪玻璃还是怪 Qt 光栅"。
    if no_glass_paint:
        from app.widgets import glass as _g

        def _stub(painter, rect, style, liquid=None):
            painter.fillPath(theme.squircle(rect, style.radius), theme.rgba(20, 20, 26, 40))
            return theme.squircle(rect, style.radius)

        _g.paint_glass = _stub

    # 复现"改前"：SDF 折射档 + 80ms 拖动节流。
    # 这两件事都在**测试脚本里**拨回去，不去改 app/ 的代码 ——
    # 否则测的就不是同一个系统了。
    if glass_mode:
        theme.glass.set_mode(glass_mode)
    if throttle_ms > 0:
        _orig_moved = FramelessWindow._on_window_moved

        def throttled_moved(self):
            """与原实现一模一样：单发定时器门控（已在跑就不重启）。"""
            if not hasattr(self, "_bench_move_timer"):
                t = QTimer(self)
                t.setSingleShot(True)
                t.setInterval(int(throttle_ms))
                t.timeout.connect(lambda: _orig_moved(self))
                self._bench_move_timer = t
            if not self._bench_move_timer.isActive():
                self._bench_move_timer.start()

        FramelessWindow._on_window_moved = throttled_moved
    else:
        pass

    # 定时器抖动探针：8ms 一跳，比 16ms 帧预算更细。
    # 主线程一旦同步阻塞 X ms，这里就会记到一个 X ms 的间隔。
    tick = {"last": None}

    def on_tick():
        now = time.perf_counter()
        if tick["last"] is not None:
            PROBE.timer_gaps.append((now - tick["last"]) * 1000.0)
        tick["last"] = now

    timer = QTimer()
    timer.setInterval(8)
    timer.timeout.connect(on_tick)

    # 窗口 paintEvent 计时（实例级覆盖，不动 app/ 代码）
    win = MainWindow(demo.demo_project())
    win.resize(1220, 800)
    win.move(160, 110)

    # 定时器在**窗口构造之后**才启动：构造 7 个页面本身要一秒多，
    # 把它算进"定时器迟到"会把首帧的玻璃开销淹没掉，数字就没意义了。
    timer.start()

    orig_paint = win.paintEvent
    first_paint = {"done": None}

    def timed_paint(event):
        if not PROBE.recording:
            return orig_paint(event)
        t0 = time.perf_counter()
        try:
            return orig_paint(event)
        finally:
            PROBE.paints.append((time.perf_counter() - t0) * 1000.0)
            if first_paint["done"] is None:
                first_paint["done"] = time.perf_counter()

    win.paintEvent = timed_paint

    # ---- M2 首帧 ------------------------------------------------------
    PROBE.reset()
    PROBE.recording = True
    tick["last"] = time.perf_counter()
    t0 = time.perf_counter()
    win.show()
    # 一直跑到"首帧真的画出来了"为止，再继续把启动期的玻璃工作收干。
    # （第一版这里是固定 2.5s 的空转循环，于是"首帧墙钟"永远约等于 2.5s，
    #   根本没有测量意义。）
    while first_paint["done"] is None and time.perf_counter() - t0 < 10.0:
        app.processEvents()
        time.sleep(0.001)
    first_paint_ms = ((first_paint["done"] or time.perf_counter()) - t0) * 1000.0
    _pump(app, 1.0)
    PROBE.recording = False

    panels = collect_liquid_widgets(win)
    first = {
        "wall_ms": first_paint_ms,
        "panels": len(panels),
        **PROBE.summary(),
    }

    # ---- 让系统安静下来（音频渲染报进度的高峰过去）---------------------
    quiesce_s = _wait_quiescent(app)

    # ---- M3 空闲 ------------------------------------------------------
    PROBE.recording = True
    PROBE.reset()
    tick["last"] = time.perf_counter()
    _pump(app, 0.8)
    idle = PROBE.summary()

    # ---- M4 拖动 ------------------------------------------------------
    PROBE.reset()
    steps = 24
    frames = {"paint": 0}
    orig_paint2 = win.paintEvent

    def count_paint(event):
        frames["paint"] += 1
        return orig_paint2(event)

    win.paintEvent = count_paint
    tick["last"] = time.perf_counter()
    t0 = time.perf_counter()
    for i in range(steps):
        win.move(160 + i * 14, 110 + i * 4)
        app.processEvents()
        time.sleep(0.004)
    drag_wall_ms = (time.perf_counter() - t0) * 1000.0
    PROBE.recording = False

    drag = {
        "wall_ms": drag_wall_ms,
        "steps": steps,
        "ms_per_step": drag_wall_ms / max(1, steps),
        "window_paints": frames["paint"],
        "fps": frames["paint"] / max(1e-6, drag_wall_ms / 1000.0),
        "quiesce_s": quiesce_s,
        **PROBE.summary(),
    }

    # ---- M5 背后内容 ---------------------------------------------------
    backdrops = measure_backdrop(win, panels, app)

    # ---- M6 单帧完整重绘（确定性口径）---------------------------------
    # 拖动时墙钟受调度、温度、桌面活动影响，实测复现性只有 ±40%
    # （对照组"抽掉玻璃绘制"甚至跑得比处理组还慢，说明噪声大于效应）。
    # 所以再加一个**确定性**口径：把"拖动时的一帧"直接量出来 ——
    # 先把背景缓存全部作废（旧代码每次 move 都这么干），再完整重绘一次窗口。
    frame_ms = measure_one_frame(win, app)

    win.controller.shutdown()
    timer.stop()
    app.processEvents()

    return {
        "tag": tag,
        "platform": os.environ.get("QT_QPA_PLATFORM", "(native)"),
        "glass_mode": theme.glass.mode,
        "first_frame": first,
        "idle": idle,
        "drag": drag,
        "frame": frame_ms,
        "backdrops": backdrops,
    }


def measure_one_frame(win, app, reps: int = 7) -> dict:
    """量"拖动时的一帧"：背景缓存全作废 + 完整重绘窗口。

    取最小值：这是"这一帧最快能多快"，对偶发干扰最不敏感，
    正是要比较的固有成本。
    """
    from PySide6.QtCore import QPoint, QRect
    from PySide6.QtGui import QImage

    dpr = win.devicePixelRatioF() or 1.0
    W, H = int(win.width() * dpr), int(win.height() * dpr)
    canvas = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(0)

    samples: list[float] = []
    PROBE.recording = True
    PROBE.reset()
    for _ in range(reps):
        # 作废缓存后**不要** processEvents —— 否则面板会在计时区间之外
        # 先重画一遍（invalidate_liquid 里的 update() 被这里消化掉），
        # 量到的就只是"热缓存下的光栅化"，把液态合成整个漏掉。
        # 计时区间必须完整覆盖"重算 + 光栅化"，那才是拖动时一帧的真实成本。
        win._invalidate_backdrop(full=True)
        t0 = time.perf_counter()
        win.render(canvas, QPoint(0, 0), QRect(0, 0, win.width(), win.height()))
        samples.append((time.perf_counter() - t0) * 1000.0)
    PROBE.recording = False
    app.processEvents()
    samples.sort()
    return {
        "n": len(samples),
        "min_ms": samples[0],
        "median_ms": samples[len(samples) // 2],
        "max_ms": samples[-1],
        # 诊断：这一帧里到底有没有真的重算液态玻璃
        "liquid_calls": len(PROBE.liquid_ms),
        "liquid_ms": sum(PROBE.liquid_ms),
        "blur_calls": len(PROBE.blur_ms),
        "blur_ms": sum(PROBE.blur_ms),
    }


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------


def report(r: dict) -> None:
    f = r["first_frame"]
    print()
    print("=" * 74)
    print(f"玻璃实时基线 · tag={r['tag']} · platform={r['platform']}")
    print("=" * 74)

    print(f"\n[M1] 液态面板数量：{f['panels']}")

    print(f"\n[M2] 首帧（show → 首个 paintEvent 画完）: {f['wall_ms']:.1f} ms")
    lm = f["liquid_ms"]
    print(f"     液态合成 liquid_fill : {lm['n']:3d} 次，合计 {lm['sum']:8.1f} ms"
          f"  最长 {lm['max']:7.2f} ms  平均 {lm['mean']:6.2f} ms")
    print(f"     整窗预模糊 blurred   : {f['blur_ms']['n']:3d} 次，合计"
          f" {f['blur_ms']['sum']:8.1f} ms  最长 {f['blur_ms']['max']:7.2f} ms")
    print(f"     裁切 crop_with_margin: {f['crop_ms']['n']:3d} 次，合计"
          f" {f['crop_ms']['sum']:8.1f} ms  最长 {f['crop_ms']['max']:7.2f} ms")
    print(f"     亮度 backdrop_lum    : {f['lum_ms']['n']:3d} 次，合计"
          f" {f['lum_ms']['sum']:8.1f} ms  最长 {f['lum_ms']['max']:7.2f} ms")
    print(f"     >>> 玻璃同步计算合计 : {f['glass_total_ms']:.1f} ms")
    print(f"     >>> 单次最长同步阻塞 : {f['worst_block_ms']:.2f} ms"
          f"   {'⚠ 超过 16ms 帧预算' if f['worst_block_ms'] > 16 else '✓ 在帧预算内'}")

    i = r["idle"]
    print(f"\n[M3] 空闲期（0.8s）定时器 8ms 设定："
          f"{i['timer_gap_ms']['n']} 次采样，"
          f"最长间隔 {i['timer_gap_ms']['max']:.2f} ms，"
          f"平均 {i['timer_gap_ms']['mean']:.2f} ms")
    gp = i["glass_paint_ms"]
    print(f"     玻璃绘制 paint_glass：{gp['n']} 次，合计 {gp['sum']:.2f} ms，"
          f"单次最长 {gp['max']:.3f} ms")

    d = r["drag"]
    print(f"\n[M4] 拖动 {d['steps']} 步（每步 move+processEvents）"
          f"   [测量前已等 GUI 安静 {d.get('quiesce_s', 0):.1f}s]")
    print(f"     墙钟 {d['wall_ms']:.1f} ms → 每步 {d['ms_per_step']:.1f} ms")
    print(f"     窗口重绘 {d['window_paints']} 次 → {d['fps']:.1f} fps 等效")
    print(f"     液态合成：{d['liquid_ms']['n']} 次，合计 {d['liquid_ms']['sum']:.1f} ms"
          f"，最长 {d['liquid_ms']['max']:.2f} ms")
    gpd = d["glass_paint_ms"]
    print(f"     玻璃绘制 paint_glass：{gpd['n']} 次，合计 {gpd['sum']:.1f} ms，"
          f"单次最长 {gpd['max']:.3f} ms，平均 {gpd['mean']:.3f} ms")
    print(f"     定时器最长间隔 {d['timer_gap_ms']['max']:.2f} ms"
          f"   {'⚠ 拖动期间主线程被卡' if d['timer_gap_ms']['max'] > 16 else '✓ 未卡'}")

    print("\n[M6] 单帧完整重绘（背景缓存全作废 + 整窗重绘；取最小值）")
    fm = r["frame"]
    flag = "✓ 在 16ms 帧预算内" if fm["min_ms"] < 16 else \
           ("△ 超预算，但已低于 33ms" if fm["min_ms"] < 33 else "⚠ 远超 16ms 预算")
    print(f"     最小 {fm['min_ms']:.2f} ms   中位 {fm['median_ms']:.2f} ms   "
          f"最大 {fm['max_ms']:.2f} ms   → {flag}")
    print(f"     这一帧里的液态合成：{fm.get('liquid_calls', 0)} 次 / "
          f"{fm.get('liquid_ms', 0.0):.1f} ms；整窗预模糊 {fm.get('blur_calls', 0)} 次 / "
          f"{fm.get('blur_ms', 0.0):.1f} ms  （共 {fm['n']} 帧）")

    print("\n[M5] 面板背后的内容（把面板整块抹掉后窗口 alpha；≈0 说明背后是窗口透明底）")
    print("     判据用 mean：max 会被面板边缘与窗口轮廓的一两个重合像素拉高。")
    for b in r["backdrops"]:
        if "error" in b:
            print(f"     {b['class']:<18} 探测失败: {b['error'][:60]}")
            continue
        if b["class"].startswith("(对照"):
            kind = "✓ 对照组为空，探针可信" if b["alpha_mean"] < 1.0 else "⚠ 对照组不空，探针不可信"
        elif b["alpha_mean"] < 8.0:
            kind = "A 类（坐在窗口透明底上 → 零成本可直接绘制）"
        else:
            kind = "B 类（背后有应用自己的内容 → 需要模糊快照）"
        print(f"     {b['class']:<18} {b['size'][0]:4d}x{b['size'][1]:<4d} "
              f"alpha_max={b['alpha_max']:3d} mean={b['alpha_mean']:6.2f}  → {kind}")


def compare(before: dict, after: dict) -> None:
    print()
    print("=" * 74)
    print("改前 → 改后")
    print("=" * 74)

    def row(name, a, b, unit="ms", better="lower"):
        if a is None or b is None:
            return
        d = b - a
        pct = (d / a * 100.0) if a else 0.0
        flag = ""
        if better == "lower":
            flag = "↓ 改善" if d < -0.5 else ("↑ 变差" if d > 0.5 else "= 持平")
        print(f"  {name:<34} {a:10.2f} → {b:10.2f} {unit}  ({pct:+6.1f}%)  {flag}")

    bf, af = before["first_frame"], after["first_frame"]
    row("首帧 · 玻璃同步计算合计", bf["glass_total_ms"], af["glass_total_ms"])
    row("首帧 · 单次最长同步阻塞", bf["worst_block_ms"], af["worst_block_ms"])
    row("首帧 · 液态合成合计", bf["liquid_ms"]["sum"], af["liquid_ms"]["sum"])
    row("首帧 · 液态合成单次最长", bf["liquid_ms"]["max"], af["liquid_ms"]["max"])
    if "frame" in before and "frame" in after:
        row("单帧完整重绘（最小）", before["frame"]["min_ms"], after["frame"]["min_ms"])
        row("单帧完整重绘（中位）",
            before["frame"]["median_ms"], after["frame"]["median_ms"])
    row("拖动 · 每步耗时", before["drag"]["ms_per_step"], after["drag"]["ms_per_step"])
    row("拖动 · 定时器最长间隔",
        before["drag"]["timer_gap_ms"]["max"], after["drag"]["timer_gap_ms"]["max"])
    print(f"  {'拖动 · 帧率(等效 fps)':<34} {before['drag']['fps']:10.1f} → "
          f"{after['drag']['fps']:10.1f}       "
          f"{'↑ 改善' if after['drag']['fps'] > before['drag']['fps'] else '↓ 变差'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="run")
    ap.add_argument("--real", action="store_true",
                    help="用真实窗口平台（默认 offscreen，更可复现）")
    ap.add_argument("--glass-mode", default=None, choices=["realtime", "quality"],
                    help="覆盖 theme.glass.mode；quality 用来复现'改前'的 SDF 路径")
    ap.add_argument("--throttle-ms", type=float, default=0.0,
                    help="复现'改前'的 80ms 拖动节流")
    ap.add_argument("--no-glass-paint", action="store_true",
                    help="抽掉玻璃绘制本身（对照组：看残余每帧成本归谁）")
    ap.add_argument("--compare", action="store_true",
                    help="与 _out/glass_bench_before.json 对比")
    args = ap.parse_args()

    if not args.real:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("MUSIC_STUDIO_SILENT_AUDIO", "1")

    OUT.mkdir(exist_ok=True)
    result = run(args.tag, args.glass_mode, args.throttle_ms, args.no_glass_paint)
    report(result)

    path = OUT / f"glass_bench_{args.tag}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  结果已存: {path}")

    if args.compare:
        ref = OUT / "glass_bench_before.json"
        if ref.exists():
            compare(json.loads(ref.read_text(encoding="utf-8")), result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
