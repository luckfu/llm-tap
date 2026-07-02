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

from PIL import Image, ImageDraw, ImageFont, ImageFilter
import pystray

import proxy_oneapi
from raw_storage import register_call_saved_callback


DEFAULT_PORT = 12345
DEFAULT_DATA_DIR = os.path.expanduser("~/.llm-tap")
SETTINGS_FILE = os.path.join(DEFAULT_DATA_DIR, "settings.json")
ACTIVE_DURATION = 2.0  # seconds the icon stays "active" after a captured call

# LANCZOS moved between Pillow versions; resolve once at import time.
_RESAMPLE = getattr(Image, "LANCZOS", None) or Image.Resampling.LANCZOS


def _lan_ip() -> str:
    """Best-effort LAN IP of this machine (for displaying in the tray menu).

    Opens a UDP socket to a public address without sending anything; the OS
    picks the LAN interface address for the route. Falls back to 127.0.0.1.
    """
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _load_font(size: int):
    """Load a bold TTF for the count badge, falling back to PIL's default."""
    for p in (
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/System/Library/Fonts/SFNS.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
    ):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_settings(settings: dict) -> None:
    os.makedirs(DEFAULT_DATA_DIR, exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


class TrayApp:
    def __init__(self, port: int = DEFAULT_PORT, data_dir: str = DEFAULT_DATA_DIR):
        self.port = port
        self.data_dir = os.path.abspath(os.path.expanduser(data_dir))
        self.lan_ip = _lan_ip()
        self.count = 0
        self.active_until = 0.0
        self.lock = threading.Lock()
        self.proxy_handle = None
        self.icon = pystray.Icon(
            "llm-tap",
            self._draw_icon(active=False),
            "llm-tap",
            menu=pystray.Menu(
                pystray.MenuItem("llm-tap", None, enabled=False),
                pystray.MenuItem(lambda _: f"Captured: {self.count}", None, enabled=False),
                pystray.MenuItem(lambda _: f"Port: {self.port}", None, enabled=False),
                pystray.MenuItem(lambda _: f"Data: {self.data_dir}", None, enabled=False),
                pystray.MenuItem(lambda _: f"http://{self.lan_ip}:{self.port}/", None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Browse Data", self._open_web),
                pystray.MenuItem("Settings...", self._open_settings),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._quit),
            ),
        )

    # ---------- icon rendering (PIL, runtime) ----------

    def _draw_icon(self, active: bool, count: int = 0) -> Image.Image:
        """A polished teardrop ("tap") icon with a vertical gradient.

        Idle   = calm blue-teal droplet.
        Active = green droplet with a soft glow halo + a red count badge.
        Drawn at 4x and downsampled with LANCZOS for anti-aliased edges.
        """
        SS = 4  # supersample factor
        size = 64
        S = size * SS

        if active:
            top, bot = (170, 245, 190), (20, 170, 95)
            glow_color = (46, 204, 113, 150)
        else:
            top, bot = (160, 225, 240), (30, 125, 170)
            glow_color = None

        # droplet geometry (in supersampled space)
        cx = S // 2
        bulb_r = int(S * 0.30)
        bulb_cy = int(S * 0.60)
        apex_y = int(S * 0.10)

        # droplet mask = bulb circle + tapered top.
        # The triangle base sits inside the bulb so the circle smooths the shoulders.
        mask = Image.new("L", (S, S), 0)
        md = ImageDraw.Draw(mask)
        md.ellipse((cx - bulb_r, bulb_cy - bulb_r, cx + bulb_r, bulb_cy + bulb_r), fill=255)
        base_y = bulb_cy - int(bulb_r * 0.42)
        chord = int(bulb_r * 0.91)  # half-width of the bulb at base_y
        md.polygon([(cx - chord, base_y), (cx + chord, base_y), (cx, apex_y)], fill=255)

        # vertical gradient clipped to the droplet mask
        grad = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        gd = ImageDraw.Draw(grad)
        for y in range(S):
            t = y / (S - 1)
            r = int(top[0] * (1 - t) + bot[0] * t)
            g = int(top[1] * (1 - t) + bot[1] * t)
            b = int(top[2] * (1 - t) + bot[2] * t)
            gd.line([(0, y), (S - 1, y)], fill=(r, g, b, 255))
        grad.putalpha(mask)

        scene = Image.new("RGBA", (S, S), (0, 0, 0, 0))

        # soft glow halo behind the droplet (active only)
        if glow_color is not None:
            glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
            gld = ImageDraw.Draw(glow)
            gr = int(bulb_r * 1.35)
            gld.ellipse((cx - gr, bulb_cy - gr, cx + gr, bulb_cy + gr), fill=glow_color)
            glow = glow.filter(ImageFilter.GaussianBlur(7 * SS))
            scene = Image.alpha_composite(scene, glow)
        scene = Image.alpha_composite(scene, grad)

        # specular highlight on the upper-left of the bulb
        hl = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        hd = ImageDraw.Draw(hl)
        hd.ellipse(
            (cx - int(bulb_r * 0.55), bulb_cy - int(bulb_r * 0.62),
             cx - int(bulb_r * 0.08), bulb_cy - int(bulb_r * 0.18)),
            fill=(255, 255, 255, 75),
        )
        scene = Image.alpha_composite(scene, hl)

        # downsample for crisp anti-aliased edges
        img = scene.resize((size, size), _RESAMPLE)

        if active and count > 0:
            self._draw_badge(img, count)
        return img

    def _draw_badge(self, img: Image.Image, count: int) -> None:
        """Red count badge in the top-right corner."""
        d = ImageDraw.Draw(img)
        size = img.width
        cx, cy = size - 13, 13
        r = 11
        d.ellipse((cx - r - 1, cy - r - 1, cx + r + 1, cy + r + 1), fill=(255, 255, 255, 235))
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(231, 76, 60, 255))
        label = str(count) if count < 10 else "9+"
        font = _load_font(14)
        try:
            bbox = font.getbbox(label)
            w = bbox[2] - bbox[0]
            h = bbox[3] - bbox[1]
            tx = cx - w / 2 - bbox[0]
            ty = cy - h / 2 - bbox[1]
        except Exception:
            tx, ty = cx - 3, cy - 7
        d.text((tx, ty), label, font=font, fill=(255, 255, 255, 255))

    def _refresh_icon(self) -> None:
        active = time.time() < self.active_until
        self.icon.icon = self._draw_icon(active=active, count=self.count)
        # Force the menu to re-evaluate its dynamic (lambda) item labels.
        # Without this, macOS caches the old "Captured: N" text and only
        # refreshes it much later, when the menu is next opened.
        try:
            self.icon.update_menu()
        except Exception:
            pass

    # ---------- callback fired from the proxy's asyncio thread ----------

    def _on_call_saved(self, meta: dict) -> None:
        with self.lock:
            self.count += 1
            self.active_until = time.time() + ACTIVE_DURATION
        print(f"[llm-tap] captured #{self.count}: {meta.get('call_id')}", flush=True)
        # pystray icon update is thread-safe; runs on main thread
        self._refresh_icon()
        # revert to idle after the active window
        threading.Timer(ACTIVE_DURATION + 0.05, self._refresh_icon).start()

    # ---------- proxy lifecycle ----------

    def _start_proxy(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        os.chdir(self.data_dir)
        self.proxy_handle = proxy_oneapi.start_proxy_in_thread(
            port=self.port,
            config=os.path.join(self.data_dir, "config.json"),
            log_level="INFO",
        )

    def _restart_proxy(self, new_port: int, new_data_dir=None) -> None:
        """Restart the proxy thread with updated settings.

        Stops the old proxy's AppRunner so its listening socket is released,
        then starts a fresh proxy bound to the new port and data directory.
        """
        if self.proxy_handle is not None:
            try:
                self.proxy_handle.stop()
            except Exception as e:
                print(f"[llm-tap] failed to stop old proxy: {e}")
            self.proxy_handle = None
        self.port = new_port
        if new_data_dir is not None:
            self.data_dir = os.path.abspath(os.path.expanduser(new_data_dir))
        self._start_proxy()

    # ---------- menu actions ----------

    def _open_web(self, icon, item) -> None:
        webbrowser.open(f"http://127.0.0.1:{self.port}/")

    def _open_settings(self, icon, item) -> None:
        """Open a settings dialog to configure the port and data directory.
        """
        values = self._settings_dialog()
        if values is None:
            return
        new_port = values.get("port")
        new_data_dir = values.get("data_dir")
        try:
            p = int(str(new_port).strip())
            if not (1 <= p <= 65535):
                raise ValueError
        except ValueError:
            self._alert(f"Invalid port: {new_port!r}. Please enter a number between 1 and 65535.")
            return
        data_dir_text = str(new_data_dir or "").strip()
        if not data_dir_text:
            self._alert("Invalid data directory. Please enter a folder path.")
            return
        data_dir = os.path.abspath(os.path.expanduser(data_dir_text))
        try:
            os.makedirs(data_dir, exist_ok=True)
        except OSError as e:
            self._alert(f"Cannot use data directory: {e}")
            return

        port_changed = p != self.port
        data_dir_changed = data_dir != self.data_dir
        if port_changed or data_dir_changed:
            self._restart_proxy(p, data_dir)
            settings = _load_settings()
            settings.update({"port": p, "data_dir": data_dir})
            _save_settings(settings)
            self._alert("Settings saved. Proxy restarted.")

    def _settings_dialog(self):
        """Show a single cross-platform settings window in a helper process."""
        import subprocess

        env = os.environ.copy()
        env["LLM_TAP_DIALOG_PORT"] = str(self.port)
        env["LLM_TAP_DIALOG_DATA_DIR"] = self.data_dir
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--settings-dialog"]
        else:
            cmd = [sys.executable, os.path.abspath(__file__), "--settings-dialog"]
        try:
            r = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )
        except Exception:
            return None
        if r.returncode != 0:
            return None
        try:
            return json.loads(r.stdout.strip() or "{}")
        except json.JSONDecodeError:
            return None

    def _alert(self, msg: str) -> None:
        """Show an info alert (mac: osascript, others: tkinter)."""
        if sys.platform == "darwin":
            import subprocess
            subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "llm-tap"'])
        else:
            try:
                import tkinter as tk
                from tkinter import messagebox
                root = tk.Tk(); root.withdraw()
                messagebox.showinfo("llm-tap", msg)
                root.destroy()
            except Exception:
                pass

    def _quit(self, icon, item) -> None:
        icon.stop()

    # ---------- main loop ----------

    def run(self) -> None:
        # start the proxy in a background daemon thread
        self._start_proxy()
        # subscribe to captured-call events
        register_call_saved_callback(self._on_call_saved)

        # pystray must run on the main thread (macOS NSApplication requirement)
        self.icon.run()


