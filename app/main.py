"""制乐 Music Studio —— 程序入口。

启动流程
--------
1. 建 QApplication，**在创建任何窗口之前**设好高 DPI 与字体策略。
2. 建主窗口（此时还没挂系统材质 —— 得等窗口真正创建出 HWND）。
3. ``show()`` 之后系统材质才应用得上；之后再触发首次渲染。

用法::

    python -m app.main                 # 打开内置示范曲
    python -m app.main path.json       # 打开已有工程
    python -m app.main --empty         # 新建空工程
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

# 让 Qt 别去找它自带的字体目录（PySide6 不再随包发布字体，会在 stderr 刷警告）
os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


def _set_app_user_model_id() -> None:
    """给进程一个独立的 AppUserModelID。

    不做这一步的话，Windows 任务栏会把 Python 进程归到 python.exe 名下，
    任务栏图标显示的是 Python 的图标 —— 自己的应用图标就白做了。
    """
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MusicStudio.ZhiYue.1")
    except Exception:
        pass


def _enable_hi_dpi() -> None:
    """开启 Per-Monitor V2 DPI 感知 —— 多显示器不同缩放时窗口才不会糊。"""
    if sys.platform != "win32":
        return
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def build_project(argv: list[str]):
    """按命令行参数决定打开什么工程。"""
    from app.audio import demo
    from app.audio.project import Project

    args = [a for a in argv[1:] if not a.startswith("-")]
    if "--empty" in argv:
        return demo.empty_project()

    if args:
        path = Path(args[0])
        if path.exists():
            try:
                return Project.load(path)
            except Exception as exc:
                print(f"[warn] 打开 {path} 失败：{exc}", file=sys.stderr)
        else:
            print(f"[warn] 找不到 {path}，改用示范曲", file=sys.stderr)

    return demo.demo_project()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)

    _set_app_user_model_id()
    _enable_hi_dpi()

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(argv)
    app.setApplicationName("制乐 Music Studio")
    app.setOrganizationName("MusicStudio")
    app.setApplicationVersion("0.1.0")

    from app import icons
    from app.ui.main_window import MainWindow

    app.setWindowIcon(icons.app_icon(256))

    project = build_project(argv)
    window = MainWindow(project)
    window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
