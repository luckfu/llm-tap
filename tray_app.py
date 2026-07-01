"""
llm-tap tray application.

Runs the transparent proxy in a background thread and shows a menu-bar (macOS)
/ system-tray (Windows) icon that turns green with a count badge whenever a new
call is captured.

Entry point for the packaged .app / .exe. Also runnable directly:
    python3 tray_app.py            # default port 8000
    LLM_TAP_PORT=9000 python3 tray_app.py
"""

import os
import sys
import time
import threading
import webbrowser

from PIL import Image, ImageDraw
import pystray

import proxy_oneapi
from raw_storage import register_call_saved_callback


DEFAULT_PORT = 8000
DATA_DIR = os.path.expanduser("~/.llm-tap")
ACTIVE_DURATION = 2.0  # seconds the icon stays "active" after a captured call


class TrayApp:
    def __init__(self, port: int = DEFAULT_PORT):
        self.port = port
        self.count = 0
        self.active_until = 0.0
        self.lock = threading.Lock()
        self.icon = pystray.Icon(
            "llm-tap",
            self._draw_icon(active=False),
            "llm-tap",
            menu=pystray.Menu(
                pystray.MenuItem("llm-tap", None, enabled=False),
                pystray.MenuItem(lambda _: f"Captured: {self.count}", None, enabled=False),
                pystray.MenuItem(f"Port: {port}", None, enabled=False),
                pystray.MenuItem(f"http://127.0.0.1:{port}/", None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Open Web UI", self._open_web),
                pystray.MenuItem("Quit", self._quit),
            ),
        )

    # ---------- icon rendering (PIL, runtime) ----------

    def _draw_icon(self, active: bool, count: int = 0) -> Image.Image:
        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        # base circle: green when active, gray when idle
        color = (46, 204, 113, 255) if active else (150, 150, 150, 255)
        d.ellipse((6, 6, size - 6, size - 6), fill=color)
        # letter "T" in the center
        try:
            d.text((26, 18), "T", fill=(255, 255, 255, 255))
        except Exception:
            pass
        # count badge only while active
        if active and count > 0:
            bx, by, br = size - 14, 14, 12
            d.ellipse((bx - br, by - br, bx + br, by + br), fill=(231, 76, 60, 255))
            label = str(count) if count < 10 else "9+"
            d.text((bx - 4, by - 8), label, fill=(255, 255, 255, 255))
        return img

    def _refresh_icon(self) -> None:
        active = time.time() < self.active_until
        self.icon.icon = self._draw_icon(active=active, count=self.count)

    # ---------- callback fired from the proxy's asyncio thread ----------

    def _on_call_saved(self, meta: dict) -> None:
        with self.lock:
            self.count += 1
            self.active_until = time.time() + ACTIVE_DURATION
        # pystray icon update is thread-safe; runs on main thread
        self._refresh_icon()

    # ---------- menu actions ----------

    def _open_web(self, icon, item) -> None:
        webbrowser.open(f"http://127.0.0.1:{self.port}/")

    def _quit(self, icon, item) -> None:
        icon.stop()

    # ---------- main loop ----------

    def run(self) -> None:
        os.makedirs(DATA_DIR, exist_ok=True)
        os.chdir(DATA_DIR)

        # start the proxy in a background daemon thread
        proxy_oneapi.start_proxy_in_thread(
            port=self.port,
            config=os.path.join(DATA_DIR, "config.json"),
            log_level="INFO",
        )
        # subscribe to captured-call events
        register_call_saved_callback(self._on_call_saved)

        # pystray must run on the main thread (macOS NSApplication requirement)
        self.icon.run()


def main() -> None:
    port = int(os.environ.get("LLM_TAP_PORT", DEFAULT_PORT))
    TrayApp(port=port).run()


if __name__ == "__main__":
    main()
