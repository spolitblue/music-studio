"""从 Twemoji 命名树里策展制乐软件需要的图标，复制进本项目 assets/icons/。

自带校验：任何码点对不上就报错，避免静默丢图标。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

SRC = Path(r"C:\Users\LZY20\Desktop\harness\emojis")
DST = Path(__file__).resolve().parent.parent / "assets" / "icons"

# 逻辑名 -> codepoint（Twemoji 命名：小写十六进制，多码点用 - 连接，省略尾部 fe0f）
ICONS: dict[str, str] = {
    # ---- 走带控制 ----------------------------------------------------
    "play": "25b6",
    "pause": "23f8",
    "stop": "23f9",
    "record": "23fa",
    "play_pause": "23ef",
    "prev": "23ee",
    "next": "23ed",
    "rewind": "23ea",
    "fast_forward": "23e9",
    "loop": "1f501",
    "loop_one": "1f502",
    "shuffle": "1f500",
    "metronome": "23f1",
    "stopwatch": "23f2",
    "timer": "23f3",
    # ---- 乐器 / 音轨类型 ---------------------------------------------
    "piano": "1f3b9",
    "guitar": "1f3b8",
    "drums": "1f941",
    "trumpet": "1f3ba",
    "violin": "1f3bb",
    "saxophone": "1f3b7",
    "banjo": "1fa95",
    "accordion": "1fa97",
    "flute": "1fa88",
    "microphone": "1f3a4",
    "studio_mic": "1f399",
    "headphones": "1f3a7",
    "level_slider": "1f39a",
    "control_knobs": "1f39b",
    # ---- 音乐元素 ----------------------------------------------------
    "note": "1f3b5",
    "notes": "1f3b6",
    "score": "1f3bc",
    "speaker_high": "1f50a",
    "speaker_medium": "1f509",
    "speaker_low": "1f508",
    "speaker_mute": "1f507",
    "radio": "1f4fb",
    "cd": "1f4bf",
    "bell": "1f514",
    "bell_off": "1f515",
    "clapper": "1f3ac",
    # ---- 动作 --------------------------------------------------------
    "add": "2795",
    "remove": "2796",
    "divide": "2797",
    "multiply": "2716",
    "edit": "270f",
    "delete": "1f5d1",
    "save": "1f4be",
    "folder": "1f4c1",
    "folder_open": "1f4c2",
    "file": "1f4c4",
    "clipboard": "1f4cb",
    "memo": "1f4dd",
    "undo": "21a9",
    "redo": "21aa",
    "refresh": "1f504",
    "settings": "2699",
    "wrench": "1f527",
    "tools": "1f6e0",
    "search": "1f50d",
    "zoom_in": "1f50e",
    "lock": "1f512",
    "unlock": "1f513",
    "pin": "1f4cc",
    "link": "1f517",
    "download": "1f4e5",
    "upload": "1f4e4",
    "arrow_up": "2b06",
    "arrow_down": "2b07",
    "arrow_right": "27a1",
    "arrow_left": "2b05",
    "arrow_back": "1f519",
    "battery": "1f50b",
    "plug": "1f50c",
    "magnet": "1f9f2",
    "ruler": "1f4d0",
    "straight_ruler": "1f4cf",
    "triangular_ruler": "1f4d0",
    "abacus": "1f9ee",
    "keyboard": "2328",
    # ---- 状态 / AI / 灵感 --------------------------------------------
    "sparkles": "2728",
    "robot": "1f916",
    "bulb": "1f4a1",
    "star": "2b50",
    "glowing_star": "1f31f",
    "fire": "1f525",
    "heart": "2764",
    "check": "2705",
    "cross": "274c",
    "warning": "26a0",
    "question": "2753",
    "exclamation": "2757",
    "brain": "1f9e0",
    "crystal_ball": "1f52e",
    "magic_wand": "1fa84",
    "dice": "1f3b2",
    "chart_up": "1f4c8",
    "bar_chart": "1f4ca",
    "target": "1f3af",
    "rocket": "1f680",
    "zap": "26a1",
    "party": "1f389",
    "crown": "1f451",
    "gem": "1f48e",
    "trophy": "1f3c6",
    "medal": "1f3c5",
    # ---- 吉祥物候选（可爱动物）--------------------------------------
    "cat": "1f431",
    "bear": "1f43b",
    "rabbit": "1f430",
    "fox": "1f98a",
    "panda": "1f43c",
    "penguin": "1f427",
    "frog": "1f438",
    "owl": "1f989",
    "hedgehog": "1f994",
    "otter": "1f9a6",
    "koala": "1f428",
    "hamster": "1f439",
    "unicorn": "1f984",
    "whale": "1f433",
    "dolphin": "1f42c",
    "octopus": "1f419",
    "butterfly": "1f98b",
    "bee": "1f41d",
    "chick": "1f425",
    "bird": "1f426",
    "music_cat_hint": "1f408",
}

#: 命名树里同一码点可能有多个变体（带/不带 fe0f），按优先级尝试
CP_VARIANTS = ("{cp}", "{cp}-fe0f", "{cp}-fe0f-200d-2642-fe0f")


def main() -> int:
    names = json.loads((SRC / "names.json").read_text(encoding="utf-8"))
    by_cp: dict[str, dict] = {e["cp"]: e for e in names["emojis"]}

    DST.mkdir(parents=True, exist_ok=True)

    resolved: dict[str, dict] = {}
    missing: list[tuple[str, str]] = []

    for logical, cp in ICONS.items():
        hit = None
        for tmpl in CP_VARIANTS:
            cand = tmpl.format(cp=cp)
            if cand in by_cp:
                hit = by_cp[cand]
                break
        if hit is None:
            missing.append((logical, cp))
            continue
        resolved[logical] = {
            "cp": hit["cp"],
            "zh": hit["zh"],
            "en": hit["en"],
            "group": hit["group"],
            "subgroup": hit["subgroup"],
            "file": hit["file"],
        }

    # 复制
    copied = 0
    for logical, info in resolved.items():
        src_svg = SRC / "svg" / f"{info['cp']}.svg"
        if not src_svg.exists():
            missing.append((logical, f"{info['cp']} (SVG 缺失)"))
            continue
        shutil.copy2(src_svg, DST / f"{logical}.svg")
        copied += 1

    # 清单
    manifest = {
        "source": "Twemoji v17.0.3 (jdecked/twemoji)",
        "license": "CC-BY 4.0",
        "attribution": "Emoji graphics by Twemoji (https://github.com/jdecked/twemoji), CC-BY 4.0.",
        "viewBox": "36x36",
        "count": len(resolved),
        "icons": resolved,
    }
    (DST / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"解析成功 {len(resolved)} / 需求 {len(ICONS)}，复制 SVG {copied} 个 -> {DST}")
    if missing:
        print(f"\n!! 未找到 {len(missing)} 个：")
        for logical, cp in missing:
            print(f"   {logical:20s} {cp}")
        return 1
    print("全部图标齐备。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