def main() -> None:
    if "--settings-dialog" in sys.argv:
        _run_settings_dialog()
        return
    settings = _load_settings()
    port = int(os.environ.get("LLM_TAP_PORT") or settings.get("port") or DEFAULT_PORT)
    data_dir = os.environ.get("LLM_TAP_DATA_DIR") or settings.get("data_dir") or DEFAULT_DATA_DIR
    TrayApp(port=port, data_dir=data_dir).run()


def _run_settings_dialog() -> None:
    try:
        import tkinter as tk
        from tkinter import ttk, filedialog
    except Exception:
        sys.exit(1)

    port_default = os.environ.get("LLM_TAP_DIALOG_PORT", str(DEFAULT_PORT))
    data_dir_default = os.environ.get("LLM_TAP_DIALOG_DATA_DIR", DEFAULT_DATA_DIR)

    root = tk.Tk()
    root.title("llm-tap Settings")
    root.geometry("560x210")
    root.resizable(False, False)

    body = ttk.Frame(root, padding=18)
    body.pack(fill="both", expand=True)
    body.columnconfigure(1, weight=1)

    ttk.Label(body, text="Listen Port:").grid(row=0, column=0, sticky="w", pady=(0, 6))
    port_var = tk.StringVar(value=port_default)
    port_entry = ttk.Entry(body, textvariable=port_var, width=12)
    port_entry.grid(row=0, column=1, sticky="w", pady=(0, 6))
    port_entry.focus_set()

    ttk.Label(body, text="Data Directory:").grid(row=1, column=0, sticky="w", pady=(8, 6))
    data_dir_var = tk.StringVar(value=data_dir_default)
    data_entry = ttk.Entry(body, textvariable=data_dir_var, width=46)
    data_entry.grid(row=1, column=1, sticky="ew", pady=(8, 6))

    def browse():
        selected = filedialog.askdirectory(initialdir=data_dir_var.get() or data_dir_default)
        if selected:
            data_dir_var.set(selected)

    ttk.Button(body, text="Browse...", command=browse).grid(row=1, column=2, padx=(8, 0), pady=(8, 6))

    result = {"value": None}

    def ok():
        result["value"] = {"port": port_var.get(), "data_dir": data_dir_var.get()}
        root.destroy()

    def cancel():
        root.destroy()

    btns = ttk.Frame(root)
    btns.pack(pady=10)
    ttk.Button(btns, text="OK", command=ok).pack(side="left", padx=8)
    ttk.Button(btns, text="Cancel", command=cancel).pack(side="left", padx=8)
    root.bind("<Return>", lambda _event: ok())
    root.bind("<Escape>", lambda _event: cancel())
    root.protocol("WM_DELETE_WINDOW", cancel)
    root.mainloop()

    if result["value"] is None:
        sys.exit(2)
    sys.stdout.write(json.dumps(result["value"], ensure_ascii=False))


if __name__ == "__main__":
    main()
