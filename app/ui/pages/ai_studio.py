"""AI 扒谱 · 复刻 —— 把一段音频变成可编辑的音符，或者照着它的气质重造一首。

这个页面到底是"AI"还是"算法"
----------------------------
**是算法，而且是故意做成算法的。** 这里没有云端模型、没有 API Key、不联网：
扒谱（起音检测 + 基频估计 + 鼓件粗分）与复刻（速度/调式/织体/明暗指纹）
全部是 :mod:`app.audio.transcribe` 与 :mod:`app.audio.replicate` 里的信号处理。
界面上把这一点写在徽标上（"离线引擎"），因为它决定了使用者该抱什么期待 ——
它不会替你写歌，它把你手上那段声音**翻译成能继续编辑的音符与参数**。

为什么不做成"对话框里问一句就出歌"：那样要么骗人（假进度条 + 随机结果），
要么把工程交给一个不可复现的外部服务。本工程从音色到分析都是"能算出来的
东西就自己算"，这一页也守同一条线。

两条主流程
----------
1. **扒谱**：参考音频 → 起音/基频/鼓件 → 两条轨道（旋律 + 鼓），直接进工程。
2. **复刻**：参考音频 → 指纹（速度/调式/密度/摇摆/明暗）→ 交给编曲助手
   按这套参数重新生成四声部 → 进工程。**不是原曲的复制**，是"同一路数的另一首"。

参考音频从哪来：外部音频文件（wav / mp3 / m4a / flac…），或者**当前工程自己**
（整曲 / 某一条轨道的分轨）。
分轨分析明显更准（实测示范曲：鼓轨 stem 速度误差 0.0%，整曲混音 37%）——
所以"用当前工程的鼓轨当参考"不是一个敷衍的备选项，而是推荐用法；
这条实测结论就写在界面的提示里，不藏着。

控制台
------
下面那个输入框支持几条中文指令（``扒谱`` / ``复刻`` / ``音色包 808`` / ``帮助``）。
它**不是**语言模型，是几十行的关键词匹配 —— 但每条命令都真的干活、真的写进工程，
所以我们叫它"指令台"而不是"聊天框"。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from app import icons, theme
from app.audio import audiodecode, render, replicate, soundpacks, transcribe
from app.audio.project import Project, Track
from app.theme import accents, labels, radii, spacing, type_scale
from app.widgets.glass import (
    GlassButton, GlassCard, GlassChip, GlassDivider, GlassImageButton, GlassPanel,
)

#: 一次分析最多显示多少条控制台日志。日志是"过程留痕"，不是历史记录 ——
#: 留太长只会让 QLabel 越画越慢（它每次都重排整段文本）
LOG_LIMIT = 120

#: 控制台里可用的指令（键是触发词，值是说明）—— 帮助文本与解析共用同一份，
#: 免得"说明书里有的命令其实不认"
COMMANDS: tuple[tuple[str, str], ...] = (
    ("扒谱", "把参考音频扒成音符（可跟 BPM，例如「扒谱 96」）"),
    ("复刻", "按参考音频的指纹重新生成一首（可跟小节数，例如「复刻 8」）"),
    ("音色包", "把音色包加成一条新轨道（例如「音色包 808」「音色包 牛铃」）"),
    ("参考", "切换参考音频：整曲 / 鼓轨 / 贝斯轨 / 旋律轨"),
    ("清空", "清空控制台日志"),
    ("帮助", "显示这份说明"),
)


# ==========================================================================
# 波形缩略图
# ==========================================================================


class WaveformView(QWidget):
    """参考音频的波形缩略图。

    为什么自己画而不是放个现成控件：Qt 没有波形控件，而这里要的东西很少 ——
    一段 (min,max) 包络加一条中线。自己画 30 行，还省掉一次 pixmap 拷贝。
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._envelope: np.ndarray | None = None
        self._label = ""
        self.setMinimumHeight(58)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def set_audio(self, audio: np.ndarray | None, label: str = "") -> None:
        if audio is None or np.asarray(audio).size == 0:
            self._envelope = None
        else:
            self._envelope = render.waveform_envelope(np.asarray(audio), 320)
        self._label = label
        self.update()

    def has_audio(self) -> bool:
        return self._envelope is not None

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(4.0, 2.0, -4.0, -2.0)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.black(0.30))
        painter.drawRoundedRect(r, radii.chip, radii.chip)

        if self._envelope is None:
            painter.setFont(type_scale.caption1.font())
            painter.setPen(QPen(labels.quaternary))
            painter.drawText(r, Qt.AlignmentFlag.AlignCenter, "还没有参考音频")
            painter.end()
            return

        mid = r.center().y()
        half = r.height() / 2.0 - 3.0
        env = self._envelope
        n = max(1, env.shape[0])
        w = r.width() / n
        grad = QLinearGradient(0.0, r.top(), 0.0, r.bottom())
        grad.setColorAt(0.0, accents.teal)
        grad.setColorAt(0.5, accents.blue_light)
        grad.setColorAt(1.0, accents.teal)
        # 波形用渐变画：QVariant 只认 QBrush，直接 QColor(grad) 会抛
        # 「QVariant must be holding a QColor」—— 而 paintEvent 里抛异常
        # 会连带 painter 不 end，后面整页绘制全乱（实测这一条就能让 AI 页测试挂掉）
        painter.setPen(QPen(QBrush(grad), 0.0))
        for i in range(n):
            lo, hi = float(env[i, 0]), float(env[i, 1])
            x = r.left() + i * w
            painter.drawLine(int(x), int(mid - hi * half), int(x), int(mid - lo * half))

        painter.setFont(type_scale.caption2.font())
        painter.setPen(QPen(labels.tertiary))
        painter.drawText(r.adjusted(6, 0, -6, 0),
                         Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
                         self._label)
        painter.end()


