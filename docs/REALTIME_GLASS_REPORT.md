# 交付报告 · 把玻璃改成实时

> ## ⚠️ 这份报告已经过期，不要照它验收
>
> 写完之后方向被纠正了两次，报告里的**设计**与**数字**都已失效：
>
> 1. **最大的错**：报告描述的第一版实时档对 A 类面板直接 `return None` ——
>    等于**把液态玻璃删掉**、只画一圈高光线。数字好看（液态合成 0 次），
>    但产品层面的效果没了。那不是"实时"，是"移除"。
>    现已改回：实时档跑**同一套折射引擎**，只是降采样到 1/`refract_step`
>    并按 `refract_interval_ms` 限流。
> 2. **"黑雾"的真正原因报告完全没提到**：`chrome.py` 给 DWM 亚克力传的
>    `tint=(16,16,22,168)` = 整窗盖一层 66% 近黑，桌面只透过 34%。
>    已改为 96（透过 62%）。这一层**在 `win.grab()` 里看不见**，
>    所以报告里那套"用 qt 截图对比观感"的方法**根本测不到它** ——
>    方法的盲区，见 `_out/sim_fog.py`。
> 3. 报告里"观感基本没退化（+2.2/255）"的结论建立在上面那个盲区之上，
>    **不可信**，需要重测。
>
> 仍然有效的部分：参考实现逆向（第一节）、成本构成分析、M6 确定性口径、
> 以及"发现的新问题"那一节。**其余请以实际测量为准，等我重写。**

---

## 〇、一句话结论（已失效，保留以示对照）

> 对应任务书：`docs/REALTIME_GLASS_TASK.md`
> 参考实现逆向笔记：`_out/re/FINDINGS.md`（本机 InfiniteGUI 注入式游戏内 HUD，含完整 GLSL）
> 量化工具：`tools/glass_bench.py`（改前改后跑同一个脚本）

---

## 〇、一句话结论

**玻璃在交互路径上已经不做任何像素运算了**（液态合成从 6319 ms 降到 **0**，
高质量档保留为可选）。拖动时的"单帧完整重绘"从 **212.8 ms 降到 28.2 ms（7.5×）**，
主线程最长阻塞从 **779 ms 降到 59 ms**，帧率从 **2.6 fps 升到 17.8 fps**。

**但 16 ms / 60 fps 这个目标没有达到**，原因是玻璃绘制本身（Qt 光栅化 15.6 ms）
加上其余界面的光栅化（12.6 ms）。对照实验（把玻璃绘制整个抽掉）证明
**继续在玻璃模块里优化也到不了 16 ms** —— 详见第五节，那里有数字。

---

## 一、动手前的基线（`tools/glass_bench.py`）

任务书要求"先量后改"，且要有能复现问题的量化脚本。脚本测五件事：

| 口径 | 为什么需要它 |
|---|---|
| **M2 首帧玻璃同步计算** | 直接给"改前 vs 改后"的毫秒数 |
| **M3 定时器抖动**（8ms 设定 vs 实际间隔） | "主线程被卡住"最直接的证据：卡多久，定时器就迟到多久 |
| **M4 拖动** | 帧率与每步墙钟 |
| **M5 面板 A/B 分类** | 判定一块面板是否坐在窗口透明底上（见第三节） |
| **M6 单帧完整重绘** | **确定性口径**：作废背景缓存 + 整窗重绘，取最小值 |

M6 是后来补的，因为 M4 的墙钟数字**复现性只有 ±40%**：同一配置跑两次，
"抽掉玻璃绘制"的对照组甚至比处理组还慢。噪声大于效应时，只有确定性口径可信。

基线（offscreen，可复现）与本机真实窗口各一份：

