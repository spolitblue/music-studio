"""音色包 —— 成套的鼓件参数（外加配套的合成器预设），一键换整套音色。

为什么需要"包"这一层
--------------------
单件鼓的参数已经很能调（``tone/snap/decay/tune``），但**风格是一整套东西**：
808 底鼓必须配长衰减的贝斯、牛铃要放在带通中心 2650Hz 附近才像 808，
这些东西单独调任何一个都不成调。把它们打包成"一次点击换一套"，
才是使用者真正需要的粒度 —— 而且包里的参数值得被写下来、被测试、
被下一个人复用（而不是散落在聊天记录里）。

设计约束
--------
* **仍然不引入任何采样文件**：包只写"合成参数"，声音照样是实时算出来的。
  所以包是几十行数据，不是几十兆素材，也能随工程一起 JSON 往返。
* **包不碰 ``DEFAULT_KIT``**：808/牛铃属于扩展鼓件（``PACK_KIT``），
  装进轨道的 ``kit`` 里才会出现在鼓机网格上。默认套鼓保持 12 件。
* **应用包 = 复制参数**（``deepcopy``），不是共享对象 —— 否则两条轨道会
  悄悄共用一个 ``DrumVoice``，改一条另一条跟着变。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace

from app.audio import drums as drums_mod
from app.audio.project import (
    ADSR, DEFAULT_KIT, DRUM_NAMES, FilterParams, Note, SynthParams, Track,
    drum_to_pitch,
)

#: 808 那套音色里最有辨识度的几条低频/金属参数，单独拎出来当常量 ——
#: 几个包会共用它们，写死两遍迟早会改歪一边。
_KICK_808_LONG = dict(level=1.0, tune=-2.0, decay=1.05, tone=0.30, snap=0.35, pan=0.0)
_KICK_808_TIGHT = dict(level=1.0, tune=-1.0, decay=0.55, tone=0.34, snap=0.55, pan=0.0)
_COWBELL_808 = dict(level=0.46, tune=0.0, decay=0.16, tone=0.55, snap=0.45, pan=0.20)
_COWBELL_LATIN = dict(level=0.44, tune=-2.0, decay=0.42, tone=0.45, snap=0.40, pan=-0.25)

#: 808 滑音低音（贝斯声部）—— 正弦 + 音高滑音，这是 trap/hip-hop 低音的骨架。
#: 为什么用 ``sine``：808 低音的能量几乎全在基频上，加谐波只会让它在小喇叭上
#: 变浑；要"听得见"靠的是包络的瞬态与音量，不是波形。
_BASS_808 = SynthParams(
    wave="sine", octave=-1, semitone=0, glide=0.05, unison=1, spread=0.0,
    amp=ADSR(attack=0.004, decay=0.45, sustain=0.88, release=0.26, curve=1.7),
    filter=FilterParams(kind="lp", cutoff=2600.0, resonance=0.8, env_amount=0.0),
    volume=0.95,
)

#: 更"长音"的一版：没有滑音，适合当持续的 sub（drill / 慢速 hip-hop）。
_BASS_808_SUSTAIN = SynthParams(
    wave="sine", octave=-1, semitone=0, glide=0.0, unison=1, spread=0.0,
    amp=ADSR(attack=0.012, decay=0.6, sustain=0.95, release=0.35, curve=1.4),
    filter=FilterParams(kind="lp", cutoff=2200.0, resonance=0.7, env_amount=0.0),
    volume=0.92,
)


@dataclass(frozen=True)
class SoundPack:
    """一个音色包：鼓件参数覆盖 + （可选）配套合成器预设 + 一键试听鼓型。"""

    key: str
    name: str
    summary: str
    icon: str
    accent: str
    #: 这个包会往套鼓里加进来的鼓件（都取自 ``PACK_KIT``）
    pieces: tuple[str, ...] = ()
    #: 鼓件名 → DrumVoice 的字段覆盖
    voices: dict[str, dict] = field(default_factory=dict)
    #: 配套的合成器音色（应用到合成器轨时使用）
    synth: SynthParams | None = None
    synth_name: str = ""
    #: 为什么这么调 —— 使用者最需要知道的一句话
    tip: str = ""

    def piece_names(self) -> list[str]:
        return [DRUM_NAMES.get(p, p) for p in self.pieces]

    def voice_of(self, piece: str):
        """构造这件鼓的 ``DrumVoice``（默认值 + 本包覆盖）。"""
        base = drums_mod.default_voices().get(piece)
        if base is None:
            from app.audio.project import DrumVoice

            base = DrumVoice()
        return replace(base, **self.voices.get(piece, {}))


SOUND_PACKS: tuple[SoundPack, ...] = (
    SoundPack(
        key="808",
        name="808 低音",
        summary="TR-808 的长尾底鼓 + 滑音 sub 贝斯，hip-hop / trap 的低频地基。",
        icon="speaker_high",
        accent="orange",
        pieces=("kick_808",),
        voices={
            "kick_808": _KICK_808_LONG,
            "kick": dict(level=0.95, tune=-1.0, decay=0.30, tone=0.32, snap=0.6, pan=0.0),
        },
        synth=_BASS_808,
        synth_name="808 滑音低音",
        tip="808 底鼓的尾巴就是贝斯 —— 别把它的 decay 调短，那是这套音色的全部",
    ),
    SoundPack(
        key="cowbell",
        name="牛铃组",
        summary="808 牛铃（窄带通）与拉丁牛铃（更低更长），一短一长配着用。",
        icon="bell",
        accent="yellow",
        pieces=("cowbell_808", "cowbell_latin"),
        voices={
            "cowbell_808": _COWBELL_808,
            "cowbell_latin": _COWBELL_LATIN,
            "cowbell": dict(level=0.4, tune=0.0, decay=0.24, tone=0.58, snap=0.5, pan=0.15),
        },
        tip="808 牛铃靠带通中心（2650Hz）立住辨识度；再亮就成镲了",
    ),
    SoundPack(
        key="808_kit",
        name="808 全套",
        summary="一整套 808 味道的鼓组：长尾底鼓 + 干脆军鼓 + 紧拍手 + 短踩镲 + 808 牛铃。",
        icon="drums",
        accent="pink",
        pieces=("kick_808", "cowbell_808", "cowbell_latin"),
        voices={
            "kick_808": _KICK_808_TIGHT,
            "kick": dict(level=1.0, tune=0.0, decay=0.26, tone=0.30, snap=0.55, pan=0.0),
            "snare": dict(level=0.80, tune=1.0, decay=0.17, tone=0.62, snap=0.72, pan=-0.05),
            "clap": dict(level=0.74, tune=0.0, decay=0.22, tone=0.58, snap=0.68, pan=0.18),
            "hat_closed": dict(level=0.5, tune=0.0, decay=0.045, tone=0.66, snap=0.55, pan=0.30),
            "hat_open": dict(level=0.42, tune=0.0, decay=0.30, tone=0.62, snap=0.45, pan=0.32),
            "rim": dict(level=0.58, tune=0.0, decay=0.10, tone=0.62, snap=0.72, pan=-0.18),
            "cowbell_808": _COWBELL_808,
            "cowbell_latin": _COWBELL_LATIN,
        },
        synth=_BASS_808,
        synth_name="808 滑音低音",
        tip="军鼓/拍手都调短了：808 那代机器的鼓几乎都没有尾巴，空隙留给低音",
    ),
    SoundPack(
        key="trap",
        name="Trap 808",
        summary="慢速长 sub + 极短踩镲 + 尖锐军鼓，现代 trap 的三件套。",
        icon="fire",
        accent="purple",
        pieces=("kick_808", "cowbell_808"),
        voices={
            "kick_808": dict(level=1.0, tune=-4.0, decay=1.35, tone=0.26, snap=0.30, pan=0.0),
            "snare": dict(level=0.82, tune=3.0, decay=0.13, tone=0.72, snap=0.80, pan=-0.04),
            "clap": dict(level=0.7, tune=0.0, decay=0.18, tone=0.64, snap=0.72, pan=0.16),
            "hat_closed": dict(level=0.46, tune=2.0, decay=0.032, tone=0.74, snap=0.5, pan=0.34),
            "cowbell_808": dict(level=0.38, tune=-1.0, decay=0.12, tone=0.5, snap=0.5, pan=0.22),
        },
        synth=_BASS_808_SUSTAIN,
        synth_name="808 长音低音",
        tip="Trap 的低音要又长又稳：滑音关掉、sustain 拉到 0.95",
    ),
)

_BY_KEY = {p.key: p for p in SOUND_PACKS}


def pack_by_key(key: str) -> SoundPack | None:
    return _BY_KEY.get(str(key))


def pack_keys() -> list[str]:
    return [p.key for p in SOUND_PACKS]


# --------------------------------------------------------------------------
# 应用：把包写进轨道
# --------------------------------------------------------------------------


def apply_pack(track: Track, pack: SoundPack) -> list[str]:
    """把音色包写进一条轨道，返回**真的被改动的部分**（鼓件名或 "synth"）。

    复制而不是共享：``replace`` / ``deepcopy`` 出来的是新对象，
    否则两条轨道会共用同一个 ``DrumVoice``，改一条另一条跟着变 ——
    这类 bug 在界面上表现为"调 A 轨，B 轨的音色也变了"，极难查。
    """
    applied: list[str] = []
    if track.kind == "drum":
        track.ensure_kit()
        for piece, fields in pack.voices.items():
            if not fields:
                continue
            existing = track.kit.get(piece)
            merged = replace(existing, **fields) if existing is not None \
                else replace(drums_mod.default_voices().get(piece) or _new_voice(), **fields)
            merged.enabled = True
            track.kit[piece] = merged
            applied.append(piece)
    elif pack.synth is not None:
        track.synth = copy.deepcopy(pack.synth)
        applied.append("synth")
    return applied


def _new_voice():
    from app.audio.project import DrumVoice

    return DrumVoice()


def make_track(project, pack: SoundPack, *, name: str = "", with_demo: bool = True,
               color: str = "") -> Track:
    """按音色包建一条新轨道（默认鼓轨，包里有合成器预设时也可建贝斯轨）。

    ``with_demo=True`` 会铺一小节示范鼓型 —— 音色包最要紧的是"立刻听见"，
    空的轨道听完什么也没有，等于没试。用户随后可以整段删掉或接着编。
    """
    label = name or pack.name
    if pack.pieces or pack.voices:
        track = Track(name=label, kind="drum", color=color or "#C1694F")
        # 默认套鼓打底，再让包覆盖：这样"包里没提到的鼓件"仍然有正常的声音，
        # 而不是一堆空鼓件（空鼓件在网格上点了没声，会被当成 bug）
        track.kit = {p: replace(v) for p, v in drums_mod.default_voices().items()}
        apply_pack(track, pack)
        for piece, step, velocity in demo_pattern(pack):
            track.add_note(Note(step=step, pitch=drum_to_pitch(piece),
                                length=1, velocity=velocity))
        return track

    track = Track(name=label, kind="synth", color=color or "#8E44AD")
    apply_pack(track, pack)
    return track


def demo_pattern(pack: SoundPack, available: set[str] | None = None
                 ) -> list[tuple[str, int, float]]:
    """一小节（16 步）的示范鼓型，用来"一键试听这个包"。

    为什么固定一小节、固定位置：这是试音色不是编曲，要让人在两秒内听出
    **这个包的底鼓有多长、牛铃有多亮** —— 所以底鼓落在 1/3、军鼓落在 5/13、
    踩镲走八分（力度交替，听起来才像人打的）、牛铃落在切分点上。

    ``available`` 不传时默认"默认套鼓 + 这个包加进来的鼓件"：
    包通常只覆盖几件鼓（例如"牛铃组"只动牛铃），但那条轨道上其余鼓件
    照样是完好的，示范鼓型没理由把它们空着。
    """
    have = set(DEFAULT_KIT) | set(pack.pieces) | set(pack.voices) \
        if available is None else {p for p in available}
    if not have:
        return []

    def pick(*candidates: str) -> str | None:
        for c in candidates:
            if c in have:
                return c
        return None

    kick = pick("kick_808", "kick")
    snare = pick("snare", "clap", "rim")
    hat = pick("hat_closed", "hat_open")
    bell = pick("cowbell_808", "cowbell_latin", "cowbell")

    out: list[tuple[str, int, float]] = []
    if kick:
        for step in (0, 6, 10):
            out.append((kick, step, 1.0))
    if snare:
        for step in (4, 12):
            out.append((snare, step, 0.85))
    if hat:
        for step in range(0, 16, 2):
            out.append((hat, step, 0.5 if step % 4 else 0.62))
    if bell:
        for step in (7, 14):
            out.append((bell, step, 0.7))
    return out


def describe(pack: SoundPack) -> str:
    """一行摘要，给状态栏/控制台用。"""
    bits = [pack.summary]
    if pack.pieces:
        bits.append("新增：" + "、".join(pack.piece_names()))
    if pack.synth is not None:
        bits.append("配套音色：" + (pack.synth_name or "合成器预设"))
    return " · ".join(bits)


__all__ = [
    "SoundPack", "SOUND_PACKS", "pack_by_key", "pack_keys", "apply_pack",
    "make_track", "demo_pattern", "describe",
]
