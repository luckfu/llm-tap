"""
llm-tap tray application.

Runs the transparent proxy in a background thread and shows a menu-bar (macOS)
/ system-tray (Windows) icon that turns green with a count badge whenever a new
call is captured.

Entry point for the packaged .app / .exe. Also runnable directly:
    python3 tray_app.py                  # default port 12345
    LLM_TAP_PORT=9000 python3 tray_app.py
"""

import os
import sys
import time
import json
import threading
import webbrowser

from PIL import Image, ImageDraw
import pystray

import proxy_oneapi
from raw_storage import register_call_saved_callback


DEFAULT_PORT = 12345
DATA_DIR = os.path.expanduser("~/.llm-tap")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
ACTIVE_DURATION = 2.0  # seconds the icon stays "active" after a captured call


def _load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_settings(settings: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


class TrayApp:
    def __init__(self, port: int = DEFAULT_PORT):
        self.port = port
        self.count = 0
        self.active_until = 0.0
        self.lock = threading.Lock()
        self.proxy_thread = None
        self.icon = pystray.Icon(
            "llm-tap",
            self._draw_icon(active=False),
            "llm-tap",
            menu=pystray.Menu(
                pystray.MenuItem("llm-tap", None, enabled=False),
                pystray.MenuItem(lambda _: f"Captured: {self.count}", None, enabled=False),
                pystray.MenuItem(lambda _: f"Port: {self.port}", None, enabled=False),
                pystray.MenuItem(lambda _: f"http://127.0.0.1:{self.port}/", None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Open Web UI", self._open_web),
                pystray.MenuItem("Settings...", self._open_settings),
                pystray.Menu.SEPARATOR,
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

    # ---------- proxy lifecycle ----------

    def _start_proxy(self) -> None:
        self.proxy_thread = proxy_oneapi.start_proxy_in_thread(
            port=self.port,
            config=os.path.join(DATA_DIR, "config.json"),
            log_level="INFO",
        )

    def _restart_proxy(self, new_port: int) -> None:
        """Restart the proxy thread with a new port.

        The old aiohttp AppRunner is inside a daemon thread's event loop which we
        can't cleanly stop from here; we simply abandon it and start a new thread
        bound to the new port. The old loop/thread exit when the app quits.
        """
        self.port = new_port
        self._start_proxy()

    # ---------- menu actions ----------

    def _open_web(self, icon, item) -> None:
        webbrowser.open(f"http://127.0.0.1:{self.port}/")

    def _open_settings(self, icon, item) -> None:
        """Open a tkinter settings window to configure the port."""
        try:
            import tkinter as tk
            from tkinter import ttk, messagebox
        except ImportError:
            return

        # tkinter must run on the main thread on macOS; pystray runs there too.
        # We create a transient window; the tray loop tolerates this.
        win = tk.Tk()
        win.title("llm-tap Settings")
        win.geometry("320x160")
        win.resizable(False, False)

        ttk.Label(win, text="Listen Port:").pack(pady=(20, 5))
        port_var = tk.StringVar(value=str(self.port))
        entry = ttk.Entry(win, textvariable=port_var, width=12, justify="center")
        entry.pack(pady=5)
        entry.focus_set()

        def _apply():
            try:
                p = int(port_var.get().strip())
                if not (1 <= p <= 65535):
                    raise ValueError
            except ValueError:
                messagebox.showerror("Invalid Port", "Please enter a port between 1 and 65535.", parent=win)
                return
            if p != self.port:
                self._restart_proxy(p)
                _save_settings({"port": p})
            win.destroy()

        btn_frame = ttk.Frame(win)
        btn_frame.pack(pady=10)
        ttk.Button(btn_frame, text="OK", command=_apply).pack(side="left", padx=8)
        ttk.Button(btn_frame, text="Cancel", command=win.destroy).pack(side="left", padx=8)

        win.mainloop()

    def _quit(self, icon, item) -> None:
        icon.stop()

    # ---------- main loop ----------

    def run(self) -> None:
        os.makedirs(DATA_DIR, exist_ok=True)
        os.chdir(DATA_DIR)

        # start the proxy in a background daemon thread
        self._start_proxy()
        # subscribe to captured-call events
        register_call_saved_callback(self._on_call_saved)

        # pystray must run on the main thread (macOS NSApplication requirement)
        self.icon.run()


def main() -> None:
    settings = _load_settings()
    port = int(os.environ.get("LLM_TAP_PORT") or settings.get("port") or DEFAULT_PORT)
    TrayApp(port=port).run()


if __name__ == "__main__":
    main()
