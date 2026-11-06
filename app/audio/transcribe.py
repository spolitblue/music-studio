"""扒谱 —— 把一段音频里的"什么时候、什么音高、哪件鼓"抠成可编辑的音符。

为什么自己做而不是接一个模型
----------------------------
本工程的原则是"所有东西都能用代码算出来"（音色合成是这个原则，分析也一样）。
扒谱这件事的核心是**起音检测 + 基频估计 + 粗分类**，全是经典 DSP，
做得诚实一点（并且把"我们做不到什么"写清楚）比挂一个云接口更有用：
离线、无依赖、可测试、结果可复现。

诚实的边界
----------
* 单声部/稀疏织体效果好；**密集和弦**会被压成一个基频（听感上最响的那个）。
* 鼓点分类是**粗分**（低频占比 = 底鼓族、噪声型按质心分军鼓/拍手/镲），
  不做真正的鼓件识别。已知的具体边界：
  - **底鼓与踩镲同时响只出一件**（按低频占比判成底鼓）。要两件都出，
    需要逐频段各检一遍，而那会把同一个军鼓报成三件鼓（实测）。
  - **闭镲/开镲靠衰减区分**，而衰减取决于音色包的参数：用 `drums.default_voices()`
    的调校值（闭镲 0.055s）时能分开；用 `Track.ensure_kit()` 的通用默认值
    （0.35s）时都会被判成开镲。同族误判，不影响节奏骨架。
  - **低嗵会被算成底鼓**（两者都是低频短音）。
* 速度估计靠起音包络自相关，会有倍/半速歧义（折到 75~150 的常用区间）；
  自由速度（rubato）估不准，这时请在界面上手填 BPM。
* 只分析前 :data:`MAX_ANALYZE_SECONDS` 秒：扒谱是"看着办"的活，
  前 90 秒足够定速度/调式/织体，全量分析一首 5 分钟的曲子要几十秒，
  界面会让人以为死了。

流程
----
1. 单声道化 + 归一化
2. 频谱通量 → 起音包络
3. 起音包络自相关 → BPM（60~180，含倍频歧义消解）
4. 自适应阈值峰值拾取 → 起音时刻（最小间隔 60ms）
5. 每段算谱平坦度 / 质心 / 衰减 → 判定"打击乐"还是"有音高"
6. 有音高段：自相关基频（55~1000Hz）+ 谐波和打分 → MIDI 音高（含倍频校正）
7. 打击乐段：按质心与衰减粗分成 kick / snare / clap / hat / cowbell
8. 全部量化到 1/16 网格
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from app.audio import dsp
from app.audio.project import Note, Track, drum_to_pitch

SR = dsp.SR

#: 分析窗口 / 跳距（样本）。1024/512 在 44.1k 下是 23ms 窗、11.6ms 跳距 ——
#: 起音的定位精度够用（16 分音符在 120BPM 下是 125ms），FFT 也不算大。
WIN = 1024
HOP = 512

#: 最多分析多少秒。扒谱是"看着办"的活，前 90 秒足够定调式/速度/织体；
#: 一首 5 分钟的曲子全量分析要几十秒，界面会让人以为死了。
MAX_ANALYZE_SECONDS = 90.0

#: 打击乐分类的阈值不是拍脑袋写的，是**量出来的**（本机实测，单件满力度渲染，
#: 取起音处前 8 帧的谱）：
#:
#:     piece        质心(Hz)  平坦度   衰减(ms)
#:     kick            240    0.007      31
#:     kick_808        126    0.000     106
#:     snare          8929    0.621      22
#:     clap           8440    0.570      46
#:     hat_closed    12117    0.507       5
#:     hat_open      11582    0.553      23
#:     rim            9176    0.499       9
#:     crash         11161    0.758     175
#:     tom_mid        1947    0.128      40
#:     cowbell        4072    0.166      22
#:     cowbell_808    3632    0.081      44
#:
#: 两条轴就够分开它们：**质心**（多低/多高）与**平坦度**（噪声还是乐音），
#: 衰减只用来分同族里的长短（闭镲/开镲/吊镲、808 底鼓/普通底鼓）。
#: 拍手与军鼓的区别不在频谱而在**包络**：拍手是 3~4 个脉冲（见 DrumFeatures.bursts）。
@dataclass
class DrumFeatures:
    """一段打击乐的可解释特征 —— 分类全靠它，测试也直接量它。"""

    centroid: float = 0.0
    flatness: float = 0.0
    decay_ms: float = 0.0
    bursts: int = 1
    #: 起音处三个频段各自的能量占比（low/mid/high）
    band_ratio: tuple[float, float, float] = (0.0, 0.0, 0.0)
    #: 能量占比最大的那个频段 —— 衰减、脉冲数、以及各种门限都在它上面量
    band: str = "mid"

    @property
    def low_ratio(self) -> float:
        return self.band_ratio[0]

    @property
    def high_ratio(self) -> float:
        return self.band_ratio[2]


def classify_drum(feat: DrumFeatures) -> str:
    """把特征映射成一件鼓。**粗分**：分到同族里相近的那件是正常的。

    判定顺序（每一步的理由）：
    1. 质心 < 500Hz → 低频族：尾巴长过 70ms 就当 808 底鼓（那是它的招牌），
       否则是普通底鼓。
    2. 平坦度 ≥ 0.42 → 噪声族（军鼓/拍手/镲/边击）。
       其中质心 ≥ 10kHz 是镲：极短=闭镲、中等=开镲、很长=吊镲。
       质心不到 10kHz 再看包络脉冲数：≥3 个脉冲是拍手（那是拍手唯一的特征），
       否则质心 ≥ 2.5kHz 是军鼓、更低是边击。
    3. 剩下的都是**乐音族**（平坦度低 = 有明确音高）：
       质心 ≥ 6kHz 是叮叮镲、≥ 2.4kHz 是牛铃（金属但有音高）、
       1.5k/1k 以上是嗵鼓（按质心分低中高）、再低就是底鼓族。
    """
    c, f = feat.centroid, feat.flatness

    if c < 500.0:
        return "kick_808" if feat.decay_ms > 70.0 else "kick"

    if f >= 0.42:
        if c >= 10000.0:
            if feat.decay_ms < 12.0:
                return "hat_closed"
            return "hat_open" if feat.decay_ms < 70.0 else "crash"
        if feat.bursts >= 3:
            return "clap"
        return "snare" if c >= 2500.0 else "rim"

    if c >= 6000.0:
        return "ride"
    if c >= 2400.0:
        return "cowbell"
    if c >= 1500.0:
        return "tom_high"
    if c >= 1000.0:
        return "tom_mid"
    if c >= 500.0:
        return "tom_low"
    return "kick_808" if feat.decay_ms > 70.0 else "kick"



@dataclass
class TranscribeResult:
    """扒谱产物：可直接变成工程里的音轨。"""

    bpm: float = 120.0
    key_pc: int = 0
    mode: str = "major"
    confidence: float = 0.0
    seconds: float = 0.0
    #: 有音高的事件：(步, MIDI 音高, 力度)
    pitched: list[tuple[int, int, float]] = field(default_factory=list)
    #: 打击乐事件：(步, 鼓件名, 力度)
    drums: list[tuple[int, str, float]] = field(default_factory=list)
    #: 量化**之前**的起音时刻（秒）。摇摆这类"时值偏移"信息一量化就没了，
    #: 所以原样留一份给复刻用（见 replicate._estimate_swing）。
    onset_seconds: list[float] = field(default_factory=list)
    onsets: int = 0
    log: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.pitched) + len(self.drums)

    def note_counts(self) -> dict[str, int]:
        return {"有音高": len(self.pitched), "打击乐": len(self.drums)}

    def summary(self) -> str:
        return (f"{self.bpm:.0f} BPM · {self.key_name} · "
                f"{len(self.pitched)} 个音 + {len(self.drums)} 个鼓点 · "
                f"置信度 {self.confidence * 100:.0f}%")

    @property
    def key_name(self) -> str:
        names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
        mode = "大调" if self.mode == "major" else "小调"
        return f"{names[int(self.key_pc) % 12]} {mode}"


# --------------------------------------------------------------------------
# 1~4：起音与速度
# --------------------------------------------------------------------------


def to_mono(audio: np.ndarray) -> np.ndarray:
    """(channels, n) 或 (n,) → float32 单声道。"""
    a = np.asarray(audio, dtype=np.float32)
    if a.ndim == 2:
        a = a.mean(axis=0)
    return np.ascontiguousarray(a.reshape(-1), dtype=np.float32)


def _stft_mag(mono: np.ndarray) -> np.ndarray:
    """幅度谱 (frames, bins)，**最前面补一帧静音**。

    为什么要补那一帧：通量是帧间差分，不补的话"开头第一下"没有前驱帧可比，
    会被整段漏掉 —— 实测一个从第 0 步开始的底鼓就是这么消失的。
    只保留幅度，相位在扒谱里没用，省一半内存。
    """
    n = mono.size
    if n < WIN:
        mono = np.pad(mono, (0, WIN - n))
        n = WIN
    frames = 1 + (n - WIN) // HOP
    # stride 视图做分帧：不复制数据，长曲子也不会额外吃掉一份内存
    idx = np.arange(WIN)[None, :] + HOP * np.arange(frames)[:, None]
    win = np.hanning(WIN).astype(np.float32)
    spec = np.abs(np.fft.rfft(mono[idx] * win, axis=1)).astype(np.float32)
    return np.vstack([np.zeros((1, spec.shape[1]), dtype=np.float32), spec])


#: 三个频段的分界（Hz）。分段的依据是**鼓件的能量分布**（本机实测质心）：
#: 底鼓 126~240Hz、军鼓/拍手 8~9kHz、踩镲 11~12kHz、牛铃 2.5~4kHz。
BAND_EDGES: tuple[float, float] = (200.0, 2500.0)

#: 频段 → 这一段的权重。低频那点增量天生比高频小得多，不加权就会被淹没。
_BAND_WEIGHT = {"low": 1.5, "mid": 1.0, "high": 0.9}


def band_envelopes(mag: np.ndarray, sr: int = SR) -> dict[str, np.ndarray]:
    """**分频段**的归一化频谱通量包络，另附一条合起来的 ``all``。

    为什么必须分频段（这是踩出来的）：只用一条宽带通量时，踩镲每 16 分一次的
    高频增量会把包络整体抬高，150Hz 以下那点底鼓增量被自适应阈值直接滤掉 ——
    实测一段"底鼓+军鼓+闭镲"的鼓型，检出的 7 个起音全是镲，底鼓一个没有。
    分频段之后，每一段各自归一化，低频那点微弱但重要的增量才浮得出来；
    而且"底鼓和踩镲同时响"这种常见写法能正确地变成**两个**事件。

    归一化用 99 分位而不是 95 分位：稀疏鼓型里非零帧可能不到 5%，
    95 分位会取到 0，整段被当成"没有能量"丢掉（第一版就是这么把鼓型全丢光的）。
    """
    if mag.shape[0] < 2:
        empty = np.zeros(1, dtype=np.float32)
        return {"low": empty, "mid": empty, "high": empty, "all": empty}

    freqs = np.fft.rfftfreq(WIN, 1.0 / sr)
    lo_edge, hi_edge = BAND_EDGES
    masks = {
        "low": freqs < lo_edge,
        "mid": (freqs >= lo_edge) & (freqs < hi_edge),
        "high": freqs >= hi_edge,
    }
    out: dict[str, np.ndarray] = {}
    total = np.zeros(mag.shape[0] - 1, dtype=np.float64)
    for name, mask in masks.items():
        if not np.any(mask):
            out[name] = np.zeros(mag.shape[0] - 1, dtype=np.float32)
            continue
        env = _flux(mag[:, mask])
        out[name] = env
        total += _BAND_WEIGHT[name] * env
    out["all"] = (total / sum(_BAND_WEIGHT.values())).astype(np.float32)
    return out


def _flux(band: np.ndarray) -> np.ndarray:
    """一段频带的归一化通量：只累加能量**上升**的部分，再去掉慢变本底。"""
    diff = np.diff(band, axis=0)
    flux = np.maximum(diff, 0.0).sum(axis=1)
    # 去局部均值：整段渐强、长音持续这些慢变本底不该被当成起音
    flux = flux - np.convolve(flux, np.ones(9) / 9.0, mode="same")
    flux = np.maximum(flux, 0.0)
    scale = float(np.percentile(flux, 99.0)) if flux.size else 0.0
    if scale <= 1e-9:
        scale = float(flux.max()) if flux.size else 0.0
    if scale <= 1e-9:
        return np.zeros(flux.shape[0], dtype=np.float32)
    # 先**钳掉离群尖峰**再归一化：从静音起步的那一下（谱图前面补了一帧静音）
    # 通量比普通起音大好几个数量级；不钳的话 99 分位会被它顶上去，
    # 其余所有起音都被压成接近 0，整段再也检不出东西。
    flux = np.minimum(flux, 6.0 * scale)
    return (flux / scale).astype(np.float32)


def onset_envelope(mag: np.ndarray, sr: int = SR) -> np.ndarray:
    """三段合起来的通量包络（给速度估计与"有没有起音"用）。"""
    return band_envelopes(mag, sr)["all"]


def estimate_bpm(env: np.ndarray, sr: int = SR, *, lo: float = 60.0,
                 hi: float = 180.0, accent_percentile: float = 60.0) -> float:
    """起音包络 → BPM。**网格对齐打分**，不是单纯取自相关峰。

    为什么要换算法（实测踩的坑）：自相关在"整体起音密度远大于拍点密度"时会被
    糊掉 —— 整曲混音（加了混响、合成器谐波、镲的尾巴）里起音有 10 个/秒，
    实测把 96 BPM 的示范曲估成了 127.6 BPM（一个 4:3 的错值，既不是倍速也不是半速，
    折进常用区间也救不回来）。

    现在的做法两步：
    1. **只留重音**：低于 60 分位的起音直接置零。拍点是重音，混响尾巴不是。
    2. **网格对齐打分**：对 60~180 之间每一个候选速度，把它的拍点网格铺到
       包络上，取"网格点上的平均强度 − 全局平均强度"作为得分，取最高者。
       拍点必须**持续**落在重音上才得高分，所以 4:3 这种错值会被压下去。
    最后再把倍/半速歧义折进 [75, 150)。
    """
    if env.size < 16:
        return 120.0
    x = env.astype(np.float64)
    thr = float(np.percentile(x, accent_percentile))
    accented = np.where(x >= thr, x, 0.0)
    if not np.any(accented):                 # 包络太平（几乎处处相等）→ 退回原包络
        accented = x
    base = float(accented.mean())

    frame_rate = sr / HOP
    best_score, best_bpm = -1e18, 120.0
    for bpm in np.arange(lo, hi + 1e-9, 0.5):
        period = frame_rate * 60.0 / bpm
        if period < 2.0:
            continue
        idx = (np.arange(1, int(accented.size / period) + 1) * period).astype(np.int64)
        idx = idx[idx < accented.size]
        if idx.size < 4:
            continue
        score = float(accented[idx].mean()) - base
        # 轻微偏向常用速度区间（90~150）：同样的对齐度下，人更可能听的是这个区间
        if 90.0 <= bpm <= 150.0:
            score *= 1.05
        if score > best_score:
            best_score, best_bpm = score, float(bpm)

    while best_bpm < 75.0:
        best_bpm *= 2.0
    while best_bpm >= 150.0:
        best_bpm /= 2.0
    return float(np.clip(best_bpm, lo, hi))


def detect_onsets(env: np.ndarray, sr: int = SR, *, min_gap: float = 0.06,
                  ratio: float = 2.0, floor: float = 0.08) -> list[int]:
    """自适应阈值峰值拾取，返回帧号。

    背景电平取的是**局部最小值再平滑**（而不是滑动均值）—— 这是第三个数出来的坑：
    密集鼓型里每个起音都会把 0.35s 窗口的均值顶上去，均值阈值于是高于踩镲的峰，
    实测"底鼓+每八分一记闭镲"只检出了底鼓/军鼓，闭镲一记都没有。
    先取 ±3 帧的局部最小值把峰压掉，再对这条"谷底线"做滑动平均，
    得到的就是与起音密度无关的本底。

    另外两个门限：
    * ``floor`` —— 起音至少达到本频段典型起音强度的 8%（包络按 99 分位归一化）。
      一个几乎全空的频段里的小抖动不该被当成起音。
    * 第 0 帧也参与判定：从第 0 步就开始的音，起音就在第 0 帧。
    """
    if env.size < 3:
        return []
    frame_rate = sr / HOP
    win = max(3, int(0.35 * frame_rate))
    e = env.astype(np.float64)

    # 局部最小值（±3 帧）：把起音的峰压掉，只留"底"
    k = 3
    padded = np.pad(e, (k, k), mode="edge")
    valley = np.minimum.reduce([padded[i: i + e.size] for i in range(2 * k + 1)])
    # 谷底线再滑动平均：得到与起音密度无关的本底电平
    vpad = np.pad(valley, (win, win), mode="edge")
    vsum = np.cumsum(np.concatenate(([0.0], vpad)))
    bg = (vsum[2 * win:] - vsum[:-2 * win]) / (2 * win)
    bg = np.maximum(bg[: e.size], 1e-9)

    thr = bg * ratio + floor
    gap = max(1, int(min_gap * frame_rate))
    peaks: list[int] = []
    for i in range(e.size):
        v = float(e[i])
        left = float(e[i - 1]) if i > 0 else 0.0
        right = float(e[i + 1]) if i + 1 < e.size else 0.0
        if v < thr[i] or v < left or v < right:
            continue
        if peaks and i - peaks[-1] < gap:
            if v > float(e[peaks[-1]]):          # 挨太近就留更响的那个
                peaks[-1] = i
            continue
        peaks.append(i)
    return peaks


def energy_rise(mono: np.ndarray, frame: int, sr: int = SR, *,
                before_frames: int = 4, after_frames: int = 4) -> float:
    """起音处短时能量 / 起音**前**短时能量。

    为什么通量已经能检出起音了还要这一条：音符的**释放**也会改变频谱形状，
    于是通量在音符尾巴上冒出一个峰（实测旋律每两个音之间都多出一个假起音）。
    但释放时能量是**降**的 —— 加一条"能量必须涨"的要求，就把"释放"这种
    假起音干净地挡在门外（比值 < 1），而真起音从近乎安静处起来，比值很大。
    """
    start = frame * HOP
    before = mono[max(0, start - before_frames * HOP):start]
    after = mono[start:min(mono.size, start + after_frames * HOP)]
    if before.size < 16 or after.size < 16:
        return 1e9 if before.size < 16 else 1.0
    eb = float(np.sqrt(np.mean(np.square(before))))
    ea = float(np.sqrt(np.mean(np.square(after))))
    return ea / max(1e-9, eb)


def band_shares(mag: np.ndarray, sr: int = SR) -> dict[str, float]:
    """各频段占了多少能量（0~1）。太小的频段直接不参与检出。

    为什么需要它：一个几乎没内容的频段（纯旋律里 200Hz 以下只占 0.2%）
    照样会被归一化成"有峰有谷"，于是被报出一串假起音。占比门限是"这一段里
    本来就没东西"的最直接证据。
    """
    freqs = np.fft.rfftfreq(WIN, 1.0 / sr)
    lo_edge, hi_edge = BAND_EDGES
    mean_spec = mag.mean(axis=0) if mag.size else np.zeros(1, dtype=np.float32)
    total = float(mean_spec.sum()) + 1e-12
    masks = {
        "low": freqs < lo_edge,
        "mid": (freqs >= lo_edge) & (freqs < hi_edge),
        "high": freqs >= hi_edge,
    }
    return {k: float(mean_spec[m].sum()) / total for k, m in masks.items()}


#: 频段能量占比低于这个值就不参与"打击乐检出"（本机实测：纯旋律的低频段占 0.2%，
#: 鼓型里底鼓所在的低频段占 9.8% —— 1.5% 这条线把两者分得很开）
MIN_BAND_SHARE = 0.015

#: 起音强度下限（包络按 99 分位归一化后的单位）。
#: 实测真鼓击在 0.5~4.8，静音尾巴上的噪声假起音只有 0.168 —— 0.35 分得开。
MIN_ONSET_STRENGTH = 0.35


# --------------------------------------------------------------------------
# 5~6：定性（打击乐 / 有音高）与基频
# --------------------------------------------------------------------------


def _segment(mag: np.ndarray, start_frame: int, stop_frame: int) -> np.ndarray:
    seg = mag[start_frame:max(start_frame + 1, stop_frame)]
    return seg.mean(axis=0) if seg.size else np.zeros(mag.shape[1], dtype=np.float32)


def spectral_flatness(spectrum: np.ndarray) -> float:
    """谱平坦度 0（纯音）~1（噪声）。几何均值 / 算术均值。"""
    s = np.maximum(np.asarray(spectrum, dtype=np.float64), 1e-10)
    return float(np.exp(np.mean(np.log(s))) / max(1e-10, np.mean(s)))


def spectral_centroid(spectrum: np.ndarray, sr: int = SR) -> float:
    s = np.asarray(spectrum, dtype=np.float64)
    freqs = np.fft.rfftfreq(2 * (s.size - 1), 1.0 / sr)[: s.size]
    total = s.sum()
    return float((freqs * s).sum() / total) if total > 1e-9 else 0.0


def _autocorr_pitch(seg: np.ndarray, sr: int = SR, *, f_lo: float = 55.0,
                    f_hi: float = 1000.0) -> tuple[int | None, float]:
    """自相关基频 → (MIDI 音高, 置信度)。

    ``f_hi=1000Hz``：再高的基频在中低频伴奏里基本靠不住；
    ``f_lo=55Hz``：低于 A1 的周期要么是噪声要么是鼓的尾巴。
    """
    x = np.asarray(seg, dtype=np.float64)
    x = x - x.mean()
    if x.size < 256 or not np.any(x):
        return None, 0.0
    # 加窗减少边界效应，否则自相关会在两端出现假的强峰
    x = x * np.hanning(x.size)
    n = 1 << int(np.ceil(np.log2(2 * x.size)))
    spec = np.fft.rfft(x, n)
    ac = np.fft.irfft(spec * np.conj(spec), n)[: x.size]
    if ac[0] <= 1e-12:
        return None, 0.0
    ac = ac / ac[0]

    lag_lo = max(2, int(sr / f_hi))
    lag_hi = min(x.size - 1, int(sr / f_lo))
    if lag_hi <= lag_lo + 1:
        return None, 0.0
    seg_ac = ac[lag_lo:lag_hi]
    # 找第一个显著的局部峰（**最早**的那个 = 基频；取全局最大常常会落到倍频上）
    best_lag, best_val = 0, -1.0
    for i in range(1, seg_ac.size - 1):
        if seg_ac[i] > seg_ac[i - 1] and seg_ac[i] >= seg_ac[i + 1]:
            if seg_ac[i] > 0.55 * seg_ac.max():
                best_lag, best_val = lag_lo + i, float(seg_ac[i])
                break
    if best_lag <= 0:
        return None, 0.0
    # 抛物线插值：整数 lag 的误差在低频段能有好几个半音
    y0, y1, y2 = ac[best_lag - 1], ac[best_lag], ac[best_lag + 1]
    denom = (y0 - 2 * y1 + y2)
    offset = 0.5 * (y0 - y2) / denom if abs(denom) > 1e-12 else 0.0
    if abs(offset) > 1.0:
        offset = 0.0
    f0 = sr / (best_lag + offset)
    if not (f_lo <= f0 <= f_hi):
        return None, 0.0
    midi = int(round(69 + 12 * np.log2(f0 / 440.0)))
    return midi, float(np.clip(best_val, 0.0, 1.0))


def _best_pitched_segment(mono: np.ndarray, start: int, stop: int, sr: int
                          ) -> tuple[int | None, float]:
    """在一段里挑"最像乐音"的一小窗估基频。

    为什么不是整段一起估：一段里常常先是瞬态（宽带噪声）再接住乐音，
    整段平均会把瞬态糊进自相关，基频直接跑偏。所以按 40ms 窗扫一遍，
    取**谱平坦度最低**（最像乐音）的那一窗。
    """
    win = max(512, int(0.04 * sr))
    hop = max(256, win // 2)
    best: tuple[int | None, float] = (None, 0.0)
    best_flat = 1e9
    for pos in range(start, max(start + 1, stop - win + 1), hop):
        chunk = mono[pos:pos + win]
        if chunk.size < win:
            chunk = np.pad(chunk, (0, win - chunk.size))
        mag = np.abs(np.fft.rfft(chunk * np.hanning(win)))
        flat = spectral_flatness(mag)
        if flat >= best_flat:
            continue
        midi, conf = _autocorr_pitch(chunk, sr)
        if midi is not None and conf > 0.25:
            best, best_flat = (midi, conf), flat
    return best


def _envelope(mono: np.ndarray, start: int, length: int, sr: int = SR, *,
              block_ms: float = 2.0, smooth: int = 3) -> np.ndarray:
    """振幅包络跟随器：分块取**最大值**再平滑。

    为什么不能直接对 ``|x|`` 做滑动平均（第一版的做法）：整流后的纯音是
    **两倍频**的纹波（262Hz 的音 → 524Hz 纹波），3ms 的滑动平均抹不平它，
    于是包络一路"跌破 15%"——持续音符的衰减被读成几毫秒，紧接着被判成"短促
    打击乐"，整条旋律都被说成嗵鼓。分块取最大值才是真正的包络跟随，
    与波形频率无关。

    ``block_ms``/``smooth`` 可调是因为两种用途的分辨率需求相反：
    量衰减要抹平（2ms 块 + 平滑），数拍手的脉冲却恰恰**不能**抹 ——
    拍手的三个脉冲间隔只有 10ms，一抹就并成一个（实测拍手因此全被判成军鼓）。
    """
    seg = np.abs(np.asarray(mono[start:start + length], dtype=np.float64))
    block = max(1, int(block_ms / 1000.0 * sr))
    n = seg.size // block
    if n < 1:
        return seg
    env = seg[: n * block].reshape(n, block).max(axis=1)
    if smooth >= 3 and env.size >= smooth:
        env = np.convolve(env, np.ones(smooth) / smooth, mode="same")
    return env


def _decay_ms(sig: np.ndarray, start: int, sr: int = SR, *,
              window: float = 0.6, floor: float = 0.05,
              peak_window: float = 0.04) -> float:
    """从起音算起，包络掉到峰值 ``floor`` 倍所用的毫秒数。

    两个必须写对的细节，都是踩出来的：

    1. **峰值只在起音后 40ms 内找**。在整段 0.6s 里取最大值的话，后面那记
       更响的鼓会当成"峰值"（实测：闭镲的衰减被量成 316ms，因为 250ms 后的
       军鼓更响），闭镲于是全被判成吊镲。起音的峰值定义上就在起音附近。
    2. 衰减是"到某个电平"，不是"到下一记的间隔"。踩镲每 16 分响一次时，
       用间隔会让所有闭镲都变成开镲。
    """
    env = _envelope(sig, start, int(window * sr), sr)
    if env.size < 2:
        return 0.0
    head = env[: max(1, int(peak_window / 0.002))]        # 每块 2ms
    peak_idx = int(np.argmax(head))
    peak = float(head[peak_idx])
    if peak <= 1e-9:
        return 0.0
    below = np.flatnonzero(env[peak_idx:] < peak * floor)
    idx = peak_idx + (int(below[0]) if below.size else env.size - peak_idx)
    return idx * 2.0                                       # 每块 2ms


def _bursts(sig: np.ndarray, start: int, sr: int = SR, *,
            window: float = 0.08) -> int:
    """起音后 80ms 内有多少个"包络脉冲"。

    拍手（3~4 个，间隔约 10ms）与军鼓（1 个）唯一的区别就在这里。

    两件都必须写对：
    * 用 1ms 块、不平滑的**细包络** —— 10ms 的脉冲间隔经不起平滑
      （3ms 平滑 + 55% 门槛时拍手也只剩 1 个脉冲，这条特征等于白给）。
    * 只看起音后 **80ms**。窗口开到"整段"的话，后面的每一记鼓都会计入，
      实测军鼓+闭镲同响被数成 10~13 个脉冲，于是军鼓全被判成拍手。
    """
    env = _envelope(sig, start, int(window * sr), sr, block_ms=1.0, smooth=0)
    if env.size < 3:
        return 1
    peak = float(env.max())
    if peak <= 1e-9:
        return 1
    above = env > peak * 0.45
    rises = int(np.sum(above[1:] & ~above[:-1]))
    return max(1, rises)


def onset_prominence(env: np.ndarray, frame: int, *, lookback: int = 6) -> float:
    """起音相对**紧邻前面**那一段的抬升倍数。

    为什么有了自适应阈值还要这个：阈值比的是**局部均值**，而均值压不住
    "持续音中间的小起伏" —— 实测一个长音符在中段被报出一次假起音（它比那段
    的均值高 35%，轻松过阈值）。而真正的鼓击是从近乎安静处起来的，抬升倍数
    通常几十倍。``lookback=6`` 帧 ≈ 70ms：短于这个的抬升才算"一次起音"。
    """
    if frame <= 0:
        return 1e9
    lo = max(0, frame - lookback)
    prev = float(np.max(env[lo:frame])) if frame > lo else 0.0
    return float(env[frame]) / max(1e-6, prev)


def band_signals(mono: np.ndarray, sr: int = SR) -> dict[str, np.ndarray]:
    """把单声道拆成三个频段的信号。

    为什么非拆不可：衰减与"包络脉冲数"必须在**这一下所在的频段**上量。
    在整个宽带上量时，别的乐器的长尾巴会把结果顶坏 —— 实测"底鼓(340ms 尾巴)
    + 每八分一记闭镲(26ms)"里，闭镲的衰减被量成 292ms，于是每一记闭镲都被
    判成吊镲。走 FFT 卷积（``dsp.biquad_static``），拆三个频段的开销可以忽略。
    """
    lo_edge, hi_edge = BAND_EDGES
    return {
        "low": dsp.biquad_static(mono, "lp", lo_edge, 0.707, sr),
        "mid": dsp.bandpass(mono, lo_edge, hi_edge, sr),
        "high": dsp.biquad_static(mono, "hp", hi_edge, 0.707, sr),
    }


def drum_features(bands: dict[str, np.ndarray], mag: np.ndarray, frame: int,
                  stop_frame: int, sr: int = SR) -> DrumFeatures:
    """量一段打击乐的特征（分类与测试都走它）。

    衰减与脉冲数取"能量占比最大的那个频段"的信号；质心/平坦度/占比取起音处
    前 8 帧的谱。
    """
    attack = mag[frame:max(frame + 1, min(frame + 8, stop_frame))]
    spectrum = attack.mean(axis=0) if attack.size else np.zeros(mag.shape[1], np.float32)

    freqs = np.fft.rfftfreq(WIN, 1.0 / sr)
    lo_edge, hi_edge = BAND_EDGES
    total = float(spectrum.sum()) + 1e-12
    names = ("low", "mid", "high")
    ratio = (
        float(spectrum[freqs < lo_edge].sum()) / total,
        float(spectrum[(freqs >= lo_edge) & (freqs < hi_edge)].sum()) / total,
        float(spectrum[freqs >= hi_edge].sum()) / total,
    )
    dominant = names[int(np.argmax(ratio))]
    sig = bands.get(dominant)
    if sig is None:
        sig = np.zeros(WIN, dtype=np.float32)

    start = frame * HOP
    stop = max(start + WIN, stop_frame * HOP)
    return DrumFeatures(
        centroid=spectral_centroid(spectrum, sr),
        flatness=spectral_flatness(spectrum),
        # floor 取 5% 而不是 15%：踩镲这类乐器的包络是"极快下坠 + 长而安静的尾巴"，
        # 15% 处闭镲与开镲都在 25ms 附近就跨过了，两个分不开；5% 处才拉得开
        # （实测 8ms vs 44ms）。
        decay_ms=_decay_ms(sig, start, sr, floor=0.05),
        bursts=_bursts(sig, start, sr),
        band_ratio=ratio,
        band=dominant,
    )


def classify_drum(feat: DrumFeatures) -> str:
    """把特征映射成一件鼓。**粗分**：分到同族相近的那件是正常的。

    判定顺序与依据（阈值全部来自 :data:`DrumFeatures` 上方那张实测表）：

    1. 低频占比 ≥ 0.45 且短促 → 底鼓族（尾巴长过 70ms 是 808）。
       为什么先看**占比**而不是质心：底鼓的能量 87% 在 200Hz 以下，
       军鼓只有 2% —— 这一条比质心更分得开，也不会被旁边的踩镲带偏。
    2. 噪声型（平坦度 ≥ 0.42）里：
       质心 ≥ 10kHz 是镲（衰减分闭镲/开镲/吊镲）；不到 10kHz 时用脉冲数
       分拍手（≥3 个脉冲，间隔约 10ms，是拍手唯一的特征），否则是军鼓。
    3. 剩下的都是有明确音高的：质心 ≥ 6kHz 是叮叮镲、≥ 2.4kHz 是牛铃、
       再低按质心落成嗵鼓。
    """
    c, f = feat.centroid, feat.flatness
    short = feat.decay_ms < 70.0

    if feat.low_ratio >= 0.32 and feat.decay_ms < 200.0:
        # 0.32 而不是"过半"：底鼓和踩镲同时响时低频只占 0.32~0.41（实测），
        # 而"底鼓+踩镲同响"是极常见的写法，卡太高会让这种写法的低音整段消失。
        # 代价：低嗵（低频占比 0.39）会被算成底鼓 —— 两者都是低频短音，
        # 靠现有特征分不开，这条偏差写在模块 docstring 的已知边界里。
        return "kick_808" if feat.decay_ms > 100.0 else "kick"

    if f >= 0.42:
        if c >= 10000.0:
            if feat.decay_ms < 15.0:
                return "hat_closed"
            if feat.decay_ms < 80.0:
                return "hat_open"
            # 长噪声里的吊镲与叮叮镲只差"有多噪"：实测平坦度 0.752 vs 0.672
            return "crash" if f >= 0.71 else "ride"
        if feat.bursts >= 3:
            return "clap"
        if feat.decay_ms < 25.0:
            return "rim"                       # 极短 + 中高质心 = 边击（军鼓 40ms）
        return "snare" if c >= 2500.0 else "rim"

    if c >= 6000.0:
        return "ride"
    if c >= 2400.0:
        return "cowbell"
    if not short:
        return "kick_808" if feat.low_ratio >= 0.30 else "tom_low"
    # 嗵鼓按实测质心分档：高 2052 / 中 1916 / 低 1723 Hz
    if c >= 2000.0:
        return "tom_high"
    if c >= 1820.0:
        return "tom_mid"
    return "tom_low"


#: 鼓件 → 粗类。同一时刻同一类里的两个事件只会留一个（同一次打击被两个频段
#: 各报一次是正常的）；跨类则都留 —— "底鼓和踩镲同时响"本来就该出两个音。
_DRUM_CLASS: dict[str, str] = {
    "kick": "low", "kick_808": "low", "tom_low": "low", "tom_mid": "mid",
    "tom_high": "mid", "snare": "mid", "clap": "mid", "rim": "mid",
    "hat_closed": "high", "hat_pedal": "high", "hat_open": "high",
    "crash": "high", "ride": "high", "cowbell": "high",
    "cowbell_808": "high", "cowbell_latin": "high",
}


def tempo_envelope(envs: dict[str, np.ndarray], fallback: np.ndarray) -> np.ndarray:
    """估计速度时该看哪条包络：只有**低频 + 高频**（鼓所在的频段）。

    为什么把中频排除：中频是持续声部（铺底、旋律、贝斯泛音）的地盘，它们
    几乎每帧都在响，把包络垫得平平的，拍点冒不出来。实测示范曲：
    用合成包络估 132 BPM（错 37%），只留鼓的频段后鼓轨 stem 上是 96.0（准）。

    ``fallback`` 在这两段内容太少时启用（例如纯旋律，鼓频段几乎是空的），
    否则会在一条噪声底上瞎打分。
    """
    te = 0.6 * envs.get("low", fallback) + 0.4 * envs.get("high", fallback)
    if float(te.sum()) < 0.15 * float(fallback.sum()):
        return fallback
    return te


def _merge_frames(events: list[tuple[int, str, float]], *, window: int = 2
                  ) -> list[tuple[int, str, float]]:
    """同一时刻（±2 帧 ≈ 23ms）同一**粗类**的事件只留最强的一个。

    同一次打击被两个频段各报一次是正常的（军鼓的能量横跨低/中/高）；
    跨类则都留 —— "底鼓和踩镲同时响"本来就该出两个音（虽然归类那一步
    目前只会给出一件，见 classify_drum）。
    """
    kept: list[tuple[int, str, float]] = []
    for frame, piece, vel in sorted(events, key=lambda e: (e[0], -e[2])):
        klass = _DRUM_CLASS.get(piece, "mid")
        clash = None
        for i, (f2, p2, _v2) in enumerate(kept):
            if abs(f2 - frame) <= window and _DRUM_CLASS.get(p2, "mid") == klass:
                clash = i
                break
        if clash is None:
            kept.append((frame, piece, vel))
        elif vel > kept[clash][2]:
            kept[clash] = (frame, piece, vel)
    return kept


def _grid_bpm(x: np.ndarray, sr: int = SR, *, lo: float = 60.0, hi: float = 180.0,
              prefer: tuple[float, float] = (90.0, 150.0)) -> float:
    """网格对齐打分求速度：对每个候选速度，把拍点网格铺到包络上取平均强度。

    拍点必须**持续**落在重音上才得高分 —— 这正是它比自相关稳的地方：
    自相关只要"有周期性"就给高分，而 4:3、11:8 这类与真实网格没关系的错值
    在网格打分下会被压下去。最后把倍/半速歧义折进 [75, 150)。
    """
    if x.size < 16 or not np.any(x):
        return 120.0
    base = float(x.mean())
    frame_rate = sr / HOP
    best_score, best_bpm = -1e18, 120.0
    for bpm in np.arange(lo, hi + 1e-9, 0.5):
        period = frame_rate * 60.0 / bpm
        if period < 2.0:
            continue
        idx = (np.arange(1, int(x.size / period) + 1) * period).astype(np.int64)
        idx = idx[idx < x.size]
        if idx.size < 4:
            continue
        score = float(x[idx].mean()) - base
        if prefer[0] <= bpm <= prefer[1]:
            # 轻微偏向常用速度区间：对齐度相同时，人更可能听的是这个区间
            score *= 1.05
        if score > best_score:
            best_score, best_bpm = score, float(bpm)

    while best_bpm < 75.0:
        best_bpm *= 2.0
    while best_bpm >= 150.0:
        best_bpm /= 2.0
    return float(np.clip(best_bpm, lo, hi))


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------


def transcribe(audio: np.ndarray, sr: int = SR, *, bpm: float | None = None,
               steps_per_beat: int = 4, max_seconds: float = MAX_ANALYZE_SECONDS,
               ) -> TranscribeResult:
    """扒谱主入口。``audio`` 是 (channels, n) 或 (n,) 的 float 缓冲。"""
    mono = to_mono(audio)
    if mono.size < WIN * 2:
        return TranscribeResult(log=["音频太短，无法分析"])

    if mono.size > int(max_seconds * sr):
        mono = mono[: int(max_seconds * sr)]

    peak = float(np.max(np.abs(mono)))
    if peak > 1e-6:
        mono = mono / peak

    mag = _stft_mag(mono)
    envs = band_envelopes(mag, sr)
    env_all = envs["all"]
    # 候选起音 = **各频段起音的并集**，不只是合并包络上的峰。
    # 为什么：整曲混音里合并包络被中频的持续声部主导，低频那点底鼓增量根本
    # 冒不出峰（实测示范曲的 40 个候选里 band 全是 mid、衰减全是 600ms 上限，
    # 一记底鼓都进不了候选）。分频段各检一遍再取并集，底鼓与踩镲才都有机会。
    # 重复不会成灾：分类是"一个时刻一件鼓"，同类的近邻事件在 _merge_frames 里合并。
    candidates = sorted({f for env in envs.values() for f in detect_onsets(env, sr)})
    all_onsets = candidates

    if not all_onsets:
        return TranscribeResult(
            bpm=float(bpm) if bpm else 120.0, seconds=mono.size / sr,
            log=["没有检出起音：确认音频不是纯静音/纯噪声"])

    bands = band_signals(mono, sr)

    def _velocity(start: int, length: int) -> float:
        """这一段有多响 → 0.25~1.0 的力度。绝对能量没有意义，这里只是相对标定。"""
        seg = mono[start:min(mono.size, start + length)]
        energy = float(np.sqrt(np.mean(np.square(seg)))) if seg.size else 0.0
        return float(np.clip(0.35 + energy * 3.2, 0.25, 1.0))

    # ---- 打击乐：一次起音 = 一件鼓 -------------------------------------
    # 为什么不再逐频段各检一遍：实测同一个军鼓会在低/中/高三个频段各被检出
    # 一次（它的能量本来就横跨三段），于是"一下军鼓"变成三件鼓
    # （kick + snare + hat_open）。改成"一次起音定一件鼓"之后，
    # 判据交给**频段能量占比**——底鼓低频占 87%、军鼓只占 2%，分得很干净。
    # 代价是"底鼓和踩镲同时响"只会出一件（先看低频占比，所以出底鼓），
    # 这条限制写在模块 docstring 里。
    raw_drums: list[tuple[int, str, float]] = []
    for frame in all_onsets:
        feat = drum_features(bands, mag, frame, mag.shape[0], sr)
        # 下面几个门限**全部在这一下所属的频段上量**（feat.band）。
        # 用宽带量会互相拖累：实测"底鼓(340ms 尾巴)+每八分一记闭镲"里，
        # 闭镲的突出度被底鼓尾巴压到 1.4（阈值 2.0），每一记闭镲都被丢掉。
        benv = envs[feat.band]
        if float(benv[frame]) < MIN_ONSET_STRENGTH:
            continue                  # 静音尾巴上的一点噪声，不是起音
        prom = onset_prominence(benv, frame)
        rise = energy_rise(bands[feat.band], frame, sr)
        # 是不是打击乐：噪声型（平坦度高）或短促的都算，又长又有音高的不算。
        # 但低频段有个例外：**底下垫着持续低音时，底鼓的"衰减"会被低音顶长**
        # （实测带贝斯声部的整曲混音里，每一记底鼓都被判成"长音"丢掉，
        # 连带把速度也估错）。所以低频再补一条路：占比够 + 冲得够猛 + 能量在涨。
        if not (feat.flatness >= 0.42 or feat.decay_ms < 150.0):
            strong_low = (feat.band == "low" and feat.low_ratio >= 0.32
                          and prom >= 3.0 and rise >= 1.5)
            if not strong_low:
                continue
        if prom < 2.0:
            continue                  # 持续音中间的小起伏
        if rise < 1.2:
            continue                  # 音符释放时能量是降的，不算新起音
        raw_drums.append((frame, classify_drum(feat),
                          _velocity(frame * HOP, WIN * 4)))
    drum_result = _merge_frames(raw_drums)

    # ---- 速度：只用"鼓所在的频段"，不用中频 -----------------------------
    # 为什么把中频排除：中频是持续声部（铺底、旋律、贝斯泛音）的地盘，它们
    # 几乎每帧都在响，把包络垫得平平的，拍点冒不出来。实测示范曲：用合成包络
    # 估 132 BPM（错 37%），只留低频+高频之后鼓轨 stem 上是 96.0（准）。
    #
    # 也试过"用检出的鼓点当稀疏包络来估"，**更差**：8 个鼓点在 0.5 与 0.75 秒
    # 两种网格上都能对齐（都是 0.25s 的整数倍），于是 120 BPM 的鼓型被估成 80。
    # 密集事件反而给了网格打分更多约束 —— 这条弯路记在这里，免得再走一遍。
    if bpm is not None:
        est_bpm, bpm_source = float(bpm), "手动指定"
    else:
        est_bpm = estimate_bpm(tempo_envelope(envs, env_all), sr)
        bpm_source = "按鼓频段包络估得"

    res = TranscribeResult(bpm=est_bpm, seconds=mono.size / sr,
                           onsets=len(all_onsets))
    res.onset_seconds = [f * HOP / sr for f in all_onsets]
    res.log.append(f"起音 {len(all_onsets)} 个 · 速度 {est_bpm:.1f} BPM（{bpm_source}）")
    step_seconds = 60.0 / res.bpm / steps_per_beat

    def _step_of(frame: int) -> int:
        """帧号 → 1/16 网格步号。扒谱产物是要拿去编辑的，落在网格外没法对拍。"""
        return int(round(frame * HOP / sr / step_seconds))

    drum_frames = {f for f, _p, _v in drum_result}
    res.drums = [(_step_of(f), p, v) for f, p, v in drum_result]

    # ---- 有音高：整段起音里"不是打击乐"的那些 -------------------------
    # 音高路径按**下一个起音**截断：截到固定 1.2s 的话，窗口会盖住后面那个音，
    # 而"挑最像乐音的一窗"会挑到后一个音上 —— 实测整条旋律被整体拔高了一个音。
    for i, frame in enumerate(all_onsets):
        if any(abs(frame - f) <= 2 for f in drum_frames):
            continue                  # 这一下已经判成鼓了，别两边都报
        feat = drum_features(bands, mag, frame, mag.shape[0], sr)
        if feat.flatness > 0.55:
            continue                  # 宽带噪声，没有可估的基频
        # 音符的**释放**也会在通量上冒一个峰（实测每个音后面都多出一个假音高事件）。
        # 释放时能量是降的，所以"能量必须涨"这一条就能把它们全部挡掉。
        if energy_rise(mono, frame, sr) < 1.2:
            continue
        if onset_prominence(env_all, frame) < 1.5:
            continue
        start = frame * HOP
        next_frame = all_onsets[i + 1] if i + 1 < len(all_onsets) else mag.shape[0]
        stop = min(mono.size, max(start + WIN, next_frame * HOP))
        midi, _conf = _best_pitched_segment(mono, start, stop, sr)
        if midi is None:
            continue
        res.pitched.append((_step_of(frame), midi, _velocity(start, stop - start)))

    res.pitched = _merge_pitched(res.pitched)
    fit = _estimate_key(res, mag, sr)
    total = res.total
    # 置信度是给使用者看的"要不要信这一把"，所以只由三件可解释的事决定：
    # 有没有扒出东西、调式模板贴不贴、事件密度是否落在合理区间
    density = total / max(1.0, res.seconds)
    density_score = 1.0 - min(1.0, abs(density - 2.0) / 6.0)
    res.confidence = float(np.clip(
        0.25 + 0.30 * float(fit) + 0.25 * density_score
        + 0.20 * min(1.0, total / 24.0), 0.0, 1.0)) if total else 0.0
    res.log.append(f"扒出 {len(res.pitched)} 个有音高事件、{len(res.drums)} 个打击乐事件")
    return res


def _merge_pitched(events: list[tuple[int, int, float]], tolerance: int = 1
                   ) -> list[tuple[int, int, float]]:
    """相邻网格上的**同一个音高**只留一个。

    起音检测在同一次发声上偶尔会连着报两下（先瞬态、后腔体）。两条都写成音符
    的话，那一格会叠两个同高音符 —— 听感上只是变响，编辑时却是两个要分别删的
    音符，非常烦人。合并时留力度更大的那个。
    """
    out: list[tuple[int, int, float]] = []
    for step, pitch, vel in sorted(events, key=lambda e: (e[0], -e[2])):
        if out and pitch == out[-1][1] and abs(step - out[-1][0]) <= tolerance:
            if vel > out[-1][2]:
                out[-1] = (out[-1][0], pitch, vel)
            continue
        out.append((step, pitch, vel))
    return out


def _estimate_key(res: TranscribeResult, mag: np.ndarray, sr: int) -> float:
    """用音高事件的音级直方图配 Krumhansl 型模板定调，返回拟合度 0~1。

    为什么不直接对整段做 chroma：鼓和噪声会把 chroma 摊平，
    而"扒出来的音高"本身就是筛过一遍的干净信息，用它的直方图更稳。

    返回的是"最佳调相对最差调"的归一化差距 —— 所有调都差不多时它接近 0，
    使用者据此知道"这个调式结论别太当真"。
    """
    pcs = [p % 12 for _s, p, _v in res.pitched]
    if len(pcs) < 3:
        res.mode = "minor"
        return 0.0
    hist = np.zeros(12, dtype=np.float64)
    for pc in pcs:
        hist[pc] += 1.0
    hist /= hist.sum()

    major = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09,
                      2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
    minor = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53,
                      2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
    scores: list[tuple[float, int, str]] = []
    for pc in range(12):
        for mode, profile in (("major", major), ("minor", minor)):
            scores.append((float(np.dot(np.roll(profile, pc), hist)), pc, mode))
    scores.sort(reverse=True)
    best, worst = scores[0], scores[-1]
    res.key_pc, res.mode = best[1], best[2]
    span = max(1e-9, best[0] - worst[0])
    return float(np.clip((best[0] - scores[1][0]) / span * 2.0, 0.0, 1.0))


# --------------------------------------------------------------------------
# 产物 → 工程轨道
# --------------------------------------------------------------------------


def make_tracks(result: TranscribeResult, project, *, name_prefix: str = "扒谱",
                palette: list | None = None) -> list[Track]:
    """把扒谱结果变成两条（或一条）可编辑的轨道：旋律 + 鼓。

    ``step`` 是整曲绝对步号；工程按小节对齐，所以直接写进去即可。
    长度按"到下一个音为止"给足 —— 扒出来的音如果都是 1/16，听上去会像断奏。
    """
    tracks: list[Track] = []
    colors = [c.name() if hasattr(c, "name") else str(c) for c in (palette or [])]

    if result.pitched:
        melody = Track(name=f"{name_prefix}·旋律", kind="synth",
                       color=colors[0] if colors else "#3B88C3")
        steps = sorted(result.pitched)
        for i, (step, pitch, vel) in enumerate(steps):
            nxt = steps[i + 1][0] if i + 1 < len(steps) else step + 2
            length = int(np.clip(nxt - step, 1, 8))
            melody.add_note(Note(step=step, pitch=pitch, length=length, velocity=vel))
        tracks.append(melody)

    if result.drums:
        kit = Track(name=f"{name_prefix}·鼓", kind="drum",
                    color=colors[1] if len(colors) > 1 else "#C1694F")
        for step, piece, vel in result.drums:
            kit.add_note(Note(step=step, pitch=drum_to_pitch(piece), length=1,
                              velocity=vel))
        tracks.append(kit)

    return tracks


__all__ = [
    "TranscribeResult", "transcribe", "make_tracks", "to_mono", "onset_envelope",
    "band_envelopes", "band_signals", "band_shares", "tempo_envelope",
    "estimate_bpm", "detect_onsets", "classify_drum", "drum_features",
    "DrumFeatures", "spectral_flatness", "spectral_centroid", "energy_rise",
    "onset_prominence", "MAX_ANALYZE_SECONDS",
]
