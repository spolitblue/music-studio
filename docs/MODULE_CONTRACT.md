# 页面模块契约

> 这份文档是**并行开发的唯一约定**。每个 agent 只负责一个页面模块，
> 彼此看不到对方的上下文 —— 所以凡是接口、命名、可用的公共设施，
> 全部以本文档为准，不要自己发明。

---

## 一、项目背景（读这一段就够）

`制乐 Music Studio` —— Python + PySide6 写的音乐制作软件。

* 工程根目录：`C:\Users\LZY20\Desktop\harness\music-studio`
* Python 解释器：`.venv\Scripts\python.exe`（**必须用它**，系统 python 没装依赖）
* 音频引擎**已经全部完成并验证**，你只需调用，不需要改：
  `app/audio/` 下有 `dsp / project / synth / drums / effects / render / wavio / player`
* 界面基础也已完成：玻璃材质、弹簧动效、Twemoji 图标、设计令牌

**你的任务**：实现**一个**尚未做的页面模块，并给它写测试。

---

## 二、硬性规则（违反即返工）

1. **只允许创建/修改这两个文件**：
   - `app/ui/pages/<你的模块名>.py`（新建）
   - `tests/test_<你的模块名>.py`（新建）

2. **绝对不要改任何其它已存在的文件**。特别是：
   - `app/ui/main_window.py` —— 接线由编排者统一做，你改了会和其他 agent 冲突
   - `app/theme.py`、`app/widgets/*` —— 公共设施，改了对别人是破坏性影响
   - 你需要的任何新辅助函数，**放进你自己的模块里**

3. **注释用中文，解释「为什么」而不是「是什么」**。这是本工程的既定风格。例如：
   ```python
   # 好：这里不用 tooltip，因为 tooltip 要悬停半秒才弹，而编曲需要即时反馈
   # 坏：设置工具提示
   ```
   遇到坑、做过取舍、参数为什么取这个值 —— 这些必须写下来。

4. **不留占位与假实现**。不允许 `pass  # TODO`、不允许返回写死的数据、
   不允许「界面画出来了但点了没反应」。功能要么真做，要么不做。

5. **必须真的跑起来验证**。写完必须执行你的测试脚本并**粘贴真实输出**，
   不许凭印象说"应该没问题"。

---

## 三、页面类的统一接口

编排者会按这个契约接线，**签名必须完全一致**：

```python
class XxxPage(QWidget):
    #: 用户改了工程内容，需要重渲染。**改动前必须先 controller.push_undo(标签)**
    edited = Signal()

    #: 请求把播放头移到某个时间（秒）
    seekRequested = Signal(float)

    def __init__(self, project: Project, parent: QWidget | None = None):
        """project 是 app.audio.project.Project。不要复制它，直接引用。"""

    def bind(self, controller) -> None:
        """可选。接上 AppController（题目里如果你需要 push_undo / 播放状态就实现它）。
        AppController 提供：
          controller.push_undo(label: str)   改动之前压撤销点
          controller.undo() / redo()
          controller.statusMessage.emit(text, ms)
          controller.play() / pause() / stop() / seek(seconds)
          controller.project / .player / .audio / .stats
        """

    def set_project(self, project: Project) -> None:
        """整个工程被换掉（打开新工程 / 撤销）时调用。"""

    def rebuild(self) -> None:
        """工程**结构**变了（增删轨道、改轨道类型）时调用，需要重建界面。"""

    def refresh_notes(self) -> None:
        """只是音符/参数变了，重画即可，不必重建控件。"""

    def set_position(self, seconds: float, playing: bool = False) -> None:
        """播放位置更新。playing 为真时通常要做「自动跟随」。"""
```

**实例化时只传 `project`**，编排者随后会调 `bind(controller)`（如果有）。

---

## 四、可以用的公共设施（直接用，别重造）

### 设计令牌 `app.theme`
```python
from app.theme import (
    accents, labels, materials, radii, spacing, blur, motion, elevation, outline,
    type_scale, tw, white, black, rgba, squircle,
)
```
* `type_scale`：`large_title / title1 / title2 / title3 / headline / body / callout /
  subhead / footnote / caption1 / caption2 / mono`，每个都有 `.font()`
* `labels`：`label / secondary / tertiary / quaternary`（玻璃面上的文字颜色）
* `radii`：`chip / control / card / panel / window / pill`
* `spacing`：`xxs / xs / sm / md / lg / xl`
* `accents`：`blue / green / orange / pink / purple / red / teal / yellow / …`，
  另有 `accents.track_palette()`
* `theme.finish` 是当前的玻璃表面档位（光面/磨砂），只读，别改

### 玻璃控件 `app.widgets.glass`
```python
from app.widgets.glass import (
    GlassPanel,      # 结构面板（GlassPanel(parent, material=..., radius=..., liquid=True)）
    GlassCard,       # 内容卡片（GlassCard(parent, radius=..., material=..., liquid=True)）
    GlassButton,     # 文字按钮（GlassButton("文字", parent, style="tinted"))
    GlassImageButton,# 图片图标按钮（GlassImageButton("play", parent, size=30, tooltip="…")）
    GlassOrb,        # 圆形玻璃球按钮
    GlassSegmented,  # 分段控件
    GlassChip,       # 小标签芯片
    GlassDivider,
    GlassStyle, paint_glass, paint_bloom,
)
```
`style` 取值：`"plain" | "tinted" | "filled" | "destructive"`