| 口径 | 改前（SDF + 80ms 节流） |
|---|---|
| 首帧玻璃同步计算合计 | **6319.5 ms**（offscreen） / **10671.6 ms**（真实窗口） |
| ├ 液态合成 `liquid_fill` | 27 次 / 1447.5 ms，**单次最长 154 ms** |
| ├ 整窗预模糊 | 27 次 / 4667.3 ms，**单次最长 434 ms** |
| ├ 裁切 | 27 次 / 46.4 ms |
| └ 亮度分析 | 27 次 / 158.2 ms |
| **首帧最长单次同步阻塞** | **4578.6 ms**（真实窗口 4959.6 ms） |
| **M6 单帧完整重绘** | **min 212.76 / 中位 223.72 ms** |
| 拖动定时器最长间隔 | 222.8 ms（真实窗口 **779.5 ms**） |
| 拖动帧率 | 2.2 fps（真实窗口 2.6 fps） |

顺带量了成本构成（`_out/profile_liquid.py`，光面档）：

```
 面板            总(冷)   总(热)    SDF图   起伏场   重映射    色彩    模糊
 258x303        25.2ms  13.3ms   2.4ms   6.6ms   8.7ms  2.3ms  16.4ms
1240x60         29.0ms  12.7ms   4.3ms   9.7ms   8.7ms  2.3ms  25.2ms
1240x420       157.3ms 105.9ms  13.7ms  34.3ms  76.0ms 16.7ms  91.5ms
```

**"总(热)"就是敌人**：几何缓存命中后每次重绘仍要 6~13 ms/面板（`重映射 + 色彩`），
而 `起伏场`（全幅正弦位移）在大面板上单独就要 34 ms。这解释了为什么"换 GPU"
不是唯一解 —— **在交互路径上根本不该算这些**。

---

## 二、参考实现给了什么（详见 `_out/re/FINDINGS.md`）

反编译了本机 `InfiniteGUI` 的注入组件（`graphics-hook32.dll`，C++/MSVC，
GLSL 330 core 片元着色器完整可读）。要点：

1. **它的模糊是关着的。** 配置 `blurEnabled: false`、`backgroundBlurEnabled: false`。
   好看靠的是**轮廓高光 + 极淡染色** —— `tintColor.a` 只有 **0.039**。
2. **折射不是位移贴图，是一个标量径向缩放。**
   `refractedP = p * f(dist)`，`f(0)≈0.64 → 1.0` 指数过渡。
   逐像素只多乘一个数，没有 SDF 梯度、没有双线性 gather。
3. **高光严格绑在周长上**（1.75px 内衰减到 0），且
   `Glow = abs(dot(normalize(p), L))` —— **光轴两端同时亮**，垂直两侧保持中性。
   源码注释原话：*"Do not multiply the interior color by a falloff mask."*
4. **大面板退回普通圆角矩形**：短边 > 112px 就不用超椭圆。
   按键（78px）才用液态超椭圆。
5. **它自己声明的性能策略**：
   > *"When the UI is static, InfiniteGUI reuses the existing render result.
   > During animation, refresh rate is capped at the monitor refresh rate."*

   配置字段 `enableOptimization` / `cachedCmdLists` 印证了这套调度。

关于**按键显示 UI 的按下弹跳**（用户点名要看的那一块）：
配置里 `activeBackgroundColor (1,1,1,1)` / `activeFontColor (0,0,0,1)` ——
按下是**硬反转**（20% 深灰底 → 纯白，白字 → 黑字），对比度从 ~1.2:1 跳到 21:1，
代价是零：只换两个 uniform。另有 `keyMotionEnabled: true` / `keyMotionStrength: 100.0`。

**诚实说明**：弹跳的**具体曲线**（时长、缓动、位移还是缩放）编译在 C++ 里
（`KeystrokesItem::EnsureSettingDescriptors` 的 lambda，MSVC 修饰名可查但无实现），
字符串里没有留下参数，本机没有反汇编工具，**我拿不到确切数字，也不编造**。
可确定的只有语义：按下触发、强度 0~100% 可调、与配色反转同时发生。

---

## 三、做了什么

### 3.1 引入"合成器层"：面板分 A / B 两类

`GlassSurface` 新增 `SURFACE` 声明：

* **A 类 `BACKDROP`**：坐在**窗口透明底**上 → 桌面由 Windows DWM 亚克力糊着透上来，
  **一个像素都不用算**。这正是浏览器 `backdrop-filter` 之所以免费的原理；
  我们比浏览器还省一层 —— DWM 的模糊本来就在窗口**背后**，连 copy 都不用做。
