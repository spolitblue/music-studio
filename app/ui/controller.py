"""应用控制器 —— 界面与音频引擎之间的唯一桥梁。

线程模型
--------
渲染一首曲子要几秒，绝不能放在 UI 线程（界面会卡死、弹簧动画会掉帧）。
所以：**渲染跑在 QThread 里，结果通过信号回到 UI 线程**。

每次编辑都立刻重渲染会很浪费（拖动旋钮时会触发几十次）。所以用**防抖**：
编辑后等 450ms 静默再渲染；如果这段时间里又有编辑，计时器重置。
这是所有 DAW 处理"响应式重渲染"的标准做法。

后台作业
--------
除了整曲渲染，还有一类**秒级的短重活**：混音台要按轨渲染一遍才知道每条轨道
多响、钢琴卷帘要算波形、效果器页要剪试听片段。它们各自只值零点几到两三秒，
但都**不能**跑在 UI 线程上 —— 混音台首次进页面时逐轨算一遍，界面就冻 5.3 秒。

所以这里还有一个通用短任务设施 :meth:`AppController.submit_background`：
线程池 + 队列信号，结果一律回 UI 线程，作业句柄支持粘性取消。
页面只管"要算什么""拿到结果怎么画"，线程生命周期全部由控制器统一收口 ——
`shutdown()` 里两条线（渲染线程与作业池）一起等。

状态轮询
--------
播放位置由 PortAudio 线程推进。UI 不监听它（跨线程回调很危险），
而是用 33ms 的定时器主动读快照 —— 单向数据流，不会有竞态。
"""

from __future__ import annotations

import hashlib
import inspect
import json
import time
import traceback
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, QRunnable, Qt, QThread, QThreadPool, QTimer, Signal

from app.audio import render, wavio
from app.audio.player import Player, PlayerState
from app.audio.project import Project


#: 防抖基准：编辑停止这么久之后才真正重渲染。
RENDER_DEBOUNCE_MS = 450
#: 连续编辑时防抖拉长到的上限。
#:
#: 为什么需要这一条：拖动推子/旋钮时每 16ms 就有一次编辑，"每次编辑都重置计时器"
#: 确实能把一次拖动合并成一次渲染；但只要渲染**正在进行**时用户还在改，
#: ``_pending`` 就会在渲染结束后立刻再排一次 —— 一次长拖动于是变成
#: "渲染→渲染→渲染"背靠背地跑，中间没有一秒空闲，而几秒后才回来的音频
#: 早就是过时的了。编辑越密就该等得越久。
RENDER_DEBOUNCE_MAX_MS = 1300
#: 判定"正在连续编辑"的窗口与编辑次数
EDIT_WINDOW_SECONDS = 1.5
EDIT_BUSY_COUNT = 4


def project_signature(project: Project) -> str:
    """工程内容的指纹（排序后的 JSON 的 sha1）。

    用途只有一个：判断"这次重渲染到底有没有必要"。编辑路径里有很多地方会发
    ``edited``（页面刷新、撤销后重建、清一个本来就不存在的静音…），它们不一定
    真的改了数据；而整曲渲染是秒级的开销，不能为"没变"买单。
    示范曲 45KB JSON，算一次几毫秒 —— 换掉几秒的渲染，怎么算都值。
    """
    try:
        payload = json.dumps(project.to_dict(), sort_keys=True, ensure_ascii=False)
    except Exception:
        return ""
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


# ==========================================================================
# 渲染工作线程
# ==========================================================================


class _Cancelled(Exception):
    """渲染被主动取消。"""


