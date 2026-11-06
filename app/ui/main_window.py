"""主窗口 —— 侧边栏 + 页面栈 + 走带条的装配。

单向数据流
----------
::

    用户操作 → 改 Project → controller.touch() → 防抖 → 后台渲染 → 信号 → 界面刷新

界面从不直接读写播放器状态，也不在编辑时同步重渲染。所有跨线程与跨模块的
通信都走 Qt 信号，因此不存在"改了一半的工程被渲染"这类竞态。
"""

from __future__ import annotations

import sys

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QMessageBox, QStackedWidget, QVBoxLayout, QWidget,
)

from app import icons, theme
from app.audio import demo
from app.audio.project import Project, SynthParams, Track
from app.theme import accents, materials, radii, spacing
from app.ui.chrome import FramelessWindow
from app.ui.controller import AppController
from app.ui.pages.overview import OverviewPage
from app.ui.pages.placeholder import PlaceholderPage
from app.ui.pages.sequencer import SequencerPage

# 页面注册表：(导航 key, 类名, 模块路径)
# 用「运行时尝试导入」而不是顶层 import —— 某个模块语法错/依赖缺失时，
# 应该只让那一个页面降级并**大声报错**，而不是整个程序起不来。
PAGE_SPECS: tuple[tuple[str, str, str], ...] = (
    ("overview", "OverviewPage", "app.ui.pages.overview"),
    ("sequencer", "SequencerPage", "app.ui.pages.sequencer"),
    ("piano_roll", "PianoRollPage", "app.ui.pages.piano_roll"),
    ("drums", "DrumPage", "app.ui.pages.drums"),
    ("mixer", "MixerPage", "app.ui.pages.mixer"),
    ("effects", "EffectsPage", "app.ui.pages.effects"),
    ("composer", "ComposerPage", "app.ui.pages.composer"),
    ("ai", "AiStudioPage", "app.ui.pages.ai_studio"),
)


def load_page_class(module_path: str, class_name: str):
    """按需导入页面类。失败返回 (None, 原因)。"""
    import importlib
    import traceback

    try:
        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        return cls, ""
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        print(f"[page-load] {module_path}.{class_name} 导入失败 —— {detail}",
              file=sys.stderr)
        traceback.print_exc()
        return None, detail
from app.ui.sidebar import Sidebar
from app.ui.transport import TransportBar


#: 导航结构：(key, 显示名, Twemoji 图标, 强调色, 说明/占位信息)
NAV_OVERVIEW = ("overview", "总览", "headphones", accents.blue)

PLACEHOLDERS = [
    ("sequencer", "音序器", "notes", accents.blue,
     "多轨步进网格：在 16 分音符网格上直接点亮音符，一屏编排整首曲子的骨架。",
     ["按轨道分行的步进网格，支持拖拽刷音符", "每格显示力度，右键调力度与音长",
      "小节循环试听，边改边听"]),
    ("piano_roll", "钢琴卷帘", "piano", accents.purple,
     "以音高为纵轴精细编排旋律：画音符、拉伸时值、框选批量编辑。",
     ["网格吸附与音符拉伸", "多选、复制、移调、量化", "底部力度条与音阶高亮"]),
    ("drums", "鼓机", "drums", accents.orange,
     "纯代码合成的套鼓：底鼓、军鼓、踩镲……全部可调音高、衰减与音色。",
     ["12 件套鼓的分轨网格", "每件独立的 tune / decay / tone / snap 调节",
      "内置常用鼓型，一键铺满一小节"]),
    ("mixer", "混音台", "control_knobs", accents.green,
     "分轨推子 + 声像 + 发送，看齐硬件调音台的操作直觉。",
     ["音量推子、声像旋钮、静音独奏", "混响/延迟发送量",
      "实时电平表与峰值保持"]),
    ("effects", "效果器", "crystal_ball", accents.pink,
     "每条轨道一串效果链，拖动即可改变信号经过的顺序。",
     ["混响 / 延迟 / 失真 / 滤波 / 合唱 / 降位", "拖拽排序，旁通单个效果",
      "参数可自动化（后续版本）"]),
    ("composer", "编曲助手", "robot", accents.yellow,
     "点一下出一段：按和弦走向生成鼓型、贝斯线、旋律与和声铺底。",
     ["指定调式与和弦走向，一键生成四个声部", "马尔可夫旋律 + 节奏型规则",
      "生成结果直接进编辑区，可继续手改"]),
]


