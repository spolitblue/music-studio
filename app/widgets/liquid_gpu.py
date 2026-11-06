"""液态玻璃 · **GPU 版**（移植上游开源着色器，MIT）。

来源与授权
----------
折射曲线、超椭圆 SDF、极角高光这三样**不是**我们发明的，来自：

    OverShifted/LiquidGlass — assets/shaders/BatchRenderer2D.glsl
    Copyright (c) 2026 Sepehr Kalanaki
    MIT License

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in
    all copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.

原文与许可全文另存一份可溯源的副本：`_out/re/upstream/`。

**注意：不要照搬 InfiniteGUI 那一版。** 它的注释自己写着
*"Superellipse refraction adapted from OverShifted/LiquidGlass"* —— 是改编。
它把 `u_fPower` 从原版的 **3.0** 改成 **1.0**，并加了
`mix(p, ..., refractionStrength)` 做可调滑块，代价是折射带被**摊宽变柔**，
观感偏糊。逐项差异与实测见 `_out/re/upstream/COMPARISON.md`：

    同一份代码只改 u_fPower：边缘带最大像素差 179，中心块 0
    —— 指数 3 把折射**挤进最外圈**，中间大片区域保持原始画面不动，
       这才是它"透亮"的原因。

去掉了什么（都写明理由）
------------------------
* **进度条那一支**（`u_progressOverlayOnly`）：那是 HUD 的血条 UI，与本工程无关。
* **色差**（`u_chromaticAberrationEnabled`）：要多两次纹理采样，默认关。
* **外阴影**：Qt 侧已有 `QGraphicsDropShadowEffect`，默认关。
* **`uv.y = 1.0 - uv.y`**：参考实现那条管线里 v 的朝向与我们不同，
  照抄会把背后画面**上下镜像**（见 `main()` 里的注释）。

为什么要有这个模块
------------------
CPU + numpy 那套（`app/widgets/liquid.py`）做的是**同一件事**，但代价是
每块面板 12~30ms、还得靠缓存和降采样去藏。参考实现是**在片元着色器里逐像素算**，
**GPU 免费代劳**，所以它能在游戏里跟着每一帧跑。

移植保留了什么
--------------
* 超椭圆 SDF `sdSuperellipse`（`u_powerFactor` 决定圆 ↔ 方）；
* **上游原版的折射** `sampleP = p * pow(f(dist), u_fPower)`，`u_fPower = 3.0`；
* **上游原版的高光** `Glow() = sin(atan(y, x) - 0.5)` —— 极角正弦，单周期、
  一边亮对面暗；
* 上游原版的表面颗粒 `u_noise`；
* 为接到 Qt 控件上，额外保留了 InfiniteGUI 那侧的：经典圆角矩形回退
  （短边 < 112px 时按 `smoothstep(48,112,·)` 混合）、边缘抗锯齿、
  染色（本工程的设计令牌需要它，默认 alpha 取 profile 里的 0.039）、
  以及 `u_rect` 用**源纹理坐标**定位面板。
  **这几样是"接线"，不是"配方"** —— 配方部分一律用上游原版。

去掉了什么（都写明理由）
------------------------
* **进度条那一支**（`u_progressOverlayOnly`）：那是 HUD 的血条/进度条 UI，
  与本工程无关（保留会让移植版多出两百行死代码）。
* **色差**（`u_chromaticAberrationEnabled`）：profile 里是关的，且要多两次
  纹理采样；留了开关与实现，默认关。
* **外阴影**：profile 里 `outerShadowEnabled: false`，且 Qt 侧本来就有
  `QGraphicsDropShadowEffect`；保留公式但默认关。
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtOpenGL import (
    QOpenGLBuffer,
    QOpenGLShader,
    QOpenGLShaderProgram,
    QOpenGLTexture,
    QOpenGLVertexArrayObject,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget

# ==========================================================================
# 顶点着色器 —— 与参考实现同名同义（aPos / aTexCoord / v_TexCoord）
# ==========================================================================

VERTEX_SRC = """
#version 330 core
layout (location = 0) in vec2 aPos;
layout (location = 1) in vec2 aTexCoord;
out vec2 v_TexCoord;
void main()
{
    gl_Position = vec4(aPos, 0.0, 1.0);
    v_TexCoord = aTexCoord;
}
"""

# ==========================================================================
# 片元着色器 —— 照搬 HudLiquidGlass
# ==========================================================================

FRAGMENT_SRC = """
#version 330 core
out vec4 FragColor;
in vec2 v_TexCoord;