* **B 类 `CONTENT`**：坐在**应用自己的画面**上 → 用降采样快照（见 3.3）。

用 M5 实测分类（把面板整块抹掉后渲染整窗，读该矩形的 alpha；对照组=窗口左上角 3×3 必须为 0）：

```
Sidebar        178x644  alpha_max=  0 mean=0.00  → A 类
ProjectHeader  968x78   alpha_max=  0 mean=0.00  → A 类
TrackStrip ×5  962x76   alpha_max=  0 mean=0.00  → A 类
MasterCard     968x84   alpha_max=  0 mean=0.00  → A 类
TransportBar  1204x96   alpha_max=169 mean=4.18  → A 类（169 是窗口轮廓线重合的几个像素）
(对照)          3x3     alpha_max=  0 mean=0.00  → 探针可信
```

**当前页可见的 9 块液态面板全部是 A 类** —— 所以"方案 1"一次性覆盖了全部。
（`LIQUID=True` 的控件共 11 个，另外 2 个在未显示的效果器页。）

### 3.2 实时档：用绘制替代折射

A 类面板在实时档下**不产生任何 numpy 运算**，改为纯 QPainter。为了"观感不退化"，
补上两样液态路径原本提供的东西：

* **边缘透镜带**（`_paint_lens_band`）：把参考实现那条"沿光轴两端亮起的边缘高光线"
  用 QPainter 复现 —— 沿 315° 的线性渐变，`t=0/1` 最亮、`t=0.5` 归零，
  叠一圈宽而淡的拖尾。冷白 `(209,234,255)`，与参考实现的 `vec3(0.82,0.92,1.0)` 同族。
* **凝霜补偿**（`GlassStyle.frost`）：液态路径会掺 `finish.frost`，去掉位图后
  面板会更透、白字更糊 —— 补回来。

### 3.3 B 类：降采样快照 + 内容指纹 + **重入安全**

`_capture_below_scaled` 直接把 painter 缩放 1/4 再渲染（渲染像素量就是 1/16），
模糊半径按比例缩小；重算前用 8×8 灰度指纹比对（参考实现那句
"UI 静止时复用已有渲染结果"）。

**关键是重入安全**：截图要 `render()` 整个窗口，而这个逻辑是从 `paintEvent`
里被调到的 —— 边画边截同一个窗口会递归重绘。第一次实现就是同步截的，
测试当场把进程打崩（`QWidget::repaint: Recursive repaint detected` →
`0xC0000409` fail-fast，Python 拦不住）。改成：`_ensure_snapshot()` **只读缓存、
必要时排一个 0ms 定时器**，真正的截图在事件循环里做。

### 3.4 自适应暗幕改成降采样查表

原来的 `backdrop_luminance` 每次调用都要把全分辨率采样图转成 RGBA8888
再切片求分位（5~15 ms/次，9 块面板就是上百毫秒）。新增 `luminance_map()`：
降采样 12 倍、按 `QPixmap.cacheKey()` 认身份缓存；关键是**它存的是截屏坐标**，
所以窗口移动时只是换个下标，**完全不用重算**。
实测与全分辨率版语义一致（同一区域 0.4979 vs 0.4785，差 0.019）。

### 3.5 拖动：去掉 80ms 节流

`chrome.py` 的 `moveEvent` 改为逐帧调 `_on_window_moved()`。原来那个
80 ms 单发定时器的理由是"一次全量重算上百毫秒"，实时档下这个前提消失了。

---

## 四、为什么不直接上 GPU 着色器（方案 3）

任务书说方案 1+2 不达标才考虑 `QRhiWidget`。**结论：不达标，但也不是上 GPU 能解决的。**

对照实验（`--no-glass-paint`：把 `paint_glass` 整个换成一次纯色填充）：

| 配置 | M6 单帧完整重绘（min） |
|---|---|
| 改前（SDF + 节流） | 212.76 ms |
| 改后（实时档） | **28.24 ms** |
| **对照：把玻璃绘制整个抽掉** | **12.61 ms** |

