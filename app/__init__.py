"""制乐软件 Music Studio —— 一个用 Python 写的可真正使用的音乐制作工具。

包结构
------
``app.theme``      设计令牌（颜色/排版/圆角/材质/动效时长）—— 视觉唯一真相来源
``app.motion``     弹簧动效（阻尼谐振子，可中断）
``app.icons``      Twemoji 图标系统
``app.platform``   Windows 平台能力（DWM 真·背景虚化、系统开关探测）
``app.widgets``    玻璃控件库
``app.audio``      音频引擎（合成 / 鼓机 / 效果 / 混音 / 导出）
``app.ui``         窗口与页面
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
