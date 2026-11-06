# 交接任务书 · 制乐 Music Studio

> 本文档面向**接手的人或 agent**。你没有上一轮的上下文，读完这一份就应当能够干活。
> 最后更新：混音台栏修复完工、15 个测试套件全绿之后。

---

## 一、这是什么

一个用 **Python + PySide6** 写的音乐制作软件。所有声音由代码实时合成/离线渲染，
不依赖任何音色采样包；界面是**液态玻璃**材质，图标取自 Twemoji。

* 工程根目录：`C:\Users\LZY20\Desktop\harness\music-studio`
* 解释器：`.venv\Scripts\python.exe` —— **必须用它**，系统 python 没装依赖
* 启动：双击 `run.bat`（或 `.venv\Scripts\python -m app.main`）
* 跑测试：双击 `run_tests.bat`（或 `.venv\Scripts\python -u tests\run_all.py`）

**当前状态：功能完整、15 个测试套件全绿、可正常使用。**

| 模块 | 状态 |
|---|---|
| 液态玻璃外壳 · 侧边栏 · 走带条 | ✅ |
| 音频引擎（合成器 / 鼓机 / 效果 / 混音 / 母带） | ✅ |
| WAV / MIDI / 工程 JSON 导出 | ✅ |
| 总览页 · 音序器 · 钢琴卷帘 · 鼓机 · 混音台 · 效果器链 · 编曲助手 | ✅ 7 个页面全部接线 |

---

## 二、接手前必须建立的三个认知

### 1. 音频引擎已经完成，你只需调用

`app/audio/` 全部做完并验证过，**不要改**：

```
dsp.py       带限振荡器 / 包络 / 滤波器 / FFT 卷积（非谐波能量 -101dB）
project.py   工程数据模型（纯 dataclass，可 JSON 往返）
synth.py     减法合成器        drums.py    鼓机（纯合成，无采样）
effects.py   六种效果           render.py   混音管线 + 母带限幅
wavio.py     WAV / MIDI 读写    player.py   播放 + 全局试听器
demo.py      内置示范曲
```

常用入口：
```python
from app.audio import render, drums, synth, effects
audio, stats = render.render_project(project)      # 整曲渲染，几秒
trk = render.render_track(track, project)          # 单轨渲染
buf = drums.render_drum(piece, voice, velocity)    # 单个鼓件
buf = synth.render_synth_note(track.synth, pitch, vel, 0.3)
from app.audio.player import audition
audition().play_note(buf)                          # 试听（常驻流，低延迟）
```

### 2. 界面有既定的设计系统，别自己发明

```python
from app.theme import accents, labels, materials, radii, spacing, type_scale, tw
from app.widgets.glass import GlassPanel, GlassCard, GlassImageButton, GlassOrb
from app.widgets.controls import GlassSlider, GlassKnob
from app import icons                        # icons.pixmap("play", 28)
from app.motion import animator, SPRING_SNAPPY
```

设计令牌是视觉的**唯一真相来源**（`app/theme.py`）。要改颜色/字号/圆角，改那里，
不要在控件里就地写死。

### 3. 有一个模块契约，而且它是并行开发的前提

`docs/MODULE_CONTRACT.md` 规定了页面类的统一接口。7 个页面全部遵守它：

```python
class XxxPage(QWidget):
    edited = Signal()             # 改了工程内容，需要重渲染
    seekRequested = Signal(float) # 请求定位播放头
    def __init__(self, project, parent=None)
    def bind(self, controller)    # 可选
    def set_project(self, project)
    def rebuild(self)             # 结构变了
    def refresh_notes(self)       # 只是音符/参数变了
    def set_position(self, seconds, playing=False)
```

`app/ui/main_window.py` 的 `PAGE_SPECS` 是页面注册表，集成层一视同仁地处理所有页面
（**不要为一个页面开特例** —— 总览页当初就是因为不守契约，导致"打开新工程后还在编辑旧工程"）。

---

## 三、本工程踩过的坑（**别重踩**）

### 崩进程类

1. **`QObject.property()` 只接受 `str`，`QPropertyAnimation` 只接受 `bytes`** ——
   传错在 PySide6 6.9 上不报错，而是**访问违例崩进程**。所以动效一律用 `app.motion.animator`。
2. **`QEasingCurve` 配 `BezierSpline` 析构即崩**，且动画运行期间一切正常，
   只在释放时炸 —— 单看功能测试完全发现不了。已改用物理积分实现弹簧。
