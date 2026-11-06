"""把控制台页面截下来（离屏 QtWebEngine），用来做视觉自查。

为什么用它：控制台是给人看的界面，"HTTP 200 + JSON 对"只证明它能跑，
不证明它**看得清**。这个脚本把真实渲染出来的页面存成 PNG，
然后可以交给视觉管线判断排版有没有塌、文字有没有被裁掉。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")

from PySide6.QtCore import QTimer, QUrl                      # noqa: E402
from PySide6.QtWidgets import QApplication                   # noqa: E402
from PySide6.QtWebEngineWidgets import QWebEngineView        # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="控制台网页离屏截图")
    ap.add_argument("url", nargs="?", default="http://127.0.0.1:8770/")
    ap.add_argument("--out", default="_out/winter_island/console.png")
    ap.add_argument("--width", type=int, default=1680)
    ap.add_argument("--height", type=int, default=1050)
    ap.add_argument("--wait", type=float, default=4.0, help="等页面跑完 JS 的秒数")
    args = ap.parse_args(argv)

    app = QApplication(sys.argv[:1])
    view = QWebEngineView()
    view.resize(args.width, args.height)
    view.show()
    view.load(QUrl(args.url))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    state = {"ok": False}

    def shoot() -> None:
        pix = view.grab()
        state["ok"] = pix.save(str(out))
        app.quit()

    QTimer.singleShot(int(args.wait * 1000), shoot)
    app.exec()
    print(("OK " if state["ok"] else "FAILED ") + str(out))
    return 0 if state["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
