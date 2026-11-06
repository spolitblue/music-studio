"""音乐流水线的公共实现 —— 命令行、MCP 服务器、实时控制台共用同一份。

为什么要把 ``replicate_cli.py`` 里的东西搬出来
--------------------------------------------
原来那条流水线只存在于命令行脚本里，于是"想让别的入口（MCP、网页）也跑一遍"
就只剩复制粘贴一条路 —— 复制出来的第二份迟早和第一份不一样。
现在流程只在这里实现一次，``replicate_cli.py`` 是薄壳，MCP 服务器也是薄壳。

所有函数都接受一个可选的 ``live``（:class:`tools.live_hub.LiveHub`）：
传了就往实时控制台推事件，不传就安静地跑（命令行/测试用）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from app.audio import (audiodecode, dsp, mastering, render, replicate, sampler,
                       transcribe, wavio)
from app.audio.project import Effect, MasterParams, Note, Project
from app.ui.pages.composer import compose, params_from_profile

SR = render.SR


# ==========================================================================
# 实时控制台（可选）
# ==========================================================================


def _ev(live, method: str, *args, **kwargs):
    """往控制台推一个事件；没有控制台就当没这回事（返回 None）。"""
    if live is None:
        return None
    try:
        return getattr(live, method)(*args, **kwargs)
    except Exception:                                   # 控制台坏了不该拖垮渲染
        return None


@dataclass
class Progress:
    """把"阶段"包一层，省得每个调用点都手写 stage/progress/done 三连。"""

    live: object | None = None
    sid: str = ""
    label: str = ""

    def __enter__(self) -> "Progress":
        _ev(self.live, "stage", self.sid, self.label, frac=0.0)
        return self

    def __call__(self, frac: float, detail: str = "") -> None:
        _ev(self.live, "stage", self.sid, self.label, frac=float(frac), detail=detail)

    def done(self, detail: str = "") -> None:
        _ev(self.live, "stage", self.sid, self.label, frac=1.0, detail=detail, done=True)

    def __exit__(self, *exc) -> None:
        if exc[0] is None:
            self.done()


# ==========================================================================
# 读参考音频 / 分析
# ==========================================================================


def load_reference(path: str | Path, sr: int = SR, live=None) -> np.ndarray:
    """读参考音频。WAV 直接读；mp3 / m4a / flac 交给 QtMultimedia 解码。"""
    src = Path(path)
    _ev(live, "stage", "read", f"读取参考音频 · {src.name}", frac=0.1)
    if src.suffix.lower() == ".wav":
        audio, file_sr = wavio.load_wav(src)
        audio = np.asarray(audio, dtype=np.float32)
        if file_sr != sr:
            raise ValueError(f"参考音频采样率是 {file_sr} Hz，本工程引擎固定 {sr} Hz；"
                             f"请先用 tools/decode_audio.py --sr {sr} 转一道")
    else:
        audio, _ = audiodecode.decode(src, sr)          # 解码器顺手重采样到 sr
        audio = np.asarray(audio, dtype=np.float32)
    seconds = audio.shape[-1] / sr
    _ev(live, "log", f"参考音频：{src.name} · {audio.shape[0]} 声道 · {seconds:.1f}s")
    _ev(live, "metric", "参考时长", f"{seconds:.1f}s")
    _ev(live, "wave", audio, sr, label=f"参考音频 · {src.name}")
    _ev(live, "spectrum", audio, sr, label=f"参考频谱 · {src.name}")
    _ev(live, "stage", "read", f"读取参考音频 · {src.name}", frac=1.0, done=True)
    return audio


def analyze(audio: np.ndarray, *, bpm: float | None = None, live=None):
    """扒谱 + 指纹。返回 ``(TranscribeResult, ReferenceProfile)``。"""
    t0 = time.perf_counter()
    with Progress(live, "transcribe", "扒谱（起音 / 基频 / 鼓件）") as pr:
        res = transcribe.transcribe(audio, SR, bpm=bpm)
        pr(1.0, res.summary())
    _ev(live, "log", f"扒谱完成：{res.summary()}（{time.perf_counter() - t0:.1f}s）",
        level="ok")
    for line in res.log:
        _ev(live, "log", "  · " + line)

    with Progress(live, "profile", "量指纹（速度 / 调式 / 密度 / 明暗）") as pr:
        profile = replicate.analyze_reference(audio, SR, bpm=bpm, transcription=res)
        pr(1.0, profile.summary())
    for key, val in profile.metrics():
        _ev(live, "metric", key, val)
    _ev(live, "log", "指纹：" + profile.summary(), level="ok")
    return res, profile


# ==========================================================================
# 编排（段落）
# ==========================================================================

#: 明暗档位 —— 与 ``ReferenceProfile.brightness_label`` 同一组阈值
TIMBRE_PLAN: dict[str, dict] = {
    "暗": {"lead_wave": "triangle", "lead_cutoff": 1700.0, "lead_delay": 0.10,
           "lead_reverb": 0.34, "pad_cutoff": 1100.0, "pad_reverb": 0.52,
           "drum_tone": 0.62, "drum_decay": 0.88, "master_reverb": 0.78},
    "中性": {"lead_wave": "triangle", "lead_cutoff": 2600.0, "lead_delay": 0.16,
             "lead_reverb": 0.28, "pad_cutoff": 1700.0, "pad_reverb": 0.46,
             "drum_tone": 0.85, "drum_decay": 0.95, "master_reverb": 0.72},
    "偏亮": {}, "很亮": {},
}


def timbre_tier(brightness_hz: float) -> str:
    for edge, name in ((1200.0, "暗"), (2600.0, "中性"), (4500.0, "偏亮")):
        if brightness_hz < edge:
            return name
    return "很亮"


def section_params(profile, *, bars: int, sections: int, seed: int) -> list:
    """逐段的生成参数（linear 形式）：每段换种子，首尾两段压密度当引子与收尾。"""
    out = []
    for s in range(sections):
        params = params_from_profile(profile, bars=bars, seed=seed + s, palette_offset=0)
        if sections > 2:
            if s == 0:
                params.density = float(np.clip(params.density * 0.80, 0.05, 1.0))
            elif s == sections - 1:
                params.density = float(np.clip(params.density * 0.72, 0.05, 1.0))
        out.append(params)
    return out


#: 三段式（A–B–A）里每个部分的性格。和声：A 用一套走向，B 换一套，最后回到 A。
#: 密度：B 的第一段收一点（对比进来时留白），最后一段再收（收尾）。
FORM_DENSITY: dict[str, tuple[float, ...]] = {
    "A": (0.85, 1.00, 1.00, 1.00),
    "B": (0.78, 1.00, 1.00, 1.00),
}


def section_plan(profile, *, bars: int, sections: int, seed: int,
                 form: str = "linear") -> tuple[list, list[str]]:
    """按曲式排出每一段的生成参数，并给出每段属于哪个部分。

    ``linear``：现在的做法，每段换一个种子（走向一路变下去）。
    ``three-part``：**三段式 A–B–A** —— 把段落三等分，A 段用同一套和弦走向
    （因此"再现"是真的再现），B 段换走向做对比，旋律细节按段给不同的种子。
    倒数第一段密度压到 0.72 收尾，B 段首段压到 0.78 让对比进来时有留白。
    """
    if form != "three-part" or sections < 3:
        return section_params(profile, bars=bars, sections=sections, seed=seed), \
            ["" for _ in range(sections)]

    third = max(1, sections // 3)
    parts = ["A"] * third + ["B"] * third + ["A"] * (sections - 2 * third)
    progression_seed = {"A": seed, "B": seed + 1}
    out = []
    seen: dict[str, int] = {}
    for i, part in enumerate(parts):
        k = seen.get(part, 0)
        seen[part] = k + 1
        # 细节种子与"走向种子"分开：同一个部分（同一套和声）也能写出不同的旋律
        params = params_from_profile(profile, bars=bars, seed=seed + i * 3 + (0 if part == "A" else 1),
                                     palette_offset=0)
        params.degrees = tuple(profile.pick_progression(progression_seed[part]))
        table = FORM_DENSITY[part]
        scale = table[min(k, len(table) - 1)]
        if i == sections - 1:
            scale = min(scale, 0.72)
        params.density = float(np.clip(params.density * scale, 0.05, 1.0))
        out.append(params)
    return out, parts


def section_of_step(step: int, *, section_bars: int, parts: list[str],
                    steps_per_bar: int) -> str:
    """一个音符落在哪一段（哪一部分）—— MC 模式按部分换乐器要靠它。"""
    if not parts or section_bars <= 0:
        return ""
    idx = int(step) // (int(section_bars) * steps_per_bar)
    if idx < 0 or idx >= len(parts):
        return parts[-1] if parts else ""
    return parts[idx]


def build_project(profile, params_list, *, name: str, total_bars: int,
                  section_bars: int) -> tuple[Project, list]:
    """空工程 + 指纹给出的速度/摇摆；多段落**并进同样四条轨道**。

    ``total_bars`` 是工程总长，``section_bars`` 是每段长度 —— 两者必须分开传：
    第一版把总长当每段长度算偏移，第 2 段起的音符全落到工程末尾之外，
    渲染出来中间一大段是**静音**（渲染器不会为越界音符报错）。
    """
    project = Project(
        name=name,
        author="Music Studio · 复刻",
        bpm=float(profile.bpm),
        bars=int(total_bars),
        swing=float(np.clip(profile.swing, 0.0, 0.6)),
        master=MasterParams(
            volume=0.92,
            reverb=Effect.reverb(size=0.70, damping=0.48, mix=1.0, predelay=0.022,
                                 width=1.0),
            delay=Effect.delay(time_beats=0.375, feedback=0.32, mix=1.0,
                               ping_pong=True, tone=4200.0),
            limiter_threshold=-1.0,
            limiter_release=0.12,
            fade_out=1.6,
        ),
    )
    results = []
    slots: dict[str, object] = {}
    for section, params in enumerate(params_list):
        result = compose(project, params)
        results.append(result)
        offset = section * int(section_bars) * project.steps_per_bar
        for track in result.tracks:
            if section == 0:
                project.add_track(track)
                slots[track.name] = track
                continue
            host = slots.get(track.name)
            if host is None:
                project.add_track(track)
                slots[track.name] = track
                continue
            for note in track.notes:
                host.notes.append(
                    type(note)(step=int(note.step) + offset, pitch=int(note.pitch),
                               length=int(note.length), velocity=float(note.velocity)))
    return project, results


def apply_timbre(project: Project, profile, live=None) -> dict:
    """按参考音频的**明暗**调音色（生成器只认速度/调式/密度/走向，音色是写死的）。"""
    tier = timbre_tier(float(profile.brightness_hz))
    plan = TIMBRE_PLAN.get(tier) or {}
    if not plan:
        return {"明暗档": tier, "处理": "原样"}
    touched: list[str] = []
    for track in project.tracks:
        if track.kind == "drum":
            for voice in (track.kit or {}).values():
                voice.tone = float(np.clip(voice.tone * plan["drum_tone"], 0.05, 1.0))
                voice.decay = float(voice.decay * plan["drum_decay"])
            touched.append(f"{track.name}(鼓件调暗)")
            continue
        synth = getattr(track, "synth", None)
        if synth is None:
            continue
        if track.name == "AI 旋律":
            synth.wave = plan["lead_wave"]
            synth.filter.cutoff = plan["lead_cutoff"]
            track.send_reverb = plan["lead_reverb"]
            for fx in track.effects:
                if fx.kind == "delay":
                    fx.params["mix"] = plan["lead_delay"]
            touched.append(f"{track.name}({plan['lead_wave']}·{plan['lead_cutoff']:.0f}Hz)")
        elif track.name == "AI 和声":
            synth.filter.cutoff = plan["pad_cutoff"]
            track.send_reverb = plan["pad_reverb"]
            touched.append(f"{track.name}(低通 {plan['pad_cutoff']:.0f}Hz)")
    project.master.reverb.params["size"] = plan["master_reverb"]
    _ev(live, "log", f"音色映射（{tier}）：{'、'.join(touched) or '无匹配轨道'}")
    return {"明暗档": tier, "处理": "、".join(touched) or "无匹配轨道"}


def check_notes_in_range(project: Project) -> dict:
    """音符是不是都在工程长度内。越界的音符渲染器不会报错，只会静默丢掉。"""
    steps = [int(n.step) for tr in project.tracks for n in tr.notes]
    info = {"音符": len(steps), "最远 step": max(steps) if steps else 0,
            "工程步数": project.total_steps,
            "越界": sum(1 for s in steps if s >= project.total_steps)}
    if info["越界"]:
        raise ValueError(f"有 {info['越界']} 个音符落在工程之外（最远 {info['最远 step']}）——"
                         f"渲染会静默丢掉，先修编排再出音频")
    return info


# ==========================================================================
# 采样：从参考音频里抠出真实音色
# ==========================================================================


def sample_reference(reference: str | Path, out_dir: str | Path, *, live=None,
                     max_seconds: float = 90.0) -> dict:
    """从参考音频里抠一套音色（鼓件单件 + 一个乐音样本），落成 WAV + kit.json。"""
    out = Path(out_dir)
    audio = load_reference(reference, SR, live=live)
    with Progress(live, "sample", "从参考里抠样本（起音 → 分类 → 挑最干净的一下）") as pr:
        kit = sampler.extract_kit(audio, SR, max_seconds=max_seconds, live=live)
        pr(1.0, kit.summary())
    kit.source = str(reference)
    samples_dir = out / "samples"
    sampler.save_kit(kit, samples_dir, live=live)
    _ev(live, "file", samples_dir / "kit.json", label="样本清单 kit.json", kind="json")
    _ev(live, "log", "采样完成：" + kit.summary(), level="ok")
    _ev(live, "metric", "样本数", len(kit.shots))
    for name, shot in kit.shots.items():
        _ev(live, "metric", f"样本·{name}",
            f"{shot.seconds * 1000:.0f}ms" + (f" · MIDI {shot.pitch:.0f}"
                                              if shot.pitch else ""))
    # 一段"样本展示"：把每个样本按顺序各放两遍，方便人耳确认抠得对不对
    order = [n for n in sampler.CUT_SECONDS if n in kit.shots]
    if kit.tone is not None:
        order.append("tone")
    if order:
        step = 1.1
        demo = sampler.render_hits(
            [(i * step, name, 0.9) for i, name in enumerate(order) for _ in (0, 1)
             if True], kit, seconds=len(order) * step + 2.5, gain=0.9)
        demo_path = samples_dir / "kit_demo.wav"
        wavio.save_wav(demo_path, np.stack([demo, demo]), SR, bit_depth=24)
        _ev(live, "file", demo_path, label="样本展示（按顺序各两遍）")
    return {"kit": kit, "dir": str(samples_dir), "summary": kit.summary(),
            "samples": {k: {"seconds": v.seconds, "pitch": v.pitch, "meta": v.meta}
                        for k, v in kit.shots.items()}}


# ==========================================================================
# 渲染 / 体检
# ==========================================================================


def render_all(project: Project, *, live=None, sid: str = "render",
               label: str = "渲染整曲") -> tuple[np.ndarray, object]:
    """渲染整曲，并把逐轨进度推到控制台。"""
    t0 = time.perf_counter()

    def _progress(frac: float, text: str) -> None:
        _ev(live, "stage", sid, label, frac=float(frac), detail=text)

    _ev(live, "stage", sid, label, frac=0.0)
    stereo, stats = render.render_project(project, SR, progress=_progress)
    _ev(live, "stage", sid, label, frac=1.0,
        detail=f"{time.perf_counter() - t0:.1f}s", done=True)
    return stereo, stats


def raw_peak_db(project: Project, live=None) -> float:
    """关掉母带限幅器渲一遍，量"进限幅器之前"的峰值。"""
    saved = project.master.limiter_enabled
    project.master.limiter_enabled = False
    stereo, _ = render.render_project(project, SR)
    project.master.limiter_enabled = saved
    mono = stereo.mean(axis=0) if stereo.ndim == 2 else stereo
    return float(dsp.gain_to_db(max(float(np.max(np.abs(mono))), 1e-12)))


def audio_health(stereo: np.ndarray, sr: int = SR) -> dict:
    """削波 / 直流 / 单声道兼容 / 静音占比。"""
    peak = float(np.max(np.abs(stereo))) if stereo.size else 0.0
    rms = float(np.sqrt(np.mean(stereo ** 2))) if stereo.size else 0.0
    dc = float(np.mean(stereo)) if stereo.size else 0.0
    corr = 0.0
    if stereo.ndim == 2 and stereo.shape[0] >= 2 and stereo.size:
        a, b = stereo[0], stereo[1]
        if np.std(a) > 1e-9 and np.std(b) > 1e-9:
            corr = float(np.corrcoef(a, b)[0, 1])
    clipped = int(np.sum(np.abs(stereo) >= 0.999)) if stereo.size else 0
    silent = 0.0
    if stereo.ndim == 2 and stereo.shape[1] > sr:
        n = (stereo.shape[1] // sr) * sr
        win = np.sqrt(np.mean(stereo[:, :n].reshape(stereo.shape[0], -1, sr) ** 2,
                              axis=(0, 2)))
        silent = float(np.mean(win < 1e-3))
    mono = stereo.mean(axis=0) if stereo.ndim == 2 else stereo
    mono_peak = float(np.max(np.abs(mono))) if mono.size else 0.0
    mono_rms = float(np.sqrt(np.mean(mono ** 2))) if mono.size else 0.0
    crest = float(dsp.gain_to_db(max(mono_peak, 1e-12))
                  - dsp.gain_to_db(max(mono_rms, 1e-12)))
    return {
        "peak_db": float(dsp.gain_to_db(max(peak, 1e-9))),
        "rms_db": float(dsp.gain_to_db(max(rms, 1e-9))),
        "crest_db": crest,          # 混单声道、不归一化：直接就是峰均比
        "dc": dc,
        "stereo_corr": corr,
        "clipped_samples": clipped,
        "silent_ratio": silent,
        "seconds": float(stereo.shape[1] / sr) if stereo.ndim == 2 else 0.0,
    }


def compare_profiles(ref, rep) -> list[tuple[str, str, str]]:
    """参考 vs 成品：同一套分析器量两遍。"""
    return [
        ("速度", f"{ref.bpm:.1f} BPM", f"{rep.bpm:.1f} BPM"),
        ("调式", ref.key_name, rep.key_name),
        ("起音密度", f"{ref.density * 100:.0f}%", f"{rep.density * 100:.0f}%"),
        ("明暗", f"{ref.brightness_hz:.0f} Hz", f"{rep.brightness_hz:.0f} Hz"),
        ("鼓占比", f"{ref.drum_ratio * 100:.0f}%", f"{rep.drum_ratio * 100:.0f}%"),
        ("摇摆", f"{ref.swing:.2f}", f"{rep.swing:.2f}"),
        ("响度", f"{ref.energy_db:+.1f} dBFS", f"{rep.energy_db:+.1f} dBFS"),
    ]


def stem_probe(project: Project) -> dict:
    """单独量鼓轨分轨 —— 整曲混音量不准速度与鼓占比（本工程已知边界）。"""
    drum = next((t for t in project.tracks if t.kind == "drum"), None)
    if drum is None:
        return {"有鼓轨": False}
    audio = render.render_track(drum, project)
    res = transcribe.transcribe(audio, SR)
    prof = replicate.analyze_reference(audio, SR, transcription=res)
    return {"有鼓轨": True, "轨名": drum.name, "bpm": float(res.bpm),
            "鼓占比": float(prof.drum_ratio), "事件": len(res.drums) + len(res.pitched),
            "明暗_hz": float(prof.brightness_hz)}


# ==========================================================================
# 混音：把"合成总线 + 采样鼓 + 低频层"按测出来的比例拼起来
# ==========================================================================

#: 编曲助手的鼓件 → 抠出来的样本。**没有对应样本的鼓件直接不发声**。
#:
#: 为什么不是"缺什么就用相近的顶替"：这首参考音频里根本没有军鼓与踩镲
#: （抠出来的只有 kick / kick_808 / cowbell / tone —— 它的打击乐是低频闷响加铃铛），
#: 于是硬套一个摇滚鼓型（军鼓在 2、4，踩镲每八分一记）正是"听着猛、听着难受"
#: 的主要来源。宁可少一件，也不要用错的音色把错的律动做响。
PIECE_MAP: dict[str, str] = {
    "kick": "kick", "kick_808": "kick_808",
    "tom_low": "kick_808", "tom_mid": "kick_808", "tom_high": "kick_808",
    "hat_closed": "cowbell", "hat_pedal": "cowbell", "hat_open": "cowbell",
    "ride": "cowbell", "cowbell": "cowbell", "cowbell_808": "cowbell",
    "snare": "", "clap": "", "rim": "", "crash": "",
}

#: 采样鼓件的音量系数（相对编曲助手给的力度）。踩镲转成的铃铛要明显退后。
PIECE_GAIN: dict[str, float] = {"kick": 0.92, "kick_808": 0.85, "cowbell": 0.34}
#: 声像：低频居中，铃铛偏右（参考里的雪橇铃就在右边）
PIECE_PAN: dict[str, float] = {"kick": 0.0, "kick_808": -0.08, "cowbell": 0.22}

#: 样本缺失时的退路（按优先级）。**不跨类乱顶**：底鼓找不到就用另一个低频闷响，
#: 但绝不会拿铃铛去当底鼓 —— 那正是"听着难受"的来源之一。
PIECE_FALLBACK: dict[str, tuple[str, ...]] = {
    "kick": ("kick", "kick_808"),
    "kick_808": ("kick_808", "kick"),
    "cowbell": ("cowbell", "hat_closed", "snare", "clap"),
}


def resolve_piece(target: str, kit) -> str | None:
    """在样本库里找 target 的可用替代（找不到就返回 None，宁缺毋滥）。"""
    for name in PIECE_FALLBACK.get(target, (target,)):
        if name in kit:
            return name
    return target if target in kit else None


# ---- Minecraft 音符盒那套映射 -------------------------------------------
#
# 音符盒的鼓是软的三件（底鼓/军鼓/踩镲），乐器是竖琴/铃/木琴那几件。
# 编曲助手的鼓件在这里**全部有对应**（不像参考采样那一套缺军鼓踩镲），
# 所以律动可以原样保留 —— 换的是音色，不是节奏。
MC_PIECE_MAP: dict[str, str] = {
    "kick": "basedrum", "kick_808": "basedrum",
    "tom_low": "basedrum", "tom_mid": "basedrum", "tom_high": "basedrum",
    "snare": "snare", "clap": "snare", "rim": "snare",
    "hat_closed": "hat", "hat_pedal": "hat", "hat_open": "hat",
    "ride": "hat", "crash": "hat",
    "cowbell": "cow_bell", "cowbell_808": "cow_bell", "cowbell_latin": "cow_bell",
}
MC_PIECE_GAIN: dict[str, float] = {"basedrum": 0.95, "snare": 0.5, "hat": 0.34,
                                   "cow_bell": 0.45}
MC_PIECE_PAN: dict[str, float] = {"basedrum": 0.0, "snare": -0.06, "hat": 0.26,
                                  "cow_bell": 0.3}

#: 四个声部各用哪件音符盒乐器。旋律默认用铃（冬季岛的叮当感），
#: 可以换 chime / xylophone / iron_xylophone / flute / pling / bit。
MC_PART_INSTRUMENT: dict[str, str] = {
    "AI 贝斯": "bass", "AI 和声": "harp", "AI 旋律": "bell",
}


@dataclass
class MixSettings:
    """一次混音的全部决定 —— 每个数都有出处，不藏在代码里。"""

    kit_source: str = "reference"   # reference（从参考抠）| minecraft（音符盒音色）
    drums: str = "sampled"          # sampled | native（引擎鼓机）| off
    lead: str = "synth"             # synth（减法合成）| sampled（音色样本）
    drum_level_db: float = -7.0     # 采样鼓相对合成总线的 RMS
    sub_level_db: float = -8.0      # 低频层相对合成总线
    sampled_lead_db: float = -3.5   # 采样主音相对合成总线
    tilt_max_db: float = 10.0       # 频谱摆平的最大修正量
    #: 母带目标峰值。默认 -3.5（而不是 -2）：这就是"减弱混音"最直接的一刀 ——
    #: 峰值低 1.5 dB，限幅器更不容易介入，整体听感也退后一步。
    target_peak_db: float = -3.5
    limiter_db: float = -1.0

    #: Minecraft 模式：四个声部全部用音符盒样本演奏，不再走合成总线。
    #: 每条给一个相对电平（dB），另有混响量。
    #: **默认值按"减弱混音"重定过**：鼓退后、主音退后、总峰值留更多余量。
    mc_part_level_db: dict = field(default_factory=lambda: {
        "AI 鼓组": -8.5, "AI 贝斯": -3.0, "AI 和声": -9.0, "AI 旋律": -6.5,
    })
    mc_instrument: dict = field(default_factory=lambda: dict(MC_PART_INSTRUMENT))
    mc_reverb: float = 0.18
    #: 演奏强度：全部音符力度乘这个值。0.85 是"弱下来"的第一步 ——
    #: 力度低了峰值就不顶着限幅器，动态（峰均比）才回得来。
    velocity_scale: float = 0.85
    #: 踩镲稀疏化：只留小节内 step 是它整数倍的踩镲（0/2 = 全留，4 = 只留四分音符）。
    #: 编曲助手原本每八分一记，是"听着密"的主要来源。
    hat_keep_steps: int = 4
    #: 三段式：每段属于哪个部分（A/B/A），以及按部分换乐器（例 {"A":"bell","B":"pling"}）
    form_parts: list = field(default_factory=list)
    section_bars: int = 0
    mc_lead_by_part: dict = field(default_factory=dict)
    #: 音符盒本身就是"亮而脆"的音色，摆平到参考那种暗调会把它的性格洗掉 ——
    #: 所以 mc 模式只做很小的修正（实测 1k-3k 比参考高 10 dB，但那是音符盒的本色）
    mc_tilt_max_db: float = 4.0

    #: 合成总线上各声部的电平修正（dB）。**这一栏才是"混音太猛"的正解** ——
    #: 母带摆平只能动 ±10 dB 且是事后补救；实测成品的 300Hz~3kHz 比参考高出
    #: 9~13 dB，说明和声铺底与主音相对贝斯太响了，得从声部比例上压下去。
    track_gain_db: dict = field(default_factory=lambda: {
        "AI 和声": -10.0, "AI 旋律": -8.0, "AI 贝斯": 2.5,
    })

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in
                ("kit_source", "drums", "lead", "drum_level_db", "sub_level_db",
                 "sampled_lead_db", "tilt_max_db", "target_peak_db", "limiter_db",
                 "track_gain_db", "mc_part_level_db", "mc_instrument", "mc_reverb",
                 "mc_tilt_max_db", "velocity_scale", "hat_keep_steps",
                 "form_parts", "section_bars", "mc_lead_by_part")}


def _stereo(mono: np.ndarray, pan: float = 0.0, sr: int = SR) -> np.ndarray:
    """单声道 → 立体声，按等功率声像。长度不足就补零，超出就截断。"""
    gl, gr = dsp.equal_power_pan(float(np.clip(pan, -1.0, 1.0)))
    return np.stack([mono * gl, mono * gr]).astype(np.float32)


def drum_events(project: Project, *, kit, live=None) -> tuple[list, dict]:
    """把工程里的鼓音符翻成"采样事件"（时刻 / 样本名 / 力度），并统计映射情况。"""
    stats: dict[str, int] = {}
    dropped: dict[str, int] = {}
    events: list[tuple[float, str, float]] = []
    for track in project.tracks:
        if track.kind != "drum":
            continue
        for note in track.sorted_notes():
            piece = project_pitch_to_drum(note.pitch)
            target = PIECE_MAP.get(piece, "")
            hit = resolve_piece(target, kit) if target else None
            if hit is None:
                dropped[piece] = dropped.get(piece, 0) + 1
                continue
            vel = float(note.velocity) * PIECE_GAIN.get(hit, 0.8)
            events.append((project.step_time(note.step), hit, vel))
            stats[f"{piece}→{hit}"] = stats.get(f"{piece}→{hit}", 0) + 1
    events.sort(key=lambda e: e[0])
    if live is not None:
        live.log("采样鼓映射：" + ("、".join(f"{k}×{v}" for k, v in sorted(stats.items()))
                                   or "无") +
                 (f" · 丢弃 {sum(dropped.values())} 个（{'、'.join(sorted(dropped))}）"
                  if dropped else ""))
    return events, {"映射": stats, "丢弃": dropped}


def project_pitch_to_drum(pitch: int) -> str:
    from app.audio.project import pitch_to_drum
    return pitch_to_drum(int(pitch))


def engine_bus(project: Project, *, mute_kinds: tuple[str, ...] = ("drum",),
               mute_names: tuple[str, ...] = (), track_gain_db: dict | None = None,
               live=None, sid: str = "bus",
               label: str = "渲染合成总线") -> np.ndarray:
    """渲染"引擎那部分"：鼓轨（和可选的采样主音轨）静音、母带限幅关掉、总线音量 1.0。

    为什么要把限幅关掉、音量设 1：后面还要往上叠采样鼓与低频层，
    此时任何总线处理都会让后面的比例失真 —— 母带只在**最后**做一次。

    ``track_gain_db`` 是**声部比例**：这是"混音太猛"的正解。实测成品在
    300Hz~3kHz 比参考高 9~13 dB，根因是铺底与主音相对贝斯太响 ——
    母带摆平只能事后拧 ±10 dB，声部比例才是源头。
    """
    saved: list[tuple[bool, float]] = []
    for track in project.tracks:
        saved.append((track.mute, track.volume))
        if track.kind in mute_kinds or track.name in mute_names:
            track.mute = True
        elif track_gain_db and track.name in track_gain_db:
            track.volume = float(track.volume) * float(dsp.db_to_gain(track_gain_db[track.name]))
    saved_vol = project.master.volume
    saved_lim = project.master.limiter_enabled
    project.master.volume = 1.0
    project.master.limiter_enabled = False
    try:
        stereo, _ = render_all(project, live=live, sid=sid, label=label)
    finally:
        for track, (mute, volume) in zip(project.tracks, saved):
            track.mute = mute
            track.volume = volume
        project.master.volume = saved_vol
        project.master.limiter_enabled = saved_lim
    return stereo


def sampled_drum_stem(project: Project, kit, *, seconds: float, live=None) -> tuple[np.ndarray, dict]:
    """用抠出来的样本重放鼓组（分件声像后相加）。"""
    events, stats = drum_events(project, kit=kit, live=live)
    buf = np.zeros((2, int(seconds * SR) + SR), dtype=np.float32)
    for t, name, vel in events:
        shot = kit.get(name)
        if shot is None:
            continue
        mono = np.zeros(buf.shape[1], dtype=np.float32)
        sampler.play_into(mono, shot, t, gain=float(vel), sr=SR)
        buf += _stereo(mono, PIECE_PAN.get(name, 0.0))
    return buf, stats


def sampled_lead_stem(project: Project, kit, *, seconds: float, live=None
                      ) -> tuple[np.ndarray, dict]:
    """用参考里抠出来的**乐音样本**演奏主旋律（按音高重采样）。"""
    shot = kit.tone
    if shot is None:
        return np.zeros((2, int(seconds * SR) + SR), dtype=np.float32), {"音符": 0}
    lead = next((t for t in project.tracks if t.name == "AI 旋律"), None)
    if lead is None:
        return np.zeros((2, int(seconds * SR) + SR), dtype=np.float32), {"音符": 0}
    buf = np.zeros((2, int(seconds * SR) + SR), dtype=np.float32)
    step_s = project.seconds_per_step
    n = 0
    for note in lead.sorted_notes():
        t = project.step_time(note.step)
        ring = max(0.12, min(shot.seconds, note.length * step_s + 0.35))
        cut = int(ring * SR)
        seg = shot.resampled(2.0 ** ((int(note.pitch) - float(shot.pitch or 60)) / 12.0))
        seg = seg[:cut].copy()
        if seg.size > 8:                       # 尾部淡出，免得被硬切出"咔"
            k = min(seg.size // 4, int(0.02 * SR))
            seg[-k:] *= np.linspace(1.0, 0.0, k, dtype=np.float32)
        mono = np.zeros(buf.shape[1], dtype=np.float32)
        sampler.play_into(mono, type(shot)(name="x", samples=seg, sr=SR),
                          t, gain=float(note.velocity), sr=SR)
        buf += _stereo(mono, float(lead.pan))
        n += 1
    if live is not None:
        live.log(f"采样主音：{n} 个音符用参考抠出的音色演奏（根音 MIDI {shot.pitch}）")
    return buf, {"音符": n, "根音": shot.pitch}


def sub_stem(project: Project, *, seconds: float) -> np.ndarray:
    """低频层：跟着贝斯轨走，低一个八度的正弦长音，专门补 20-100Hz。

    为什么要单独加：实测参考音频在 20-100Hz 的电平是 -24.0 dBFS，
    而我的成品只有 -30.6 dBFS —— 相对各自的总电平差 **16 dB**。
    成品的中低频被垫得很满（300Hz-3kHz 高出参考 9~13 dB），低频却是空的，
    于是听感就是"又薄又吵"。合成的贝斯受低通与音区限制填不满这一段，
    所以补一层正弦 —— 这是混音动作，不是音色设计。
    """
    bass = next((t for t in project.tracks if t.name == "AI 贝斯"), None)
    if bass is None:
        return np.zeros((2, int(seconds * SR) + SR), dtype=np.float32)
    from app.audio.project import ADSR, FilterParams, SynthParams
    from app.audio import synth as synth_engine
    sub = SynthParams(wave="sine", unison=1, amp=ADSR(0.02, 0.12, 0.88, 0.25, 1.4),
                      filter=FilterParams(kind="lp", cutoff=120.0, resonance=0.4),
                      volume=1.0)
    buf = np.zeros((2, int(seconds * SR) + SR), dtype=np.float32)
    step_s = project.seconds_per_step
    seen: set[int] = set()
    for note in bass.sorted_notes():
        if int(note.step) in seen:             # 同一步只补一个（和弦不叠低频）
            continue
        seen.add(int(note.step))
        dur = max(0.25, note.length * step_s)
        tone = synth_engine.render_synth_note(sub, int(note.pitch) - 12,
                                              min(1.0, note.velocity), dur, SR)
        start = int(project.step_time(note.step) * SR)
        take = min(tone.shape[1], buf.shape[1] - start)
        if take > 0:
            buf[:, start:start + take] += tone[:, :take]
    return buf


def sampled_track_stem(project: Project, track, shot, *, seconds: float, sr: int = SR,
                       ring_extra: float = 0.35, max_ring: float | None = None,
                       velocity_scale: float = 1.0,
                       only_steps: tuple[int, int] | None = None) -> np.ndarray:
    """用一个样本演奏一条轨道的全部音符（按音高重采样）。

    音符盒是**短促的样本**（40~800ms），所以这里按"音符时值 + 尾巴"截断并淡出，
    而不是让长和弦一路嗡嗡响 —— 那正是琶音与糊成一片的区别。

    ``only_steps`` 给一个 ``[起, 止)`` 的 step 区间，只渲染落在里面音符
    （三段式里"哪一段用哪件乐器"就靠它）。
    """
    buf = np.zeros((2, int(seconds * sr) + sr), dtype=np.float32)
    root = float(shot.pitch) if shot.pitch is not None else None
    step_s = project.seconds_per_step
    ring_cap = float(max_ring) if max_ring else shot.seconds
    for note in track.sorted_notes():
        if only_steps is not None and not (only_steps[0] <= int(note.step) < only_steps[1]):
            continue
        t = project.step_time(note.step)
        ring = max(0.10, min(ring_cap, note.length * step_s + ring_extra))
        if root is None:
            seg = shot.samples[: int(ring * sr)].copy()
        else:
            seg = shot.resampled(2.0 ** ((int(note.pitch) - root) / 12.0))[: int(ring * sr)].copy()
        if seg.size > 8:
            k = min(seg.size // 3, int(0.015 * sr))
            if k > 1:
                seg[-k:] *= np.linspace(1.0, 0.0, k, dtype=np.float32)
        mono = np.zeros(buf.shape[1], dtype=np.float32)
        sampler.play_into(mono, sampler.OneShot(name="tmp", samples=seg, sr=sr), t,
                          gain=float(note.velocity) * float(velocity_scale), sr=sr)
        buf += _stereo(mono, float(track.pan))
    return buf


def mc_drum_stem(project: Project, kit, *, seconds: float, live=None,
                 velocity_scale: float = 1.0, hat_keep_steps: int = 0
                 ) -> tuple[np.ndarray, dict]:
    """用音符盒的鼓三件（底鼓/军鼓/踩镲）重放鼓组。

    ``hat_keep_steps`` 把踩镲稀疏化：只保留小节内 step 为它整数倍的踩镲。
    编曲助手原本每八分一记，而音符盒的踩镲很亮，密起来就是"吵"。
    """
    stats: dict[str, int] = {}
    dropped: dict[str, int] = {}
    thinned = 0
    buf = np.zeros((2, int(seconds * SR) + SR), dtype=np.float32)
    count = 0
    spb = max(1, project.steps_per_bar)
    for track in project.tracks:
        if track.kind != "drum":
            continue
        for note in track.sorted_notes():
            piece = project_pitch_to_drum(note.pitch)
            hit = MC_PIECE_MAP.get(piece)
            shot = kit.get(hit) if hit else None
            if shot is None:
                dropped[piece] = dropped.get(piece, 0) + 1
                continue
            if hit == "hat" and hat_keep_steps >= 2 and int(note.step) % int(hat_keep_steps) != 0:
                thinned += 1
                continue
            vel = float(note.velocity) * MC_PIECE_GAIN.get(hit, 0.6) * float(velocity_scale)
            mono = np.zeros(buf.shape[1], dtype=np.float32)
            sampler.play_into(mono, shot, project.step_time(note.step), gain=vel, sr=SR)
            buf += _stereo(mono, MC_PIECE_PAN.get(hit, 0.0))
            stats[f"{piece}→{hit}"] = stats.get(f"{piece}→{hit}", 0) + 1
            count += 1
    if live is not None:
        live.log(f"音符盒鼓：{count} 击 · " +
                 "、".join(f"{k}×{v}" for k, v in sorted(stats.items()))
                 + (f" · 踩镲稀疏化掉 {thinned} 击（只留每 {hat_keep_steps} 步）"
                    if thinned else "")
                 + (f" · 丢弃 {sum(dropped.values())}" if dropped else ""))
    return buf, {"计数": count, "映射": stats, "丢弃": dropped, "踩镲稀疏化": thinned}


def mc_mix(project: Project, kit, *, settings: MixSettings, live=None
           ) -> tuple[np.ndarray, dict]:
    """Minecraft 音符盒模式：四个声部全部由音符盒样本演奏。

    为什么连贝斯与和声也换成音符盒：混一半（鼓用音符盒、贝斯还用减法合成）
    听起来像两首曲子叠在一起 —— 音色体系必须成套。
    """
    seconds = project.duration_seconds()
    report: dict = {"mode": "minecraft", "parts": {}, "form": list(settings.form_parts)}
    layers: list[np.ndarray] = []

    drums, drum_info = mc_drum_stem(project, kit, seconds=seconds, live=live,
                                    velocity_scale=settings.velocity_scale,
                                    hat_keep_steps=settings.hat_keep_steps)
    report["parts"]["鼓"] = drum_info
    if float(np.sqrt(np.mean(drums ** 2))) > 1e-9:
        layers.append(("AI 鼓组", drums))

    spb = max(1, project.steps_per_bar)
    for track in project.tracks:
        if track.kind == "drum":
            continue
        instrument = settings.mc_instrument.get(track.name)
        if not instrument:
            continue
        # 三段式按部分换乐器：同一轨音符切成几块，各自用不同样本演奏
        by_part = settings.mc_lead_by_part if track.name == "AI 旋律" else None
        if by_part and settings.form_parts and settings.section_bars:
            chunks: dict[str, list[tuple[int, int]]] = {}
            for i, part in enumerate(settings.form_parts):
                lo = i * settings.section_bars * spb
                chunks.setdefault(part, []).append((lo, lo + settings.section_bars * spb))
            made = 0
            for part, ranges in chunks.items():
                shot = kit.get(by_part.get(part, instrument))
                if shot is None:
                    continue
                in_part = 0
                for note in track.notes:
                    if any(lo <= int(note.step) < hi for lo, hi in ranges):
                        in_part += 1
                stem = np.zeros((2, int(seconds * SR) + SR), dtype=np.float32)
                for lo, hi in ranges:
                    stem += sampled_track_stem(
                        project, track, shot, seconds=seconds,
                        ring_extra=0.30, velocity_scale=settings.velocity_scale,
                        only_steps=(lo, hi))
                if float(np.sqrt(np.mean(stem ** 2))) > 1e-9:
                    layers.append((f"{track.name}·{part}", stem))
                made += 1
                _ev(live, "log", f"{track.name} 第 {part} 部分 → 音符盒「{shot.name}」"
                                 f"（{in_part} 音符）")
            report["parts"][track.name] = {"by_part": dict(by_part), "chunks": made}
            continue

        shot = kit.get(instrument)
        if shot is None:
            _ev(live, "log", f"{track.name}：样本库里没有「{instrument}」，跳过", level="warn")
            continue
        stem = sampled_track_stem(project, track, shot, seconds=seconds,
                                  ring_extra=0.30 if track.name != "AI 和声" else 0.55,
                                  velocity_scale=settings.velocity_scale)
        if float(np.sqrt(np.mean(stem ** 2))) <= 1e-9:
            continue
        layers.append((track.name, stem))
        report["parts"][track.name] = {"instrument": instrument, "notes": len(track.notes)}
        _ev(live, "log", f"{track.name} → 音符盒「{instrument}」"
                         f"（{len(track.notes)} 音符 · 根音 MIDI {shot.pitch}）")

    # 各声部按设定电平对齐到鼓的 RMS —— 用固定锚点，避免"谁响谁定标"。
    # 三段式里主音会拆成 "AI 旋律·A" / "AI 旋律·B" 两层，查表要按基础名回落。
    anchor = float(np.sqrt(np.mean(layers[0][1] ** 2))) or 1e-9
    mix = np.zeros_like(layers[0][1])
    for name, buf in layers:
        base = name.split("·")[0]
        level_db = settings.mc_part_level_db.get(name,
                                                 settings.mc_part_level_db.get(base))
        if level_db is None:
            gain = 1.0
        else:
            gain = dsp.db_to_gain(level_db) * anchor / max(float(np.sqrt(np.mean(buf ** 2))), 1e-9)
        mix += buf * gain
        report["parts"].setdefault(name, {})["gain_db"] = float(dsp.gain_to_db(gain))

    # 混响：合成那条路里混响是总线发送，这里没有引擎总线，就自己挂一级
    if settings.mc_reverb > 0:
        from app.audio import effects as fx
        params = {**project.master.reverb.params, "mix": 1.0}
        wet = fx.apply_reverb(mix, params, SR)
        mix = mix + wet * float(settings.mc_reverb)
    report["layers"] = [n for n, _ in layers]
    return mix, report


def mixdown(project: Project, *, kit, reference=None, settings: MixSettings | None = None,
            live=None) -> tuple[np.ndarray, dict]:
    """合成总线 + 采样鼓 + 低频层 → 测比例拼起来 → 过一遍 gentle_master。

    返回 ``(成品, 报告)``。报告里的每一项都是量出来的，不是设定值。
    """
    st = settings or MixSettings()
    seconds = project.duration_seconds()
    report: dict = {"settings": st.as_dict()}
    tilt_max = st.tilt_max_db

    if st.kit_source == "minecraft":
        _ev(live, "log", "混音：Minecraft 音符盒模式（四个声部全部用音符盒样本）",
            level="step")
        pre, mc_report = mc_mix(project, kit, settings=st, live=live)
        report["mc"] = mc_report
        tilt_max = st.mc_tilt_max_db
    else:
        mute_names: tuple[str, ...] = ()
        if st.lead == "sampled":
            mute_names = ("AI 旋律",)
        _ev(live, "log", f"混音：鼓={st.drums} 主音={st.lead}", level="step")
        bus = engine_bus(project, mute_kinds=("drum",) if st.drums != "native" else (),
                         mute_names=mute_names, track_gain_db=st.track_gain_db, live=live)
        bus_rms = float(np.sqrt(np.mean(bus ** 2))) or 1e-6
        report["bus_rms_db"] = float(dsp.gain_to_db(bus_rms))

        layers: list[np.ndarray] = [bus]
        if st.drums == "sampled":
            drums, drum_stats = sampled_drum_stem(project, kit, seconds=seconds, live=live)
            report["drums"] = drum_stats
            if float(np.sqrt(np.mean(drums ** 2))) > 1e-9:
                g = dsp.db_to_gain(st.drum_level_db) * bus_rms / float(np.sqrt(np.mean(drums ** 2)))
                layers.append(drums * g)
                report["drum_gain_db"] = float(dsp.gain_to_db(g))
                _ev(live, "log", f"采样鼓电平 {st.drum_level_db:+.1f} dB（相对合成总线）")
        if st.lead == "sampled":
            lead_buf, lead_stats = sampled_lead_stem(project, kit, seconds=seconds, live=live)
            report["lead"] = lead_stats
            if float(np.sqrt(np.mean(lead_buf ** 2))) > 1e-9:
                g = dsp.db_to_gain(st.sampled_lead_db) * bus_rms / float(np.sqrt(np.mean(lead_buf ** 2)))
                layers.append(lead_buf * g)
                report["lead_gain_db"] = float(dsp.gain_to_db(g))
        sub = sub_stem(project, seconds=seconds)
        if float(np.sqrt(np.mean(sub ** 2))) > 1e-9:
            g = dsp.db_to_gain(st.sub_level_db) * bus_rms / float(np.sqrt(np.mean(sub ** 2)))
            layers.append(sub * g)
            report["sub_gain_db"] = float(dsp.gain_to_db(g))

        n = min(layer.shape[1] for layer in layers)
        pre = np.sum([layer[:, :n] for layer in layers], axis=0).astype(np.float32)

    report["pre_master"] = audio_health(pre)
    _ev(live, "wave", pre, SR, label="混音完成 · 母带前")
    _ev(live, "spectrum", pre, SR, label="母带前频谱")

    final, master_report = mastering.gentle_master(
        pre, SR, reference=reference, max_tilt_db=tilt_max,
        target_peak_db=st.target_peak_db, limiter_db=st.limiter_db)
    report["master"] = master_report
    report["after"] = audio_health(final)
    b = master_report["before"]
    a = master_report["after"]
    _ev(live, "log", f"母带：质心 {b['centroid_hz']:.0f} → {a['centroid_hz']:.0f} Hz · "
                     f"峰均比 {b['crest_db']:.2f} → {a['crest_db']:.2f} dB · "
                     f"高频(3k+) {b['hf_level_db']:.1f} → {a['hf_level_db']:.1f} dBFS",
        level="ok")
    # 只有真的量过参考才谈"频段差"——没有参考时 match_spectrum 会明确说自己没动手，
    # 此时去读它的字段就是拿不存在的键（这个 KeyError 是被自检用例抓出来的）
    tilt = master_report.get("tilt") or {}
    if tilt.get("applied"):
        _ev(live, "log", "频段对比（参考 / 成品 / 差）：\n" +
                         mastering.balance_table(tilt["ref_levels_db"],
                                                 a["band_levels_db"]))
        for i, name in enumerate(mastering.BAND_NAMES):
            _ev(live, "metric", f"{name} 差",
                f"{a['band_levels_db'][i] - tilt['ref_levels_db'][i]:+.1f} dB")
    else:
        _ev(live, "log", "没有给参考音频，跳过频谱摆平（只做电平与峰值）")
    _ev(live, "metric", "成品峰均比", f"{a['crest_db']:.2f} dB")
    _ev(live, "metric", "成品质心", f"{a['centroid_hz']:.0f} Hz")
    return final, report


# ==========================================================================
# 一键复刻（CLI / MCP / 控制台共用）
# ==========================================================================


@dataclass
class ReplicateResult:
    profile: object
    transcription: object
    candidates: list[dict] = field(default_factory=list)
    out_dir: str = ""


def run_replicate(reference: str | Path, out_dir: str | Path, *, live=None,
                  bpm: float | None = None, bars: int | None = None,
                  sections: int = 1, target_seconds: float | None = None,
                  seeds: int = 1, seed: int = 101, headroom_db: float | None = -2.0,
                  name: str = "复刻", save_midi: bool = True,
                  mix: str = "gentle", drums: str = "sampled", lead: str = "synth",
                  kit_dir: str | Path | None = None,
                  kit_source: str = "reference", mc_lead: str | None = None,
                  mc_kit_dir: str | Path | None = None, form: str = "linear",
                  mc_lead_by_part: dict | None = None,
                  soften: bool = True,
                  sample_first: bool = True) -> ReplicateResult:
    """读参考 → 扒谱 → 指纹 → 段落编排 →（采样 →）音色/混音 → 导出 → 复测。

    ``mix="gentle"``（默认）走新混音：从参考里抠样本 → 采样鼓 + 低频层 +
    频谱摆平的母带，目标峰值 ``headroom_db``（默认 -2 dBFS）。
    ``mix="app"`` 是软件原本那条路：引擎整曲渲染 + 母带限幅器做电平设定。
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    audio = load_reference(reference, SR, live=live)
    res, profile = analyze(audio, bpm=bpm, live=live)

    per_bar_steps = 16
    bars = int(bars if bars is not None else profile.bars)
    section_seconds = bars * per_bar_steps * (60.0 / max(1e-6, profile.bpm)) / 4.0
    sections = max(1, min(10, int(sections)))
    if target_seconds:
        sections = max(1, min(10, int(round(target_seconds / max(1e-6, section_seconds)))))
    total_bars = bars * sections
    _ev(live, "log", f"曲式：{'三段式 A–B–A' if form == 'three-part' else '线性'} · "
                     f"{sections} 段 × {bars} 小节 = {total_bars} 小节 "
                     f"({section_seconds * sections:.0f}s) · 摇摆 {profile.swing:.2f}",
        level="step")

    # ---- 采样：从参考音频或 Minecraft 里拿音色（gentle 混音的前提）----
    kit = None
    settings = MixSettings(drums=drums, lead=lead,
                           target_peak_db=(-3.5 if headroom_db is None else float(headroom_db)))
    if not soften:
        # --no-soften：回到"手更重"的那套（峰值 -2、力度 1.0、踩镲全留）
        settings.target_peak_db = -2.0
        settings.velocity_scale = 1.0
        settings.hat_keep_steps = 0
        settings.mc_part_level_db = {"AI 鼓组": -6.0, "AI 贝斯": -2.0,
                                     "AI 和声": -8.5, "AI 旋律": -5.0}
        settings.mc_reverb = 0.22
    if mix == "gentle":
        try:
            if kit_source == "minecraft":
                settings.kit_source = "minecraft"
                if mc_lead:
                    settings.mc_instrument["AI 旋律"] = mc_lead
                if mc_kit_dir:
                    kit = sampler.load_kit(mc_kit_dir)
                    _ev(live, "log", f"用现成的音符盒样本：{kit.summary()}")
                else:
                    with Progress(live, "sample", "读 Minecraft 音符盒音色（本机游戏资产）") as pr:
                        kit = sampler.load_note_block_kit(live=live)
                        pr(1.0, kit.summary())
                    sampler.save_kit(kit, out / "mc_kit", live=live)
            elif kit_dir:
                kit = sampler.load_kit(kit_dir)
                _ev(live, "log", f"用现成样本：{kit.summary()}")
            elif sample_first:
                with Progress(live, "sample", "从参考里抠样本（起音 → 分类 → 挑最干净的一下）") as pr:
                    kit = sampler.extract_kit(audio, SR, live=live)
                    pr(1.0, kit.summary())
                kit.source = str(reference)
                sampler.save_kit(kit, out / "samples", live=live)
        except Exception as exc:                       # noqa: BLE001
            _ev(live, "log", f"拿音色失败，退回引擎合成：{type(exc).__name__}: {exc}",
                level="warn")
            kit = None
        if kit is None:
            settings.kit_source = "reference"
            settings.drums = "native"
            settings.lead = "synth"
        elif settings.kit_source != "minecraft":
            if settings.drums == "sampled":
                have = [n for n in ("kick", "kick_808", "cowbell") if n in kit]
                if not have:
                    _ev(live, "log", "抠出来的样本里没有可用的鼓件，退回引擎鼓机", level="warn")
                    settings.drums = "native"
            if settings.lead == "sampled" and kit.tone is None:
                _ev(live, "log", "没有抠到乐音样本，主音仍用合成器", level="warn")
                settings.lead = "synth"
        _ev(live, "metric", "音色来源",
            "Minecraft 音符盒" if settings.kit_source == "minecraft" else "参考音频采样")
        _ev(live, "metric", "混音力度",
            f"{'减弱' if soften else '原样'}（峰值 {settings.target_peak_db:+.1f} dBFS · "
            f"力度 ×{settings.velocity_scale:.2f} · "
            f"踩镲{'全留' if settings.hat_keep_steps < 2 else f'每 {settings.hat_keep_steps} 步一记'}）")
        _ev(live, "metric", "混音模式",
            f"gentle · 音色={settings.kit_source} · 鼓={settings.drums} · 主音={settings.lead}")

    result = ReplicateResult(profile=profile, transcription=res, out_dir=str(out))
    for i in range(max(1, min(8, int(seeds)))):
        cand_seed = int(seed) + i
        sid = f"cand{cand_seed}"
        label = f"候选 {cand_seed}（{sections} 段）"
        _ev(live, "stage", sid, label, frac=0.0)
        params_list, parts = section_plan(profile, bars=bars, sections=sections,
                                          seed=cand_seed, form=form)
        if form == "three-part":
            settings.form_parts = list(parts)
            settings.section_bars = int(bars)
            if mc_lead_by_part:
                settings.mc_lead_by_part = dict(mc_lead_by_part)
            _ev(live, "log", "段落归属：" + " ".join(
                f"{i + 1}:{p or '—'}" for i, p in enumerate(parts)))
        project, results = build_project(profile, params_list, name=f"{name} · 种子 {cand_seed}",
                                         total_bars=total_bars, section_bars=bars)
        oob = check_notes_in_range(project)
        timbre = apply_timbre(project, profile, live=live)
        _ev(live, "progress", sid, 0.15, detail="音符已生成")

        mix_report: dict = {}
        gain_db: float | None = None
        if mix == "gentle":
            stereo, mix_report = mixdown(project, kit=kit, reference=audio,
                                         settings=settings, live=live)
            _ev(live, "progress", sid, 0.9, detail="混音 + 母带完成")
        else:
            # 软件原本那条路：整曲渲染 + 母带余量
            if headroom_db is not None:
                before = raw_peak_db(project)
                gain_db = float(headroom_db) - before
                project.master.volume = float(project.master.volume) * (10.0 ** (gain_db / 20.0))
                _ev(live, "log", f"进限幅器前峰值 {before:+.2f} dBFS · 限幅器原本要压 "
                                 f"{before + 1.0:.1f} dB → 整体 {gain_db:+.1f} dB", level="warn")
                _ev(live, "metric", "母带余量", f"{gain_db:+.1f} dB")
            stereo, _stats = render_all(project, live=live, sid=sid, label=label)
            mix_report = {"headroom_db": gain_db}

        health = audio_health(stereo)
        _ev(live, "progress", sid, 0.85, detail="渲染完成，正在复测")
        _ev(live, "wave", stereo, SR, label=f"成品波形 · 种子 {cand_seed}")
        _ev(live, "spectrum", stereo, SR, label=f"成品频谱 · 种子 {cand_seed}")
        for key in ("peak_db", "rms_db", "crest_db", "clipped_samples", "silent_ratio"):
            _ev(live, "metric", key, f"{health[key]:.2f}" if isinstance(health[key], float)
                else health[key])

        stem_path = out / f"replica_{cand_seed:03d}"
        wavio.save_wav(stem_path.with_suffix(".wav"), stereo, SR, bit_depth=24)
        project.save(stem_path.with_suffix(".musicproj.json"))
        if save_midi:
            wavio.save_midi(stem_path.with_suffix(".mid"), project)
        _ev(live, "file", stem_path.with_suffix(".wav"),
            label=f"复刻成品 · 种子 {cand_seed}")

        rep_profile = replicate.analyze_reference(stereo, SR)
        compare = compare_profiles(profile, rep_profile)
        _ev(live, "log", "参考 → 成品： " + " | ".join(f"{k} {a} → {b}"
                                                       for k, a, b in compare))
        stem_measure = stem_probe(project)
        if stem_measure.get("有鼓轨"):
            _ev(live, "log", f"鼓轨分轨复测：{stem_measure['bpm']:.1f} BPM"
                             f"（目标 {profile.bpm:.1f}）· 鼓占比 "
                             f"{stem_measure['鼓占比'] * 100:.0f}%")

        notes = sum(len(tr.notes) for tr in project.tracks)
        _ev(live, "progress", sid, 1.0, detail=f"{notes} 音符 · {health['seconds']:.1f}s")
        _ev(live, "stage", sid, label, frac=1.0, done=True,
            detail=f"{notes} 音符 · 峰均比 {health['crest_db']:.1f} dB")
        result.candidates.append({
            "seed": cand_seed, "path": str(stem_path.with_suffix(".wav")),
            "project": str(stem_path.with_suffix(".musicproj.json")),
            "notes": notes, "health": health, "sections": sections, "bars": total_bars,
            "progressions": " → ".join("-".join(str(d) for d in r.params.degrees)
                                       for r in results),
            "note_range": oob, "timbre": timbre, "stem_probe": stem_measure,
            "headroom_db": gain_db, "mix": mix_report,
            "compare": {k: [a, b] for k, a, b in compare},
            "replica_summary": rep_profile.summary(),
        })
    _ev(live, "status", f"完成：{len(result.candidates)} 个候选", running=False,
        note=str(out))
    return result


__all__ = [
    "SR", "Progress", "load_reference", "analyze", "section_params", "build_project",
    "apply_timbre", "check_notes_in_range", "render_all", "raw_peak_db", "audio_health",
    "compare_profiles", "stem_probe", "run_replicate", "ReplicateResult", "TIMBRE_PLAN",
    "timbre_tier", "Note", "Project", "sample_reference", "mixdown", "MixSettings",
    "drum_events", "PIECE_MAP",
]