uniform sampler2D u_source;          // 面板背后的画面（这里是预模糊的环境背景）
uniform vec2  u_screenSize;          // u_source 的像素尺寸
uniform vec4  u_rect;                // 面板在 u_source 里的矩形 (x, y, w, h)
uniform float u_rounding;            // 经典圆角半径（px）
uniform int   u_liquidGlassStyle;    // 0=经典圆角矩形, 1=液态超椭圆
uniform int   u_refractionEnabled;
uniform int   u_highlightEnabled;
uniform int   u_chromaticAberrationEnabled;
uniform int   u_outerShadowEnabled;
uniform vec4  u_tintColor;
uniform float u_refractionStrength;
uniform float u_highlightStrength;
uniform float u_outerShadowStrength;
uniform float u_curvature;
uniform float u_shapeExpansion;
uniform float u_shadowPadding;
uniform vec2  u_highlightDirection;

// ---- ★ 上游 OverShifted/LiquidGlass 原版的四个折射曲线常量 -----------------
// 见 _out/re/upstream/COMPARISON.md：InfiniteGUI 把 u_fPower 从 3.0 改成了 1.0
// 并加了 mix(p,...,strength) 做滑块，折射带因此被摊宽、观感变糊。
// **要"透亮"，就得用原版的 3.0。**
uniform float u_fPower;      // 默认 3.0
uniform float u_glowWeight;  // 默认 0.3
uniform float u_glowBias;    // 默认 0.0
uniform float u_glowEdge0;   // 默认 0.06
uniform float u_glowEdge1;   // 默认 0.0
uniform float u_noise;       // 默认 0.1（表面颗粒）

// ---- 参考实现的常量，一个没改 -------------------------------------------
const float M_E = 2.718281828459045;
const float OverA = 0.7;
const float OverB = 2.3;
const float OverC = 5.2;
const float OverD = 6.9;
const float OverGlowWeight = 0.55;
const float OverGlowDirectionPower = 2.25;
const float HighlightEdgeSizePx = 1.75;
const float EdgeThickness = 1.0;
const float ChromaticFullStrengthPx = 4.0;
const float ChromaticMaxOffsetPx = 7.0;
const float AspectSliceStart = 1.15;
const float AspectSliceEnd = 1.75;
const float MaxSlicedCornerFraction = 0.42;
const float LargeShapeBlendStart = 48.0;
const float LargeShapeBlendEnd = 112.0;
const float MinVisibleCurvature = 0.15;

float sdBox(vec2 p, vec2 b) {
    vec2 q = abs(p) - b;
    return length(max(q, vec2(0.0))) + min(max(q.x, q.y), 0.0);
}

float sdSuperellipse(vec2 p, float n, float r) {
    vec2 pAbs = abs(p);
    if (n <= 16.0) {
        float numerator = pow(pAbs.x, n) + pow(pAbs.y, n) - pow(r, n);
        float denX = pow(pAbs.x, (2.0 * n) - 2.0);
        float denY = pow(pAbs.y, (2.0 * n) - 2.0);
        float denominator = n * sqrt(denX + denY) + 0.00001;
        return numerator / denominator;
    }
    float safeR = max(r, 0.0001);
    vec2 scaled = pAbs / safeR;
    float maxComponent = max(max(scaled.x, scaled.y), 0.0001);
    if (maxComponent < 0.25 && n > 16.0) {
        return sdBox(p, vec2(safeR));
    }
    vec2 unit = scaled / maxComponent;
    float powerSum = pow(unit.x, n) + pow(unit.y, n);
    float gradPowerSum = pow(unit.x, (2.0 * n) - 2.0) + pow(unit.y, (2.0 * n) - 2.0);
    float inverseMaxPower = exp(clamp(-n * log(maxComponent), -40.0, 40.0));
    float numerator = powerSum - inverseMaxPower;
    float denominator = n * sqrt(gradPowerSum) + 0.00001;
    return safeR * maxComponent * numerator / denominator;
}