### 连续量控件 `app.widgets.controls`
```python
from app.widgets.controls import GlassSlider, GlassKnob
GlassSlider(parent, 最小值, 最大值, 初值, accent=颜色)   # .valueChanged 信号
GlassKnob(parent, 最小值, 最大值, 初值, accent=颜色)     # .valueChanged 信号
```

### 图标 `app.icons`
```python
from app import icons
icons.pixmap("play", 28, opacity=0.9, dpr=self.devicePixelRatioF())
icons.label("play")     # 中文名，用于 tooltip
```
**可用图标名**见 `assets/icons/manifest.json`（127 个）。常用：
`play pause stop record loop metronome piano guitar drums trumpet violin saxophone
microphone headphones level_slider control_knobs note notes score speaker_high
speaker_mute bell add remove edit delete save folder clipboard undo redo refresh
settings wrench search zoom_in lock link arrow_up arrow_down arrow_right arrow_back
sparkles robot bulb star fire heart check cross warning brain crystal_ball
magic_wand dice chart_up bar_chart target rocket zap party crown gem trophy medal`

### 动效 `app.motion`
```python
from app.motion import animator, SPRING_SNAPPY, SPRING_DEFAULT, SPRING_GENTLE
animator(widget).to("属性名", 目标值, SPRING_DEFAULT)
```
用途：控件需要**可中断**的弹簧动画时（例如自己实现了带 float 属性的控件）。
**不要**用 `QPropertyAnimation`。

### 音频引擎
```python
from app.audio import dsp, render, effects, drums, synth
from app.audio.project import Project, Track, Note, Effect, ADSR, FilterParams, SynthParams, DrumVoice
from app.audio.player import audition          # 全局试听器（常驻流，低延迟）

audition().play_note(stereo_buffer)            # 试听一个音
buf = synth.render_synth_note(track.synth, pitch, velocity, 0.3)   # 渲染单音
buf = drums.render_drum(piece, voice, velocity)                    # 渲染单个鼓件
audio, stats = render.render_project(project)                      # 整曲渲染
```
`Note(step, pitch, length, velocity)`；`step` 以 1/16 音符为单位。

### 试听（强烈建议做）
```python
from app.audio.player import audition
p = audition()
if p: p.play_note(buf)
```
**边调边听**是本软件的核心体验：改任何参数都该立刻能听到。

---

## 五、测试的要求

文件：`tests/test_<模块名>.py`，风格照抄 `tests/test_sequencer_ux.py`：

```python
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# --- 项目根路径引导（本文件位于子目录）---
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parent.parent
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))
# --- 引导结束 ---

from PySide6.QtWidgets import QApplication
app = QApplication([])

from app.audio import demo
from app.ui.pages.<模块名> import XxxPage

ok = True
def check(label, cond, detail=""):
    global ok
    if not cond: ok = False
    print(f"  [{'OK  ' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")

# …用 demo.demo_project() 拿一个真实工程来测…

print("全部通过" if ok else "存在失败项")
sys.exit(0 if ok else 1)
```

**测试必须覆盖**：
* 页面能构造、能绘制（`page.grab()` 不报错）
* 每个交互动作真的改了 `project` 里的数据（不是只改了界面）
* 边界情况：空轨道、只有一个音符、超出范围的值
* 试听/渲染路径能产出**非静音**的音频缓冲

**跑测试的命令**（在工程根目录）：
```powershell
.venv\Scripts\python.exe -u tests\test_<模块名>.py
```

---

## 六、已知的坑（踩过的，别再踩）

1. **离屏截图里中文会变成豆腐块** —— 这是 offscreen 平台缺字体回退，**不是你的 bug**。
   判断中文是否正常要在真实窗口下测，或者干脆跳过视觉判断。

2. **`QObject.property()` 只接受 `str`，`QPropertyAnimation` 只接受 `bytes`** ——
   两边要求相反，传错不报错而是崩进程。所以动效一律用 `app.motion.animator`。

3. **`.bat` 文件必须 CRLF 换行**，否则 cmd 解析器会把行劈开。
   （你不需要碰 .bat，只是提醒别用脚本写坏它。）

4. **不要用 `QGraphicsDropShadowEffect`**：Qt 每个控件只能挂一个 graphics effect，
   会和别处的透明度效果打架。

5. **`paint_glass` 里的 `bloom` 参数是「淡晕染」**，传全不透明的强调色进去会被
   内部钳制到 0.14；正常用法是直接传 `accents.xxx`。

6. 工程数据模型是纯 dataclass、可 JSON 往返。**改音符要用 `Track.add_note /
   remove_note / note_at / toggle_note`**，不要直接操作 `notes` 列表做复杂逻辑
   （简单过滤可以）。

---

## 七、交付时请报告

用 JSON 报告（编排者会解析），字段：

```json
{
  "module": "模块名",
  "page_file": "app/ui/pages/xxx.py",
  "test_file": "tests/test_xxx.py",
  "test_passed": true,
  "test_summary": "12 项全部通过",
  "lines": 640,
  "features": ["做出来的功能1", "功能2"],
  "audition": true,
  "notes": "有什么需要编排者注意的（接口特殊之处、遗留问题）"
}
```