class RenderWorker(QObject):
    """在后台线程渲染工程。

    支持**协作式取消**：``render_project`` 每个轨道开始时都会回调进度，
    这里趁机检查取消标志并抛异常中止。没有这一步的话，用户渲染到一半关窗口，
    ``QThread`` 还在跑，Qt 会在进程退出时直接 fail-fast 崩掉。
    """

    progress = Signal(float, str)
    finished = Signal(object, object)      # (audio, stats)
    failed = Signal(str)

    def __init__(self):
        super().__init__()
        self._project: Project | None = None
        self._cancelled = False
        #: 一旦进入关闭流程就永久置位。`cancel()` 只应从 shutdown 调用 ——
        #: 若允许 `configure()` 把它清掉，关闭期间排队送达的渲染请求就会
        #: 把取消抹掉，线程收不了工，进程退出时被 Qt __fastfail 带走。
        self._dead = False

    def configure(self, project: Project) -> None:
        if self._dead:
            return
        self._project = project
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True
        self._dead = True

    def _report(self, frac: float, message: str) -> None:
        if self._cancelled:
            raise _Cancelled()
        self.progress.emit(float(frac), str(message))

    def run(self) -> None:
        try:
            if self._dead:
                return                      # 已在关闭流程中，直接收工
            if self._project is None:
                self.failed.emit("没有要渲染的工程")
                return
            audio, stats = render.render_project(self._project, progress=self._report)
            if self._cancelled:
                return
            self.finished.emit(audio, stats)
        except _Cancelled:
            return                       # 静默退出，不当作错误
        except Exception as exc:  # pragma: no cover
            self.failed.emit(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")


# ==========================================================================
# 通用后台作业设施
# ==========================================================================

BACKGROUND_THREADS = 3
"""后台短任务的并发上限。

numpy 的大运算会放开 GIL，所以几条并行确实比串行快；但也不能贪多 ——
作业要抢 CPU 与内存，而**整曲渲染线程**也在同一台机器上跑，把核心占满
只会让那一边变慢。3 是"混音台一次测 5 条轨道"与整曲渲染共存时的折中。
"""


class JobCancelled(Exception):
    """作业被取消：作业函数在能中断的地方抛它，用来提前收工。

    与"出错"分开是有意的 —— 取消不是失败，不该走 ``on_error``，
    也不该在界面上报错。
    """


class BackgroundJob:
    """一次后台作业的句柄：只回答「还在跑吗」「请停下」。

    结果不从这里取 —— 结果走 ``submit_background`` 的 ``on_done``，
    而且保证在 UI 线程里被调用。页面因此不必自己接信号，也不可能忘记
    回到 UI 线程（后台设施最容易出的错，就是"顺手在后台线程里改了控件"）。
    """

    __slots__ = ("_id", "_label", "_cancelled", "_done")

    def __init__(self, job_id: int, label: str):
        self._id = int(job_id)
        self._label = str(label)
        # 取消是**粘性**的：置位之后没有任何路径能把它抹回去。
        # RenderWorker 的第一版就是被"关闭期间排队送达的 configure"复活了取消
        # 标志，线程收不了工，进程退出时被 Qt __fastfail 带走。这里是一次性作业，
        # 结构上就不存在"复活"的入口。
        self._cancelled = False
        self._done = False

    @property
    def id(self) -> int:
        return self._id

    @property
    def label(self) -> str:
        return self._label

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def done(self) -> bool:
        """作业函数已经返回（无论结果被采用还是被丢弃）。"""
        return self._done

    def cancel(self) -> None:
        """请求取消。协作式：作业函数在下一个检查点才会停。"""
        self._cancelled = True

    def raise_if_cancelled(self) -> None:
        """作业函数在能中断的地方调它。

        在长循环里插一个检查点，取消就从"等它跑完"变成"下一站就停" ——
        这正是 :class:`RenderWorker` 用进度回调做的事，也是本工程对
        "不可中断的重活"唯一有效的办法。
        """
        if self._cancelled:
            raise JobCancelled()

    def __repr__(self) -> str:              # pragma: no cover - 只为日志好读
        state = "已取消" if self._cancelled else ("已完成" if self._done else "在跑")
        return f"<BackgroundJob #{self._id} {self._label} {state}>"


class _JobBridge(QObject):
    """工作线程 → UI 线程的单向搬运工。

    从工作线程 ``emit``、接收者住在本对象所属的线程（UI 线程），连接天生
    就是队列式的 —— 回调一定在 UI 线程里跑，不会有例外。
    """

    delivered = Signal(int, object, object)     # (作业 id, 结果, 异常)

    def report(self, job_id: int, result, error) -> None:
        self.delivered.emit(int(job_id), result, error)


#: 结果被丢弃的哨兵（作业被取消，或关闭流程已经开始）。
#: 用哨兵而不是 ``None``：``None`` 完全可能是一个合法结果。
_SKIPPED = object()


def _takes_job_handle(fn) -> bool:
    """``fn`` 是不是想要一个作业句柄参数？

    两种签名都允许：``fn()`` 最自然（算个波形、剪段试听根本不需要取消）；
    秒级的重活则写成 ``fn(job)``，好在循环里插检查点。判断依据是"必填的
    位置参数恰好一个" —— 作业函数不可能有别的必填位置参数，所以可靠；
    拿不准（签名不可内省）时一律按 0 处理，最坏只是失去协作式取消，
    绝不会 TypeError。
    """
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    required = [p for p in params
                if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                and p.default is p.empty]
    return len(required) == 1


def _invoke_job(fn, job: BackgroundJob):
    return fn(job) if _takes_job_handle(fn) else fn()


class _BackgroundTask(QRunnable):
    """线程池里跑的一次调用。

    ``QRunnable`` 不是 QObject，自己没有信号，所以结果靠 :class:`_JobBridge`
    搬回 UI 线程。这里**只**做两件事：调 ``fn``、把结果交给桥 ——
    绝不去碰 ``on_done``，那是 UI 线程的活。
    """

    def __init__(self, job: BackgroundJob, fn, bridge: _JobBridge):
        super().__init__()
        # 不交给 C++ 自动删：Python 侧还要持有这个对象，直到作业收工为止。
        # autoDelete 打开的话，C++ 端会在 run() 返回后立刻删掉它，而 Python
        # 包装器还活着 —— 之后再碰到它就是访问已释放内存。
        self.setAutoDelete(False)
        self._job = job
        self._fn = fn
        self._bridge = bridge

    def run(self) -> None:                  # pragma: no cover - 在工作线程里跑
        if self._job._cancelled:
            self._report(_SKIPPED, None)    # 排到队里才被取消的，直接空转
            return
        try:
            result = _invoke_job(self._fn, self._job)
        except JobCancelled:
            result, error = _SKIPPED, None  # 取消不是失败，不走 on_error
        except Exception as exc:
            result, error = None, exc
        else:
            error = None
        self._report(result, error)

    def _report(self, result, error) -> None:
        try:
            self._bridge.report(self._job.id, result, error)
        except RuntimeError:
            # 桥已经随控制器一起销毁（控制器先于线程池被回收）。没有接收方，
            # 结果丢掉即可 —— 但绝不能让这个异常抛回线程池。
            pass


# ==========================================================================
# 控制器
# ==========================================================================


class AppController(QObject):
    """持有工程、播放器与渲染结果，对外只暴露信号。"""

    projectChanged = Signal()
    """工程的**结构**变了（增删轨道、改音符）—— 界面需要重建列表。"""

    paramsChanged = Signal()
    """只改了参数（音量、包络）—— 界面只需刷新数值，不必重建。"""

    renderStarted = Signal()
    renderProgress = Signal(float, str)
    renderFinished = Signal(object)        # RenderStats
    renderFailed = Signal(str)

    transportChanged = Signal(object)      # PlayerState
    statusMessage = Signal(str, int)       # (文字, 毫秒)
    undoStateChanged = Signal(bool, bool)  # (可撤销, 可重做)

    #: 内部用：请求工作线程开始渲染。**必须走队列连接** —— 直接调用或
    #: QTimer.singleShot 都会在 UI 线程里执行，渲染照样会卡住界面。
    _renderRequest = Signal()

    def __init__(self, project: Project, parent: QObject | None = None):
        super().__init__(parent)
        self.project = project
        self.player = Player(samplerate=render.SR)
        self.audio: np.ndarray | None = None
        self.stats = render.RenderStats()

        self._rendering = False
        self._pending = False
        self._dirty = True
        self._last_state: PlayerState | None = None

        # 渲染预算的记账（见 project_signature 与 touch）：
        #   _edit_times    最近这一阵子的编辑时刻，用来判断"是不是还在连续编辑"
        #   _last_signature 上一次真正渲染的那份内容；内容没变就不再渲染
        #   _render_count/_skip_count 渲染了几次、省下几次 —— 验收要看数字
        self._edit_times: list[float] = []
        self._last_signature = ""
        self._render_signature = ""
        self._render_count = 0
        self._skip_count = 0
        self._last_render_seconds = 0.0

        # 撤销栈：存工程的 JSON 快照。
        # 工程很小（示范曲约 45KB JSON），整份快照比「记录增量操作」简单得多，
        # 也不会因为某条编辑路径忘了记录而漏掉撤销点。
        self._undo: list[tuple[str, dict]] = []
        self._redo: list[tuple[str, dict]] = []
        self._undo_limit = 60
        #: 关闭中。置位后一切会排新渲染的入口全部失效 ——
        #: shutdown 用 processEvents 等线程收工时，排队的信号仍会被投递，
        #: 不挡住的话会在这里排上一个新渲染，把取消白做掉。
        self._shutting_down = False

        # 通用后台短任务设施（见 submit_background）。
        # 与渲染线程**分开**：渲染线程专职整曲渲染、独占一条线；这里是一池
        # 通用短任务，可以有多个并发，收工也一起收。
        self._bg_seq = 0
        self._bg_jobs: dict[int, tuple[BackgroundJob, object, object]] = {}
        self._bg_tasks: dict[int, _BackgroundTask] = {}
        self._bg_shutdown = False       # 粘性：一旦置位，再也不接新作业
        self._bg_bridge = _JobBridge(self)
        self._bg_bridge.delivered.connect(
            self._bg_deliver, Qt.ConnectionType.QueuedConnection)
        self._bg_pool = QThreadPool(self)
        self._bg_pool.setMaxThreadCount(BACKGROUND_THREADS)
        #: 上一次 shutdown 的诊断（耗时毫秒 / 池子是否排空 / 渲染线程是否收工）。
        #: 留着它是为了能被测试与日志读到 —— "测试全绿"和"进程能正常退出"
        #: 是两件事（本工程栽过一次：119 项全绿，退出码却是 0xC0000409）。
        self.shutdown_report: dict[str, object] = {}

        # 防抖：编辑停止一小会儿之后才真正重渲染。间隔不是常数 ——
        # 连续编辑时会被 touch() 拉长（见 RENDER_DEBOUNCE_MAX_MS）。
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(RENDER_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._start_render)

        # 渲染线程
        self._thread = QThread(self)
        self._worker = RenderWorker()
        self._worker.moveToThread(self._thread)
        # 队列连接：run() 会在工作线程的事件循环里执行，而不是调用方的线程
        self._renderRequest.connect(self._worker.run, Qt.ConnectionType.QueuedConnection)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._thread.start()

        # 播放状态轮询（单向：UI 读，回调不反向通知）
        self._poll = QTimer(self)
        self._poll.setInterval(33)
        self._poll.timeout.connect(self._poll_transport)
        self._poll.start()

    # -- 生命周期 --------------------------------------------------------

    def shutdown(self, wait_ms: int = 6000) -> None:
        """停掉播放、取消渲染与后台作业、关闭线程。窗口关闭时必须调用。

        **这里必须等所有后台作业收工**：线程池还有活干时进程退出，Qt 会直接
        fail-fast（退出码 ``0xC0000409``），Python 拦不住。取消一律是粘性的 ——
        关闭期间排队送达的信号不许把取消标志复活（RenderWorker 的教训）。
        """
        t0 = time.perf_counter()
        self._shutting_down = True
        self._bg_shutdown = True        # 粘性：之后 submit_background 一律空转
        self._poll.stop()
        self._debounce.stop()
        try:
            self.player.stop()
        except Exception:
            pass

        # 先把两边的取消请求都发出去，再一起等 —— 见下面的循环
        self._worker.cancel()           # 粘性取消，configure 不会复活它
        self._thread.quit()
        for job, _done, _err in self._bg_jobs.values():
            job.cancel()
        # 回调登记清空 = 关闭期间不可能再有结果落地（排队送达的信号也不行：
        # 它们回来时这里已经没有对应的条目，直接丢掉）。
        self._bg_jobs.clear()

        from PySide6.QtWidgets import QApplication

        # 后台作业与渲染线程**并行**等。串行等的话最坏耗时是两者之和
        # （单轨分析最长 2.6s + 整曲渲染最长 5.7s），必然超预算。
        deadline = time.perf_counter() + wait_ms / 1000.0
        pool_empty = False
        while True:
            QApplication.processEvents()    # 保持界面响应，避免"未响应"
            if self._thread.isRunning():
                self._thread.wait(20)
            pool_empty = self._bg_pool.waitForDone(20)
            if (not self._thread.isRunning() and pool_empty) \
                    or time.perf_counter() >= deadline:
                break
        if pool_empty:
            # 池子真空了才可以丢引用：还有 run() 在跑的话，Python 侧一回收就是
            # use-after-free（C++ 对象被删，而工作线程还在用它，或者反过来）。
            self._bg_tasks.clear()
        self.shutdown_report = {
            "ms": (time.perf_counter() - t0) * 1000.0,
            "pool_drained": bool(pool_empty),
            "thread_running": bool(self._thread.isRunning()),
            "callbacks_left": len(self._bg_jobs),
        }

    # -- 通用后台作业 ----------------------------------------------------

    def submit_background(self, fn, *, label: str = "后台任务",
                          on_done=None, on_error=None) -> BackgroundJob:
        """在后台线程跑 ``fn()``，结果通过队列信号回到 UI 线程。

        与 :class:`RenderWorker` 分开：那个专职整曲渲染、独占一条线程、
        能被 ``render_project`` 的进度回调取消；这个是通用的短任务设施
        （单轨渲染、波形分析、试听片段…），**可以并发多个**。

        * ``fn`` 两种签名都行：``fn()``，或 ``fn(job)`` —— 后者多拿一个句柄，
          好在长循环里用 ``job.raise_if_cancelled()`` 主动收工。
        * ``on_done(result)`` / ``on_error(exc)`` **一定在 UI 线程里**被调用。
          作业被取消、或控制器已开始关闭时，两者都不会被调用。
        * 返回的句柄可以 ``cancel()``；取消是粘性的，之后结果一律丢弃。

        必须在 :meth:`shutdown` 里一并收工 —— 否则线程池还活着，进程退出时
        被 Qt fail-fast 带走。
        """
        self._bg_seq += 1
        job = BackgroundJob(self._bg_seq, label)
        if self._bg_shutdown:
            # 关闭流程开始后**绝不**再往池子里放东西：新作业会拖住线程，而
            # shutdown 正在等池子空。返回一个已取消的句柄，调用方按正常流程走
            # 也不会出事 —— 回调永远不会被调。
            job.cancel()
            job._done = True
            return job
        task = _BackgroundTask(job, fn, self._bg_bridge)
        self._bg_jobs[job.id] = (job, on_done, on_error)
        self._bg_tasks[job.id] = task       # 拿住引用，别让 GC 在 run() 前收走它
        self._bg_pool.start(task)
        return job

    @property
    def background_active(self) -> int:
        """还在跑（含已排队）的后台作业数。"""
        return len(self._bg_jobs)

    @property
    def background_jobs(self) -> tuple[BackgroundJob, ...]:
        return tuple(job for job, _d, _e in self._bg_jobs.values())

    def _bg_deliver(self, job_id: int, result, error) -> None:
        """作业回到 UI 线程的落地点 —— **唯一**碰回调的地方。

        工作线程从不直接调 ``on_done``；否则用户代码就会在后台线程里动控件，
        而 Qt 控件不是线程安全的，症状是随机绘制错乱或访问违例。
        """
        entry = self._bg_jobs.pop(job_id, None)
        if entry is None:
            return                      # 已被 shutdown 清掉：结果不再落地
        job, on_done, on_error = entry
        job._done = True
        self._bg_tasks.pop(job_id, None)
        if self._bg_shutdown or job.cancelled or result is _SKIPPED:
            return                      # 关闭中 / 已取消：粘性丢弃
        try:
            if error is not None:
                if on_error is not None:
                    on_error(error)
            elif on_done is not None:
                on_done(result)
        except Exception:
            # 回调自己的 bug 不该掀掉整个事件循环，但必须留下痕迹
            traceback.print_exc()

    # -- 撤销 / 重做 ------------------------------------------------------

    def push_undo(self, label: str = "编辑") -> None:
        """在**改动之前**调用，把当前状态压进撤销栈。

        约定：所有会改工程的用户操作都要先调它。漏调的后果是「这一步撤不掉」，
        比「记录了但没改」轻得多，所以宁可多压几次。
        """
        try:
            snap = self.project.to_dict()
        except Exception:
            return
        self._undo.append((label, snap))
        if len(self._undo) > self._undo_limit:
            self._undo.pop(0)
        self._redo.clear()
        self.undoStateChanged.emit(True, False)

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def pop_undo(self, label: str = "") -> bool:
        """丢弃最近一个撤销点，**不**推进重做栈。

        用途：页面在动作**之前**压了一个推测性的撤销点，但动作最终没有产生任何
        改动（例如移调被占位挡住、拖动又拖回原位）。此时那个点是个空条目，
        用户按撤销会「没反应」一步，体验很差。

        与 :meth:`undo` 的区别：``undo()`` 是「回退一步并把它放到重做栈」，
        ``pop_undo()`` 是「这一笔压根没发生，抹掉它」。

        ``label`` 非空时只在标签匹配时丢弃，避免页面误删掉别的操作的撤销点。
        """
        if not self._undo:
            return False
        if label and self._undo[-1][0] != label:
            return False
        self._undo.pop()
        self.undoStateChanged.emit(self.can_undo, self.can_redo)
        return True

    def undo_label(self) -> str:
        return self._undo[-1][0] if self._undo else ""

    def redo_label(self) -> str:
        return self._redo[-1][0] if self._redo else ""

    def undo(self) -> bool:
        if not self._undo:
            self.statusMessage.emit("没有可撤销的操作", 2000)
            return False
        label, snap = self._undo.pop()
        # 当前状态先进重做栈，才能「撤了再撤回来」
        try:
            self._redo.append((label, self.project.to_dict()))
        except Exception:
            pass
        self._restore(snap, f"已撤销：{label}")
        return True

    def redo(self) -> bool:
        if not self._redo:
            self.statusMessage.emit("没有可重做的操作", 2000)
            return False
        label, snap = self._redo.pop()
        try:
            self._undo.append((label, self.project.to_dict()))
        except Exception:
            pass
        self._restore(snap, f"已重做：{label}")
        return True

    def _restore(self, snap: dict, message: str) -> None:
        from app.audio.project import Project

        try:
            restored = Project.from_dict(snap)
        except Exception as exc:
            self.statusMessage.emit(f"撤销失败：{exc}", 4000)
            return
        # 就地替换内容，而不是换对象 —— 界面各处都持有 self.project 的引用，
        # 换对象会让它们全都指向被丢弃的旧实例，表现为「撤销后界面不再更新」。
        self.project.__dict__.update(restored.__dict__)
        self.projectChanged.emit()
        self.undoStateChanged.emit(self.can_undo, self.can_redo)
        self.touch(structural=True)
        self.statusMessage.emit(message, 2500)

    # -- 编辑通知 --------------------------------------------------------

    def touch(self, structural: bool = False, immediate: bool = False) -> None:
        """告诉控制器工程被改动了。

        ``structural`` 为真时界面需要重建列表；``immediate`` 跳过防抖（用于
        用户明确点了"重新渲染"）。

        防抖间隔是**自适应**的：最近 1.5 秒里编辑得越密，等得越久
        （450ms → 1300ms）。理由见 :data:`RENDER_DEBOUNCE_MAX_MS`。
        """
        self._dirty = True
        if structural:
            self.projectChanged.emit()
        else:
            self.paramsChanged.emit()
        now = time.perf_counter()
        self._edit_times.append(now)
        self._edit_times = [t for t in self._edit_times
                            if now - t <= EDIT_WINDOW_SECONDS]
        self._debounce.stop()
        if immediate:
            self._start_render()
            return
        busy = min(1.0, len(self._edit_times) / float(EDIT_BUSY_COUNT))
        self._debounce.setInterval(int(
            RENDER_DEBOUNCE_MS + (RENDER_DEBOUNCE_MAX_MS - RENDER_DEBOUNCE_MS) * busy))
        self._debounce.start()

    def set_project(self, project: Project) -> None:
        self.project = project
        self.player.stop()
        self.audio = None
        self._dirty = True
        self.projectChanged.emit()
        self.render_now()

    # -- 渲染 ------------------------------------------------------------

    @property
    def rendering(self) -> bool:
        return self._rendering

    @property
    def has_audio(self) -> bool:
        return self.audio is not None and self.audio.size > 0

    def render_now(self) -> None:
        """立刻渲染（跳过防抖，但仍受"内容没变就不重算"的约束）。"""
        self._debounce.stop()
        self._start_render()

    def render_stats(self) -> dict[str, object]:
        """渲染预算的记账。验收与状态栏都用它：**省下来的次数是可见的**。"""
        return {
            "rendered": self._render_count,
            "skipped": self._skip_count,
            "last_seconds": self._last_render_seconds,
            "pending": self._pending,
            "debounce_ms": self._debounce.interval(),
            "edits_in_window": len(self._edit_times),
        }

    def _start_render(self) -> None:
        if self._shutting_down:
            return
        if self._rendering:
            self._pending = True   # 正在渲染时又来请求 → 完成后补一次
            return
        if not self._dirty and self.has_audio:
            return

        # 内容指纹：跟上次**真正渲染过**的那份一样就跳过。
        # 这条挡掉的是"发了 edited 但数据没变"的那一类（页面刷新、撤销后重建、
        # 点了清除但本来就没东西可清…），它们占编辑路径里相当一部分。
        signature = project_signature(self.project)
        if self.has_audio and signature and signature == self._last_signature:
            self._skip_count += 1
            self._dirty = False
            self._pending = False
            self.statusMessage.emit("内容没有变化 · 跳过重渲染", 2000)
            return

        self._rendering = True
        self._pending = False
        self._render_signature = signature
        self._render_count += 1
        self.renderStarted.emit()
        self._worker.configure(self.project.clone())   # 快照，避免渲染中工程被改
        self._renderRequest.emit()                     # 投递到工作线程执行

    def _on_progress(self, frac: float, message: str) -> None:
        self.renderProgress.emit(frac, message)

    def _on_finished(self, audio, stats) -> None:
        self._rendering = False
        self._dirty = False
        self.audio = audio
        self.stats = stats
        # 记账：这份内容已经渲染过了，之后再收到"内容相同"的请求直接跳过
        self._last_signature = self._render_signature or project_signature(self.project)
        self._last_render_seconds = float(getattr(stats, "seconds", 0.0))

        was_playing = self.player.playing
        pos = self.player.position
        loop = self.player.looping

        self.player.load(audio, stats.sample_rate)
        lo, hi = self.project.loop_range
        self.player.set_loop(loop, lo * self.project.seconds_per_step,
                             hi * self.project.seconds_per_step)
        if pos > 0.05:
            self.player.seek(min(pos, self.player.duration))
        if was_playing:
            self.player.play()

        self.renderFinished.emit(stats)
        self.statusMessage.emit(
            f"渲染完成 · {stats.seconds:.1f}s · 峰值 {stats.peak_db:+.1f} dBFS", 4000
        )
        # 强制刷一次走带条：此时位置没变，靠轮询的增量判断是刷不出来的
        self._poll_transport(force=True)
        if self._pending:
            # 渲染期间还有编辑 → **回到空闲防抖**，不要立刻再渲染一遍。
            # 立刻重渲染等于"用户还在拖的时候就一路背靠背算下去"，
            # 而每一轮算出来的音频在下一轮开始时就已经过时了。
            self._pending = False
            self._debounce.setInterval(RENDER_DEBOUNCE_MAX_MS)
            self._debounce.start()

    def _on_failed(self, message: str) -> None:
        self._rendering = False
        self.renderFailed.emit(message)
        self.statusMessage.emit("渲染失败", 6000)

    # -- 传输 ------------------------------------------------------------

    def play(self) -> None:
        if not self.has_audio:
            self.statusMessage.emit("还没有可播放的音频，正在渲染…", 2500)
            self.render_now()
            return
        if not self.player.play():
            err = self.player.device_error or "音频设备不可用"
            self.statusMessage.emit(f"无法播放：{err}", 6000)

    def pause(self) -> None:
        self.player.pause()

    def toggle_play(self) -> None:
        if self.player.playing:
            self.pause()
        else:
            self.play()

    def stop(self) -> None:
        self.player.stop()
        self._poll_transport()

    def seek(self, seconds: float) -> None:
        self.player.seek(seconds)
        self._poll_transport()

    def seek_step(self, step: float) -> None:
        self.seek(step * self.project.seconds_per_step)

    def set_loop(self, enabled: bool) -> None:
        lo, hi = self.project.loop_range
        self.player.set_loop(
            enabled,
            lo * self.project.seconds_per_step,
            hi * self.project.seconds_per_step,
        )

    def _poll_transport(self, force: bool = False) -> None:
        state = self.player.snapshot()
        last = self._last_state
        # 位置变化很小且播放状态没变时就不发信号 —— 省掉大量无谓的重绘。
        # force=True 用于渲染完成这类"数值突变但位置没动"的场合，
        # 否则走带条上的时长会一直停在 0:00.0。
        if not force and last is not None and last.playing == state.playing \
                and last.looping == state.looping:
            if abs(last.position - state.position) < 0.008 \
                    and abs(last.peak_db - state.peak_db) < 0.6 \
                    and abs(last.duration - state.duration) < 0.01:
                return
        self._last_state = state
        self.transportChanged.emit(state)

    # -- 导出 ------------------------------------------------------------

    def export(self, out_dir: str | Path, stem: str, bit_depth: int = 16,
               want_midi: bool = True) -> dict[str, Path]:
        if not self.has_audio:
            self.render_now()
            raise RuntimeError("音频尚未渲染完成")
        return wavio.export_all(
            out_dir, stem, self.audio, self.project,
            self.stats.sample_rate, bit_depth, want_midi, want_project=True,
        )

    def save_project(self, path: str | Path) -> Path:
        return self.project.save(path)


__all__ = [
    "AppController", "RenderWorker", "BackgroundJob", "JobCancelled",
    "BACKGROUND_THREADS",
]