float aspectRatioForSize(vec2 size) {
    vec2 safeSize = max(size, vec2(1.0));
    return max(safeSize.x, safeSize.y) / max(min(safeSize.x, safeSize.y), 1.0);
}

float sdSlicedLiquidRoundedRect(vec2 p, vec2 bodySize, float curvature, float powerFactor) {
    vec2 safeSize = max(bodySize, vec2(1.0));
    float shortSide = max(min(safeSize.x, safeSize.y), 1.0);
    float radiusPx = shortSide * MaxSlicedCornerFraction * clamp(curvature, 0.0, 1.0);
    if (radiusPx <= 0.001) {
        return sdBox(p, vec2(1.0)) * (shortSide * 0.5);
    }
    vec2 halfSize = safeSize * 0.5;
    vec2 localPx = p * halfSize;
    vec2 q = abs(localPx) - max(halfSize - vec2(radiusPx), vec2(0.0));
    float insideDist = min(max(q.x, q.y), 0.0);
    vec2 outside = max(q, vec2(0.0)) / max(vec2(radiusPx), vec2(1.0));
    float outsideDist = pow(pow(outside.x, powerFactor) + pow(outside.y, powerFactor),
                            1.0 / powerFactor) * radiusPx;
    return outsideDist + insideDist - radiusPx;
}

float sdClassicRoundedRect(vec2 p, vec2 bodySize, float radiusPx) {
    vec2 safeSize = max(bodySize, vec2(1.0));
    vec2 halfSize = safeSize * 0.5;
    float safeRadiusPx = clamp(radiusPx, 0.0, min(halfSize.x, halfSize.y));
    vec2 localPx = p * halfSize;
    vec2 q = abs(localPx) - max(halfSize - vec2(safeRadiusPx), vec2(0.0));
    return length(max(q, vec2(0.0))) + min(max(q.x, q.y), 0.0) - safeRadiusPx;
}

float f(float x) {
    return 1.0 - OverB * pow(OverC * M_E, -OverD * x - OverA);
}

float effectiveLiquidCurvature(float curvature) {
    float c = clamp(curvature, 0.0, 1.0);
    if (c <= 0.001) {
        return 0.0;
    }
    return mix(MinVisibleCurvature, 1.0, clamp((c - 0.01) / 0.99, 0.0, 1.0));
}

float curvatureToSuperellipsePower(float curvature) {
    float c = clamp(curvature, 0.0, 1.0);
    const float CircleDiagonal = 0.70710678;
    const float BoxLikeDiagonal = 0.98922801;
    float diagonal = mix(1.0, CircleDiagonal, c);
    diagonal = min(diagonal, BoxLikeDiagonal);
    float power = log(0.5) / log(diagonal);
    return clamp(power, 2.0, 64.0);
}

float adaptiveShapeDistance(vec2 p, vec2 bodySize, float curvature, float powerFactor) {
    float fullDistance = curvature <= 0.001
        ? sdBox(p, vec2(1.0))
        : sdSuperellipse(p, powerFactor, 1.0);
    float slicedDistancePx = sdSlicedLiquidRoundedRect(p, bodySize, curvature, powerFactor);
    float halfMinBody = max(min(bodySize.x, bodySize.y) * 0.5, 1.0);
    float slicedDistance = slicedDistancePx / halfMinBody;
    float aspect = aspectRatioForSize(bodySize);
    float sliceBlend = smoothstep(AspectSliceStart, AspectSliceEnd, aspect);
    return mix(fullDistance, slicedDistance, sliceBlend);
}

