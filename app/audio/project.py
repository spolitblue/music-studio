"""工程数据模型 —— 可序列化的音乐工程。

整棵树都是纯 dataclass，不依赖 Qt、numpy 之外的任何东西，因此：
* 可以 JSON 存盘 / 读盘（:meth:`Project.save` / :meth:`Project.load`）；
* 可以脱离界面被渲染器直接消费；
* 后续加模块（AI 作曲、MIDI 导入）只需往这里挂新字段，不用动渲染管线。

时间轴约定
----------
* 最小单位是 **step**（默认 1/16 音符）。
* ``steps_per_beat`` 默认 4，``beats_per_bar`` 默认 4 → 一小节 16 步。
* 音高用 **MIDI 音高**（60 = 中央 C）。
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# 鼓件映射（靠拢 General MIDI，便于日后导出/互通）
# --------------------------------------------------------------------------

#: MIDI 音高 → 鼓件名
DRUM_MAP: dict[int, str] = {
    35: "kick_808",
    36: "kick",
    37: "rim",
    38: "snare",
    39: "clap",
    42: "hat_closed",
    44: "hat_pedal",
    46: "hat_open",
    41: "tom_low",
    43: "tom_mid",
    45: "tom_high",
    49: "crash",
    51: "ride",
    55: "cowbell_808",
    56: "cowbell",
    57: "cowbell_latin",
}

DRUM_NAMES: dict[str, str] = {
    "kick": "底鼓",
    "rim": "边击",
    "snare": "军鼓",
    "clap": "拍手",
    "hat_closed": "闭踩镲",
    "hat_pedal": "踏踩镲",
    "hat_open": "开踩镲",
    "tom_low": "低嗵",
    "tom_mid": "中嗵",
    "tom_high": "高嗵",
    "crash": "吊镲",
    "ride": "叮叮镲",
    "cowbell": "牛铃",
    "kick_808": "808 底鼓",
    "cowbell_808": "808 牛铃",
    "cowbell_latin": "拉丁牛铃",
}

#: 默认套鼓里出现的顺序（也是鼓机网格的显示顺序）
DEFAULT_KIT: tuple[str, ...] = (
    "kick", "snare", "clap", "hat_closed", "hat_open",
    "tom_low", "tom_mid", "tom_high", "rim", "crash", "ride", "cowbell",
)

#: 音色包可以往套鼓里**加进来**的扩展鼓件。
#:
#: 为什么刻意不放进 ``DEFAULT_KIT``：默认套鼓要一屏看得完（12 件），
#: 而 808 底鼓/牛铃是有明确风格指向的音色，属于"要用的时候才装"。
#: 一旦进了 ``DEFAULT_KIT``，每条新鼓轨都会多出这些行，默认视图就永远变长了。
PACK_KIT: tuple[str, ...] = ("kick_808", "cowbell_808", "cowbell_latin")

#: 鼓机网格的显示顺序 = 默认套鼓 + 扩展鼓件。
#: 网格只画"真的在这条轨的 kit 里、且启用"的那些，所以默认工程仍然是 12 行。
KIT_ORDER: tuple[str, ...] = DEFAULT_KIT + PACK_KIT

DRUM_PITCH: dict[str, int] = {v: k for k, v in DRUM_MAP.items()}


def pitch_to_drum(pitch: int) -> str:
    return DRUM_MAP.get(int(pitch), "kick")


def drum_to_pitch(name: str) -> int:
    return DRUM_PITCH.get(name, 36)


# --------------------------------------------------------------------------
# 基础构件
# --------------------------------------------------------------------------


@dataclass
class ADSR:
    """音量/滤波包络。时间单位秒，sustain 是 0..1 的电平。"""

    attack: float = 0.005
    decay: float = 0.12
    sustain: float = 0.7
    release: float = 0.18
    curve: float = 2.0   # >1 指数形，=1 线性

    @property
    def total(self) -> float:
        return self.attack + self.decay + self.release


@dataclass
class FilterParams:
    """谐振低通 + 独立滤波包络。

    ``env_amount`` > 0 时截止频率从 ``cutoff * 2^(env_amount*octaves)`` 扫到 cutoff，
    这是减法合成器"哇音"的来源。
    """

    kind: str = "lp"            # lp | hp | bp
    cutoff: float = 6000.0      # Hz
    resonance: float = 0.9      # Q
    env_amount: float = 0.0     # 0..1，映射到 +5 个八度
    env: ADSR = field(default_factory=lambda: ADSR(0.002, 0.25, 0.0, 0.15, 2.5))
    keytrack: float = 0.0       # 0..1，跟随音高提升截止频率

    def cutoff_range(self, pitch: int) -> tuple[float, float]:
        """返回 (起始截止, 稳定截止)，单位 Hz。"""
        base = self.cutoff * (2.0 ** (self.keytrack * (pitch - 60) / 12.0))
        start = base * (2.0 ** (self.env_amount * 5.0))
        return float(np_clip(start, 30.0, 20000.0)), float(np_clip(base, 30.0, 20000.0))


def np_clip(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


@dataclass
class SynthParams:
    """减法合成器音色。"""

    wave: str = "saw"           # sine | triangle | saw | square | organ | noise
    octave: int = 0             # -3..3
    semitone: int = 0           # -12..12
    detune_cents: float = 0.0   # 与 unison 配合做合唱
    unison: int = 1             # 1..5 个失谐振荡器堆叠
    spread: float = 0.5         # unison 的立体声展开 0..1
    glide: float = 0.0          # 滑音时长（秒），0 = 关闭
    amp: ADSR = field(default_factory=lambda: ADSR(0.005, 0.12, 0.7, 0.18, 2.0))
    filter: FilterParams = field(default_factory=FilterParams)
    pitch_env_amount: float = 0.0  # 半音数，打击式音高下坠
    pitch_env_time: float = 0.06
    volume: float = 0.8

    def detuned_pitches(self, pitch: int) -> list[float]:
        """展开 unison，返回每个振荡器的实际 MIDI 音高。"""
        base = pitch + self.octave * 12 + self.semitone
        n = max(1, min(5, int(self.unison)))
        if n == 1:
            return [base + self.detune_cents / 100.0]
        # 对称铺开：n=3 → -1,0,+1 份失谐
        half = (n - 1) / 2.0
        return [
            base + self.detune_cents / 100.0 + (i - half) * (self.detune_cents or 8.0) / 100.0 * 2.0
            for i in range(n)
        ]


@dataclass
class DrumVoice:
    """单个鼓件的合成参数 —— 全部由代码合成，不依赖任何采样文件。"""

    level: float = 0.8
    tune: float = 0.0        # 半音
    decay: float = 0.35      # 秒
    tone: float = 0.5        # 0..1，音色明暗
    snap: float = 0.5        # 0..1，瞬态冲击量
    pan: float = 0.0
    enabled: bool = True


@dataclass
class Effect:
    """一个效果器实例。``params`` 用字典存，方便自由增删而不用改序列化。"""

    kind: str = "reverb"     # reverb | delay | distortion | filter | chorus | bitcrush
    enabled: bool = True
    params: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def reverb(size: float = 0.6, damping: float = 0.5, mix: float = 0.25,
               predelay: float = 0.02, width: float = 1.0) -> "Effect":
        return Effect("reverb", True,
                      {"size": size, "damping": damping, "mix": mix,
                       "predelay": predelay, "width": width})

    @staticmethod
    def delay(time_beats: float = 0.375, feedback: float = 0.35, mix: float = 0.22,
              ping_pong: bool = False, tone: float = 5000.0) -> "Effect":
        return Effect("delay", True,
                      {"time_beats": time_beats, "feedback": feedback, "mix": mix,
                       "ping_pong": ping_pong, "tone": tone})

    @staticmethod
    def distortion(drive: float = 3.0, tone: float = 4500.0, mix: float = 0.6,
                   mode: str = "soft") -> "Effect":
        return Effect("distortion", True,
                      {"drive": drive, "tone": tone, "mix": mix, "mode": mode})

    @staticmethod
    def filter(kind: str = "lp", cutoff: float = 8000.0, resonance: float = 0.7) -> "Effect":
        return Effect("filter", True,
                      {"kind": kind, "cutoff": cutoff, "resonance": resonance})

    @staticmethod
    def bitcrush(bits: int = 8, downsample: int = 2, mix: float = 1.0) -> "Effect":
        return Effect("bitcrush", True, {"bits": bits, "downsample": downsample, "mix": mix})

    @staticmethod
    def chorus(rate: float = 0.8, depth: float = 0.4, mix: float = 0.35,
               spread: float = 0.6) -> "Effect":
        return Effect("chorus", True,
                      {"rate": rate, "depth": depth, "mix": mix, "spread": spread})


@dataclass
class Note:
    """一个音符。``step`` 与 ``length`` 都以 step 为单位。"""

    step: int
    pitch: int
    length: int = 1
    velocity: float = 0.85

    def end_step(self) -> int:
        return self.step + max(1, self.length)


@dataclass
class Track:
    """一条轨道。synth / drum 两种类型共用同一套音符列表，只是渲染方式不同。"""

    name: str = "轨道"
    kind: str = "synth"                  # synth | drum
    icon: str = "piano"                  # Twemoji 图标名
    color: str = "#3B88C3"               # 取自 Twemoji 调色板
    notes: list[Note] = field(default_factory=list)
    synth: SynthParams = field(default_factory=SynthParams)
    kit: dict[str, DrumVoice] = field(default_factory=dict)
    effects: list[Effect] = field(default_factory=list)
    volume: float = 0.8                  # 0..1.5 线性增益
    pan: float = 0.0                     # -1..1
    mute: bool = False
    solo: bool = False
    send_reverb: float = 0.0             # 送往总线混响的量 0..1
    send_delay: float = 0.0
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    #: 这条轨道是「编曲助手」生成的。
    #: 为什么把归属做成数据模型里的一等字段，而不是页面自己记一份 id 列表：
    #: 撤销 / 重做 / 打开工程都会把整棵轨道树**从 JSON 重建**，对象与 id 全部换新，
    #: 页面手里的 id 会集体失效（而且再没有别的办法把"撤销还原回来的那批"认回来）。
    #: 标记跟着工程一起往返，于是"哪些轨道是 AI 生成的"这个问题永远能从工程数据
    #: 里重新算出来 —— 与页面被换过几次无关。
    ai_generated: bool = False

    # -- 鼓套件 ----------------------------------------------------------

    def voice(self, piece: str) -> DrumVoice:
        if piece not in self.kit:
            self.kit[piece] = DrumVoice()
        return self.kit[piece]

    def ensure_kit(self) -> None:
        for piece in DEFAULT_KIT:
            self.kit.setdefault(piece, DrumVoice())

    # -- 音符编辑 --------------------------------------------------------

    def add_note(self, note: Note) -> Note:
        self.notes.append(note)
        return note

    def note_at(self, step: int, pitch: int) -> Note | None:
        for n in self.notes:
            if n.step == step and n.pitch == pitch:
                return n
        return None

    def remove_note(self, step: int, pitch: int) -> bool:
        for i, n in enumerate(self.notes):
            if n.step == step and n.pitch == pitch:
                del self.notes[i]
                return True
        return False

    def toggle_note(self, step: int, pitch: int, length: int = 1) -> bool:
        """网格点击的语义：有则删，无则加。返回点击后是否存在。"""
        if self.remove_note(step, pitch):
            return False
        self.add_note(Note(step=step, pitch=pitch, length=length))
        return True

    def prune(self) -> None:
        self.notes = [n for n in self.notes if n.length >= 1]

    def sorted_notes(self) -> list[Note]:
        return sorted(self.notes, key=lambda n: (n.step, n.pitch))


# --------------------------------------------------------------------------
# 母带
# --------------------------------------------------------------------------


@dataclass
class MasterParams:
    """总线。混响/延迟在这里做（发送式），末端挂限幅器保底。"""

    volume: float = 0.9
    reverb: Effect = field(default_factory=lambda: Effect.reverb(size=0.7, damping=0.5, mix=1.0))
    delay: Effect = field(default_factory=lambda: Effect.delay(time_beats=0.5, feedback=0.3, mix=1.0))
    limiter_threshold: float = -1.0    # dBFS
    limiter_release: float = 0.10      # 秒
    limiter_enabled: bool = True
    fade_in: float = 0.01              # 秒
    fade_out: float = 1.2              # 秒


# --------------------------------------------------------------------------
# 工程
# --------------------------------------------------------------------------


@dataclass
class Project:
    """一首曲子。"""

    name: str = "未命名工程"
    author: str = ""
    bpm: float = 120.0
    steps_per_beat: int = 4
    beats_per_bar: int = 4
    bars: int = 8
    swing: float = 0.0                 # 0..0.6，偶数 step 延后
    tracks: list[Track] = field(default_factory=list)
    master: MasterParams = field(default_factory=MasterParams)
    loop_start: int = 0
    loop_end: int = 0                  # 0 = 用 total_steps

    # -- 时间轴 ----------------------------------------------------------

    @property
    def steps_per_bar(self) -> int:
        return self.steps_per_beat * self.beats_per_bar

    @property
    def total_steps(self) -> int:
        return max(1, self.bars * self.steps_per_bar)

    @property
    def seconds_per_step(self) -> float:
        return 60.0 / max(1.0, self.bpm) / max(1, self.steps_per_beat)

    @property
    def step_seconds(self) -> float:
        return self.seconds_per_step

    def duration_seconds(self, extra_tail: float = 2.0) -> float:
        """总时长（含释放尾巴，保证最后的长音不被切掉）。"""
        return self.total_steps * self.seconds_per_step + extra_tail

    def step_time(self, step: float) -> float:
        """step → 秒。含摇摆（swing）处理。"""
        return self.swung_step(step) * self.seconds_per_step

    def swung_step(self, step: float) -> float:
        """摇摆：把每个偶数 step 组的后半拍往后推。"""
        if self.swing <= 0.0:
            return float(step)
        s = float(step)
        idx = int(math.floor(s))
        frac = s - idx
        if idx % 2 == 1:
            return float(idx) + frac + self.swing * 0.5
        return s

    @property
    def loop_range(self) -> tuple[int, int]:
        start = max(0, min(self.total_steps - 1, self.loop_start))
        end = self.loop_end if self.loop_end > start else self.total_steps
        return start, min(self.total_steps, end)

    # -- 轨道管理 --------------------------------------------------------

    def add_track(self, track: Track) -> Track:
        self.tracks.append(track)
        return track

    def remove_track(self, track_or_index: Track | int) -> None:
        if isinstance(track_or_index, int):
            if 0 <= track_or_index < len(self.tracks):
                del self.tracks[track_or_index]
        elif track_or_index in self.tracks:
            self.tracks.remove(track_or_index)

    def any_solo(self) -> bool:
        return any(t.solo for t in self.tracks)

    def is_audible(self, track: Track) -> bool:
        """独奏优先于静音 —— 这是所有 DAW 的一致语义。"""
        if self.any_solo():
            return track.solo and not track.mute
        return not track.mute

    def track_by_id(self, tid: str) -> Track | None:
        for t in self.tracks:
            if t.id == tid:
                return t
        return None

    def max_pitch(self) -> int:
        pitches = [n.pitch for t in self.tracks for n in t.notes]
        return max(pitches) if pitches else 72

    def min_pitch(self) -> int:
        pitches = [n.pitch for t in self.tracks for n in t.notes]
        return min(pitches) if pitches else 48

    # -- 序列化 ----------------------------------------------------------

    def to_dict(self) -> dict:
        return _to_plain(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Project":
        return _from_plain(cls, data)

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return p

    @classmethod
    def load(cls, path: str | Path) -> "Project":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)

    def clone(self) -> "Project":
        return Project.from_dict(self.to_dict())


# --------------------------------------------------------------------------
# 通用 dataclass <-> dict（容忍缺字段，方便旧工程升级）
# --------------------------------------------------------------------------


def _to_plain(obj: Any) -> Any:
    if isinstance(obj, list):
        return [_to_plain(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _to_plain(v) for k, v in obj.items()}
    if hasattr(obj, "__dataclass_fields__"):
        return {f.name: _to_plain(getattr(obj, f.name)) for f in fields(obj)}
    return obj


def _from_plain(cls: type, data: Any) -> Any:
    if data is None:
        return None
    if not hasattr(cls, "__dataclass_fields__"):
        return data
    if not isinstance(data, dict):
        return cls()

    kwargs = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        raw = data[f.name]
        ftype = f.type
        # 用字符串比较即可 —— from __future__ import annotations 让 type 是字符串
        type_name = ftype if isinstance(ftype, str) else getattr(ftype, "__name__", str(ftype))
        kwargs[f.name] = _coerce(type_name, raw, f)
    return cls(**kwargs)


def _coerce(type_name: str, raw: Any, f) -> Any:
    if raw is None:
        return None
    base = type_name.split("[")[0].strip()

    simple = {"int": int, "float": float, "str": str, "bool": bool}
    if base in simple and not isinstance(raw, (dict, list)):
        try:
            return simple[base](raw)
        except (TypeError, ValueError):
            return raw

    if base == "list":
        inner = type_name[type_name.find("[") + 1: type_name.rfind("]")].strip()
        if not isinstance(raw, list):
            return []
        if inner == "Note":
            return [_from_plain(Note, v) for v in raw]
        if inner == "Track":
            return [_from_plain(Track, v) for v in raw]
        if inner == "Effect":
            return [_from_plain(Effect, v) for v in raw]
        return list(raw)

    if base == "dict":
        if not isinstance(raw, dict):
            return {}
        inner = type_name[type_name.find("[") + 1: type_name.rfind("]")]
        parts = [p.strip() for p in inner.split(",")]
        if len(parts) == 2:
            value_cls = _DATACLASS_BY_NAME.get(parts[1])
            if value_cls is not None:
                # 例如 dict[str, DrumVoice] —— 键保持字符串，值必须还原成 dataclass
                return {k: _from_plain(value_cls, v) for k, v in raw.items()}
        return dict(raw)

    # 嵌套 dataclass
    inner_cls = _DATACLASS_BY_NAME.get(base)
    if inner_cls is not None and isinstance(raw, dict):
        return _from_plain(inner_cls, raw)
    return raw


#: 反序列化时按类型名查找目标 dataclass
_DATACLASS_BY_NAME: dict[str, type] = {
    "ADSR": ADSR,
    "FilterParams": FilterParams,
    "SynthParams": SynthParams,
    "DrumVoice": DrumVoice,
    "Effect": Effect,
    "MasterParams": MasterParams,
    "Note": Note,
    "Track": Track,
    "Project": Project,
}


__all__ = [
    "DRUM_MAP", "DRUM_NAMES", "DEFAULT_KIT", "PACK_KIT", "KIT_ORDER",
    "DRUM_PITCH", "pitch_to_drum", "drum_to_pitch",
    "ADSR", "FilterParams", "SynthParams", "DrumVoice", "Effect", "Note", "Track",
    "MasterParams", "Project",
]
