"""Windows 平台的"真·玻璃"底座。

浏览器里做玻璃拟态用 `backdrop-filter`；Qt 没有这个能力，但 Windows 自己在
合成器（DWM）里就有 —— 我们直接借用，拿到的是**真实的桌面背景虚化**，
而不是拿半透明色块假装。

两条技术路线（本模块都实现，可运行时切换）：

* ``acrylic`` / ``blur`` —— ``SetWindowCompositionAttribute`` 的
  ``ACCENT_ENABLE_ACRYLICBLURBEHIND``。兼容 Win10 1803+，支持无边框窗口，
  可以自由指定染色。代价：拖动窗口时略吃 GPU。
* ``mica`` / ``mica_alt`` —— ``DWMWA_SYSTEMBACKDROP_TYPE``。Win11 22H2+ 的
  官方材质，性能最好，但对窗口样式有要求（需要标准边框的合成路径）。

系统的"透明效果"总开关关闭时，以上全部静默失效 —— 所以 :func:`transparency_enabled`
必须先查，UI 据此降级为自绘不透明玻璃。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from enum import IntEnum

IS_WINDOWS = sys.platform == "win32"

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------


class AccentState(IntEnum):
    DISABLED = 0
    ENABLE_GRADIENT = 1
    ENABLE_TRANSPARENTGRADIENT = 2
    ENABLE_BLURBEHIND = 3
    ENABLE_ACRYLICBLURBEHIND = 4
    ENABLE_HOSTBACKDROP = 5
    INVALID = 6


class BackdropType(IntEnum):
    """DWMWA_SYSTEMBACKDROP_TYPE 取值（Windows 11 22H2+）。"""

    AUTO = 0
    NONE = 1
    MICA = 2          # 桌面壁纸着色 —— 最"安静"的材质
    ACRYLIC = 3       # 更模糊，适合短期浮现的表面
    MICA_ALT = 4      # 标签页式材质，偏暗


class CornerPreference(IntEnum):
    DEFAULT = 0
    DONOTROUND = 1
    ROUND = 2
    ROUNDSMALL = 3


class DwmWindowAttribute(IntEnum):
    USE_IMMERSIVE_DARK_MODE = 20
    WINDOW_CORNER_PREFERENCE = 33
    BORDER_COLOR = 34
    CAPTION_COLOR = 35
    TEXT_COLOR = 36
    SYSTEMBACKDROP_TYPE = 38


class WindowCompositionAttribute(IntEnum):
    ACCENT_POLICY = 19


WCA_ACCENT_FLAG_DRAW_ALL_BORDERS = 0x2

SPI_GETCLIENTAREAANIMATION = 0x1042

#: 运行时探测结果：本机是否真的支持取用 DWM 材质
AVAILABLE = False
REASON = "非 Windows 平台"


# --------------------------------------------------------------------------
# 结构体
# --------------------------------------------------------------------------


class ACCENT_POLICY(ctypes.Structure):
    _fields_ = [
        ("AccentState", ctypes.c_int),
        ("AccentFlags", ctypes.c_int),
        ("GradientColor", ctypes.c_uint32),  # AABBGGRR
        ("AnimationId", ctypes.c_int),
    ]


class WINCOMPATTRDATA(ctypes.Structure):
    _fields_ = [
        ("Attribute", ctypes.c_int),
        ("Data", ctypes.POINTER(ACCENT_POLICY)),
        ("SizeOfData", ctypes.c_size_t),
    ]


def _abgr(r: int, g: int, b: int, a: int) -> int:
    """打包成 DWM 需要的 AABBGGRR。"""
    return ((a & 0xFF) << 24) | ((b & 0xFF) << 16) | ((g & 0xFF) << 8) | (r & 0xFF)


def _load() -> None:
    global AVAILABLE, REASON, _dwm, _user32
    if not IS_WINDOWS:
        return
    try:
        _dwm = ctypes.windll.dwmapi
        _user32 = ctypes.windll.user32
    except Exception as exc:  # pragma: no cover
        REASON = f"加载系统 DLL 失败: {exc}"
        return

    # Win11 22H2 = build 22621
    try:
        build = sys.getwindowsversion().build
    except Exception:
        build = 0
    if build and build < 22000:
        REASON = f"Windows build {build} 不支持系统材质（需 22621+）"
        return

    AVAILABLE = True
    REASON = f"DWM 可用 (build {build})"


_dwm = None
_user32 = None
_load()


# --------------------------------------------------------------------------
# 系统开关
# --------------------------------------------------------------------------


def transparency_enabled() -> bool:
    """Windows 设置 → 个性化 → 颜色 → 透明效果。

    关掉它，任何亚克力/云母都会失效，窗口会变成死黑 —— 必须提前知道。
    """
    if not IS_WINDOWS:
        return False
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        try:
            value, _ = winreg.QueryValueEx(key, "EnableTransparency")
            return bool(value)
        finally:
            winreg.CloseKey(key)
    except Exception:
        # 读不到就假定开启（Win11 默认开启）
        return True


def animations_enabled() -> bool:
    """无障碍：设置 → 辅助功能 → 视觉效果 → 动画效果。

    对应 Web 端的 `prefers-reduced-motion`。关闭时弹簧/缩放动效要退化为
    短时长交叉淡入，而不是直接消失（反馈仍然要保留）。
    """
    if not IS_WINDOWS or _user32 is None:
        return True
    enabled = wintypes.BOOL(True)
    try:
        ok = _user32.SystemParametersInfoW(
            SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(enabled), 0
        )
        return bool(enabled.value) if ok else True
    except Exception:
        return True


# --------------------------------------------------------------------------
# 窗口材质
# --------------------------------------------------------------------------


def apply_accent(hwnd: int, state: AccentState, tint: tuple[int, int, int, int]) -> bool:
    """通过 SetWindowCompositionAttribute 施加亚克力/模糊。

    ``tint`` 为 (r, g, b, a)，a 用 0..255。alpha 越低桌面透得越多。
    """
    if not IS_WINDOWS or _user32 is None:
        return False
    r, g, b, a = tint
    policy = ACCENT_POLICY(
        AccentState=int(state),
        AccentFlags=WCA_ACCENT_FLAG_DRAW_ALL_BORDERS,
        GradientColor=_abgr(r, g, b, a),
        AnimationId=0,
    )
    data = WINCOMPATTRDATA(
        Attribute=int(WindowCompositionAttribute.ACCENT_POLICY),
        Data=ctypes.pointer(policy),
        SizeOfData=ctypes.sizeof(policy),
    )
    try:
        fn = _user32.SetWindowCompositionAttribute
        fn.argtypes = [wintypes.HWND, ctypes.POINTER(WINCOMPATTRDATA)]
        fn.restype = wintypes.BOOL
        return bool(fn(wintypes.HWND(hwnd), ctypes.byref(data)))
    except Exception:
        return False


def set_backdrop(hwnd: int, backdrop: BackdropType) -> bool:
    """设置 Win11 系统背景材质（DWMWA_SYSTEMBACKDROP_TYPE）。"""
    if not AVAILABLE or _dwm is None:
        return False
    value = ctypes.c_int(int(backdrop))
    try:
        hr = _dwm.DwmSetWindowAttribute(
            wintypes.HWND(hwnd),
            ctypes.c_uint(int(DwmWindowAttribute.SYSTEMBACKDROP_TYPE)),
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
        return hr == 0
    except Exception:
        return False


def set_dark_mode(hwnd: int, dark: bool = True) -> bool:
    """让系统绘制的边框/阴影/标题遵循暗色（避免出现白色描边）。"""
    if not AVAILABLE or _dwm is None:
        return False
    value = ctypes.c_int(1 if dark else 0)
    try:
        hr = _dwm.DwmSetWindowAttribute(
            wintypes.HWND(hwnd),
            ctypes.c_uint(int(DwmWindowAttribute.USE_IMMERSIVE_DARK_MODE)),
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
        return hr == 0
    except Exception:
        return False


def set_corner_preference(hwnd: int, pref: CornerPreference) -> bool:
    if not AVAILABLE or _dwm is None:
        return False
    value = ctypes.c_int(int(pref))
    try:
        hr = _dwm.DwmSetWindowAttribute(
            wintypes.HWND(hwnd),
            ctypes.c_uint(int(DwmWindowAttribute.WINDOW_CORNER_PREFERENCE)),
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
        return hr == 0
    except Exception:
        return False


def set_border_color(hwnd: int, r: int, g: int, b: int) -> bool:
    """把系统 1px 边框染成玻璃边缘色（DWMWA_COLOR_DEFAULT = 0xFFFFFFFF）。"""
    if not AVAILABLE or _dwm is None:
        return False
    # COLORREF = 0x00BBGGRR
    value = ctypes.c_uint((b << 16) | (g << 8) | r)
    try:
        hr = _dwm.DwmSetWindowAttribute(
            wintypes.HWND(hwnd),
            ctypes.c_uint(int(DwmWindowAttribute.BORDER_COLOR)),
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
        return hr == 0
    except Exception:
        return False


def extend_frame(hwnd: int, margins: tuple[int, int, int, int] = (-1, -1, -1, -1)) -> bool:
    """把 DWM 边框延伸进客户区 —— 无边框窗口能拿到系统投影与圆角裁剪。"""
    if not AVAILABLE or _dwm is None:
        return False

    class MARGINS(ctypes.Structure):
        _fields_ = [("cxLeftWidth", ctypes.c_int), ("cxRightWidth", ctypes.c_int),
                    ("cyTopHeight", ctypes.c_int), ("cyBottomHeight", ctypes.c_int)]

    m = MARGINS(*margins)
    try:
        return _dwm.DwmExtendFrameIntoClientArea(wintypes.HWND(hwnd), ctypes.byref(m)) == 0
    except Exception:
        return False


# --------------------------------------------------------------------------
# 一种材质配置 = 一次应用
# --------------------------------------------------------------------------


class GlassMode:
    """可选的窗口玻璃方案。"""

    ACRYLIC = "acrylic"    # 亚克力：模糊强，桌面色彩透过来
    BLUR = "blur"          # 纯模糊：不做饱和度提升，最轻
    MICA = "mica"          # 云母：桌面壁纸着色，最安静
    MICA_ALT = "mica_alt"
    NONE = "none"


def apply_glass(
    hwnd: int,
    mode: str = GlassMode.ACRYLIC,
    tint: tuple[int, int, int, int] = (18, 18, 24, 180),
    dark: bool = True,
) -> tuple[bool, str]:
    """把选定材质一次性作用到窗口上。

    返回 ``(是否成功, 说明)``。失败时调用方应降级为自绘不透明玻璃 —— 视觉上
    依然成立，只是失去桌面透射。
    """
    if not IS_WINDOWS:
        return False, "非 Windows 平台，使用自绘玻璃"

    if not transparency_enabled():
        return False, "系统「透明效果」已关闭，使用自绘玻璃（可在 Windows 设置中开启）"

    set_dark_mode(hwnd, dark)
    set_corner_preference(hwnd, CornerPreference.ROUND)

    if mode == GlassMode.ACRYLIC:
        ok = apply_accent(hwnd, AccentState.ENABLE_ACRYLICBLURBEHIND, tint)
        if ok:
            set_border_color(hwnd, 255, 255, 255)
            return True, "亚克力（DWM 实时背景虚化）"
        return False, "亚克力设置被系统拒绝"

    if mode == GlassMode.BLUR:
        ok = apply_accent(hwnd, AccentState.ENABLE_BLURBEHIND, tint)
        return (True, "模糊") if ok else (False, "模糊设置被系统拒绝")

    if mode in (GlassMode.MICA, GlassMode.MICA_ALT):
        backdrop = BackdropType.MICA if mode == GlassMode.MICA else BackdropType.MICA_ALT
        ok = set_backdrop(hwnd, backdrop)
        if ok:
            return True, "云母（系统材质）"
        return False, "云母不受支持（需 Win11 22H2+）"

    # NONE：清掉材质
    apply_accent(hwnd, AccentState.DISABLED, (0, 0, 0, 0))
    set_backdrop(hwnd, BackdropType.NONE)
    return True, "无系统材质（纯自绘玻璃）"


def describe() -> str:
    return f"{'可用' if AVAILABLE else '不可用'} — {REASON}"


__all__ = [
    "IS_WINDOWS", "AVAILABLE", "REASON",
    "AccentState", "BackdropType", "CornerPreference", "GlassMode",
    "apply_glass", "apply_accent", "set_backdrop", "set_dark_mode",
    "set_corner_preference", "set_border_color", "extend_frame",
    "transparency_enabled", "animations_enabled", "describe",
]