float shapeDistance(vec2 p, vec2 rectSize, vec2 bodySize, float curvature,
                    float powerFactor) {
    if (u_liquidGlassStyle == 0) {
        float halfMinBody = max(min(bodySize.x, bodySize.y) * 0.5, 1.0);
        return sdClassicRoundedRect(p, bodySize, u_rounding) / halfMinBody;
    }
    float effectiveCurvature = effectiveLiquidCurvature(curvature);
    float liquidDistance = adaptiveShapeDistance(p, bodySize, effectiveCurvature,
                                                 powerFactor);
    float classicRadiusPx = u_rounding;
    float halfMinBody = max(min(bodySize.x, bodySize.y) * 0.5, 1.0);
    float classicDistance = sdClassicRoundedRect(p, bodySize, classicRadiusPx)
                            / halfMinBody;
    float rectShortSide = min(rectSize.x, rectSize.y);
    float largeShapeBlend = smoothstep(LargeShapeBlendStart, LargeShapeBlendEnd,
                                       rectShortSide);
    return mix(liquidDistance, classicDistance, largeShapeBlend);
}

vec2 shapeDistanceGradient(vec2 p, vec2 rectSize, vec2 bodySize, float curvature,
                           float powerFactor) {
    const float h = 0.0025;
    float dx = shapeDistance(p + vec2(h, 0.0), rectSize, bodySize, curvature,
                             powerFactor)
             - shapeDistance(p - vec2(h, 0.0), rectSize, bodySize, curvature,
                             powerFactor);
    float dy = shapeDistance(p + vec2(0.0, h), rectSize, bodySize, curvature,
                             powerFactor)
             - shapeDistance(p - vec2(0.0, h), rectSize, bodySize, curvature,
                             powerFactor);
    return vec2(dx, dy) / (2.0 * h);
}

float shapeDistancePx(vec2 p, float d, vec2 rectSize, vec2 bodySize,
                      float curvature, float powerFactor) {
    vec2 gradP = shapeDistanceGradient(p, rectSize, bodySize, curvature, powerFactor);
    vec2 gradPx = gradP / (bodySize * 0.5);
    return d / max(length(gradPx), 0.0001);
}

float Glow(vec2 p, vec2 direction) {
    vec2 safeDirection = length(direction) > 0.0001
        ? normalize(direction)
        : normalize(vec2(0.70710678, -0.70710678));
    vec2 safeP = length(p) > 0.0001 ? normalize(p) : vec2(0.0);
    // Liquid-glass highlights are axis-aligned: both ends of the light axis
    // catch the same reflection.  Using the absolute projection also keeps
    // the perpendicular sides neutral instead of turning the opposite side
    // into an artificial inner shadow.        ← 参考实现原文注释
    return abs(dot(safeP, safeDirection));
}

vec2 limitChromaticOffset(vec2 offsetUv) {
    vec2 offsetPx = offsetUv * u_screenSize;
    float rawLengthPx = length(offsetPx);
    float limitedLengthPx = rawLengthPx;
    if (rawLengthPx > ChromaticFullStrengthPx) {
        float compressionRangePx = ChromaticMaxOffsetPx - ChromaticFullStrengthPx;
        limitedLengthPx = ChromaticFullStrengthPx +
            compressionRangePx *
            (1.0 - exp(-(rawLengthPx - ChromaticFullStrengthPx) / compressionRangePx));
    }
    offsetPx *= limitedLengthPx / max(rawLengthPx, 0.0001);
    return offsetPx / u_screenSize;
}