class MainWindow(FramelessWindow):
    """应用主窗口。"""

    def __init__(self, project: Project | None = None):
        super().__init__()
        self.project = project or demo.demo_project()
        self.controller = AppController(self.project, self)

        self.setWindowTitle("制乐 Music Studio")
        self.setWindowIcon(icons.app_icon(256))

        # ---- 装配 ------------------------------------------------------
        content = QWidget()
        content.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        row = QHBoxLayout(content)
        row.setContentsMargins(spacing.sm, 0, spacing.sm, 0)
        row.setSpacing(spacing.sm)

        self.sidebar = Sidebar(content)
        self._build_nav()
        row.addWidget(self.sidebar)

        self.stack = QStackedWidget(content)
        self.stack.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        row.addWidget(self.stack, 1)

        self.pages: dict[str, QWidget] = {}
        self.overview = None
        self.sequencer = None
        self.page_errors: dict[str, str] = {}

        for key, class_name, module_path in PAGE_SPECS:
            cls, err = load_page_class(module_path, class_name)
            if cls is None:
                # 降级成占位页，但把原因记下来 —— 用户点进去会看到「加载失败：…」
                self.page_errors[key] = err
                page = PlaceholderPage(
                    class_name.replace("Page", ""), "warning",
                    f"这个页面加载失败了：\n{err}\n\n请看控制台的 traceback。",
                    ["模块导入时抛了异常", "其余页面不受影响", "修好后重启即可"],
                    accents.red, "加载失败",
                )
            else:
                try:
                    page = cls(self.project)
                except Exception as exc:
                    import traceback

                    detail = f"{type(exc).__name__}: {exc}"
                    self.page_errors[key] = detail
                    print(f"[page-load] {class_name} 构造失败 —— {detail}", file=sys.stderr)
                    traceback.print_exc()
                    page = PlaceholderPage(
                        class_name.replace("Page", ""), "warning",
                        f"这个页面构造失败了：\n{detail}",
                        ["其余页面不受影响", "修好后重启即可"], accents.red, "加载失败",
                    )
            if key == "overview":
                self.overview = page
            elif key == "sequencer":
                self.sequencer = page
            self.pages[key] = page
            self.stack.addWidget(page)
            self._wire_page(page)

        # 未实现的模块仍放占位页
        for key, name, icon_name, accent, summary, bullets in PLACEHOLDERS:
            if key in self.pages:
                continue
            page = PlaceholderPage(name, icon_name, summary, bullets, accent, "下一轮实现")
            self.pages[key] = page
            self.stack.addWidget(page)

        self.transport = TransportBar()
        self.assemble(content, self.transport)

        # ---- 接线 ------------------------------------------------------
        self.sidebar.navigated.connect(self._navigate)
        if self.page_errors:
            # 有页面没能加载时，开局就在状态栏说清楚，别让用户自己发现
            self.transport.set_status(
                "有 %d 个页面加载失败，点进去可看原因" % len(self.page_errors), 9000)
        self.overview.projectEdited.connect(lambda: self.controller.touch(False))
        self.overview.trackDeleted.connect(self._delete_track)
        self.overview.addTrackRequested.connect(self._add_track)
        self.overview.trackSelected.connect(self._preview_track)

        self.transport.playToggled.connect(self.controller.toggle_play)
        self.transport.stopRequested.connect(self.controller.stop)
        self.transport.seekRequested.connect(self.controller.seek)
        self.transport.loopToggled.connect(self._toggle_loop)
        self.transport.exportRequested.connect(self._export)
        self.transport.renderRequested.connect(self.controller.render_now)

        self.controller.transportChanged.connect(self.transport.set_state)
        self.controller.renderStarted.connect(self._on_render_started)
        self.controller.renderProgress.connect(self._on_render_progress)
        self.controller.renderFinished.connect(self._on_render_finished)
        self.controller.renderFailed.connect(self._on_render_failed)
        self.controller.projectChanged.connect(self._on_project_changed)
        self.controller.renderFinished.connect(self._on_render_finished)
        self.controller.transportChanged.connect(self._on_transport_changed)
        self.controller.statusMessage.connect(self.transport.set_status)

        self._install_shortcuts()
        self._sync_project_meta()

        # 启动后立刻渲染一次，让用户第一眼就有波形可看、有声音可听
        QTimer.singleShot(260, self.controller.render_now)

    # -- 导航 ------------------------------------------------------------

    def _build_nav(self) -> None:
        sb = self.sidebar
        sb.add_section("工程")
        sb.add_item(*NAV_OVERVIEW)
        sb.add_section("创作")
        for key, name, icon_name, accent, _s, _b in PLACEHOLDERS[:3]:
            sb.add_item(key, name, icon_name, accent)
        sb.add_section("调整")
        for key, name, icon_name, accent, _s, _b in PLACEHOLDERS[3:]:
            sb.add_item(key, name, icon_name, accent)
        # AI 页不在 PLACEHOLDERS 里：那批是"功能页"的说明卡，而这一页的说明
        # 写在页面自己身上（它是分析工具，不是又一个编辑器）
        sb.add_section("分析")
        sb.add_item("ai", "AI 扒谱 · 复刻", "robot", accents.purple)
        sb.add_stretch()
        sb.add_divider()
        sb.add_item("about", "关于", "sparkles", accents.purple)
        sb.set_current("overview")
        sb.item("overview").set_accent(accents.blue)

    def _navigate(self, key: str) -> None:
        self.sidebar.set_current(key)
        page = self.pages.get(key)
        if page is not None:
            self.stack.setCurrentWidget(page)
            # 切过去时补一次位置 —— 否则新页面上的播放头会停在 0 直到下次播放
            st = self.controller.player.snapshot()
            handler = getattr(page, "set_position", None)
            if handler is not None:
                try:
                    handler(st.position, st.playing)
                except TypeError:
                    handler(st.position)
                except Exception:
                    pass
            return
        if key == "about":
            self._show_about()
            # 关于只是弹窗，不算导航目标 —— 把选中态还给当前页
            current = self.stack.currentWidget()
            for k, p in self.pages.items():
                if p is current:
                    self.sidebar.set_current(k)
                    break

    # -- 快捷键 ----------------------------------------------------------

    def _install_shortcuts(self) -> None:
        for seq, fn in (
            ("Space", self.controller.toggle_play),
            ("Ctrl+R", self.controller.render_now),
            ("Ctrl+E", self._export),
            ("Ctrl+S", self._save_project),
            ("Ctrl+O", self._open_project),
            ("Home", lambda: self.controller.seek(0.0)),
            ("Ctrl+Z", self._undo),
            ("Ctrl+Y", self._redo),
            ("Ctrl+Shift+Z", self._redo),
        ):
            sc = QShortcut(QKeySequence(seq), self)
            sc.activated.connect(fn)

    # -- 工程操作 --------------------------------------------------------

    # -- 页面接线 --------------------------------------------------------

    def _wire_page(self, page: QWidget) -> None:
        """按契约把页面的信号接到控制器上。

        契约规定页面暴露 `edited`（改了工程内容）与 `seekRequested`（请求定位）。
        历史模块里还有叫 `notesEdited` 的，一并兼容 —— 不为了统一命名去改已经
        验证过的代码，那是纯粹的返工风险。
        """
        ctrl = self.controller
        try:
            if hasattr(page, "bind"):
                page.bind(ctrl)
        except Exception as exc:
            print(f"[page-wire] bind 失败：{exc}", file=sys.stderr)

        for sig_name in ("edited", "notesEdited"):
            sig = getattr(page, sig_name, None)
            if sig is not None and hasattr(sig, "connect"):
                try:
                    sig.connect(lambda: ctrl.touch(False))
                except Exception:
                    pass

        seek = getattr(page, "seekRequested", None)
        if seek is not None and hasattr(seek, "connect"):
            try:
                seek.connect(ctrl.seek)
            except Exception:
                pass

    def _current_page(self) -> QWidget | None:
        return self.stack.currentWidget()

    def _on_transport_changed(self, st) -> None:
        """播放位置只转发给**当前可见页**。

        7 个页面都收 30fps 的位置更新是纯浪费 —— 只有用户正在看的那个需要画播放头，
        其余的收到了也只是白白重绘。
        """
        self.transport.set_state(st)
        page = self._current_page()
        handler = getattr(page, "set_position", None)
        if handler is not None:
            try:
                handler(st.position, st.playing)
            except TypeError:
                # 少数页面只收一个参数
                handler(st.position)
            except Exception:
                pass

    def _on_render_finished(self, _stats) -> None:
        for page in self.pages.values():
            fn = getattr(page, "refresh_notes", None)
            if fn is not None:
                try:
                    fn()
                except Exception:
                    pass

    def _sync_pages(self) -> None:
        """工程换对象或结构变了之后，同步所有页面。"""
        for page in self.pages.values():
            # 撤销是「就地替换 dict 内容」，打开工程是「换对象」——
            # 后者必须让页面重新拿到引用，否则它们还在改一个没人读的旧工程
            if hasattr(page, "set_project") and getattr(page, "project", self.project) is not self.project:
                try:
                    page.set_project(self.project)
                except Exception:
                    pass
            fn = getattr(page, "rebuild", None)
            if fn is not None:
                try:
                    fn()
                except Exception:
                    pass

    def _undo(self) -> None:
        if self.controller.undo():
            self._sync_pages()

    def _redo(self) -> None:
        if self.controller.redo():
            self._sync_pages()

    def _sync_project_meta(self) -> None:
        p = self.project
        self.transport.set_project_info(
            p.bpm, p.steps_per_bar, p.seconds_per_step, p.loop_range
        )
        self.transport.set_audio(self.controller.audio, self.controller.player.duration)
        self.transport.set_loop(self.controller.player.looping)
        self.transport.set_rendering(self.controller.rendering)
        self.titlebar.set_title(p.name, f"{p.bpm:g} BPM · {len(p.tracks)} 轨")
        self._sync_pages()

    def _on_project_changed(self) -> None:
        # **先跟随控制器**，再同步页面。
        # MainWindow.project 与 controller.project 曾经是两个独立真相源：
        # 任何只调 controller.set_project() 的路径（不做 self.project = ...）
        # 都会让 _sync_pages 拿旧对象去比对，于是所有页面静默地继续指着旧工程，
        # 用户之后的编辑全写进一个没人读的对象里 —— 不报错、不崩，只是"改了没反应"。
        # 现在 MainWindow 只做追随者，控制器是唯一真相源。
        self.project = self.controller.project
        self._sync_project_meta()

    def _add_track(self, kind: str) -> None:
        palette = accents.track_palette()
        idx = len(self.project.tracks)
        color = palette[idx % len(palette)]
        if kind == "drum":
            from app.audio import drums

            t = Track(name=f"鼓组 {idx + 1}", kind="drum", icon="drums",
                      color=color.name(), volume=0.9)
            t.kit = drums.default_voices()
        else:
            t = Track(name=f"合成器 {idx + 1}", kind="synth", icon="piano",
                      color=color.name(), volume=0.8, send_reverb=0.2)
        self.project.add_track(t)
        self.controller.touch(structural=True)
        self.transport.set_status(f"已添加「{t.name}」", 3000)

    def _delete_track(self, track: Track) -> None:
        if len(self.project.tracks) <= 1:
            self.transport.set_status("至少要保留一条轨道", 3000)
            return
        self.project.remove_track(track)
        self.controller.touch(structural=True)
        self.transport.set_status(f"已删除「{track.name}」", 3000)

    def _preview_track(self, track: Track) -> None:
        self.transport.set_status(
            f"「{track.name}」· {len(track.notes)} 音符 · "
            f"{'鼓机' if track.kind == 'drum' else track.synth.wave}",
            3000,
        )

    def _toggle_loop(self, enabled: bool) -> None:
        self.controller.set_loop(enabled)
        self.transport.set_loop(enabled)
        self.transport.set_status("循环播放已开启" if enabled else "循环播放已关闭", 2000)

    # -- 渲染回调 --------------------------------------------------------

    def _on_render_started(self) -> None:
        self.transport.set_rendering(True, 0.0, "准备中")

    def _on_render_progress(self, frac: float, message: str) -> None:
        self.transport.set_rendering(True, frac, message)

    def _on_render_finished(self, stats) -> None:
        self.transport.set_rendering(False)
        self.transport.set_audio(self.controller.audio, self.controller.player.duration)

    def _on_render_failed(self, message: str) -> None:
        self.transport.set_rendering(False)
        QMessageBox.warning(self, "渲染失败", message[:1200])

    # -- 文件 ------------------------------------------------------------

    def _export(self) -> None:
        if not self.controller.has_audio:
            self.transport.set_status("还在渲染，稍后再导出", 3000)
            self.controller.render_now()
            return

        path, _sel = QFileDialog.getSaveFileName(
            self, "导出音频", str(Path.home() / f"{self.project.name}.wav"),
            "WAV 16 位 (*.wav);;WAV 24 位 (*.wav);;WAV 32 位浮点 (*.wav)",
        )
        if not path:
            return
        bit = 16
        if "24" in _sel:
            bit = 24
        elif "32" in _sel:
            bit = 32

        base = Path(path)
        try:
            written = self.controller.export(base.parent, base.stem, bit, want_midi=True)
        except Exception as exc:
            QMessageBox.warning(self, "导出失败", str(exc))
            return
        names = "、".join(f"{p.name}" for p in written.values())
        self.transport.set_status(f"已导出：{names}", 6000)
        QMessageBox.information(self, "导出完成", f"已写入到\n{base.parent}\n\n{names}")

    def _save_project(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "保存工程", str(Path.home() / f"{self.project.name}.musicproj.json"),
            "制乐工程 (*.musicproj.json);;JSON (*.json)",
        )
        if not path:
            return
        self.controller.save_project(path)
        self.transport.set_status(f"工程已保存到 {Path(path).name}", 5000)

    def _open_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "打开工程", str(Path.home()),
            "制乐工程 (*.musicproj.json *.json);;所有文件 (*)",
        )
        if not path:
            return
        try:
            project = Project.load(path)
        except Exception as exc:
            QMessageBox.warning(self, "打开失败", f"{type(exc).__name__}: {exc}")
            return
        self.project = project
        # 页面引用统一由 _sync_pages 挨个换（现在 7 个页面都实现了 set_project）。
        # 早先这里要手工改 overview.project / overview.master.project ——
        # 那就是「总览页不守契约」留下的补丁，已经不需要了。
        self.controller.set_project(project)
        self.transport.set_status(f"已打开 {Path(path).name}", 5000)

    # -- 关于 ------------------------------------------------------------

    def _show_about(self) -> None:
        from app.platform import win_glass

        glass_line = "系统亚克力（真实背景虚化）" if self.glass_active else f"自绘玻璃（{self.glass_note}）"
        QMessageBox.information(
            self,
            "关于 制乐 Music Studio",
            "制乐 Music Studio\n"
            "一个用 Python + PySide6 写的音乐制作工具。\n\n"
            f"玻璃材质：{glass_line}\n"
            f"图标：Twemoji（CC-BY 4.0）\n"
            f"音频：numpy 离线渲染 + sounddevice 实时播放\n\n"
            "快捷键\n"
            "  空格      播放 / 暂停\n"
            "  Ctrl+R    重新渲染\n"
            "  Ctrl+E    导出\n"
            "  Ctrl+S    保存工程\n"
            "  Ctrl+O    打开工程\n"
            "  Home      回到开头",
        )

    # -- 关闭 ------------------------------------------------------------

    def closeEvent(self, event):
        try:
            self.controller.shutdown()
        except Exception:
            pass
        super().closeEvent(event)


__all__ = ["MainWindow", "PLACEHOLDERS"]