# ==========================================================================
# 页面
# ==========================================================================


class AiStudioPage(QWidget):
    """AI 扒谱 · 复刻页。"""

    #: 用户改了工程内容（扒谱/复刻把新轨道写进了工程），需要重渲染
    edited = Signal()
    #: 请求把播放头移到某个时间（秒）—— 本页不产生，但契约要求有这个信号
    seekRequested = Signal(float)

    def __init__(self, project: Project, parent: QWidget | None = None):
        super().__init__(parent)
        self.project = project
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._controller = None
        self._position = 0.0
        self._playing = False
        self._audio: np.ndarray | None = None
        self._audio_label = ""
        self._transcription: transcribe.TranscribeResult | None = None
        self._profile: replicate.ReferenceProfile | None = None
        self._log: list[str] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(spacing.md, spacing.sm, spacing.md, spacing.sm)
        root.setSpacing(spacing.sm)
        root.addWidget(self._build_header())

        body = QHBoxLayout()
        body.setSpacing(spacing.sm)
        body.addWidget(self._build_left(), 5)
        body.addWidget(self._build_right(), 4)
        root.addLayout(body, 1)

        self._refresh_buttons()

    # -- 构建 ------------------------------------------------------------

    def _build_header(self) -> QWidget:
        bar = GlassPanel(self, material=theme.black(0.20), radius=radii.control)
        bar.setFixedHeight(44)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(8)

        self.badge = GlassImageButton("robot", bar, size=30, icon_size=18,
                                      tooltip="离线引擎：全部在本机用信号处理算出来")
        lay.addWidget(self.badge)
        title = QLabel("AI 扒谱 · 复刻", bar)
        title.setFont(type_scale.headline.font())
        title.setStyleSheet(f"color: rgba({labels.label.red()},{labels.label.green()},"
                            f"{labels.label.blue()},{labels.label.alphaF():.3f});"
                            "background:transparent;")
        lay.addWidget(title)

        engine = GlassChip("离线引擎 · 无需联网", bar, accent=accents.teal)
        engine.setToolTip("扒谱与复刻全部由 numpy 信号处理完成：起音检测、基频估计、"
                          "指纹统计。没有云端模型，也没有假进度条。")
        lay.addWidget(engine)
        self.engine_badge = engine
        lay.addStretch(1)

        self.status = QLabel("", bar)
        self.status.setFont(type_scale.caption1.font())
        self.status.setStyleSheet(
            f"color: rgba({labels.tertiary.red()},{labels.tertiary.green()},"
            f"{labels.tertiary.blue()},{labels.tertiary.alphaF():.3f});background:transparent;")
        lay.addWidget(self.status)
        return bar

    def _build_left(self) -> QWidget:
        area = QScrollArea(self)
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        area.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        host.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        col = QVBoxLayout(host)
        col.setContentsMargins(0, 0, 6, 0)
        col.setSpacing(spacing.sm)

        # ---- 参考音频 ----
        src = GlassCard(host, radius=radii.card)
        sl = QVBoxLayout(src)
        sl.setContentsMargins(12, 10, 12, 10)
        sl.setSpacing(6)
        cap = QLabel("参考音频", src)
        cap.setFont(type_scale.caption1.font())
        cap.setStyleSheet(f"color: rgba({labels.secondary.red()},{labels.secondary.green()},"
                          f"{labels.secondary.blue()},{labels.secondary.alphaF():.3f});"
                          "background:transparent;")
        sl.addWidget(cap)

        self.wave = WaveformView(src)
        sl.addWidget(self.wave)

        self.source_label = QLabel("还没有参考音频：选一个音频文件（wav / mp3 / m4a / flac），"
                                   "或者直接用当前工程", src)
        self.source_label.setFont(type_scale.caption2.font())
        self.source_label.setWordWrap(True)
        self.source_label.setStyleSheet(
            f"color: rgba({labels.quaternary.red()},{labels.quaternary.green()},"
            f"{labels.quaternary.blue()},{labels.quaternary.alphaF():.3f});"
            "background:transparent;")
        sl.addWidget(self.source_label)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.btn_open = GlassButton("选择音频…", src, accent=accents.blue, style="tinted",
                                    compact=True)
        self.btn_open.clicked.connect(self.choose_file)
        row.addWidget(self.btn_open)
        for text, stem in (("用整曲", "mix"), ("用鼓轨", "drums"), ("用旋律轨", "lead")):
            btn = GlassButton(text, src, accent=accents.teal, compact=True)
            btn.clicked.connect(lambda _=False, s=stem: self.use_project_audio(s))
            row.addWidget(btn)
        row.addStretch(1)
        sl.addLayout(row)

        hint = QLabel("提示：**分轨**比整曲准得多 —— 实测同一首示范曲，"
                      "鼓轨扒速度误差 0.0%，整曲混音 37%（持续声部会盖住拍点）。",
                      src)
        hint.setFont(type_scale.caption2.font())
        hint.setWordWrap(True)
        hint.setStyleSheet(
            f"color: rgba({labels.quaternary.red()},{labels.quaternary.green()},"
            f"{labels.quaternary.blue()},{labels.quaternary.alphaF():.3f});"
            "background:transparent;")
        sl.addWidget(hint)
        col.addWidget(src)

        # ---- 控制台 ----
        con = GlassCard(host, radius=radii.card)
        cl = QVBoxLayout(con)
        cl.setContentsMargins(12, 10, 12, 10)
        cl.setSpacing(6)
        cap2 = QLabel("指令台", con)
        cap2.setFont(type_scale.caption1.font())
        cap2.setStyleSheet(f"color: rgba({labels.secondary.red()},{labels.secondary.green()},"
                           f"{labels.secondary.blue()},{labels.secondary.alphaF():.3f});"
                           "background:transparent;")
        cl.addWidget(cap2)

        self.log_label = QLabel("", con)
        self.log_label.setFont(type_scale.mono.font())
        self.log_label.setWordWrap(True)
        self.log_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.log_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.log_label.setMinimumHeight(130)
        self.log_label.setStyleSheet(
            f"color: rgba({labels.tertiary.red()},{labels.tertiary.green()},"
            f"{labels.tertiary.blue()},{labels.tertiary.alphaF():.3f});"
            "background:transparent;")
        cl.addWidget(self.log_label)

        line = QHBoxLayout()
        line.setSpacing(6)
        self.input = QLineEdit(con)
        self.input.setPlaceholderText("例如：扒谱 96 · 复刻 8 · 音色包 808 · 帮助")
        self.input.setFont(type_scale.callout.font())
        self.input.setStyleSheet(
            "QLineEdit{background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.10);"
            "border-radius:7px;padding:0 8px;color:rgba(255,255,255,0.86);}"
            "QLineEdit:focus{border:1px solid rgba(94,204,196,0.55);}")
        self.input.returnPressed.connect(self.submit_command)
        line.addWidget(self.input, 1)
        self.btn_send = GlassButton("发送", con, accent=accents.teal, style="tinted",
                                    compact=True)
        self.btn_send.clicked.connect(self.submit_command)
        line.addWidget(self.btn_send)
        cl.addLayout(line)

        chips = QHBoxLayout()
        chips.setSpacing(4)
        for text in ("扒谱", "复刻", "音色包 808", "音色包 牛铃", "帮助"):
            chip = GlassChip(text, con, accent=accents.purple)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.mousePressEvent = (lambda _e, t=text: self.run_command(t))
            chips.addWidget(chip)
        chips.addStretch(1)
        cl.addLayout(chips)
        col.addWidget(con)
        col.addStretch(1)
        area.setWidget(host)
        return area

    def _build_right(self) -> QWidget:
        area = QScrollArea(self)
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        area.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        host.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        col = QVBoxLayout(host)
        col.setContentsMargins(6, 0, 0, 0)
        col.setSpacing(spacing.sm)

        # ---- 扒谱结果 ----
        card = GlassCard(host, radius=radii.card)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(12, 10, 12, 10)
        cl.setSpacing(6)
        cl.addWidget(self._section_label("扒谱结果", card))
        self.trans_summary = self._readout("还没有扒过谱", card)
        cl.addWidget(self.trans_summary)
        self.trans_detail = self._readout("", card, dim=True)
        cl.addWidget(self.trans_detail)
        trow = QHBoxLayout()
        trow.setSpacing(6)
        self.btn_transcribe = GlassButton("扒谱", card, accent=accents.blue,
                                          style="tinted", compact=True)
        self.btn_transcribe.clicked.connect(lambda: self.run_transcribe())
        self.btn_apply_sheet = GlassButton("生成轨道", card, accent=accents.green,
                                           compact=True)
        self.btn_apply_sheet.clicked.connect(self.apply_transcription)
        trow.addWidget(self.btn_transcribe)
        trow.addWidget(self.btn_apply_sheet)
        trow.addStretch(1)
        cl.addLayout(trow)
        col.addWidget(card)

        # ---- 复刻指纹 ----
        card2 = GlassCard(host, radius=radii.card)
        c2 = QVBoxLayout(card2)
        c2.setContentsMargins(12, 10, 12, 10)
        c2.setSpacing(6)
        c2.addWidget(self._section_label("复刻指纹", card2))
        self.profile_summary = self._readout("还没有量过指纹", card2)
        c2.addWidget(self.profile_summary)
        self.profile_detail = self._readout("", card2, dim=True)
        c2.addWidget(self.profile_detail)

        prow = QHBoxLayout()
        prow.setSpacing(6)
        self.btn_replicate = GlassButton("复刻", card2, accent=accents.orange,
                                         style="tinted", compact=True)
        self.btn_replicate.clicked.connect(lambda: self.run_replicate())
        self.btn_apply_cover = GlassButton("生成并加入", card2, accent=accents.green,
                                           compact=True)
        self.btn_apply_cover.clicked.connect(self.apply_replication)
        prow.addWidget(self.btn_replicate)
        prow.addWidget(self.btn_apply_cover)
        prow.addStretch(1)
        c2.addLayout(prow)

        c2.addWidget(GlassDivider(card2))
        c2.addWidget(self._section_label("音色包", card2))
        pac = QHBoxLayout()
        pac.setSpacing(4)
        self.pack_chips: list[GlassChip] = []
        for pack in soundpacks.SOUND_PACKS:
            chip = GlassChip(pack.name, card2, accent=getattr(accents, pack.accent,
                                                             accents.orange))
            chip.setToolTip(soundpacks.describe(pack))
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.mousePressEvent = (lambda _e, k=pack.key: self.apply_pack(k))
            pac.addWidget(chip)
            self.pack_chips.append(chip)
        pac.addStretch(1)
        c2.addLayout(pac)
        self.pack_note = self._readout("点一个音色包 = 新建一条带示范鼓型的轨道",
                                       card2, dim=True)
        c2.addWidget(self.pack_note)
        col.addWidget(card2)
        col.addStretch(1)
        area.setWidget(host)
        return area

    def _section_label(self, text: str, parent: QWidget) -> QLabel:
        lab = QLabel(text, parent)
        lab.setFont(type_scale.caption1.font())
        lab.setStyleSheet(f"color: rgba({labels.secondary.red()},{labels.secondary.green()},"
                          f"{labels.secondary.blue()},{labels.secondary.alphaF():.3f});"
                          "background:transparent;")
        return lab

    def _readout(self, text: str, parent: QWidget, *, dim: bool = False) -> QLabel:
        lab = QLabel(text, parent)
        lab.setFont(type_scale.caption1.font())
        lab.setWordWrap(True)
        c = labels.quaternary if dim else labels.label
        lab.setStyleSheet(f"color: rgba({c.red()},{c.green()},{c.blue()},{c.alphaF():.3f});"
                          "background:transparent;")
        return lab

    # -- 契约接口 --------------------------------------------------------

    def bind(self, controller) -> None:
        self._controller = controller
        self.log("已接上控制器。选一段参考音频，然后「扒谱」或「复刻」。")
        self.log("提示：" + "；".join(f"{k} — {v}" for k, v in COMMANDS[:3]))

    def set_project(self, project: Project) -> None:
        self.project = project
        self.rebuild()

    def rebuild(self) -> None:
        self._refresh_buttons()

    def refresh_notes(self) -> None:
        self._refresh_buttons()

    def set_position(self, seconds: float, playing: bool = False) -> None:
        self._position = float(seconds)
        self._playing = bool(playing)

    # -- 参考音频 --------------------------------------------------------

    def set_audio(self, audio: np.ndarray, label: str) -> bool:
        """直接喂一段缓冲当参考（测试与"用当前工程"都走这条路）。"""
        if audio is None or np.asarray(audio).size == 0:
            self._status("参考音频是空的")
            return False
        self._audio = np.asarray(audio, dtype=np.float32)
        self._audio_label = label
        self._transcription = None
        self._profile = None
        seconds = self._audio.shape[-1] / render.SR
        self.wave.set_audio(self._audio, f"{label} · {seconds:.1f}s")
        self.source_label.setText(f"参考音频：{label} · {seconds:.1f} 秒")
        self.trans_summary.setText("还没有扒过谱")
        self.trans_detail.setText("")
        self.profile_summary.setText("还没有量过指纹")
        self.profile_detail.setText("")
        self.log(f"参考音频已就位：{label}（{seconds:.1f}s）")
        self._refresh_buttons()
        return True

    def choose_file(self, path: str | None = None) -> bool:
        """选一段参考音频。``path`` 传了就直接用（测试/指令台都走这条）。

        **不止 WAV**：mp3 / m4a / flac 走 ``audiodecode``（PySide6 自带的
        QtMultimedia，Windows 上是 Media Foundation）。以前的对话框只列 ``*.wav``，
        于是"手上只有 mp3"的用户在这一页根本进不来 —— 而那恰恰是最常见的情况。
        """
        if path is None:
            from PySide6.QtWidgets import QFileDialog

            chosen, _ = QFileDialog.getOpenFileName(
                self, "选择参考音频", "", audiodecode.FILE_FILTER)
            if not chosen:
                return False
            path = chosen
        target = Path(path)
        if not target.exists():
            self._status(f"找不到文件：{target}")
            self.log(f"× 找不到文件：{target}")
            return False

        try:
            audio, sr = audiodecode.load_audio_any(target)
        except Exception as exc:
            self._status(f"读不了这个音频：{exc}")
            self.log(f"× 读不了「{target.name}」：{exc}")
            return False
        if sr != render.SR:
            # 采样率不一致时重采样：扒谱的时间定位全靠采样率，不换会把 BPM 算歪
            ratio = render.SR / float(sr)
            idx = np.arange(int(audio.shape[1] * ratio)) / ratio
            audio = np.stack([np.interp(idx, np.arange(audio.shape[1]), audio[c])
                              for c in range(audio.shape[0])]).astype(np.float32)
        self.log(f"已载入「{target.name}」（{sr} Hz → {render.SR} Hz）")
        return self.set_audio(audio, target.name)

    def use_project_audio(self, stem: str = "mix") -> bool:
        """拿当前工程当参考。``stem`` 取 ``mix`` / ``drums`` / ``bass`` / ``lead``。"""
        if stem == "mix":
            audio, _stats = render.render_project(self.project)
            label = "当前工程 · 整曲"
        else:
            track = self._pick_track(stem)
            if track is None:
                self._status(f"工程里没有可用于「{stem}」的轨道")
                self.log(f"× 工程里找不到 {stem} 轨")
                return False
            audio = render.render_track(track, self.project)
            label = f"当前工程 · {track.name}（分轨）"
        return self.set_audio(audio, label)

    def _pick_track(self, stem: str) -> Track | None:
        if stem == "drums":
            for t in self.project.tracks:
                if t.kind == "drum" and t.notes:
                    return t
            return None
        if stem == "bass":
            # 贝斯没有"类型"，只能按名字与音高猜：名字里带"贝斯/bass"最优先
            for t in self.project.tracks:
                if t.kind == "synth" and ("贝斯" in t.name or "bass" in t.name.lower()):
                    return t
            lows = [t for t in self.project.tracks
                    if t.kind == "synth" and t.notes
                    and sum(n.pitch for n in t.notes) / len(t.notes) < 52]
            return lows[0] if lows else None
        for t in self.project.tracks:
            if t.kind == "synth" and t.notes:
                return t
        return None

    # -- 扒谱 ------------------------------------------------------------

    def run_transcribe(self, bpm: float | None = None) -> transcribe.TranscribeResult | None:
        if self._audio is None:
            self._status("先选一段参考音频")
            self.log("× 还没有参考音频")
            return None
        self._status("正在扒谱…")
        self.log(f"扒谱中…（{self._audio_label}）")
        try:
            res = transcribe.transcribe(self._audio, render.SR, bpm=bpm)
        except Exception as exc:                     # pragma: no cover - 兜底
            self._status(f"扒谱失败：{exc}")
            self.log(f"× 扒谱失败：{type(exc).__name__}: {exc}")
            return None
        self._transcription = res
        self.trans_summary.setText(res.summary())
        detail = [f"有音高 {len(res.pitched)} · 打击乐 {len(res.drums)} · 起音 {res.onsets}"]
        detail += res.log
        self.trans_detail.setText("\n".join(detail))
        for line in res.log:
            self.log("  · " + line)
        self._status(f"扒谱完成：{res.summary()}")
        self._refresh_buttons()
        return res

    def apply_transcription(self) -> list[Track]:
        """把扒谱结果变成工程里的轨道（撤销点压在改动之前）。"""
        res = self._transcription
        if res is None or res.total == 0:
            self._status("还没有可用的扒谱结果")
            self.log("× 先扒谱，且结果里要有事件")
            return []
        tracks = transcribe.make_tracks(res, self.project)
        if not tracks:
            self.log("× 扒谱结果里没有可用的轨道")
            return []
        self._commit("AI 扒谱")
        for t in tracks:
            self.project.add_track(t)
        self.edited.emit()
        self.log("＋ 已加入 " + "、".join(f"「{t.name}」{len(t.notes)} 音符" for t in tracks))
        self._status(f"已加入 {len(tracks)} 条轨道")
        return tracks

    # -- 复刻 ------------------------------------------------------------

    def run_replicate(self, bpm: float | None = None
                      ) -> replicate.ReferenceProfile | None:
        if self._audio is None:
            self._status("先选一段参考音频")
            self.log("× 还没有参考音频")
            return None
        self._status("正在量指纹…")
        self.log(f"复刻分析中…（{self._audio_label}）")
        try:
            profile = replicate.analyze_reference(
                self._audio, render.SR, bpm=bpm, transcription=self._transcription)
        except Exception as exc:                     # pragma: no cover - 兜底
            self._status(f"复刻分析失败：{exc}")
            self.log(f"× 复刻分析失败：{type(exc).__name__}: {exc}")
            return None
        self._profile = profile
        self.profile_summary.setText(profile.summary())
        self.profile_detail.setText(
            "\n".join(f"{k}：{v}" for k, v in profile.metrics()))
        self.log("指纹：" + profile.summary())
        self._status("指纹量好了，可以「生成并加入」")
        self._refresh_buttons()
        return profile

    def apply_replication(self, bars: int | None = None, seed: int | None = None,
                          pack_key: str | None = None) -> list[Track]:
        """按指纹生成四声部并写进工程。

        生成的参数来自指纹（速度/调式/密度/走向），随机性来自种子 ——
        同一个种子必然得到同一首，这一点对"复刻"很重要：不满意就换个种子重掷，
        而不是每次点都得到无从比较的结果。
        """
        profile = self._profile
        if profile is None:
            self._status("先量一次指纹（点「复刻」）")
            self.log("× 还没有指纹")
            return []
        from app.ui.pages.composer import compose, params_from_profile

        if bars is None:
            bars = profile.bars
        params = params_from_profile(profile, bars=bars, seed=seed,
                                     palette_offset=len(self.project.tracks))
        self._commit("AI 复刻")
        # 速度跟着参考走：网格与小节都要对得上，否则生成的音符位置没有意义
        self.project.bpm = float(profile.bpm)
        result = compose(self.project, params)
        for t in result.tracks:
            self.project.add_track(t)
        if pack_key:
            self._apply_pack_to_tracks(result.tracks, pack_key)
        self.edited.emit()
        self.log(f"＋ 复刻生成：{params.key_pc}/{params.mode} · {params.bpm:.0f} BPM · "
                 f"{bars} 小节 · 种子 {result.seed} · 共 {result.total_notes} 音符")
        self._status(f"已生成 {len(result.tracks)} 条轨道（种子 {result.seed}）")
        return list(result.tracks)

    def _apply_pack_to_tracks(self, tracks: list[Track], pack_key: str) -> None:
        pack = soundpacks.pack_by_key(pack_key)
        if pack is None:
            return
        for t in tracks:
            if t.kind == "drum":
                soundpacks.apply_pack(t, pack)
                self.log(f"  · 鼓组套上音色包「{pack.name}」")

    # -- 音色包 ----------------------------------------------------------

    def apply_pack(self, key: str) -> Track | None:
        """把音色包加成一条新轨道（带一小节示范鼓型，立刻能听）。"""
        pack = soundpacks.pack_by_key(key)
        if pack is None:
            self._status(f"没有叫「{key}」的音色包")
            self.log(f"× 不认识的音色包：{key}")
            return None
        self._commit(f"音色包·{pack.name}")
        track = soundpacks.make_track(
            self.project, pack,
            color=accents.track_palette()[len(self.project.tracks)
                                          % len(accents.track_palette())].name())
        self.project.add_track(track)
        self.edited.emit()
        self.pack_note.setText(f"已加入「{track.name}」· {len(track.notes)} 音符示范")
        self.log(f"＋ 音色包「{pack.name}」→ 新轨道「{track.name}」"
                 f"（{len(track.notes)} 音符示范鼓型）")
        self.log("  · " + (pack.tip or pack.summary))
        self._status(f"已加入音色包「{pack.name}」")
        return track

    # -- 指令台 ----------------------------------------------------------

    def submit_command(self) -> str:
        text = self.input.text().strip()
        if not text:
            return ""
        self.input.clear()
        return self.run_command(text)

    def run_command(self, text: str) -> str:
        """解析一条中文指令并执行。返回回显文本（测试直接断言它）。"""
        text = (text or "").strip()
        if not text:
            return ""
        self.log(f"› {text}")
        parts = text.replace("：", " ").replace(",", " ").split()
        verb = parts[0]
        args = parts[1:]

        if verb in ("帮助", "help", "?"):
            for key, desc in COMMANDS:
                self.log(f"  {key} — {desc}")
            self._status("指令台：见日志")
            return "帮助"

        if verb.startswith("扒谱"):
            bpm = self._first_number(args)
            res = self.run_transcribe(bpm)
            if res is None:
                return "扒谱失败"
            if res.total:
                self.apply_transcription()
                return f"扒谱 {len(res.pitched)} 音 + {len(res.drums)} 鼓"
            return "扒谱没找到事件"

        if verb.startswith("复刻"):
            num = self._first_number(args)
            profile = self.run_replicate()
            if profile is None:
                return "复刻失败"
            tracks = self.apply_replication(bars=int(num) if num else None)
            return f"复刻 {len(tracks)} 轨"

        if verb.startswith("音色包"):
            key = args[0] if args else ""
            pack = soundpacks.pack_by_key(key) or self._match_pack(key)
            if pack is None:
                self.log(f"× 不认识的音色包「{key}」，现有："
                         + "、".join(p.name for p in soundpacks.SOUND_PACKS))
                return "音色包未识别"
            track = self.apply_pack(pack.key)
            return f"音色包 {pack.name}" if track else "音色包失败"

        if verb.startswith("参考"):
            stem = args[0] if args else "mix"
            mapping = {"整曲": "mix", "鼓": "drums", "鼓轨": "drums",
                       "贝斯": "bass", "旋律": "lead"}
            stem = mapping.get(stem, stem)
            ok = self.use_project_audio(stem)
            return f"参考 {stem}" if ok else "切换参考失败"

        if verb.startswith("清空"):
            self._log.clear()
            self.log_label.setText("")
            self._status("日志已清空")
            return "清空"

        self.log(f"× 不认识的指令「{verb}」。输入「帮助」看能做什么")
        self._status("不认识的指令")
        return "未知指令"

    @staticmethod
    def _first_number(args: list[str]) -> float | None:
        for a in args:
            try:
                return float(a)
            except ValueError:
                continue
        return None

    @staticmethod
    def _match_pack(text: str):
        """按 key 或名字的一部分找音色包（"牛铃"→ cowbell 那个包）。"""
        if not text:
            return None
        low = text.lower()
        for pack in soundpacks.SOUND_PACKS:
            if low in pack.key.lower() or low in pack.name.lower() \
                    or any(low in piece.lower() for piece in pack.pieces):
                return pack
        return None

    # -- 杂项 ------------------------------------------------------------

    def log(self, text: str) -> None:
        self._log.append(str(text))
        if len(self._log) > LOG_LIMIT:
            del self._log[: len(self._log) - LOG_LIMIT]
        self.log_label.setText("\n".join(self._log))

    def _status(self, text: str) -> None:
        self.status.setText(text)
        ctrl = self._controller
        if ctrl is not None:
            try:
                ctrl.statusMessage.emit(text, 2500)
            except Exception:
                pass

    def _commit(self, label: str) -> None:
        """改动**之前**压撤销点（契约要求：撤得掉 AI 干的事）。"""
        ctrl = self._controller
        if ctrl is not None:
            ctrl.push_undo(label)

    def _refresh_buttons(self) -> None:
        """按钮的可用性：**不禁用**，只改提示。

        为什么不用 ``setEnabled(False)``：``GlassButton`` 没有禁用态的视觉
        （画出来跟能点的一模一样），禁用就成了"点了没反应"的陷阱 ——
        本工程已经在"从不 emit 的 clicked 信号"上栽过一次同类问题。
        所以按钮一律可点，动作自己守门：没参考音频时点「扒谱」会得到
        "先选一段参考音频"这句话，而不是一声不响。
        """
        has_audio = self._audio is not None
        tip = "" if has_audio else "先选一段参考音频（或者「用鼓轨」）"
        for btn in (self.btn_transcribe, self.btn_replicate):
            btn.setToolTip(tip)
        self.btn_apply_sheet.setToolTip(
            "" if self._transcription and self._transcription.total else "先扒一次谱")
        self.btn_apply_cover.setToolTip(
            "" if self._profile is not None else "先点「复刻」量一次指纹")

    def showEvent(self, event):
        super().showEvent(event)
        if self._audio is None and not self._log:
            self.log("欢迎。左边选一段参考音频（或直接「用鼓轨」），"
                     "右边点「扒谱」或「复刻」。")


__all__ = ["AiStudioPage", "WaveformView", "COMMANDS"]