void main()
{
    vec2 rectSize = max(u_rect.zw - u_rect.xy, vec2(1.0));
    float expansion = max(u_shapeExpansion, 0.0);
    float drawExpansion = expansion;
    vec2 bodySize = max(rectSize + vec2(expansion * 2.0), vec2(1.0));
    vec2 drawSize = max(rectSize + vec2(drawExpansion * 2.0), vec2(1.0));
    vec2 bodyOffset = vec2(drawExpansion - expansion);
    vec2 localPx = v_TexCoord * drawSize;
    vec2 bodyLocalPx = localPx - bodyOffset;
    vec2 p = (bodyLocalPx / bodySize) * 2.0 - vec2(1.0);

    float effectiveCurvature = effectiveLiquidCurvature(u_curvature);
    float powerFactor = curvatureToSuperellipsePower(effectiveCurvature);
    float d = shapeDistance(p, rectSize, bodySize, u_curvature, powerFactor);
    float aaWidth = max(fwidth(d), 0.001);
    float edgeAlpha = 1.0 - smoothstep(-aaWidth, aaWidth, d);

    float shadowPadding = max(u_shadowPadding, 0.0);
    float signedDistPx = shapeDistancePx(p, d, rectSize, bodySize, u_curvature,
                                         powerFactor);
    float shadowDecayPx = max(shadowPadding * 0.72, 0.0001);
    float outsideMask = smoothstep(-aaWidth, aaWidth, d);
    float outsideDistPx = max(signedDistPx, 0.0);
    float outerShadowMask = float(u_outerShadowEnabled)
                            * clamp(u_outerShadowStrength, 0.0, 1.0);
    float centeredOuterShadow = (1.0 - smoothstep(0.0, shadowDecayPx, outsideDistPx))
                                * outsideMask;
    float outerShadowAlpha = clamp(centeredOuterShadow * 0.082 * outerShadowMask,
                                   0.0, 0.12);
    float materialAlpha = max(edgeAlpha, outerShadowAlpha);
    if (materialAlpha <= 0.001)
    {
        discard;
    }

    float dist = max(-d, 0.0);
    // ★★ 上游原版的折射：**一个纯径向缩放，没有 mix(p, ...) 强度混合。**
    //     `pow(f(dist), 3.0)` 把曲线再压一次：
    //        边界 f(0)≈0.64 → 0.64³≈0.262（缩得更狠 → 折射更强）
    //        dist>0.35 之后 pow 已经 >0.999（内部几乎立刻回到恒等）
    //     于是"折射"被挤进最靠边的一两个像素，中间大片区域是**未经改动的原画面**
    //     —— 这正是它看起来"透亮"的原因。
    vec2 sampleP = p * pow(f(dist), u_fPower);

    vec2 sampleLocalPx = bodyOffset + (sampleP * 0.5 + vec2(0.5)) * bodySize;
    vec2 screenUv = (u_rect.xy - vec2(drawExpansion) + localPx) / u_screenSize;
    vec2 refractedUv = (u_rect.xy - vec2(drawExpansion) + sampleLocalPx) / u_screenSize;

    // ★ 移植处的唯一一处语义改动：**去掉了参考实现的 `uv.y = 1.0 - uv.y`**。
    //
    // 参考实现这么做是对的 —— 它的 u_source 是"截屏拷贝"纹理，在那条
    // 管线里 v 的朝向与它的 p 空间相反，翻一下才对齐。
    // 而我们喂进来的是 QPixmap/QImage（**第一行在顶部**）直接上传的纹理，
    // v=0 就是顶行；再翻一次会把背后的画面**上下镜像**。
    // 实测过：竖向上暗下亮的源图，镜完面板顶端取到的是底部的亮色。
    // 所以这一处必须按我们自己的纹理约定改掉，而不是照抄。

    vec4 base = texture(u_source, refractedUv);
    if (u_chromaticAberrationEnabled == 1 && u_refractionEnabled == 1)
    {
        vec2 rawChromaOffset = (refractedUv - screenUv) * 0.25;
        vec2 chromaOffset = limitChromaticOffset(rawChromaOffset);
        base.r = texture(u_source, refractedUv + chromaOffset).r;
        base.b = texture(u_source, refractedUv - chromaOffset).b;
    }

    vec3 color = mix(base.rgb, u_tintColor.rgb, clamp(u_tintColor.a, 0.0, 1.0));

    // ★★ 上游原版的高光：**极角的正弦**。
    //     `sin(atan(y, x) - 0.5)` —— 绕边界走一圈是一个完整周期，
    //     所以是"一边亮、对面暗"的单侧高光，而不是两端对称。
    //     InfiniteGUI 换成了 `abs(dot(normalize(p), L))`（两端同亮），
    //     那是它自己的取舍，不是原版。
    float glow = sin(atan(p.y, p.x) - 0.5);
    float mul = glow * u_glowWeight * smoothstep(u_glowEdge0, u_glowEdge1, dist)
                + 1.0 + u_glowBias;
    color *= vec3(max(mul, 0.0));

    // ★ 上游原版的表面颗粒（u_noise，默认 0.1）
    float nz = fract(sin(dot(gl_FragCoord.xy * 1e-3, vec2(12.9898, 78.233)))
                     * 43758.5453) - 0.5;
    color += vec3(nz * u_noise);

    vec3 shadowColor = vec3(0.015, 0.018, 0.022);
    vec3 finalColor = mix(shadowColor, color, edgeAlpha / materialAlpha);
    FragColor = vec4(finalColor, materialAlpha);
}
"""


class LiquidGlassShader:
    """着色器程序的薄封装：编译一次、多次复用。

    编译失败时把 driver 的日志原样抛出来 —— GLSL 的错误信息比
    "程序没链接上"有用得多，藏起来只会浪费下一次调试。
    """

    def __init__(self) -> None:
        self.program = QOpenGLShaderProgram()
        if not self.program.addShaderFromSourceCode(
                QOpenGLShader.ShaderTypeBit.Vertex, VERTEX_SRC):
            raise RuntimeError("顶点着色器编译失败: " + self.program.log())
        if not self.program.addShaderFromSourceCode(
                QOpenGLShader.ShaderTypeBit.Fragment, FRAGMENT_SRC):
            raise RuntimeError("片元着色器编译失败: " + self.program.log())
        if not self.program.link():
            raise RuntimeError("着色器链接失败: " + self.program.log())

    def __getattr__(self, name):
        return getattr(self.program, name)


class LiquidGlassParams:
    """一次绘制需要的全部 uniform —— 默认值**直接取参考实现的 profile**。

    取自 `profile.json` 的 `type: "keystrokes"` 元素（用户实际启用的那份）：

        refractionStrength 1.5   curvature 0.62   highlightStrength 0.7
        highlightStaticAngleDegrees 315   edgeThickness 1.0
        tintColor (0.878431, 0.960784, 1.0, 0.039216)
        shapeExpansion 0.0   blur/outerShadow/chromatic 全关
    """

    __slots__ = ("rect", "screen_size", "rounding", "style", "refraction",
                 "refraction_strength", "highlight", "highlight_strength",
                 "highlight_angle_deg", "chromatic", "outer_shadow",
                 "outer_shadow_strength", "tint", "curvature", "shape_expansion",
                 "shadow_padding", "f_power", "glow_weight", "glow_bias",
                 "glow_edge0", "glow_edge1", "noise")

    def __init__(self) -> None:
        self.rect = QRectF(0, 0, 100, 100)
        self.screen_size = (100, 100)
        self.rounding = 14.0
        self.style = 1                 # 1 = 液态超椭圆
        self.refraction = 1
        self.refraction_strength = 1.5
        self.highlight = 1
        self.highlight_strength = 0.7
        self.highlight_angle_deg = 315.0
        self.chromatic = 0
        self.outer_shadow = 0
        self.outer_shadow_strength = 0.3
        self.tint = (0.878431, 0.960784, 1.0, 0.039216)
        self.curvature = 0.62
        self.shape_expansion = 0.0
        self.shadow_padding = 0.0
        # ---- 上游 OverShifted/LiquidGlass 原版默认值（一个没改）----------
        # 见 _out/re/upstream/COMPARISON.md：`f_power = 3.0` 是"透亮"的关键，
        # InfiniteGUI 改成 1.0 并加了强度滑块，观感因此变糊。
        self.f_power = 3.0
        self.glow_weight = 0.3
        self.glow_bias = 0.0
        self.glow_edge0 = 0.06
        self.glow_edge1 = 0.0
        self.noise = 0.1

    def highlight_direction(self) -> QPointF:
        """按 `highlightDirectionMode: "auto"` 的静态角度算方向向量。

        注意 GLSL 里 `p` 是**归一化到 [-1,1] 且 y 向下**的坐标，
        而屏幕角度是数学惯例（y 向上），所以这里取 −sin。
        """
        import math

        a = math.radians(self.highlight_angle_deg)
        return QPointF(math.cos(a), -math.sin(a))


# ==========================================================================
# 控件：把玻璃画在 GPU 上
# ==========================================================================


class GpuGlassWidget(QOpenGLWidget):
    """一块**用 GPU 画**的液态玻璃面（B 类：叠在我们自己的画面上）。

    它要什么
    --------
    一张"它下面是什么"的位图（`set_source`）。这块面坐在**我们自己的内容**
    之上，所以那张图是"本窗口已经画好的画面"，可以正常取到。

    ★ 为什么**只对 B 类成立** —— 这条边界绕不过去，写在这里免得以后再撞：
    游戏 overlay 能把整幅画面当纹理采样，是因为游戏画面和它在**同一个进程的
    同一个 GL 上下文**里；而我们的**桌面由 DWM 在进程外合成**，任何进程都
    拿不到那个纹理。所以坐在桌面上的 A 类面板只能走 CPU 那条降采样折射
    （`GlassSurface._ensure_realtime_refraction`），不能搬到这里来。

    绘制流程
    --------
    `paintGL()` 里画一个覆盖整个控件的全屏 quad，片元着色器按 `u_rect`
    从源纹理里取面板那一块、做超椭圆折射与极角高光。
    着色器公式**全部是上游 OverShifted/LiquidGlass 原版**（MIT，见模块头）。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysStackOnTop, True)
        # 注意：QOpenGLWidget 没有 setClearColor()（那是别的类上的 API）。
        # 清屏颜色在 paintGL() 里用 glClearColor(0,0,0,0) 直接设 —— 这样
        # 形状以外保持全透明，父控件的画面才能透上来。

        self._params = LiquidGlassParams()
        self._source_img: QImage | None = None
        self._source_dirty = False
        self._program: LiquidGlassShader | None = None
        self._tex: QOpenGLTexture | None = None
        self._vao: QOpenGLVertexArrayObject | None = None
        self._vbo: QOpenGLBuffer | None = None
        self._gl_ready = False
        self._gl_error = ""

    # -- 对外 ------------------------------------------------------------

    @property
    def params(self) -> LiquidGlassParams:
        return self._params

    @property
    def gl_ready(self) -> bool:
        return self._gl_ready

    @property
    def gl_error(self) -> str:
        return self._gl_error

    def set_source(self, pixmap: QPixmap | QImage | None,
                   rect: QRectF | None = None) -> None:
        """喂"这块面下面是什么"。

        ``rect`` 是**面板在源图里的矩形**（源图像素坐标）。着色器要的是
        ``u_rect = (x0, y0, x1, y1)`` 两个角 —— **不是 (x, y, w, h)**，
        传错会让采样整体偏移（这个坑踩过一次，见 `_out/test_gpu_shader.py`）。
        """
        if pixmap is None or (hasattr(pixmap, "isNull") and pixmap.isNull()):
            self._source_img = None
        else:
            img = pixmap.toImage() if isinstance(pixmap, QPixmap) else pixmap
            self._source_img = img.convertToFormat(QImage.Format.Format_RGBA8888)
            if rect is None:
                rect = QRectF(0, 0, self._source_img.width(),
                              self._source_img.height())
            self._params.rect = QRectF(rect)
            self._params.screen_size = (self._source_img.width(),
                                        self._source_img.height())
        self._source_dirty = True
        self.update()

    # -- GL --------------------------------------------------------------

    def initializeGL(self) -> None:
        try:
            self._program = LiquidGlassShader()
        except Exception as exc:  # noqa: BLE001
            self._gl_error = str(exc)[:300]
            return

        self._vao = QOpenGLVertexArrayObject()
        self._vao.create()
        self._vbo = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
        self._vbo.create()

        fns = self.context().functions()
        fns.glDisable(0x0BE2)      # GL_BLEND
        fns.glDisable(0x0B71)      # GL_DEPTH_TEST
        self._gl_ready = True

    def _upload_source(self) -> None:
        if self._source_img is None:
            return
        img = self._source_img
        w, h = img.width(), img.height()
        if self._tex is not None:
            same = (self._tex.width() == w and self._tex.height() == h)
        else:
            same = False
        if not same:
            if self._tex is not None:
                self._tex.destroy()
            self._tex = QOpenGLTexture(QOpenGLTexture.Target.Target2D)
            self._tex.setSize(w, h)
            self._tex.setFormat(QOpenGLTexture.TextureFormat.RGBA8_UNorm)
            self._tex.setMipLevels(1)
            self._tex.allocateStorage()
            self._tex.setWrapMode(QOpenGLTexture.WrapMode.ClampToEdge)
            self._tex.setMinificationFilter(QOpenGLTexture.Filter.Linear)
            self._tex.setMagnificationFilter(QOpenGLTexture.Filter.Linear)
        self._tex.setData(QOpenGLTexture.PixelFormat.RGBA,
                          QOpenGLTexture.PixelType.UInt8, img.constBits())
        self._source_dirty = False

    def paintGL(self) -> None:
        fns = self.context().functions()
        fns.glClearColor(0.0, 0.0, 0.0, 0.0)
        fns.glClear(0x4000)          # GL_COLOR_BUFFER_BIT
        if not self._gl_ready or self._program is None:
            return
        if self._source_dirty:
            self._upload_source()
        if self._tex is None:
            return

        dpr = self.devicePixelRatioF() or 1.0
        fns.glViewport(0, 0, int(self.width() * dpr), int(self.height() * dpr))

        p = self._program.program
        p.bind()
        self._vao.bind()
        self._vbo.bind()
        quad = _QUAD.tobytes()
        self._vbo.allocate(quad, len(quad))
        p.enableAttributeArray(0)
        p.setAttributeBuffer(0, 0x1406, 0, 2, 16)
        p.enableAttributeArray(1)
        p.setAttributeBuffer(1, 0x1406, 8, 2, 16)

        self._tex.bind(0)
        prm = self._params
        r = prm.rect
        p.setUniformValue1i("u_source", 0)
        p.setUniformValue("u_screenSize", float(prm.screen_size[0]),
                          float(prm.screen_size[1]))
        # ★ 两个角，不是 (x, y, w, h)
        p.setUniformValue("u_rect", float(r.x()), float(r.y()),
                          float(r.x() + r.width()), float(r.y() + r.height()))
        p.setUniformValue1f("u_rounding", float(prm.rounding))
        p.setUniformValue1i("u_liquidGlassStyle", int(prm.style))
        p.setUniformValue1i("u_refractionEnabled", int(prm.refraction))
        p.setUniformValue1i("u_highlightEnabled", int(prm.highlight))
        p.setUniformValue1i("u_chromaticAberrationEnabled", int(prm.chromatic))
        p.setUniformValue1i("u_outerShadowEnabled", int(prm.outer_shadow))
        d = prm.highlight_direction()
        p.setUniformValue("u_highlightDirection", float(d.x()), float(d.y()))
        p.setUniformValue1f("u_refractionStrength", float(prm.refraction_strength))
        p.setUniformValue1f("u_highlightStrength", float(prm.highlight_strength))
        p.setUniformValue1f("u_outerShadowStrength", float(prm.outer_shadow_strength))
        p.setUniformValue1f("u_curvature", float(prm.curvature))
        p.setUniformValue1f("u_shapeExpansion", float(prm.shape_expansion))
        p.setUniformValue1f("u_shadowPadding", float(prm.shadow_padding))
        p.setUniformValue1f("u_fPower", float(prm.f_power))
        p.setUniformValue1f("u_glowWeight", float(prm.glow_weight))
        p.setUniformValue1f("u_glowBias", float(prm.glow_bias))
        p.setUniformValue1f("u_glowEdge0", float(prm.glow_edge0))
        p.setUniformValue1f("u_glowEdge1", float(prm.glow_edge1))
        p.setUniformValue1f("u_noise", float(prm.noise))
        p.setUniformValue("u_tintColor", float(prm.tint[0]), float(prm.tint[1]),
                          float(prm.tint[2]), float(prm.tint[3]))

        fns.glDrawArrays(0x0005, 0, 4)      # GL_TRIANGLE_STRIP

        self._vbo.release()
        self._vao.release()
        p.release()
        self._tex.release(0)


#: 全屏三角带：aPos(x,y) + aTexCoord(u,v)
_QUAD = np.array([
    -1.0, -1.0, 0.0, 0.0,
     1.0, -1.0, 1.0, 0.0,
    -1.0,  1.0, 0.0, 1.0,
     1.0,  1.0, 1.0, 1.0,
], dtype=np.float32)