3. **Qt fail-fast（`0xC0000409`）Python 拦不住**：半构造的 QWidget 若已把 QThread 带起来，
   进程会整体挂掉。实测连续 7 个页面构造失败后进程死亡。
4. **`QThread: Destroyed while thread is still running`** → 退出时 fail-fast。
   任何开线程的地方都要在 `shutdown()` 里等它收工。**取消必须是粘性的** ——
   关闭期间排队送达的信号会把取消标志复活（已在 `RenderWorker` 修掉）。

### 静默失效类（不报错、不崩，只是"改了没反应"）

5. **`push_undo` 必须压在改动之前**。Qt 的天然写法是"改数据→发信号"，
   于是撤销点会压在改动**之后**，快照里已是新值，撤销等于没撤。
   **三个模块独立地犯过这个错** —— 契约里那句话太弱。正确形态：
   在赋值之前 `editStarted.emit(标签)`。
6. **`check(..., True)` 这类恒真断言**凑数——混音台测试里有两处，
   而静音/独奏的撤销缺陷恰好藏在这个缺口里。
7. **退出码必须单独核对**。曾出现 119 项**全绿**但退出码是 `0xC0000409` 的情况。
   "测试全绿"和"程序正常退出"是两件事。

### 环境类

8. **`.bat` 必须 CRLF 换行**，否则 `cmd` 会把行从中间劈开，双击一闪就没。
   `tools/fix_bat_eol.py --check` 可检查，`tests/test_design.py` 有常驻断言。
9. **离屏（offscreen）截图里中文是豆腐块** —— 平台缺字体回退，**不是 bug**。
   判断中文是否正常要在真实窗口下测。
10. **别用 shell 读 UTF-8 文件**（PowerShell 会显示乱码）。用 read/grep 工具。
11. **命令行里传含三引号的 Python 代码会被 Windows 命令行改坏** ——
    补丁脚本请写成文件再执行，不要 `python -c` 内联长代码。

---

## 四、验证方式

```powershell
.venv\Scripts\python.exe -u tests\run_all.py            # 全部 15 套
.venv\Scripts\python.exe -u tests\run_all.py mixer      # 只跑指定套
```

各套件职责：

| 套件 | 覆盖 |
|---|---|
| `design` | 设计令牌 / 图标 / 玻璃几何 / 平台能力 / 批处理换行符 |
| `motion` | 弹簧过冲与理论值比对 / **中断继承速度** / 生命周期 |
| `liquid` | **边缘折射确实吸取轮廓外画面** / SDF 几何 / 凝霜 / 性能 |
| `dsp` | 带限振荡器无混叠 / 包络 / 滤波 / 卷积 / 性能 |
| `render` | 端到端渲染 / 音频健康度 / WAV 往返 / 工程往返 |
| `seq` `sequx` | 音序器：坐标换算往返 / 增删改 / 10 项易用性 |
| `piano` `drums` `mixer` `fx` `comp` | 五个编辑器页面 |
| `glass` | 玻璃无色度 / 拖动时高频刷新且不重新截图 |
| `integ` | **跨页面集成**：7 页共存 / 契约齐备 / 换工程换引用 / 端到端渲染 |
| `ui` | 全部控件构造 + 逐页绘制 |

**测试默认静音**（`MUSIC_STUDIO_SILENT_AUDIO=1`，`run_all.py` 自动设）。
它只影响"是否真开音频流"，缓冲渲染与断言强度不变。想听试听效果就设成 `0`。

---

## 五、已知问题（未修，按优先级）

### 【本次任务】① 混音台首次进入页面时冻结界面约 5.3 秒

**现象**：首次切到 `混音台` 页面，界面卡住约 5 秒（分 5 段，最长一段 2.61 秒）。

**根因**：`app/ui/pages/mixer.py` 的参考电平分析在 **GUI 线程**上逐轨调
`render.render_track()`。实测单轨耗时：

```
鼓组 0.24s | 贝斯 1.46s | 铺底 0.79s | 主旋律 2.61s | 分解音 0.19s   → 合计 ~5.3s
```

贵在**轨道效果链**（`render_track` 会把 `track.effects` 整条跑一遍）。
`render.meter`（4~7ms）与 `_audition_excerpt`（9~13ms）可忽略。

现在的缓解只有"两条轨道之间让事件循环喘 16ms"，界面能出来但表是逐条亮起来的，
每亮一条就冻一次。

**为什么上一轮没修**：`render_track` 不可中断；页面私自开 QThread 需要有人负责
join / 取消 / 工程快照，而页面生命周期由 `main_window` 编排。
正确位置是 **controller 的线程模型**里统一安排。