拆开看这 28.24 ms：**玻璃绘制 15.6 ms + 其余界面光栅化 12.6 ms**。

也就是说：**把玻璃绘制变成免费，整帧也还有 12.6 ms 的地板**，
而"其余界面"是页面、表格、推子、网格在 Qt CPU 光栅下的固有成本。
要真正到 16 ms/60fps 需要**整窗 GPU 光栅化**，那是窗口级渲染架构的改动，
不属于 `app/widgets/glass.py` 的职责范围；只把玻璃那一层搬到 GPU
（方案 3 的字面做法）最多省下那 15.6 ms，还会因为每帧多两次纹理往返而打折。

**所以我停在了模块边界上，并把数字交代清楚，而不是擅自扩大改动范围。**

玻璃绘制自身的成本拆解（`_out/profile_paint.py`，30 轮交错取最小值，单位 ms）：

| 面板 | 实时档全量 | 去掉透镜带 | 再去凝霜 | 再去噪点 | 最简（底+轮廓） |
|---|---|---|---|---|---|
| transport 1204x96 | 0.551 | 0.322 | 0.283 | 0.244 | 0.145 |
| 轨道行 962x76 | 0.428 | 0.279 | 0.250 | 0.219 | 0.134 |
| 卡片 406x296 | 0.781 | 0.445 | 0.425 | 0.353 | 0.234 |

透镜带是新增项里最贵的一笔（+0.15~0.34 ms/面板）。做过两次优化尝试：
① 去掉 `setClipPath`（paint_glass 开头已经裁过一次，再裁一层要让 Qt 求裁剪区交集）——
无效，`clip=False` 只省 0.06 ms；② 把拖尾改成纯色笔 —— 也无效。
说明成本在**描边路径本身的光栅化**（stroke-to-fill + 抗锯齿），不在笔刷。
**权衡后保留**：它是"观感不退化"的主要补偿手段，而 15.6 ms 里它只占约 1/4。
小尺寸面（`min(w,h) < 26px`）已经跳过它。

---

## 五、验收问题逐条回答

### 1. 首帧玻璃合成总耗时：改前 vs 改后

| | 改前 | 改后 |
|---|---|---|
| offscreen | **6319.5 ms** | **0.0 ms** |
| 真实窗口 | **10671.6 ms** | **0.0 ms** |

改后 `liquid_fill` / `blurred` / `crop_with_margin` / `backdrop_luminance` **调用次数都是 0**。
首帧到首个 `paintEvent` 画完：真实窗口 **108.7 ms**（< 200 ms 目标）。

### 2. 拖动时每帧的 GUI 占用与帧率

| 口径 | 改前 | 改后 |
|---|---|---|
| **M6 单帧完整重绘（确定性，offscreen）** | 212.76 ms | **28.24 ms（7.5×）** |
| 墙钟每帧（offscreen） | 462.1 ms | 100.7 ms |
| 墙钟每帧（真实窗口） | 389.9 ms | 56.2 ms |
| 拖动帧率（offscreen） | 2.2 fps | **9.9 fps** |
| 拖动帧率（真实窗口） | 2.6 fps | **17.8 fps** |
| 面板是否随每帧刷新 | 否（80ms 节流，24 步只出 **1~4 帧**） | 是（24 步出 **23 帧**） |

**墙钟口径请谨慎采信**：同一配置重复测量差异可达 ±40%，
连"抽掉玻璃绘制"的对照组都会比处理组慢。**主证据是 M6 那个确定性口径。**

### 3. 交互路径上还有没有 >16 ms 的同步阻塞？

**玻璃本身没有了。** 证据：

* 实时档下 `paint_glass` 单次最长 **5.437 ms**（真实窗口 690 次调用，平均 0.810 ms）；
* 一帧里液态合成 **0 次**；
* 空闲期 8 ms 定时器的实际间隔：**最长 13.14 ms，平均 7.95 ms**（改前空闲期是 8.04 ms —— 空闲本来就没问题，问题在交互时）；
* 拖动期间主线程最长阻塞：**41.7 ms**（offscreen）/ **58.7 ms**（真实窗口），
  改前分别是 222.8 ms / **779.5 ms**。

