"""音频引擎。

子模块
------
``dsp``       DSP 基元（带限振荡器、包络、滤波器、卷积）
``project``   工程数据模型（可 JSON 序列化）
``synth``     减法合成器声部
``drums``     鼓机（纯合成，无采样）
``effects``   效果器（混响/延迟/失真/滤波/合唱/降位）
``render``    混音渲染管线 + 母带限幅
``wavio``     WAV / MIDI 读写
``player``    sounddevice 实时播放
``demo``      内置示范工程
"""

from app.audio import dsp, drums, effects, player, project, render, synth, wavio

__all__ = ["dsp", "project", "synth", "drums", "effects", "render", "wavio", "player", "demo"]


def __getattr__(name: str):
    # demo 只在需要时才导入（它拉进了 drums 等一堆东西）。
    # 注意必须用 importlib —— 写成 `from app.audio import demo` 会在
    # 本 __getattr__ 内部再次触发 __getattr__，直接无限递归。
    if name == "demo":
        import importlib

        module = importlib.import_module("app.audio.demo")
        globals()["demo"] = module      # 缓存，下次走正常属性查找
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