### ② 渲染在途时关窗要等 5.7 秒

取消是协作式的，只在轨道/音符边界生效；遇到带效果链的长轨道就得等它跑完一段。
实测逼近 `shutdown()` 的 6 秒上限 —— 一旦超时线程收不了工，进程退出会被 Qt fail-fast 带走。

### ③ 钢琴卷帘：长音符拖动的占位判定只看起始步

把一个长音符拖到与另一个长音符**中段**重叠的位置不会被挡住。属既有行为，已注明。

### ④ 其它

* `app/ui/pages/mixer.py` 约 633 行 `VerticalMeter.DECAY_DB` 上方的 `#:` 注释
  多缩进一档（8 空格 vs 属性 4 空格），纯 cosmetic。
* `GlassSurface` 带一个**从不 emit** 的 `clicked` 信号 —— 子类忘接就是
  "点了没反应"的陷阱。编曲助手页的标题徽标曾因此变成假按钮（已改成纯装饰控件）。

---

## 六、本次任务（①）

### 目标

把混音台的**每轨参考电平分析**从 GUI 线程挪走，使首次进入页面时界面**不冻结**，
同时保住现有的全部 119 项断言（含"失败必须可见""表读数口径一致"等）。

### 建议方案（可另行论证，但要说明理由）

在 `AppController` 里加一个**通用的后台作业设施**，而不是让页面自己开线程：

```python
# app/ui/controller.py —— 新增，不改动现有渲染工作线程的行为
def submit_background(self, fn, *, label: str = "后台任务",
                      on_done=None, on_error=None) -> object:
    """在后台线程跑 fn()，结果通过队列信号回到 UI 线程。

    与现有的 RenderWorker 分开：那个专职整曲渲染、独占一条线程；
    这个是通用的短任务设施（单轨渲染、波形分析等），可以有多个并发。
    必须在 shutdown() 里一并收工 —— 否则就是 fail-fast。
    """
```

理由：
* 页面只管"要算什么"和"拿到结果怎么画"，不碰线程生命周期；
* controller 已有 `shutdown()` 与线程管理，多收一条线风险最小；
* 别的页面将来也需要（例如钢琴卷帘算波形、效果器页算试听片段）。

### 硬性要求

1. **`shutdown()` 必须等所有后台作业收工**。参考 `RenderWorker` 的教训：
   取消要**粘性**（关闭期间排队送达的信号不许把取消标志复活）。
2. **工程快照**：后台线程读的必须是**快照**，不能让 GUI 线程边改边被读。
   `Project.clone()` 可用（`demo_project()` 的 JSON 约 45KB，clone 很便宜）。
3. **结果回 UI 线程**：用信号，**绝不**在后台线程里碰任何 QWidget。
4. **失败必须可见**：混音台已有 `_note_failure()` / `_failures` 机制，
   失败时电平退回表底并给出可见提示 —— 这套语义要保住。
5. **过期结果丢弃**：用户在分析过程中改了工程，回来的旧结果不能覆盖新的。
   建议用一个递增的 generation 计数。
6. **不许降低验证强度**：现有 119 项断言全绿是底线。**先补能复现"界面冻结"的断言再改**
   （例如：分析期间主线程能及时处理一次定时器事件 / 分析不阻塞 `processEvents`）。
7. 触摸范围：`app/ui/controller.py`、`app/ui/pages/mixer.py`、`tests/test_mixer.py`，
   以及你新建的测试文件。**其他文件一律不动**（尤其 `main_window.py`、`app/theme.py`、
   `app/widgets/*`、`app/audio/*`）。

### 验收

```powershell
.venv\Scripts\python.exe -u tests\run_all.py          # 15 套全绿，EXIT=0
```

外加你自己写的量化证据，至少要能回答：

* 首次进入混音台页面时，**单次 GUI 阻塞最长是多少毫秒**？（改前 vs 改后）
* 分析进行中，主线程的事件循环有没有被卡住？用数字说明（例如定时器实际间隔 vs 设定间隔）。
* 关窗时后台线程是否都能收工？给出 `shutdown()` 耗时与线程状态。
* 现有 119 项断言是否仍然全绿？

### 交付报告请包含

* 每个新增断言的「改前 FAIL 输出 → 改后 PASS 输出」
* 上面四个量化问题的实测数字
* **没能修好的部分要如实说**，不要谎报成完成
* 过程中发现的新问题（哪怕不属于你的模块 —— 报告即可，别擅自扩大改动范围）