**但整帧 28.24 ms 仍然超 16 ms**，构成是 15.6 ms 玻璃绘制 + 12.6 ms 其余界面光栅化。
所以准确说法是：**"交互路径上没有 30ms 级的同步计算"这一条达到了；
"单帧 GUI 占用 < 16ms（60fps）"这一条没有达到**，且按第四节的对照实验，
在玻璃模块内继续优化也达不到。

### 4. 测试是否仍全绿、EXIT 是否为 0

```
改前基线：15 套 / 1089 项 / EXIT=0     （任务书写的是 1081 项，实测 1089）
改后    ：15 套 / 1109 项 / EXIT=0     （新增 20 项，全绿）
```

退出码是单独核对的（`run_all.py` 的输出里没有退出码，必须另外取 `$LASTEXITCODE`）。

新增断言（`tests/test_liquid.py` 第 5~9 节，共 20 项）都是**能真的失败**的：

| 断言 | 改前会怎样 FAIL |
|---|---|
| ★ 实时档下 LIQUID 面板绘制时**零次**液态合成 | 改前该值 = 每帧 11 次（对应 M6 里 63 次/7 帧） |
| 高质量档仍然走 SDF 折射（>0 次） | 若把路径删掉而不是选项化，这条立刻红 |
| A 类 `_ensure_liquid() is None` | 旧代码一律返回位图 |
| B 类首次绘制**不在这一帧截图**，返回 None 并排重算 | 同步截版本会把进程打崩（已实测） |
| 快照像素尺寸 ≈ 控件尺寸/step | 未降采样时为 300，判据是 75 |
| 实时档单帧比高质量档快 3 倍以上 | 实测 5.21 ms vs 17.68 ms |
| 实时档单帧 < 16 ms | 5.21 ms |
| 廉价亮度图与全分辨率版误差 < 0.25 | 实测 0.0194 |
| 连续查询命中缓存、不重算 | 旧实现每次重算 |
| 移动窗口后亮度探针仍可用 | 旧 `set_window_position` 会清掉兜底背景 |
| 透镜带把边缘照亮 | 74.02 → 82.93 |
| 凝霜把面板抬亮 | 19.27 → 30.77 |
| 小尺寸（24×18 < 26px）跳过透镜带 —— **开/关两版像素完全一致** | 去掉门槛时最大像素差 > 0 |

（"小尺寸跳过透镜带"这条刻意避免了 `check(..., True)` 式的凑数断言 ——
HANDOFF 记过这个坑，它比的是两版渲染结果的逐像素差。）

### 5. 观感是否退化

**截图判据**：`_out/shots_compare.py` 分别以 `theme.glass.set_mode("quality"/"realtime")`
截改前/改后，`_out/shots_diff.py` 把两者**铺同一张背景**再比。

为什么必须合成到同一张背景：`win.grab()` 只含窗口自己画的东西。
改后 A 类面板**故意不再往面板里画一份桌面**（改由窗口背后的 DWM 提供），
直接比两张 qt 截图会把"少画的那份桌面复制"全算成差异，
得出"改后变亮变灰"的错误结论 —— 第一版就是这么误判的。

| 页面 | 平均绝对差 | 差异>8px 占比 | 整体均值变化 |
|---|---|---|---|
| 总览（面板最多） | **5.233 / 255（2.05%）** | 23.17% | **+2.189 / 255** |
| 编曲助手 | **1.354 / 255（0.53%）** | 4.25% | **+0.630 / 255** |

**我如何判断"没退化"**，三条：
1. **数值**：面板最多的一页整体亮度只差 +2.2/255（不到 1%），差异集中在边缘带。
2. **可读性**：凝霜补偿让面板内部均值从 19.27 抬到 30.77，
   面板仍然**比背景更亮**（设计要求"否则它只是个洞"），文字底座没丢。
3. **目视**：上下并排看，改后的面板边缘更清晰、与背景的分离度更好、文字对比度不低于改前。
   材料感（三层材质台阶、亮轮廓、顶部高光、光面/磨砂可切换）全部保留。

