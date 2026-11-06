"""弹簧动效 —— 行为优先于动画。

Apple 的做法不是"给属性选一条缓动曲线"，而是给对象一个物理模型，让动画
从**当前值**启动、继承**当前速度**、并在运动途中可被再次抓取。
贝塞尔缓动做不到这一点（它只有"进度"，没有状态）；阻尼谐振子天然可以。

本模块把弹簧的**阶跃响应**采样成 ``QEasingCurve`` 的自定义函数，从而直接
套用到 Qt 的属性动画上 —— 每个属性各自跑一条真实弹簧曲线。

无障碍：系统关闭动画效果时，弹簧退化为短时长交叉淡入（:class:`ReducedMotion`），
而不是把反馈一并取消。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from PySide6.QtCore import (
    QEasingCurve,
    QObject,
    QPointF,
    QPropertyAnimation,
    Qt,
    QTimer,
    Property,
)
from PySide6.QtWidgets import QGraphicsOpacityEffect, QWidget

from app.platform import win_glass

# --------------------------------------------------------------------------
# 弹簧规格
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Spring:
    """一条弹簧的物理参数。

    ``response`` 是"到第一次过冲附近所需的时间"（秒），越小越脆；
    ``damping`` 是阻尼比 —— 1.0 恰好不过冲，<1 会弹，>1 迟钝。
    Apple 常用 (0.35, 0.85) 做默认交互，(0.5, 1.0) 做克制的淡入。
    """

    response: float = 0.35
    damping: float = 0.85
    mass: float = 1.0

    # -- 派生量 ---------------------------------------------------------
    @property
    def omega0(self) -> float:
        """无阻尼角频率 ω₀ = 2π / response。"""
        return (2.0 * math.pi) / max(1e-4, self.response)

    @property
    def zeta(self) -> float:
        return max(0.0, self.damping)

    def settle_time(self, tolerance: float = 0.001) -> float:
        """包络衰减到 tolerance 以内所需时间（秒）—— 我们的动画时长。"""
        z = self.zeta
        w0 = self.omega0
        if z <= 0:
            return self.response * 6.0  # 无阻尼：纯振荡，给个上限
        if z < 1.0:
            # 包络 e^{-ζω₀t} 衰减到 tolerance
            return max(0.05, -math.log(tolerance) / (z * w0))
        # 过阻尼：用较慢的那个实极点
        disc = math.sqrt(max(0.0, z * z - 1.0))
        slow = w0 * (z - disc)
        if slow <= 1e-6:
            return self.response * 3.0
        return max(0.05, -math.log(tolerance) / slow)

    def value_at(self, t: float) -> float:
        """归一化阶跃响应 h(t)：0 → 1，可能过冲。"""
        z = self.zeta
        w0 = self.omega0
        if t <= 0.0:
            return 0.0

        if z < 1.0 - 1e-6:
            wd = w0 * math.sqrt(1.0 - z * z)          # 阻尼振荡频率
            env = math.exp(-z * w0 * t)
            return 1.0 - env * (math.cos(wd * t) + (z * w0 / wd) * math.sin(wd * t))

        if abs(z - 1.0) <= 1e-6:
            env = math.exp(-w0 * t)
            return 1.0 - env * (1.0 + w0 * t)

        disc = math.sqrt(z * z - 1.0)
        r1 = -w0 * (z - disc)
        r2 = -w0 * (z + disc)
        c1 = -r2 / (r1 - r2)
        c2 = r1 / (r1 - r2)
        return 1.0 - (c1 * math.exp(r1 * t) + c2 * math.exp(r2 * t))

    def to_easing(self, segments: int = 24) -> QEasingCurve:
        """（**已废弃，请勿使用**）把弹簧采样成贝塞尔样条。

        保留此方法只为记录踩过的坑。

        PySide6 6.9 里 ``QEasingCurve`` 一旦通过 ``addCubicBezierSegment`` 变成
        ``BezierSpline``，在析构时会**访问违例崩掉进程** —— 实测与动画的 parent、
        与曲线段数均无关，只与曲线类型有关：

        ==============  ================  ========
        动画 parent      曲线类型           结果
        ==============  ================  ========
        None            内置 OutCubic     正常
        None            弹簧 BezierSpline 崩溃
        目标控件         内置 OutCubic     正常
        目标控件         弹簧 BezierSpline 崩溃
        ==============  ================  ========

        更麻烦的是：动画**运行期间一切正常**（采样值正确、过冲也对），只在释放
        曲线对象时炸，所以单看功能测试完全发现不了。

        现在的弹簧走 :class:`_Animator` 里的物理积分（半隐式欧拉），
        既不依赖 Qt 的缓动机制，还能真正做到"继承中断时的速度"。
        """
        raise NotImplementedError(
            "QEasingCurve(BezierSpline) 在 PySide6 6.9 上析构会崩溃，已改用物理积分实现弹簧"
        )


#: 常用弹簧档位
SPRING_SNAPPY = Spring(0.26, 0.86)     # 按钮、开关：要脆
SPRING_DEFAULT = Spring(0.35, 0.85)    # 面板进出
SPRING_GENTLE = Spring(0.52, 1.0)      # 大表面：克制，不过冲
SPRING_BOUNCY = Spring(0.42, 0.68)     # 强调时刻（成功反馈）
SPRING_PAGE = Spring(0.44, 0.92)       # 页面切换


# --------------------------------------------------------------------------
# 无障碍降级
# --------------------------------------------------------------------------


_reduced_motion: bool | None = None


def reduced_motion() -> bool:
    """系统是否要求减少动效（缓存 —— 这个查询会走系统调用）。"""
    global _reduced_motion
    if _reduced_motion is None:
        _reduced_motion = not win_glass.animations_enabled()
    return _reduced_motion


def refresh_reduced_motion() -> bool:
    global _reduced_motion
    _reduced_motion = None
    return reduced_motion()


@dataclass(frozen=True)
class ReducedMotion:
    """降级后的动画参数：保留透明度/颜色变化（它们有助理解），
    去掉位移、弹簧与过冲。"""

    duration: int = 160
    easing = None

    def __post_init__(self):
        object.__setattr__(self, "easing", QEasingCurve(QEasingCurve.Type.InOutCubic))


# --------------------------------------------------------------------------
# 动画执行器
# --------------------------------------------------------------------------


class _SpringAnim:
    """一条正在跑的弹簧，用**半隐式欧拉**积分阻尼谐振子::

        a = −ω₀²·(x − goal) − 2ζω₀·v
        v += a·dt
        x += v·dt

    半隐式（先更新速度再更新位置）比显式欧拉稳定得多，dt 抖动也不会发散。

    之所以自己积分而不是套 Qt 的缓动曲线，是因为**只有真正的仿真才能继承速度** ——
    阶跃响应曲线每次都从 v=0 重新开始，做不到 Apple 要求的"动画与手势无缝衔接"。
    """

    __slots__ = ("target", "prop", "x", "v", "goal", "omega0", "zeta", "elapsed", "budget")

    def __init__(self, target: QObject, prop: str, x: float, v: float,
                 goal: float, spring: Spring):
        self.target = target
        self.prop = prop
        self.x = float(x)
        self.v = float(v)
        self.goal = float(goal)
        self.omega0 = spring.omega0
        self.zeta = max(0.0, spring.zeta)
        self.elapsed = 0.0
        self.budget = max(0.35, spring.settle_time() * 4.0)   # 异常参数下的兜底

    def step(self, dt: float) -> bool:
        """推进一步并**立即把值写回属性**，返回 True 表示已落定。

        每帧都必须落位 —— 只在落定时写回的话，属性会从起点直接跳到终点，
        中间一帧都没有，"动画"就名存实亡了。
        """
        self.elapsed += dt
        # 子步长固定上限：卡顿之后一次跳太远会让积分失稳
        steps = max(1, min(8, int(dt / (1.0 / 240.0)) or 1))
        h = min(dt / steps, 1.0 / 120.0)
        w2 = self.omega0 ** 2
        c = 2.0 * self.zeta * self.omega0

        for _ in range(steps):
            accel = -w2 * (self.x - self.goal) - c * self.v
            self.v += accel * h
            self.x += self.v * h

        travel = abs(self.goal - self.x)
        settled = travel < 2e-4 and abs(self.v) < 2e-3
        if settled:
            self.x = self.goal
            self.v = 0.0

        self.apply()
        return settled or self.elapsed > self.budget

    def apply(self) -> None:
        try:
            self.target.setProperty(self.prop, self.x)
        except (RuntimeError, TypeError):
            pass


class _LinearAnim:
    """减少动效模式下的退化动画：短时长、无位移、无过冲。"""

    __slots__ = ("target", "prop", "x0", "x1", "elapsed", "duration")

    def __init__(self, target: QObject, prop: str, x0: float, x1: float, duration: float):
        self.target = target
        self.prop = prop
        self.x0 = float(x0)
        self.x1 = float(x1)
        self.elapsed = 0.0
        self.duration = max(1e-3, duration)

    def step(self, dt: float) -> bool:
        self.elapsed += dt
        p = min(1.0, self.elapsed / self.duration)
        e = p * p * (3.0 - 2.0 * p)      # smoothstep：柔和但不过冲
        self.apply()
        return p >= 1.0

    def apply(self) -> None:
        p = min(1.0, self.elapsed / self.duration)
        e = p * p * (3.0 - 2.0 * p)
        try:
            self.target.setProperty(self.prop, self.x0 + (self.x1 - self.x0) * e)
        except (RuntimeError, TypeError):
            pass


# --------------------------------------------------------------------------
# 全局心跳：一个 60fps 定时器驱动所有弹簧
# --------------------------------------------------------------------------

_TICK_MS = 16
_ticker: QTimer | None = None
_active: list = []
_last_tick: float = 0.0


def _tick() -> None:
    global _last_tick
    now = time.perf_counter()
    dt = (now - _last_tick) if _last_tick else (_TICK_MS / 1000.0)
    _last_tick = now
    dt = max(1e-4, min(0.1, dt))

    for anim in list(_active):
        try:
            done = anim.step(dt)      # step() 内部已负责把值写回属性
        except (RuntimeError, TypeError):
            done = True               # 目标已销毁：丢弃，别让整个 ticker 挂掉
        if done and anim in _active:
            _active.remove(anim)

    if not _active and _ticker is not None:
        _ticker.stop()


def _ensure_ticker() -> None:
    global _ticker, _last_tick
    if _ticker is None:
        _ticker = QTimer()
        _ticker.setInterval(_TICK_MS)
        _ticker.setTimerType(Qt.TimerType.PreciseTimer)
        _ticker.timeout.connect(_tick)
    if not _ticker.isActive():
        _last_tick = 0.0
        _ticker.start()


class _Animator:
    """管理某个对象上的一组属性动画。

    核心能力是**可中断性**：对同一属性再次调用 :meth:`to` 时，新弹簧从
    **当前位置**出发并**继承当前速度**，而不是从名义起点重来。

    为什么不用 QPropertyAnimation
    ----------------------------
    见 :meth:`Spring.to_easing`：PySide6 6.9 的 ``QEasingCurve`` 在 BezierSpline
    模式下析构即崩，而 ``setCustomType`` 又依赖 Python 包装对象存活，同样脆弱。
    自己积分反而更简单、更可控，还能做速度继承。
    """

    def __init__(self, target: QObject):
        self._target = target
        self._running: dict[str, object] = {}

    @staticmethod
    def _name(prop: str | bytes) -> str:
        return prop.decode("ascii") if isinstance(prop, (bytes, bytearray)) else str(prop)

    def _current(self, name: str, default: float = 0.0) -> float:
        try:
            value = self._target.property(name)
        except (RuntimeError, TypeError):
            return default
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _velocity(self, name: str) -> float:
        """中断时继承的速度（单位/秒）。这是"可中断性"的实质。"""
        prev = self._running.get(name)
        if isinstance(prev, _SpringAnim):
            return prev.v
        if isinstance(prev, _LinearAnim) and prev.duration > 0:
            return (prev.x1 - prev.x0) / prev.duration
        return 0.0

    def to(
        self,
        prop: str | bytes,
        end: float,
        spring: Spring = SPRING_DEFAULT,
        duration: int | None = None,
    ):
        """把属性弹到 ``end``。返回内部动画对象（仅用于调试，可能为 None）。"""
        name = self._name(prop)
        start = self._current(name)
        velocity = self._velocity(name)

        # 旧动画作废 —— 它的速度已经被新弹簧继承了
        old = self._running.pop(name, None)
        if old is not None and old in _active:
            _active.remove(old)

        if abs(end - start) < 1e-12 and abs(velocity) < 1e-12:
            self._safe_set(name, float(end))
            return None

        if reduced_motion():
            anim: object = _LinearAnim(
                self._target, name, start, float(end),
                (duration or ReducedMotion().duration) / 1000.0,
            )
        else:
            anim = _SpringAnim(self._target, name, start, velocity, float(end), spring)

        self._running[name] = anim
        _active.append(anim)
        try:
            _ensure_ticker()
        except Exception:
            # 没有 QApplication（离线测试）时退化为立即落位，不抛异常
            if anim in _active:
                _active.remove(anim)
            self._running.pop(name, None)
            self._safe_set(name, float(end))
        return anim

    def _safe_set(self, name: str, value: float) -> None:
        try:
            self._target.setProperty(name, value)
        except (RuntimeError, TypeError):
            pass

    def stop(self, prop: str | bytes | None = None) -> None:
        if prop is None:
            for a in list(self._running.values()):
                if a in _active:
                    _active.remove(a)
            self._running.clear()
        else:
            name = self._name(prop)
            a = self._running.pop(name, None)
            if a is not None and a in _active:
                _active.remove(a)

    @property
    def busy(self) -> bool:
        return any(a in _active for a in self._running.values())


#: 动画执行器挂在 target 的 Python 属性上（而不是全局字典 —— 全局字典以 id()
#: 为键会在对象回收后指向错的对象，同样会引发难查的崩溃）
_ANIMATOR_ATTR = "_music_studio_animator"


def animator(target: QObject) -> _Animator:
    """取（或创建）某个对象的动画执行器。"""
    existing = getattr(target, _ANIMATOR_ATTR, None)
    if isinstance(existing, _Animator):
        return existing
    created = _Animator(target)
    try:
        setattr(target, _ANIMATOR_ATTR, created)
    except Exception:
        # 少数对象（纯 C++ 类型）不接受动态属性，那就每次新建一个 ——
        # 只是失去"从当前速度接管"的能力，不会出错
        pass
    return created


# --------------------------------------------------------------------------
# 常用组合动作
# --------------------------------------------------------------------------


def fade_in(widget: QWidget, duration: int | None = None) -> None:
    """透明度淡入。用于降级模式，也用于轻量提示。"""
    effect = widget.graphicsEffect()
    if not isinstance(effect, QGraphicsOpacityEffect):
        effect = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(effect)
    effect.setOpacity(0.0)
    a = QPropertyAnimation(effect, b"opacity", widget)
    a.setDuration(duration or (ReducedMotion().duration if reduced_motion() else 240))
    a.setStartValue(0.0)
    a.setEndValue(1.0)
    a.setEasingCurve(QEasingCurve(QEasingCurve.Type.OutCubic))
    a.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)


def cross_fade(widget: QWidget, out: bool = False, duration: int | None = None) -> None:
    """交叉淡入淡出 —— 减少动效时唯一允许的过渡形式。"""
    fade_in(widget, duration)


def on_next_frame(callback) -> None:
    """把回调推迟到下一个事件循环轮次（用于让布局先稳定再启动动画）。"""
    QTimer.singleShot(0, callback)


def after(ms: int, callback) -> None:
    QTimer.singleShot(ms, callback)


__all__ = [
    "Spring", "SPRING_SNAPPY", "SPRING_DEFAULT", "SPRING_GENTLE",
    "SPRING_BOUNCY", "SPRING_PAGE", "ReducedMotion",
    "reduced_motion", "refresh_reduced_motion",
    "animator", "fade_in", "cross_fade", "on_next_frame", "after",
    "Property",
]
