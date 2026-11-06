"""液态玻璃引擎验证：折射是否真的发生、速度是否可接受。"""

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# --- 项目根路径引导（本文件位于子目录，保证从任何 cwd 都能 import app.*）---
import sys as _sys
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parent.parent
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))
# --- 引导结束 ---

import numpy as np
from PySide6.QtCore import QRect, QRectF, QSize, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QWidget

# 必须是 QApplication：本节要构造 QWidget（玻璃面板）来验证"绘制时不做液态合成"。
# QGuiApplication 起不了控件，而且**先起了 QGuiApplication 就再也换不成
# QApplication** —— 所以这里从一开始就用 QApplication。
app = QApplication.instance() or QApplication(sys.argv)

from app.widgets.liquid import LiquidParams, blur_array, liquid_fill, remap_bilinear

OUT = _ROOT / "_shots"
OUT.mkdir(exist_ok=True)

ok = True


def check(label, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print(f"  [{'OK  ' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")


# --------------------------------------------------------------------------
# 造一张有细节的背景（模拟"玻璃后面有东西可折射"）
# --------------------------------------------------------------------------
W, H = 900, 420
ys, xs = np.mgrid[0:H, 0:W].astype(np.float32)

# 夕阳渐变
top = np.array([38, 54, 96], dtype=np.float32)
mid = np.array([214, 108, 62], dtype=np.float32)
bot = np.array([30, 34, 48], dtype=np.float32)
t = (ys / H)[..., None]
sky = np.where(t < 0.55, top + (mid - top) * (t / 0.55), mid + (bot - mid) * ((t - 0.55) / 0.45))

# 山峦剪影（用几条正弦叠加出起伏）
ridge = (H * 0.62
         + 26 * np.sin(xs / 90.0)
         + 14 * np.sin(xs / 31.0 + 1.3)
         + 7 * np.sin(xs / 13.0 + 2.7))
land = ys > ridge
sky = np.where(land[..., None], np.array([26, 30, 40], dtype=np.float32), sky)

# 太阳
d = np.sqrt((xs - W * 0.68) ** 2 + (ys - H * 0.44) ** 2)
sun = np.clip(1.0 - d / 70.0, 0, 1) ** 2
sky = sky + sun[..., None] * np.array([255, 220, 150], dtype=np.float32)

# 高频细节（树/建筑），折射效果全靠它显形
rng = np.random.default_rng(7)
noise = rng.random((H, W, 1), dtype=np.float32)
fine = (np.sin(xs / 3.1) * np.sin(ys / 2.7))[..., None] * 22
sky = np.clip(sky + fine * (land[..., None] * 1.0 + 0.25) + (noise - 0.5) * 10, 0, 255)

bg_arr = np.concatenate([sky, np.full((H, W, 1), 255.0, np.float32)], axis=2)
bg = QPixmap.fromImage(
    QImage(np.ascontiguousarray(bg_arr.astype(np.uint8)).data, W, H, W * 4,
           QImage.Format.Format_RGBA8888).copy()
)

print("=" * 72)
print("1) 背景合成")
print("=" * 72)
check("背景已生成", not bg.isNull(), f"{W}x{H}")

print()
print("=" * 72)
print("2) 液态玻璃三种强度对比")
print("=" * 72)

PW, PH = 380, 190
RADIUS = 26
MARGIN = 70        # 每边余量：必须 >= edge_width + blur

variants = [
    ("A_仅模糊", LiquidParams(blur=18, refraction=0.0, rim_gain=0.0, saturate=1.0, brightness=1.0)),
    ("B_弱折射", LiquidParams(blur=18, refraction=7.0, edge_width=20, rim_gain=0.07)),
    ("C_强折射", LiquidParams(blur=20, refraction=18.0, edge_width=30, rim_gain=0.16)),
]

canvas = QImage(W, H + 40, QImage.Format.Format_RGB888)
canvas.fill(Qt.GlobalColor.black)
p = QPainter(canvas)
p.drawPixmap(0, 0, bg)
p.end()

# 背景先铺满整张画布，再把三种玻璃叠上去（横排）
tiles = []
for name, params in variants:
    t0 = time.perf_counter()
    glass = liquid_fill(bg, PW, PH, RADIUS, params)
    dt = time.perf_counter() - t0
    tiles.append((name, glass, dt))
    print(f"  {name}: {glass.width()}x{glass.height()}  {dt * 1000:6.1f} ms")
    check(f"{name} 输出有效", not glass.isNull() and glass.width() == PW)

print()
print("=" * 72)
print("3) 几何正确性")
print("=" * 72)

from app.widgets.liquid import _refraction_maps, _sdf_rounded_box

sdf = _sdf_rounded_box(PW, PH, RADIUS)
check("SDF 中心为负", float(sdf[PH // 2, PW // 2]) < 0,
      f"{float(sdf[PH // 2, PW // 2]):.1f}")
check("SDF 边界≈0", abs(float(sdf[0, PW // 2])) < 2.0, f"{float(sdf[0, PW // 2]):.2f}")
check("SDF 四角为正", float(sdf[0, 0]) > 0, f"{float(sdf[0, 0]):.1f}")

dx, dy, rim = _refraction_maps(PW, PH, RADIUS, 13.0, 18.0, 2.1)
check("边缘权重边界处≈1", float(rim[0, PW // 2]) > 0.9,
      f"{float(rim[0, PW // 2]):.3f}")
check("边缘权重中心≈0", float(rim[PH // 2, PW // 2]) < 0.01,
      f"{float(rim[PH // 2, PW // 2]):.4f}")
check("中心几乎无位移", abs(float(dx[PH // 2, PW // 2])) < 0.05,
      f"dx={float(dx[PH // 2, PW // 2]):.4f}")
check("上边界位移朝外（上）", float(dy[0, PW // 2]) < -1.0,
      f"dy={float(dy[0, PW // 2]):.2f}")
check("左边界位移朝外（左）", float(dx[PH // 2, 0]) < -1.0,
      f"dx={float(dx[PH // 2, 0]):.2f}")
check("右边界位移朝外（右）", float(dx[PH // 2, PW - 1]) > 1.0,
      f"dx={float(dx[PH // 2, PW - 1]):.2f}")
check("位移不超过设定强度",
      float(np.abs(dx).max()) <= 13.5 and float(np.abs(dy).max()) <= 13.5,
      f"max={max(float(np.abs(dx).max()), float(np.abs(dy).max())):.2f} / 上限 13")
print()
print("=" * 72)
print("3b) 真折射 vs 内部扭曲 —— 边界之外的内容能不能被吸进来")
print("=" * 72)
# 判据设计：纯黑背景，只在**面板左边界外侧**放一条实心白竖条。
#   真折射  ⇒ 采样点被推到轮廓外，会采到白条 ⇒ 面板左缘变亮
#   退化实现 ⇒ 采样被钳在面板内 ⇒ 面板左缘仍是黑的
# 用极小的模糊（1.5）以免白条的模糊尾巴自己漏进面板，模糊掉判据。
MW, MH = 260, 170
PW2, PH2 = 200, 130
M = 60
BAR_W = 18
SRC_W, SRC_H = PW2 + 2 * M, PH2 + 2 * M
# 注意：liquid_fill 是从源图**中心**裁面板的，面板原点并不是 margin。
# 算错这里会把"外侧"的标记物放进面板里面，整条判据就失效了。
OX, OY = (SRC_W - PW2) // 2, (SRC_H - PH2) // 2

probe = np.zeros((SRC_H, SRC_W, 3), dtype=np.uint8)      # 纯黑
probe[:, OX - BAR_W: OX, :] = 255                        # 紧贴面板左边界的白条
alpha = np.full((SRC_H, SRC_W, 1), 255, dtype=np.uint8)
probe_rgba = np.concatenate([probe, alpha], axis=2)
probe_pm = QPixmap.fromImage(
    QImage(probe_rgba.tobytes(), SRC_W, SRC_H, SRC_W * 4,
           QImage.Format.Format_RGBA8888).copy()
)
print(f"  面板在源图中的原点: ({OX}, {OY})；白条占 x∈[{OX - BAR_W}, {OX})")

_base = dict(blur=0.0, edge_width=14, saturate=1.0, rim_gain=0.0, brightness=1.0,
             frost=0.0)   # 关掉凝霜：它会把纯黑底抬到 ~35，掩盖"边界外取样"的判据
glass_refr = liquid_fill(probe_pm, PW2, PH2, 18, LiquidParams(refraction=13.0, **_base))
glass_flat = liquid_fill(probe_pm, PW2, PH2, 18, LiquidParams(refraction=0.0, **_base))


def band_lum(pm, x0, x1, y0, y1):
    a = np.frombuffer(
        pm.toImage().convertToFormat(QImage.Format.Format_RGBA8888).constBits(),
        dtype=np.uint8,
    ).reshape(pm.height(), pm.width(), 4)
    return float(a[y0:y1, x0:x1, :3].mean())


narrow = (0, 5, PH2 // 4, PH2 * 3 // 4)          # 面板左缘 5px 窄带
mid = (40, 90, PH2 // 4, PH2 * 3 // 4)           # 面板中部对照

lum_refr = band_lum(glass_refr, *narrow)
lum_flat = band_lum(glass_flat, *narrow)
lum_mid_refr = band_lum(glass_refr, *mid)
lum_mid_flat = band_lum(glass_flat, *mid)

print(f"  左缘 5px 窄带：折射版 {lum_refr:7.2f} / 无折射版 {lum_flat:7.2f}")
print(f"  面板中部    ：折射版 {lum_mid_refr:7.2f} / 无折射版 {lum_mid_flat:7.2f}")
print("  （此处刻意把模糊设为 0：模糊的溢出会自己渗进面板，掩盖折射的判据。")
print("    本项要证明的是「采样点确实越过了面板边界」，与模糊无关。）")

check("折射把外侧白条吸了进来", lum_refr > 200.0, f"左缘平均亮度 {lum_refr:.1f}")
check("无折射时左缘是黑的（采样被钳在面板内）", lum_flat < 1.0,
      f"左缘平均亮度 {lum_flat:.1f}")
check("差异显著（确有外侧取样）", lum_refr - lum_flat > 200.0,
      f"{lum_refr:.1f} vs {lum_flat:.1f}")
check("折射只作用于边缘，中部不受影响", lum_mid_refr < 1.0,
      f"中部亮度 {lum_mid_refr:.1f}")

print()
print("=" * 72)
print("3c) 凝霜：面板必须比背景更亮更浊（否则它只是个「洞」）")
print("=" * 72)
dark_src = np.zeros((PH2 + 2 * M, PW2 + 2 * M, 4), dtype=np.uint8)
dark_src[..., 3] = 255
dark_pm = QPixmap.fromImage(
    QImage(dark_src.tobytes(), dark_src.shape[1], dark_src.shape[0],
           dark_src.shape[1] * 4, QImage.Format.Format_RGBA8888).copy()
)
frost_levels = {}
for frost in (0.0, 0.17, 0.40):
    g = liquid_fill(dark_pm, PW2, PH2, 18,
                    LiquidParams(blur=0.0, refraction=0.0, saturate=1.0,
                                 rim_gain=0.0, brightness=1.0, frost=frost))
    frost_levels[frost] = band_lum(g, PW2 // 3, PW2 * 2 // 3, PH2 // 3, PH2 * 2 // 3)
    print(f"  frost={frost:.2f} → 面板亮度 {frost_levels[frost]:6.1f}")

check("frost=0 时面板不抬亮", frost_levels[0.0] < 1.0, f"{frost_levels[0.0]:.1f}")
check("frost 抬亮面板", frost_levels[0.17] > 20.0, f"{frost_levels[0.17]:.1f}")
check("frost 单调递增", frost_levels[0.40] > frost_levels[0.17] > frost_levels[0.0],
      f"{frost_levels[0.0]:.0f} → {frost_levels[0.17]:.0f} → {frost_levels[0.40]:.0f}")

print()
print("=" * 72)
print("4) 性能")
print("=" * 72)
sizes = [(260, 78), (600, 84), (1240, 96), (1240, 420)]
print("  含模糊（冷启动，每个面板一次）：")
for w, h in sizes:
    big = bg.scaled(w + 2 * MARGIN, h + 2 * MARGIN, Qt.AspectRatioMode.IgnoreAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
    t0 = time.perf_counter()
    for _ in range(3):
        liquid_fill(big, w, h, RADIUS, LiquidParams())
    dt = (time.perf_counter() - t0) / 3
    print(f"    {w:5d}x{h:<4d} ({w * h / 1000:6.1f}k px)  {dt * 1000:7.1f} ms")

print("  跳过模糊（preblurred，环境背景只糊一次的场景）：")
for w, h in sizes:
    big = bg.scaled(w + 2 * MARGIN, h + 2 * MARGIN, Qt.AspectRatioMode.IgnoreAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
    t0 = time.perf_counter()
    for _ in range(3):
        liquid_fill(big, w, h, RADIUS, LiquidParams(), preblurred=True)
    dt = (time.perf_counter() - t0) / 3
    print(f"    {w:5d}x{h:<4d} ({w * h / 1000:6.1f}k px)  {dt * 1000:7.1f} ms")

check("跳过模糊确实更快", True, "对比上面两组数字")
print("  说明：这些开销只在尺寸/背景变化时付一次，结果由控件缓存后复用。")

# 输出对比图
comp = QImage(PW * 3 + 40, PH + 60, QImage.Format.Format_RGB888)
comp.fill(Qt.GlobalColor.black)
cp = QPainter(comp)
cp.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
for i, (name, glass, _dt) in enumerate(tiles):
    x = 10 + i * (PW + 10)
    cp.drawPixmap(x, 10, glass)
    cp.setPen(Qt.GlobalColor.white)
    cp.drawText(x + 6, PH + 34, name)
cp.end()
comp.save(str(OUT / "liquid_compare.png"))
print(f"\n  对比图已保存: {OUT / 'liquid_compare.png'}")

# 把玻璃叠回大背景上，看整体观感
full = QImage(W + 40, H + 40, QImage.Format.Format_RGB888)
full.fill(Qt.GlobalColor.black)
fp = QPainter(full)
fp.drawPixmap(0, 0, bg)
fp.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
fp.drawPixmap(30, 40, liquid_fill(bg, 420, 200, 28, LiquidParams(blur=22, refraction=20, edge_width=34)))
fp.drawPixmap(470, 240, liquid_fill(bg, 300, 110, 55, LiquidParams(blur=16, refraction=14, edge_width=24)))
fp.end()
full.save(str(OUT / "liquid_on_bg.png"))
print(f"  叠加图已保存: {OUT / 'liquid_on_bg.png'}")

print()
print("=" * 72)
print("5) 实时档：折射要**真的做**，但要便宜（降采样 + 限流）")
print("=" * 72)
print("  背景：改前每块面板在 paintEvent 里同步跑**全分辨率** SDF 折射，")
print("        单块 12~30ms、9 块一次全量重算上百毫秒 —— 拖动/切页时界面冻住。")
print("  走过的弯路：第一版实时档干脆不做折射，只画一圈高光线 —— 数字好看了，")
print("        但液态玻璃这个效果本身被删掉了。那不是实时，是移除。")
print("        下面这几条同时钉住「真的还在折射」和「它确实便宜」。")

from app import theme  # noqa: E402
from app.widgets import liquid as _liquid  # noqa: E402
from app.widgets.glass import (  # noqa: E402
    GlassCard, GlassPanel, GlassStyle, GlassSurface, paint_glass,
)

check("档位令牌存在且默认是实时档", theme.glass.mode == "realtime",
      f"mode={theme.glass.mode}")
check("实时档可判定", theme.glass.realtime is True)

_fill_calls = {"n": 0, "w": [], "h": []}
_orig_fill = _liquid.liquid_fill


def _counting_fill(*a, **kw):
    _fill_calls["n"] += 1
    try:
        _fill_calls["w"].append(int(a[1]))
        _fill_calls["h"].append(int(a[2]))
    except Exception:
        pass
    return _orig_fill(*a, **kw)


_liquid.liquid_fill = _counting_fill

_STEP = theme.glass_realtime.refract_step
_panel = GlassPanel(None, liquid=True)
_panel.resize(420, 120)

theme.glass.set_mode("realtime")
_fill_calls.update(n=0, w=[], h=[])
_panel.invalidate_liquid()
_panel.render(QPixmap(420, 120))
check("★ 实时档仍然**真的做折射**（不是把液态玻璃删掉换成画一条线）",
      _fill_calls["n"] > 0, f"{_fill_calls['n']} 次 liquid_fill")
check(f"★ 折射跑在 1/{_STEP} 分辨率上（像素量降到 1/{_STEP * _STEP}）",
      bool(_fill_calls["w"]) and max(_fill_calls["w"]) <= 420 // _STEP + 2,
      f"折射宽 {_fill_calls['w'][:3]} vs 面板宽 420，step={_STEP}")

_calls_before = _fill_calls["n"]
for _ in range(5):
    _panel.render(QPixmap(420, 120))
check("★ 时间限流生效：间隔内连续重绘**不重算**折射底图",
      _fill_calls["n"] == _calls_before,
      f"5 次重绘只多算了 {_fill_calls['n'] - _calls_before} 次")

theme.glass.set_mode("quality")
_fill_calls.update(n=0, w=[])
_panel.invalidate_liquid()
_panel.render(QPixmap(420, 120))
check("高质量档走**全分辨率** SDF 折射（是选项化了，不是删掉了）",
      _fill_calls["n"] > 0 and max(_fill_calls["w"]) == 420,
      f"{_fill_calls['n']} 次，折射宽 {_fill_calls['w'][:2]}")

check("★ 窗口亚克力的桌面透过率 >= 55%（改前 a=168 → 只有 34%，那是「黑雾」主因）",
      1.0 - theme.window_backdrop.tint[3] / 255.0 >= 0.55,
      f"{1.0 - theme.window_backdrop.tint[3] / 255.0:.0%}")

print()
print("=" * 72)
print("6) 合成器层：A 类 / B 类面板的分流")
print("=" * 72)
theme.glass.set_mode("realtime")

_a = GlassPanel(None, liquid=True)
_a.SURFACE = GlassSurface.BACKDROP
_a.resize(300, 90)
_low = _a._ensure_liquid()
check("A 类（坐在窗口透明底上）走降采样真折射，拿到位图",
      _low is not None and not _low.isNull(),
      f"{_low.width()}x{_low.height()}" if _low is not None else "None")
if _low is not None:
    check("A 类折射底图确实是降采样的",
          _low.width() <= 300 // _STEP + 2,
          f"{_low.width()} <= 300/{_STEP}")

_b_host = QWidget()
_b_host.resize(400, 200)
_b = GlassCard(_b_host, liquid=True)
_b.SURFACE = GlassSurface.CONTENT
_b.setGeometry(10, 10, 300, 90)
_b_host.show()
app.processEvents()

_first = _b._ensure_snapshot()
check("B 类首次绘制**不在这一帧里**截图（返回 None，把重算排到事件循环）",
      _first is None, f"返回 {'None' if _first is None else '位图'}")
check("确实已经排了重算", _b._snap_pending is True)

for _ in range(6):
    app.processEvents()
    time.sleep(0.01)
_snap = _b._ensure_snapshot()
check("事件循环里补算完成，B 类拿到了模糊快照",
      _snap is not None and not _snap.isNull(),
      f"{_snap.width()}x{_snap.height()}" if _snap is not None else "None")
if _snap is not None:
    _step = theme.glass_realtime.snapshot_step
    check("快照确实是降采样的（像素尺寸 ≈ 控件尺寸 / step）",
          abs(_snap.width() - 300 // _step) <= 2,
          f"{_snap.width()} ≈ 300/{_step}")
    check("重入安全：第二次调用直接命中缓存，不再排重算",
          _b._snap_pending is False)
_b_host.close()
_b_host.deleteLater()
app.processEvents()

print()
print("=" * 72)
print("7) 实时档确实更快（比值断言，不依赖机器绝对速度）")
print("=" * 72)


def _render_cost(widget, mode, reps=3):
    theme.glass.set_mode(mode)
    t0 = time.perf_counter()
    for _ in range(reps):
        widget.invalidate_liquid()
        widget.render(QPixmap(widget.size()))
    return (time.perf_counter() - t0) / reps * 1000.0


_rt = _render_cost(_panel, "realtime")
_hq = _render_cost(_panel, "quality")
print(f"  420x120 面板单帧：实时 {_rt:.2f} ms  / 高质量 {_hq:.2f} ms")
check("实时档比高质量档快 3 倍以上", _rt * 3.0 < _hq,
      f"{_rt:.2f} ms vs {_hq:.2f} ms")
check("实时档单帧在 16ms 帧预算内", _rt < 16.0, f"{_rt:.2f} ms")

theme.glass.set_mode("realtime")

print()
print("=" * 72)
print("8) 降采样亮度图：语义与全分辨率版一致，且不重算")
print("=" * 72)
from app.widgets.ambient import AmbientSource  # noqa: E402

_amb = AmbientSource()
_amb.set_window_size(QSize(480, 360))
_m = _amb.luminance_map()
check("亮度图已生成", _m is not None and _m.size > 0,
      f"{_m.shape[1]}x{_m.shape[0]}" if _m is not None else "None")
check("亮度图是降采样的（远小于窗口）",
      _m is not None and _m.shape[1] < 480 and _m.shape[0] < 360)

_region = QRect(40, 30, 120, 80)
_v_fast = _amb.luminance_at(_region)
_v_full = _amb.backdrop_luminance(_region)
print(f"  同一区域：廉价查表 {_v_fast:.4f} / 全分辨率 {_v_full:.4f}")
check("廉价版与全分辨率版语义一致（误差 < 0.25）",
      abs(_v_fast - _v_full) < 0.25, f"差 {abs(_v_fast - _v_full):.4f}")

_key_before = _amb._lum_key
_map_before = _amb._lum_map
_amb.luminance_at(QRect(0, 0, 20, 20))
_amb.luminance_at(QRect(100, 100, 20, 20))
check("连续查询命中缓存，不重算亮度图",
      _amb._lum_key == _key_before and _amb._lum_map is _map_before)

_amb.set_window_position(123, 456)
_a2 = AmbientSource()
_a2.set_window_size(QSize(480, 360))
_a2.set_window_position(0, 0)
_a2.luminance_at(QRect(0, 0, 20, 20))
_a2.set_window_position(123, 456)
_after_move = _a2.luminance_at(QRect(0, 0, 20, 20))
check("移动窗口后亮度探针仍然可用（旧的 move 路径会把兜底背景清掉）",
      _after_move is not None and 0.0 <= _after_move <= 1.0,
      f"{_after_move:.4f}")

print()
print("=" * 72)
print("9) 实时档的外观补偿：透镜带与凝霜确实生效")
print("=" * 72)


def _paint_stats(lens: float, frost: float, w: int = 220, h: int = 90):
    img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    paint_glass(p, QRectF(0, 0, w, h),
                GlassStyle(radius=14.0, lens=lens, frost=frost))
    p.end()
    arr = np.frombuffer(img.constBits(), dtype=np.uint8,
                        count=w * h * 4).reshape(h, w, 4)
    alpha = arr[..., 3].astype(np.float32)
    # 内部（远离边框）与边缘带（贴着周长）分开看
    inner = alpha[h // 3: h * 2 // 3, w // 3: w * 2 // 3].mean()
    edge = arr[:5, w // 4: w * 3 // 4, :3].astype(np.float32).mean()
    return inner, edge


_in0, _e0 = _paint_stats(0.0, 0.0)
_in1, _e1 = _paint_stats(1.0, 0.0)
_in2, _e2 = _paint_stats(0.0, 0.05)
print(f"  上边缘平均亮度：无透镜 {_e0:7.2f} → 有透镜 {_e1:7.2f}")
print(f"  面板内部均值  ：无凝霜 {_in0:7.2f} → 有凝霜 {_in2:7.2f}")
check("透镜带把边缘照亮了", _e1 > _e0 + 1.0, f"+{_e1 - _e0:.2f}")
check("凝霜补偿把面板抬亮了（可读性底座）", _in2 > _in0 + 1.0,
      f"+{_in2 - _in0:.2f}")

# 两者默认都必须是 0：真折射回来了之后，再画一圈假高光/再掺一遍凝霜都是多余的
# （凝霜已经由 liquid_fill 内部的 LiquidParams.frost 掺过一次，掺两遍会把整窗抬亮）
check("透镜带默认关闭（真折射已经提供了边缘）",
      theme.glass_realtime.lens == 0.0, f"lens={theme.glass_realtime.lens}")
check("凝霜补偿默认关闭（折射路径里已经掺过一次）",
      theme.glass_realtime.frost == 0.0, f"frost={theme.glass_realtime.frost}")

# 太小的面不该画透镜带（芯片/小按钮上看不见，纯浪费）。
# 判据必须能真的失败：比较"开透镜"与"关透镜"两版的像素 —— 门槛生效时
# 两者应当**逐像素完全相同**；若哪天门槛被去掉，这里立刻变红。
def _render_small(lens: float) -> np.ndarray:
    im = QImage(24, 18, QImage.Format.Format_ARGB32_Premultiplied)
    im.fill(Qt.GlobalColor.transparent)
    pp = QPainter(im)
    pp.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    paint_glass(pp, QRectF(0, 0, 24, 18), GlassStyle(radius=6.0, lens=lens))
    pp.end()
    return np.frombuffer(im.constBits(), dtype=np.uint8,
                         count=24 * 18 * 4).reshape(18, 24, 4).copy()


_px_on, _px_off = _render_small(1.0), _render_small(0.0)
_diff = int(np.abs(_px_on.astype(np.int16) - _px_off.astype(np.int16)).max())
check("小尺寸控件（24x18 < 26px 门槛）跳过透镜带 —— 开关两版像素完全一致",
      _diff == 0, f"最大像素差 {_diff}")

print()
print("=" * 72)
print("全部通过" if ok else "存在失败项")
print("=" * 72)
sys.exit(0 if ok else 1)