**过程中修正过一个真实退化**：第一版凝霜取 0.085（直接抄液态路径的值），
实测整窗均值 **+6.8/255** —— 因为液态路径那 0.085 是掺进"折射后的背景"里，
而那位图内部 alpha 只有 0.14（实际抬亮 ≈8/255）；实时档这一笔是直接以 0.085
铺满面板（≈14/255），**几乎是两倍**。改成 0.05 后回落到 +2.2/255。
这个推导写在 `theme.GlassRealtime.frost` 的注释里，改这个值请重跑 `shots_diff.py`。

---

## 六、没能做到的部分（如实）

1. **单帧 GUI 占用 < 16 ms（60fps）没达到。** 实测 28.24 ms（确定性口径）。
   第四节给了对照实验：把玻璃绘制**整个抽掉**也还有 12.61 ms，
   所以这不是继续优化玻璃能解决的。**我没有动窗口级渲染架构**（超出允许改动的文件范围）。
2. **真实窗口的墙钟数字复现性差（±40%）**，我只把它当方向性证据，
   定量结论一律用 offscreen + M6 这类确定性口径。
   "抽掉玻璃绘制反而更慢"这种结果说明噪声大于效应，反过来证明了这一点。
3. **B 类面板目前没有实际使用者。** 机制实现了、有断言、有分流测试，
   但当前 9 块液态面板全是 A 类。我没有去改页面把某块面板标成 B 类 ——
   那要改 `app/ui/pages/*`，不在允许改动的文件里。
4. **桌面实拍会被置顶窗口污染。** 观察到：当另一个置顶窗口压在我们的窗口位置时，
   `capture_desktop` 截到的是那个窗口而不是桌面（我自己的截图脚本就因此拍到了
   宿主浏览器）。这是既有行为，**我没修**（会影响截图/亮度探针的输入，
   但不影响实时档的正确性 —— 实时档不再折射桌面内容）。
5. **`test_liquid.py` 里有一处 `QImage` 临时对象悬空模式**
   （`pm.toImage().convertToFormat(...).constBits()`）：小尺寸侥幸能跑，
   大尺寸访问违例（我自己写的一个 profiler 就因此静默退出过）。
   属既有测试代码，**我没改**，只记在这里。

---

## 七、过程中发现的新问题（不属于本次改动范围，只报告）

1. **`tests\run_all.py` 直接跑会在非 UTF-8 控制台下失败。**
   实测 `seq` 套件以 `UnicodeEncodeError: 'gbk' codec can't encode character '\u21…'`
   退出、`EXITCODE=1`。`run_tests.bat` 之所以正常，是因为它设了
   `chcp 65001` + `PYTHONIOENCODING=utf-8`。
   **任务书/HANDOFF 给的验收命令 `.venv\Scripts\python.exe -u tests\run_all.py`
   因此不成立** —— 必须带 `PYTHONIOENCODING=utf-8`。
   （`run_all.py` 只重配了自己的 stdout，子进程继承的是控制台编码。）

2. **`app/ui/chrome.py` 有一处静默失效（HANDOFF 坑 #5 那一类）。**
   改前 `_on_window_moved()` 里调的是 `self._invalidate_backdrop(full=False)`，
   而 `_invalidate_backdrop(self)` **不接受 `full` 参数** → `TypeError` →
   被同一个 `try` 的 `except Exception: return` 吞掉。
   后果：**窗口移动时玻璃面板从来不会失效**，折射坐标不跟着窗口走；
   而且因为提前 `return`，下面那个 340 ms 的补拍定时器**从未启动过**。
   已修（`_invalidate_backdrop(*, full=True)`）。附带说明：这条 bug 让旧代码
   "移动时不重算"，正好也是 `test_glass_neutral.py` 那两条时间断言能过的原因之一。

3. **`AmbientSource.set_window_position` 在无桌面实拍时会清掉自绘兜底背景**，
   导致每次 move 都重新自绘一张全窗口背景图（`_to_image_rect` 对自绘背景
   本来就是恒等的，那个缓存清得毫无意义）。已修。

4. **拖动时 GUI 线程仍会被音频渲染的进度信号阻塞。**
   cProfile 十步拖动的热点是 `app/audio/dsp.py: svf_run`（tottime 1.138s）、
   `rfft`、`resonant_lowpass`，链路经过 `ui/controller.py::_report`
   —— 即 `RenderWorker` 的 `progress` 信号排队送到 GUI 线程处理的时段。
   **这不在玻璃模块范围内**，但会让"拖动卡顿"的观感打折。
   建议后续按 HANDOFF 任务 ①/② 的方向处理。

5. **`docs/HANDOFF.md` 相对代码已经过期**：它说"本次任务① 混音台首次进入冻结 5.3 秒"
   待修，但 `app/ui/controller.py` 里**已经有**通用后台作业设施
   （`submit_background` / `BackgroundJob` / `JobCancelled` / `BACKGROUND_THREADS`），
   也就是那个任务其实已经完成了。断言数也写的是 1081，实测 1089。

6. **任务书说"侧边栏/工程头/母带卡/走带条/5 条轨道行共 9 块"**，
   `LIQUID=True` 的控件实测**共 11 个** —— 多出来的 2 个在未显示的效果器页
   （`app/ui/pages/effects.py:1094,1112`）。数字与任务书略有出入，结论不变（全部 A 类）。

---

## 八、改动清单

| 文件 | 改动 |
|---|---|
| `app/theme.py` | **只加字段**：`GlassQuality` + `glass`、`GlassRealtime` + `glass_realtime`、`GlassFinish.frost_color`。现有令牌语义未动 |
| `app/widgets/glass.py` | 合成器层（`SURFACE`/`BACKDROP`/`CONTENT`）、实时档绘制路径、`_paint_lens_band`、B 类降采样快照（重入安全）、廉价暗幕、`GlassStyle.lens/frost` |
| `app/widgets/ambient.py` | `luminance_map()` / `luminance_at()`、`set_window_size()`、修 `set_window_position` 的无谓失效 |
| `app/widgets/liquid.py` | `LiquidParams.from_finish` 一并取 `frost_color`（两档共用同一色值来源） |
| `app/ui/chrome.py` | `moveEvent` 逐帧刷新（去掉 80ms 节流）、修 `_invalidate_backdrop(full=)` 的 TypeError |
| `tests/test_liquid.py` | 新增第 5~9 节共 20 项断言 |
| `tools/glass_bench.py` | **新增**：量化基线/对比工具 |
| `docs/REALTIME_GLASS_REPORT.md` | **新增**：本报告 |
| `_out/re/FINDINGS.md` | 新增：参考实现逆向笔记（`_out/` 已被 gitignore） |

未改动：`app/audio/*`（一行没碰）、`app/ui/main_window.py`、`app/widgets/controls.py`、
`app/ui/pages/*`、`tools/screenshot.py`。

## 九、怎么复现

```powershell
$env:PYTHONIOENCODING='utf-8'          # 必须！见第七节第 1 条
cd C:\Users\LZY20\Desktop\harness\music-studio

# 全部测试（15 套 / 1109 项 / EXIT=0）
.venv\Scripts\python.exe -u tests\run_all.py

# 量化对比：改前 → 改后
.venv\Scripts\python.exe -u tools\glass_bench.py --tag before `
    --glass-mode quality --throttle-ms 80
.venv\Scripts\python.exe -u tools\glass_bench.py --tag after
.venv\Scripts\python.exe -u tools\glass_bench.py --tag after_noglass --no-glass-paint
.venv\Scripts\python.exe -u tools\glass_bench.py --tag after --compare

# 追加 --real 用真实窗口测（数字噪声大，见第六节第 2 条）

# 观感对比
.venv\Scripts\python.exe -u _out\shots_compare.py before
.venv\Scripts\python.exe -u _out\shots_compare.py after
.venv\Scripts\python.exe -u _out\shots_diff.py
```

切回高质量档（SDF 折射）：`theme.glass.set_mode("quality")`。
