#!/usr/bin/env python3
"""
Lovable Pull — one file.

    lovable-pull "https://lovable.dev/projects/YOUR-PROJECT-ID"
    python lovable_crawler.py "https://lovable.dev/projects/YOUR-PROJECT-ID"
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8765
UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.I,
)
JWT_RE = re.compile(rb"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}")
CDP_URL = "http://127.0.0.1:9222"
API = "https://api.lovable.dev"
APP = "https://lovable.dev"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
READ_TOKEN_JS = """
() => new Promise((resolve) => {
  const done = (v) => resolve(v || null);
  try {
    const req = indexedDB.open("firebaseLocalStorageDb");
    req.onerror = () => done(null);
    req.onsuccess = () => {
      const db = req.result;
      if (![...db.objectStoreNames].includes("firebaseLocalStorage")) {
        done(null);
        return;
      }
      const tx = db.transaction("firebaseLocalStorage", "readonly");
      const store = tx.objectStore("firebaseLocalStorage");
      const all = store.getAll();
      all.onerror = () => done(null);
      all.onsuccess = () => {
        for (const row of all.result || []) {
          const val = row && row.value ? row.value : row;
          const token = val && val.stsTokenManager && val.stsTokenManager.accessToken;
          if (token) { done(token); return; }
        }
        done(null);
      };
    };
  } catch (e) {
    done(null);
  }
})
"""
EXPAND_AND_LIST_JS = """
() => {
  const clickText = (labels) => {
    const nodes = [...document.querySelectorAll("button, [role='button'], a")];
    for (const el of nodes) {
      const t = (el.innerText || el.getAttribute("aria-label") || "").trim();
      if (labels.some((l) => t.toLowerCase() === l || t.toLowerCase().includes(l))) {
        el.click();
        return t;
      }
    }
    return null;
  };
  clickText(["code", "view code", "code view"]);
  clickText(["expand all"]);
  const paths = new Set();
  for (const el of document.querySelectorAll("[data-file-path], [data-path], [data-filepath]")) {
    const p = el.getAttribute("data-file-path") || el.getAttribute("data-path") || el.getAttribute("data-filepath");
    if (p) paths.add(p.replace(/\\\\/g, "/"));
  }
  for (const el of document.querySelectorAll("[role='treeitem']")) {
    const p = el.getAttribute("data-path") || el.getAttribute("title") || "";
    const t = (el.innerText || "").trim().split("\\n")[0];
    const cand = p || t;
    if (cand && cand.includes(".")) paths.add(cand.replace(/\\\\/g, "/"));
  }
  return [...paths];
}
"""
MONACO_JS = """
() => {
  if (!(window.monaco && monaco.editor && monaco.editor.getModels)) return [];
  return monaco.editor.getModels().map((m) => ({
    path: (m.uri && m.uri.path) ? m.uri.path.replace(/^\\//, "") : "",
    value: m.getValue(),
  }));
}
"""


def bootstrap() -> None:
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        print("Installing playwright...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "playwright"])
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            exe = Path(p.chromium.executable_path)
            if not exe.exists():
                raise FileNotFoundError(str(exe))
    except Exception:
        print("Installing Chromium for Playwright...")
        subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])


class Hub:
    def __init__(self) -> None:
        self._cv = threading.Condition()
        self.events: list[dict] = []
        self.busy = False

    def emit(self, kind: str, **payload) -> dict:
        with self._cv:
            event = {"id": len(self.events) + 1, "type": kind, **payload, "t": time.time()}
            self.events.append(event)
            self._cv.notify_all()
        if kind == "log":
            print(payload.get("message", ""), flush=True)
        elif kind == "error":
            print("ERROR:", payload.get("message"), flush=True)
        elif kind == "done":
            print(f"Saved {payload.get('count')} files -> {payload.get('folder')}", flush=True)
        elif kind == "file":
            mark = "ok" if payload.get("ok") else "fail"
            print(f"  {mark} {payload.get('path')}", flush=True)
        return event

    def wait_after(self, last_id: int, timeout: float = 25.0) -> list[dict]:
        end = time.time() + timeout
        with self._cv:
            while True:
                nxt = [e for e in self.events if e["id"] > last_id]
                if nxt:
                    return nxt
                remaining = end - time.time()
                if remaining <= 0:
                    return []
                self._cv.wait(remaining)


HUB = Hub()
RUN_LOCK = threading.Lock()


def find_chrome() -> Path | None:
    roots = [
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    rels = [
        Path("Google") / "Chrome" / "Application" / "chrome.exe",
        Path("Microsoft") / "Edge" / "Application" / "msedge.exe",
    ]
    for root in roots:
        if not root:
            continue
        for rel in rels:
            cand = Path(root) / rel
            if cand.is_file():
                return cand
    return None


def chrome_user_data(exe: Path) -> Path:
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    if "msedge" in exe.name.lower():
        return local / "Microsoft" / "Edge" / "User Data"
    return local / "Google" / "Chrome" / "User Data"


def cdp_ready(timeout: float = 0.8) -> bool:
    try:
        urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=timeout)
        return True
    except Exception:
        return False


def chrome_is_running(exe: Path) -> bool:
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        out = subprocess.check_output(
            ["tasklist", "/FI", f"IMAGENAME eq {exe.name}", "/NH"],
            text=True,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        return exe.name.lower() in out.lower()
    except Exception:
        return False


def wait_for_cdp(seconds: float = 20.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if cdp_ready():
            return True
        time.sleep(0.25)
    return False


def window_title(hwnd: int) -> str:
    user32 = ctypes.windll.user32
    length = user32.GetWindowTextLengthW(hwnd)
    if length < 1:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return (buf.value or "").strip()


def chrome_window_handles() -> list[int]:
    user32 = ctypes.windll.user32
    found: list[int] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        name = window_title(hwnd)
        if not name:
            return True
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if cls.value != "Chrome_WidgetWin_1":
            return True
        low = name.lower()
        if "chrome legacy" in low or "cursor" in low:
            return True
        if "google chrome" in low or "microsoft edge" in low or "lovable" in low:
            found.append(hwnd)
        return True

    cb = WNDENUMPROC(_cb)
    user32.EnumWindows(cb, 0)
    lovable = [h for h in found if "lovable" in window_title(h).lower()]
    return lovable + [h for h in found if h not in lovable]


def wait_for_chrome_window(seconds: float = 40.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if chrome_window_handles():
            return True
        time.sleep(0.25)
    return False


def wait_for_lovable_title(seconds: float = 25.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if any("lovable" in t.lower() for t in chrome_titles()):
            return True
        time.sleep(0.4)
    return False


def focus_chrome_window() -> bool:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    hwnds = chrome_window_handles()
    if not hwnds:
        return False
    hwnd = hwnds[0]
    try:
        user32.AllowSetForegroundWindow(-1)
    except Exception:
        pass
    user32.ShowWindow(hwnd, 9)
    fg = user32.GetForegroundWindow()
    cur = kernel32.GetCurrentThreadId()
    other = user32.GetWindowThreadProcessId(hwnd, None)
    user32.AttachThreadInput(cur, other, True)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    user32.AttachThreadInput(cur, other, False)
    if fg:
        user32.AttachThreadInput(user32.GetWindowThreadProcessId(fg, None), other, False)
    return True


def place_chrome_on_primary_screen() -> bool:
    """Move Chrome onto the visible primary monitor before coordinate clicks."""
    hwnds = chrome_window_handles()
    if not hwnds:
        return False
    user32 = ctypes.windll.user32
    hwnd = hwnds[0]
    width = max(user32.GetSystemMetrics(0), 1024)
    height = max(user32.GetSystemMetrics(1), 768)
    user32.ShowWindow(hwnd, 9)
    user32.SetWindowPos(hwnd, 0, 0, 0, width, height, 0x0040)
    user32.ShowWindow(hwnd, 3)
    time.sleep(0.6)
    return focus_chrome_window()


def _send_vk(vk: int, up: bool = False) -> None:
    ctypes.windll.user32.keybd_event(vk, 0, 0x0002 if up else 0, 0)


def _send_unicode(text: str) -> None:
    KEYEVENTF_UNICODE = 0x0004
    KEYEVENTF_KEYUP = 0x0002
    INPUT_KEYBOARD = 1
    ULONG_PTR = ctypes.c_size_t

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", ctypes.c_ushort),
            ("wScan", ctypes.c_ushort),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", ctypes.c_long),
            ("dy", ctypes.c_long),
            ("mouseData", ctypes.c_ulong),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", ctypes.c_ulong),
            ("wParamL", ctypes.c_ushort),
            ("wParamH", ctypes.c_ushort),
        ]

    class INPUTUNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", ctypes.c_ulong), ("union", INPUTUNION)]

    extra = ULONG_PTR(0)
    for ch in text:
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            inp = INPUT()
            inp.type = INPUT_KEYBOARD
            inp.union.ki = KEYBDINPUT(0, ord(ch), flags, 0, extra)
            ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        time.sleep(0.012)


def set_clipboard_text(text: str) -> None:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    env = os.environ.copy()
    env["LOVABLE_URL"] = text
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value $env:LOVABLE_URL"],
        env=env,
        creationflags=flags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def type_url_in_search_bar(url: str) -> None:
    """Focus Chrome's address bar, paste the project URL, press Enter."""
    deadline = time.time() + 15
    while time.time() < deadline:
        if focus_chrome_window():
            break
        time.sleep(0.3)
    else:
        raise RuntimeError("Chrome opened but the window could not be focused.")
    time.sleep(1.2)
    set_clipboard_text(url)
    vk_ctrl, vk_l, vk_v, vk_enter = 0x11, 0x4C, 0x56, 0x0D
    _send_vk(vk_ctrl)
    _send_vk(vk_l)
    time.sleep(0.08)
    _send_vk(vk_l, up=True)
    _send_vk(vk_ctrl, up=True)
    time.sleep(0.35)
    _send_vk(vk_ctrl)
    _send_vk(vk_v)
    time.sleep(0.08)
    _send_vk(vk_v, up=True)
    _send_vk(vk_ctrl, up=True)
    time.sleep(0.25)
    _send_vk(vk_enter)
    time.sleep(0.08)
    _send_vk(vk_enter, up=True)


def last_profile_directory(user_data: Path) -> str:
    state_path = user_data / "Local State"
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        last = (data.get("profile") or {}).get("last_used")
        if isinstance(last, str) and last.strip():
            return last.strip()
    except Exception:
        pass
    return "Default"


def decode_jwt_payload(token: str) -> dict | None:
    try:
        payload = token.split(".")[1]
        pad = "=" * ((4 - len(payload) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(payload + pad))
    except Exception:
        return None


def is_lovable_token(token: str) -> bool:
    data = decode_jwt_payload(token)
    if not isinstance(data, dict):
        return False
    blob = json.dumps(data)
    return "gpt-engineer" in blob or "securetoken.google.com" in blob


def chrome_profile_dirs() -> list[Path]:
    exe = find_chrome()
    if exe is None:
        return []
    root = chrome_user_data(exe)
    found: list[Path] = []
    for name in ("Default", last_profile_directory(root)):
        path = root / name
        if path.is_dir() and path not in found:
            found.append(path)
    for path in sorted(root.glob("Profile *")):
        if path.is_dir() and path not in found:
            found.append(path)
    return found


def extract_token_from_chrome_profile() -> str:
    ranked: list[tuple[int, str]] = []
    for profile in chrome_profile_dirs():
        for sub in ("IndexedDB", "Local Storage", "Session Storage"):
            folder = profile / sub
            if not folder.is_dir():
                continue
            for file in folder.rglob("*"):
                if not file.is_file():
                    continue
                try:
                    size = file.stat().st_size
                except OSError:
                    continue
                if size == 0 or size > 40_000_000:
                    continue
                try:
                    blob = file.read_bytes()
                except OSError:
                    continue
                for match in JWT_RE.finditer(blob):
                    token = match.group().decode("ascii", "ignore")
                    if not is_lovable_token(token):
                        continue
                    payload = decode_jwt_payload(token) or {}
                    exp = int(payload.get("exp") or 0)
                    ranked.append((exp, token))
    if not ranked:
        return ""
    ranked.sort(key=lambda item: item[0], reverse=True)
    now = int(time.time())
    for exp, token in ranked:
        if exp >= now - 60:
            return token
    return ranked[0][1]


def click_uia_button(name: str) -> bool:
    script = (
        "Add-Type -AssemblyName UIAutomationClient; "
        "$root = [System.Windows.Automation.AutomationElement]::RootElement; "
        "$cond = New-Object System.Windows.Automation.PropertyCondition("
        "[System.Windows.Automation.AutomationElement]::NameProperty, '" + name.replace("'", "''") + "'); "
        "$el = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond); "
        "if ($el -eq $null) { Write-Output 'MISS'; exit 2 }; "
        "$pat = $el.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern); "
        "$pat.Invoke(); Write-Output 'CLICKED'"
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=flags,
        )
    except Exception:
        return False
    return "CLICKED" in (result.stdout or "")


def chrome_titles() -> list[str]:
    return [window_title(hwnd) for hwnd in chrome_window_handles() if window_title(hwnd)]


def ensure_chrome_on_project(url: str) -> None:
    exe = find_chrome()
    if exe is None:
        raise RuntimeError("Google Chrome was not found.")
    titles = chrome_titles()
    on_editor = any("lovable" in t.lower() for t in titles)
    if chrome_is_running(exe) and on_editor:
        focus_chrome_window()
        time.sleep(0.4)
        return
    if chrome_is_running(exe):
        type_url_in_search_bar(url)
    else:
        restart_user_chrome(url)
        type_url_in_search_bar(url)
    wait_for_lovable_title(25)
    time.sleep(1.5)


FILE_EXTS = {
    "ts", "tsx", "js", "jsx", "mjs", "cjs", "json", "css", "scss", "html", "md",
    "svg", "ico", "txt", "toml", "lock", "yml", "yaml", "map", "woff", "woff2",
    "png", "jpg", "jpeg", "gif", "webp", "gitignore", "prettierignore", "prettierrc",
}


def is_probably_file(name: str) -> bool:
    if name in (".gitignore", ".prettierignore", ".prettierrc"):
        return True
    if "." not in name:
        return False
    ext = name.rsplit(".", 1)[-1].lower()
    return ext in FILE_EXTS


def user_downloads() -> Path:
    for cand in (Path.home() / "Downloads", Path.home() / "OneDrive" / "Downloads"):
        if cand.is_dir():
            return cand
    return Path.home() / "Downloads"


def click_xy(x: float, y: float) -> None:
    user32 = ctypes.windll.user32
    hwnds = chrome_window_handles()
    dpi = user32.GetDpiForWindow(hwnds[0]) if hwnds and hasattr(user32, "GetDpiForWindow") else 96
    scale = max(float(dpi) / 96.0, 1.0)
    x = float(x) / scale
    y = float(y) / scale
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.08)
    vx = user32.GetSystemMetrics(76)
    vy = user32.GetSystemMetrics(77)
    vw = max(user32.GetSystemMetrics(78), 1)
    vh = max(user32.GetSystemMetrics(79), 1)
    abs_x = int((int(x) - vx) * 65535 / max(vw - 1, 1))
    abs_y = int((int(y) - vy) * 65535 / max(vh - 1, 1))
    ULONG_PTR = ctypes.c_size_t

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", ctypes.c_long),
            ("dy", ctypes.c_long),
            ("mouseData", ctypes.c_ulong),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", ctypes.c_ulong), ("union", INPUTUNION)]

    extra = ULONG_PTR(0)
    abs_flags = 0x0001 | 0x8000 | 0x4000
    for flags, dx, dy in (
        (abs_flags, abs_x, abs_y),
        (0x0002 | 0x8000 | 0x4000, abs_x, abs_y),
        (0x0004 | 0x8000 | 0x4000, abs_x, abs_y),
    ):
        inp = INPUT()
        inp.type = 0
        inp.union.mi = MOUSEINPUT(dx, dy, 0, flags, 0, extra)
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        time.sleep(0.04)


def screenshot_chrome(dest: Path) -> Path | None:
    hwnds = chrome_window_handles()
    if not hwnds:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    env = os.environ.copy()
    env["LOVABLE_HWND"] = str(int(hwnds[0]))
    env["LOVABLE_SHOT"] = str(dest)
    script = r"""
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class WinShot {
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr hWnd, IntPtr hdcBlt, int nFlags);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
}
"@
Add-Type -AssemblyName System.Drawing
$hwnd = [IntPtr][int64]$env:LOVABLE_HWND
$rect = New-Object WinShot+RECT
[WinShot]::GetWindowRect($hwnd, [ref]$rect) | Out-Null
$w = $rect.Right - $rect.Left
$h = $rect.Bottom - $rect.Top
if ($w -lt 50 -or $h -lt 50) { exit 2 }
$bmp = New-Object System.Drawing.Bitmap $w, $h
$g = [System.Drawing.Graphics]::FromImage($bmp)
$hdc = $g.GetHdc()
[WinShot]::PrintWindow($hwnd, $hdc, 2) | Out-Null
$g.ReleaseHdc($hdc)
$g.Dispose()
$bmp.Save($env:LOVABLE_SHOT, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()
Write-Output 'OK'
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
            creationflags=flags,
        )
    except Exception:
        return None
    if dest.is_file() and dest.stat().st_size > 100:
        return dest
    return None


def _uia_window_script() -> str:
    return r"""
Add-Type -AssemblyName UIAutomationClient
$root = [System.Windows.Automation.AutomationElement]::RootElement
$winCond = New-Object System.Windows.Automation.PropertyCondition(
  [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
  [System.Windows.Automation.ControlType]::Window)
$windows = $root.FindAll([System.Windows.Automation.TreeScope]::Children, $winCond)
$target = $null
foreach ($w in $windows) {
  $n = $w.Current.Name
  if ($n -match 'Lovable|Approval Hub') { $target = $w; break }
}
if ($target -eq $null) {
  foreach ($w in $windows) {
    if ($w.Current.Name -match 'Chrome') { $target = $w; break }
  }
}
if ($target -eq $null) { $target = $root }
"""


def _uia_run(script: str, extra_env: dict | None = None, timeout: float = 20) -> list[dict]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            creationflags=flags,
        )
    except Exception:
        return []
    raw = (result.stdout or "").strip()
    start = raw.find("[")
    if start < 0:
        return []
    raw = raw[start:]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def uia_named(name: str) -> list[dict]:
    script = _uia_window_script() + r"""
$name = $env:LOVABLE_UIA_NAME
$cond = New-Object System.Windows.Automation.PropertyCondition(
  [System.Windows.Automation.AutomationElement]::NameProperty, $name)
$found = $target.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)
$rows = @()
foreach ($el in $found) {
  try {
    $r = $el.Current.BoundingRectangle
    if ($r.Width -lt 4 -or $r.Height -lt 4) { continue }
    $rows += ('{"n":' + ($name | ConvertTo-Json -Compress) + ',"x":' + [int]$r.X + ',"y":' + [int]$r.Y + ',"w":' + [int]$r.Width + ',"h":' + [int]$r.Height + '}')
  } catch {}
}
'[' + ($rows -join ',') + ']'
"""
    return _uia_run(script, {"LOVABLE_UIA_NAME": name})


def uia_snapshot() -> list[dict]:
    script = _uia_window_script() + r"""
$rows = @()
foreach ($ct in @([System.Windows.Automation.ControlType]::TreeItem, [System.Windows.Automation.ControlType]::ListItem, [System.Windows.Automation.ControlType]::Button, [System.Windows.Automation.ControlType]::Hyperlink)) {
  $cond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty, $ct)
  $found = $target.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)
  foreach ($el in $found) {
    try {
      $n = $el.Current.Name
      if ([string]::IsNullOrWhiteSpace($n) -or $n.Length -gt 80) { continue }
      $r = $el.Current.BoundingRectangle
      if ($r.Width -lt 8 -or $r.Height -lt 8) { continue }
      $rows += ('{"n":' + ($n | ConvertTo-Json -Compress) + ',"x":' + [int]$r.X + ',"y":' + [int]$r.Y + ',"w":' + [int]$r.Width + ',"h":' + [int]$r.Height + '}')
    } catch {}
  }
}
'[' + ($rows -join ',') + ']'
"""
    return _uia_run(script, timeout=35)


def pick_tree_item(elements: list[dict], name: str, below_y: float | None = None) -> dict | None:
    cands = [e for e in elements if e.get("n") == name]
    rows = [e for e in cands if 10 <= int(e.get("h") or 0) <= 48 and int(e.get("w") or 0) < 560]
    use = rows or cands
    if below_y is not None:
        lower = [e for e in use if int(e.get("y") or 0) >= int(below_y) - 4]
        if lower:
            use = lower
    if not use:
        return None
    use.sort(key=lambda e: (int(e.get("x") or 0), int(e.get("y") or 0)))
    return use[0]


def pick_download_button(elements: list[dict]) -> dict | None:
    names = {"download", "download file", "download codebase"}
    cands = [e for e in elements if str(e.get("n") or "").strip().lower() in names]
    if not cands:
        return None
    cands.sort(key=lambda e: (-int(e.get("x") or 0), int(e.get("y") or 0)))
    return cands[0]


def _uia_text(script: str, extra_env: dict | None = None, timeout: float = 20) -> str:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            creationflags=flags,
        )
    except Exception:
        return ""
    return (result.stdout or "").strip()


def uia_invoke_name(name: str) -> bool:
    script = _uia_window_script() + r"""
$name = $env:LOVABLE_UIA_NAME
$cond = New-Object System.Windows.Automation.PropertyCondition(
  [System.Windows.Automation.AutomationElement]::NameProperty, $name)
$found = $target.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)
$pick = $null
foreach ($el in $found) {
  try {
    $r = $el.Current.BoundingRectangle
    if ($r.Height -lt 10 -or $r.Height -gt 48 -or $r.Width -ge 560) { continue }
    $pick = $el
    break
  } catch {}
}
if ($pick -eq $null -and $found.Count -gt 0) { $pick = $found.Item(0) }
if ($pick -eq $null) { Write-Output 'MISS'; exit 0 }
$ok = $false
foreach ($pat in @(
  [System.Windows.Automation.InvokePattern]::Pattern,
  [System.Windows.Automation.SelectionItemPattern]::Pattern,
  [System.Windows.Automation.LegacyIAccessiblePattern]::Pattern
)) {
  try {
    $p = $pick.GetCurrentPattern($pat)
    if ($pat -eq [System.Windows.Automation.InvokePattern]::Pattern) { $p.Invoke(); $ok = $true; break }
    if ($pat -eq [System.Windows.Automation.SelectionItemPattern]::Pattern) { $p.Select(); $ok = $true; break }
    if ($pat -eq [System.Windows.Automation.LegacyIAccessiblePattern]::Pattern) { $p.DoDefaultAction(); $ok = $true; break }
  } catch {}
}
try {
  $exp = $pick.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern)
  if ($exp.Current.ExpandCollapseState -ne [System.Windows.Automation.ExpandCollapseState]::Expanded) {
    $exp.Expand()
  }
  $ok = $true
} catch {}
if ($ok) { Write-Output 'INVOKED' } else { Write-Output 'NO_PATTERN' }
"""
    out = _uia_text(script, {"LOVABLE_UIA_NAME": name})
    return "INVOKED" in out


def click_tree_name(name: str, below_y: float | None = None) -> dict | None:
    el = pick_tree_item(uia_named(name), name, below_y=below_y)
    if not el:
        return None
    click_element(el)
    return el


def set_code_search(value: str) -> bool:
    search = pick_tree_item(uia_named("Search code"), "Search code")
    if not search:
        return False
    click_element(search)
    time.sleep(0.1)
    _press_shortcut(0x11, 0x41)  # Ctrl+A
    if value:
        set_clipboard_text(value)
        _press_shortcut(0x11, 0x56)  # Ctrl+V
    else:
        _send_vk(0x08)
        _send_vk(0x08, up=True)
    time.sleep(0.45)
    return True


def click_file_path(rel: str) -> dict | None:
    """Filter the tree by path, then click the matching file row."""
    name = rel.replace("\\", "/").rsplit("/", 1)[-1]
    set_code_search(rel)
    node = click_tree_name(name)
    if not node:
        set_code_search(name)
        node = click_tree_name(name)
    return node


def click_more_options_near(file_el: dict) -> bool:
    cands = uia_named("More options")
    if not cands:
        return False
    fy = int(file_el.get("y") or 0)
    fx = int(file_el.get("x") or 0) + int(file_el.get("w") or 0)
    cands.sort(key=lambda e: abs(int(e.get("y") or 0) - fy) + abs(int(e.get("x") or 0) - fx))
    click_element(cands[0])
    return True


def click_download_toolbar() -> bool:
    els = []
    for name in ("Download file", "Download", "Download codebase"):
        els.extend(uia_named(name))
    dl = pick_download_button(els)
    if not dl:
        return False
    click_element(dl)
    return True


def wait_for_file_tree(timeout: float = 60.0) -> list[str]:
    markers = ("Search code", "package.json", "src", "public", ".gitignore")
    deadline = time.time() + timeout
    seen: list[str] = []
    while time.time() < deadline:
        seen = [name for name in markers if uia_named(name)]
        if len(seen) >= 2:
            time.sleep(1.5)
            return seen
        time.sleep(0.8)
    return seen


def wait_click_download(timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if click_download_toolbar():
            return True
        time.sleep(0.6)
    return False


def save_as_dialog_handle() -> int | None:
    user32 = ctypes.windll.user32
    found: list[int] = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        title = window_title(hwnd).lower()
        if cls.value == "#32770" and ("save" in title or "download" in title):
            found.append(int(hwnd))
            return False
        return True

    user32.EnumWindows(callback_type(callback), 0)
    return found[0] if found else None


def _press_shortcut(modifier: int, key: int) -> None:
    _send_vk(modifier)
    _send_vk(key)
    time.sleep(0.06)
    _send_vk(key, up=True)
    _send_vk(modifier, up=True)


def save_download_dialog(dest: Path, timeout: float = 15.0) -> bool:
    """Navigate to the destination folder and keep Chrome's filename."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    hwnd = None
    while time.time() < deadline:
        hwnd = save_as_dialog_handle()
        if hwnd:
            break
        if dest.is_file():
            return True
        time.sleep(0.25)
    if not hwnd:
        return False

    user32 = ctypes.windll.user32
    user32.ShowWindow(hwnd, 9)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.2)

    # Only change the folder. Lovable/Chrome already supplies the correct
    # filename, including names such as .gitignore and project.json.
    set_clipboard_text(str(dest.parent))
    _press_shortcut(0x11, 0x4C)  # Ctrl+L: folder navigator
    time.sleep(0.1)
    _press_shortcut(0x11, 0x41)  # Ctrl+A
    _press_shortcut(0x11, 0x56)  # Ctrl+V
    time.sleep(0.1)
    _send_vk(0x0D)
    _send_vk(0x0D, up=True)
    time.sleep(0.35)
    _press_shortcut(0x12, 0x53)  # Alt+S: Save
    deadline = time.time() + timeout
    while time.time() < deadline:
        if dest.is_file():
            return True
        confirm = save_as_dialog_handle()
        if confirm:
            # Handles an overwrite confirmation when the test is repeated.
            _press_shortcut(0x12, 0x59)  # Alt+Y
        time.sleep(0.3)
    return dest.is_file()


def click_element(el: dict) -> None:
    x = int(el["x"]) + max(int(el["w"]) // 2, 8)
    y = int(el["y"]) + max(int(el["h"]) // 2, 6)
    click_xy(x, y)


def wait_and_move_download(filename: str, dest: Path, started: float, timeout: float = 25.0) -> bool:
    folder = user_downloads()
    deadline = time.time() + timeout
    dest.parent.mkdir(parents=True, exist_ok=True)
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    while time.time() < deadline:
        candidates = []
        for path in folder.glob("*"):
            if not path.is_file() or path.name.endswith(".crdownload"):
                continue
            if path.name == filename or path.name.startswith(stem):
                try:
                    if path.stat().st_mtime >= started - 1:
                        candidates.append(path)
                except OSError:
                    continue
        if candidates:
            newest = max(candidates, key=lambda p: p.stat().st_mtime)
            shutil.move(str(newest), str(dest))
            return True
        time.sleep(0.35)
    return False


def stop_chrome(exe: Path) -> None:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    kwargs = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "creationflags": flags,
    }
    subprocess.run(["taskkill", "/F", "/IM", exe.name, "/T"], **kwargs)
    subprocess.run(["taskkill", "/F", "/IM", "chrome.exe", "/T"], **kwargs)
    for _ in range(40):
        if not chrome_is_running(exe):
            return
        time.sleep(0.2)


def launch_user_chrome(exe: Path, url: str | None = None) -> None:
    user_data = chrome_user_data(exe)
    profile = last_profile_directory(user_data)
    port_file = user_data / "DevToolsActivePort"
    try:
        port_file.unlink()
    except OSError:
        pass
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    cmd = [
        str(exe),
        "--remote-debugging-port=9222",
        "--remote-allow-origins=*",
        f"--user-data-dir={user_data}",
        f"--profile-directory={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--force-renderer-accessibility",
    ]
    if url:
        cmd.append(url)
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
    )


def restart_user_chrome(url: str | None = None) -> None:
    exe = find_chrome()
    if exe is None:
        raise RuntimeError("Google Chrome was not found.")
    stop_chrome(exe)
    for _ in range(25):
        if not chrome_is_running(exe):
            break
        time.sleep(0.2)
    time.sleep(1.0)
    launch_user_chrome(exe, url)
    if not wait_for_chrome_window(40):
        raise RuntimeError("Your Chrome did not reopen.")
    time.sleep(2.5)


def chrome_debug_url() -> str | None:
    if cdp_ready():
        return CDP_URL
    exe = find_chrome()
    if exe is None:
        return None
    port_file = chrome_user_data(exe) / "DevToolsActivePort"
    try:
        port = int(port_file.read_text(encoding="utf-8").splitlines()[0].strip())
        probe = f"http://127.0.0.1:{port}"
        urllib.request.urlopen(f"{probe}/json/version", timeout=0.8)
        return probe
    except Exception:
        return None


def type_url_in_omnibox_playwright(page, url: str) -> None:
    page.bring_to_front()
    page.keyboard.press("Control+l")
    time.sleep(0.2)
    page.keyboard.press("Control+a")
    page.keyboard.type(url, delay=15)
    page.keyboard.press("Enter")


def connect_chrome_tab(playwright, url: str | None = None):
    last_err: Exception | None = None
    for _ in range(30):
        endpoint = chrome_debug_url() or CDP_URL
        try:
            browser = playwright.chromium.connect_over_cdp(endpoint)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = None
            for candidate in context.pages:
                if "lovable.dev" in (candidate.url or ""):
                    page = candidate
                    break
            if page is None and context.pages:
                page = context.pages[-1]
            if page is None:
                page = context.new_page()
            if url:
                type_url_in_omnibox_playwright(page, url)
            return browser, context, page
        except Exception as exc:
            last_err = exc
            time.sleep(0.4)
    raise RuntimeError(f"Could not use your Chrome: {last_err}")


def open_user_browser(playwright, url: str):
    """Use the Chrome you are already logged into. Type the link as soon as it opens."""
    if not (chrome_debug_url() or cdp_ready()):
        restart_user_chrome(url)
    print("Typing the Lovable link in Chrome's address bar...", flush=True)
    type_url_in_search_bar(url)
    if not (chrome_debug_url() or cdp_ready()):
        wait_for_cdp(20)
    if chrome_debug_url() or cdp_ready():
        browser, context, page = connect_chrome_tab(playwright, url)
        return browser, context, page, True
    raise RuntimeError(
        "Typed the link in your Chrome, but could not attach to download files. "
        "Leave that Chrome window open and run again."
    )


def desktop_dir() -> str:
    for cand in (Path.home() / "Desktop", Path.home() / "OneDrive" / "Desktop"):
        if cand.is_dir():
            return str(cand)
    return str(Path.home())


def extract_project_id(text: str) -> str | None:
    m = UUID_RE.search(text or "")
    return m.group(0).lower() if m else None


def editor_url(raw: str) -> str:
    project_id = extract_project_id(raw)
    if not project_id:
        raise ValueError("Paste a Lovable project link that contains the project id.")
    return f"{APP}/projects/{project_id}?view=codeEditor"


def safe_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\\\|?*]+', "-", (name or "").strip()) or "lovable-project"
    return cleaned.strip(" .")[:80]


def safe_join(root: Path, rel: str) -> Path:
    rel = (rel or "").replace("\\", "/").lstrip("/")
    parts = [p for p in rel.split("/") if p not in ("", ".", "..")]
    if not parts:
        raise ValueError("empty path")
    dest = (root.joinpath(*parts)).resolve()
    dest.relative_to(root.resolve())
    return dest


def api_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "*/*",
        "Origin": APP,
        "Referer": f"{APP}/",
        "User-Agent": UA,
    }


def http_get(url: str, token: str, timeout: int = 90) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=api_headers(token))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() if exc.fp else b""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, str(exc).encode("utf-8", "replace")


def coerce_file_body(body: bytes) -> bytes:
    if not body or body[:1] not in (b"{", b"["):
        return body
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body
    if not isinstance(data, dict):
        return body
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    if not isinstance(inner, dict):
        return body
    content = inner.get("content", inner.get("text"))
    if isinstance(content, str):
        if inner.get("encoding") == "base64" or inner.get("binary"):
            try:
                return base64.b64decode(content)
            except Exception:
                return content.encode("utf-8")
        return content.encode("utf-8")
    return body


def parse_file_list(payload: object) -> list[dict]:
    if payload is None:
        return []
    data = payload
    if isinstance(data, dict):
        if isinstance(data.get("files"), list):
            data = data["files"]
        elif isinstance(data.get("data"), list):
            data = data["data"]
        elif isinstance(data.get("data"), dict) and isinstance(data["data"].get("files"), list):
            data = data["data"]["files"]
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if isinstance(item, str):
            out.append({"path": item.replace("\\", "/"), "binary": False})
        elif isinstance(item, dict):
            path = item.get("path") or item.get("name") or item.get("filename")
            if path:
                out.append({
                    "path": str(path).replace("\\", "/"),
                    "binary": bool(item.get("binary")),
                    "size": item.get("size"),
                })
    return out


def pick_folder() -> str:
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.wm_attributes("-topmost", True)
        chosen = filedialog.askdirectory(title="Save Lovable project here")
        root.destroy()
        return chosen or ""
    except Exception:
        return ""


def open_folder(path: str) -> None:
    target = Path(path)
    if not target.exists():
        return
    if sys.platform == "win32":
        os.startfile(target)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target)])


def unzip_safe(zip_path: Path, dest: Path) -> int:
    count = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            if name.endswith("/"):
                continue
            try:
                out = safe_join(dest, name)
            except ValueError:
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(out, "wb") as dst:
                dst.write(src.read())
            count += 1
    return count


class Crawler:
    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self.token = ""
        self.file_list: list[dict] = []
        self.network_hits: list[str] = []

    def log(self, msg: str, **extra) -> None:
        self.hub.emit("log", message=msg, **extra)

    def run(self, raw_url: str, dest_dir: str) -> None:

        project_id = extract_project_id(raw_url)
        if not project_id:
            raise ValueError("Paste a Lovable project link that contains the project id.")
        dest_root = Path(dest_dir).expanduser()
        if not dest_dir.strip():
            raise ValueError("Choose a folder to save into.")
        dest_root.mkdir(parents=True, exist_ok=True)

        url = editor_url(raw_url)

        self.log("Reading your logged-in Lovable session")
        self.hub.emit("status", phase="session", detail="Reading Lovable session")
        self.token = extract_token_from_chrome_profile()
        files = self._list_via_api(project_id) if self.token else []
        if not files:
            self.log("Session unavailable — opening the project once to refresh it")
            self.hub.emit("status", phase="browser", detail="Refreshing Lovable session")
            ensure_chrome_on_project(url)
            self.token = extract_token_from_chrome_profile()
            files = self._list_via_api(project_id) if self.token else []
        if not files:
            raise RuntimeError("Could not load the complete file list from your Lovable session.")
        self.log(f"Found {len(files)} files.")
        self.hub.emit("files", total=len(files))

        project_name = self._project_name(project_id) or f"lovable-{project_id[:8]}"
        base = dest_root / safe_name(project_name)
        out_dir = base
        suffix = 2
        while out_dir.exists():
            out_dir = dest_root / f"{base.name} {suffix}"
            suffix += 1
        out_dir.mkdir(parents=True, exist_ok=True)
        self.hub.emit("status", phase="saving", detail=str(out_dir))
        self.log(f"Saving into {out_dir}")

        self.log("Downloading files directly in parallel")
        saved = self._download_files(project_id, files, out_dir, page=None)
        missing = []
        for item in files:
            try:
                if not safe_join(out_dir, item["path"]).is_file():
                    missing.append(item)
            except ValueError:
                missing.append(item)

        if missing:
            self.log(f"Recovering {len(missing)} files through the browser")
            self.hub.emit("status", phase="fallback", detail=f"Recovering {len(missing)} files")
            ensure_chrome_on_project(url)
            place_chrome_on_primary_screen()
            seen = wait_for_file_tree(40)
            if len(seen) >= 2:
                expand = click_tree_name("Expand all folders")
                if expand:
                    time.sleep(1.0)
                saved += self._download_by_clicks(missing, out_dir)
            else:
                self.log("Browser fallback unavailable: file tree did not load")
        if saved != len(files):
            raise RuntimeError(f"Downloaded {saved} of {len(files)} files into {out_dir}.")
        self.hub.emit("done", folder=str(out_dir), count=saved, method="parallel-api")
        self.log(f"Done. {saved} files saved.")

    def _test_one_download(self, out_dir: Path) -> int:
        place_chrome_on_primary_screen()
        time.sleep(0.4)
        self.hub.emit("files", total=1)
        self.log("Click folder public")
        folder = click_tree_name("public")
        file_name = "robots.txt"
        rel = "public/robots.txt"
        if not folder:
            self.log("Could not click public")
            return 0
        time.sleep(0.9)
        self.log(f"Click file {file_name}")
        node = click_tree_name(file_name)
        if not node:
            self.log("public was already open — clicking it again")
            click_tree_name("public")
            time.sleep(0.9)
            node = click_tree_name(file_name)
        if not node:
            self.log("Falling back to package.json")
            file_name = "package.json"
            rel = "package.json"
            node = click_tree_name(file_name)
        if not node:
            self.log("Could not click a file in the tree")
            return 0
        time.sleep(1.4)
        self.log("Click Download file once")
        started = time.time()
        if not wait_click_download(12):
            self.log("Opening the file ... menu")
            if node:
                click_more_options_near(node)
                time.sleep(0.6)
            if not wait_click_download(8):
                shot = screenshot_chrome(Path(__file__).resolve().parent / "_chrome.png")
                self.log("Download file button not found after opening the file")
                if shot:
                    self.log(f"Screenshot {shot}")
                return 0
        dest = safe_join(out_dir, rel)
        self.log(f"Paste folder path into Save As: {dest.parent}")
        if save_download_dialog(dest, timeout=20):
            self.hub.emit("file", path=rel, ok=True, done=1, total=1)
            self.log(f"Saved {rel}")
            return 1
        if wait_and_move_download(file_name, dest, started, timeout=30):
            self.hub.emit("file", path=rel, ok=True, done=1, total=1)
            self.log(f"Saved {rel}")
            return 1
        self.hub.emit("file", path=rel, ok=False, error="download missing", done=1, total=1)
        self.log("Download click happened but the file did not land in Downloads")
        return 0

    def _download_by_clicks(self, files: list[dict], out_dir: Path) -> int:
        saved = 0
        probe = uia_named("Search code") or uia_named("src") or uia_named("package.json")
        if not probe:
            self.log("Could not see the file tree in Chrome.")
            return 0
        place_chrome_on_primary_screen()
        total = len(files)
        for i, item in enumerate(files, start=1):
            rel = item["path"].replace("\\", "/")
            name = rel.rsplit("/", 1)[-1]
            if not name:
                continue
            try:
                self.log(f"[{i}/{total}] Search {rel}")
                node = click_file_path(rel)
                if not node:
                    self.hub.emit("file", path=rel, ok=False, error="file not in tree", done=i, total=total)
                    self.log(f"  fail {rel}: file not found in search")
                    continue
                self.log(f"Open {rel}")
                time.sleep(1.0)
                started = time.time()
                if not wait_click_download(8):
                    self.hub.emit("file", path=rel, ok=False, error="no Download", done=i, total=total)
                    self.log(f"  fail {rel}: Download file button not found")
                    continue
                dest = safe_join(out_dir, rel)
                self.log(f"Save into folder {dest.parent}")
                ok = save_download_dialog(dest, timeout=15)
                if not ok:
                    ok = wait_and_move_download(name, dest, started, timeout=12)
                if ok:
                    saved += 1
                    self.hub.emit("file", path=rel, ok=True, done=i, total=total)
                    self.log(f"  ok {rel}")
                else:
                    self.hub.emit("file", path=rel, ok=False, error="download missing", done=i, total=total)
                    self.log(f"  fail {rel}: downloaded file missing")
            except Exception as exc:
                self.hub.emit("file", path=rel, ok=False, error=str(exc), done=i, total=total)
                self.log(f"  fail {rel}: {exc}")
        set_code_search("")
        return saved

    def _wait_for_session(self, page, project_id: str, playwright, url: str, bind) -> object:
        deadline = time.time() + 15 * 60
        told = False
        while time.time() < deadline:
            try:
                closed = page.is_closed()
            except Exception:
                closed = True
            if closed:
                if cdp_ready():
                    self.log("Chrome switched profile — reconnecting to your window.")
                    _browser, _context, page = connect_chrome_tab(playwright, url)
                    bind(page)
                    page.goto(url, wait_until="domcontentloaded", timeout=120000)
                    told = False
                    continue
                raise RuntimeError("Browser window was closed.")
            try:
                token = page.evaluate(READ_TOKEN_JS)
            except Exception:
                token = None
            if token:
                self.token = token
            loc = page.url
            logged_in = bool(self.token) and (
                "/projects/" in loc or project_id in loc
            ) and "login" not in loc and "sign-in" not in loc
            if logged_in:
                self.hub.emit("status", phase="session", detail="Signed in")
                self.log("Session ready.")
                return page
            if not told:
                self.hub.emit("status", phase="login", detail="If Lovable asks, sign in in that tab")
                self.log("If Lovable asks, sign in in that Chrome tab.")
                told = True
            time.sleep(1.2)
        raise TimeoutError("Timed out waiting for Lovable sign-in.")

    def _open_code_view(self, page) -> None:
        self.hub.emit("status", phase="code", detail="Opening code view")
        for label in ("Code", "View code", "Code view"):
            loc = page.get_by_role("button", name=re.compile(rf"^{re.escape(label)}$", re.I))
            try:
                if loc.count() and loc.first.is_visible():
                    loc.first.click(timeout=3000)
                    self.log("Opened code view.")
                    page.wait_for_timeout(1200)
                    return
            except Exception:
                pass
        try:
            page.get_by_text("Code", exact=True).first.click(timeout=2500)
            page.wait_for_timeout(1200)
            self.log("Opened code view.")
        except Exception:
            self.log("Code view control not found — continuing from the current page.")

    def _click_download_button(self, page) -> None:
        self.log("Clicking Download in the code editor.")
        for loc in (
            page.get_by_role("button", name=re.compile(r"^download codebase$", re.I)),
            page.get_by_role("button", name=re.compile(r"^download$", re.I)),
            page.get_by_text("Download", exact=True),
        ):
            try:
                if loc.count() and loc.first.is_visible():
                    loc.first.click(timeout=4000)
                    page.wait_for_timeout(800)
                    self.log("Clicked Download.")
                    return
            except Exception:
                continue
        self.log("Download button not found.")

    def _download_tree_via_toolbar(self, page, files: list[dict], out_dir: Path) -> int:
        saved = 0
        total = len(files)
        for i, item in enumerate(files, start=1):
            rel = item["path"]
            name = rel.split("/")[-1]
            try:
                node = page.get_by_text(name, exact=True)
                if node.count():
                    node.first.click(timeout=4000)
                    page.wait_for_timeout(350)
                btn = page.get_by_role("button", name=re.compile(r"^download$", re.I))
                if not btn.count():
                    continue
                with page.expect_download(timeout=20000) as pending:
                    btn.first.click()
                download = pending.value
                dest = safe_join(out_dir, rel)
                dest.parent.mkdir(parents=True, exist_ok=True)
                download.save_as(str(dest))
                saved += 1
                self.hub.emit("file", path=rel, ok=True, done=i, total=total)
            except Exception as exc:
                self.hub.emit("file", path=rel, ok=False, error=str(exc), done=i, total=total)
        return saved

    def _try_official_zip(self, page, out_dir: Path) -> int:
        btn = page.get_by_text(re.compile(r"download codebase", re.I))
        try:
            if not btn.count() or not btn.first.is_visible():
                return 0
        except Exception:
            return 0
        self.log("Using Lovable's Download codebase button.")
        try:
            with page.expect_download(timeout=180000) as pending:
                btn.first.click()
            download = pending.value
            tmp = out_dir / "_lovable_codebase.zip"
            download.save_as(str(tmp))
            n = unzip_safe(tmp, out_dir)
            try:
                tmp.unlink()
            except OSError:
                pass
            return n
        except Exception as exc:
            self.log(f"Zip download skipped: {exc}")
            return 0

    def _project_name(self, project_id: str) -> str | None:
        if not self.token:
            return None
        for url in (
            f"{API}/v1/projects/{project_id}",
            f"{API}/projects/{project_id}",
        ):
            status, body = http_get(url, self.token, timeout=30)
            if status != 200:
                continue
            try:
                data = json.loads(body.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                inner = data.get("data") if isinstance(data.get("data"), dict) else data
                for key in ("display_name", "displayName", "name", "title", "slug"):
                    val = inner.get(key) if isinstance(inner, dict) else None
                    if isinstance(val, str) and val.strip():
                        return val.strip()
        return None

    def _list_via_api(self, project_id: str) -> list[dict]:
        if not self.token:
            return []
        urls = [
            f"{API}/projects/{project_id}/git/files?ref=main",
            f"{API}/v1/projects/{project_id}/git/files?ref=main",
            f"{API}/v1/git/files?project_id={project_id}&ref=main",
        ]
        collected: list[dict] = []
        for url in urls:
            cursor = None
            for _ in range(40):
                full = url + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else "")
                status, body = http_get(full, self.token, timeout=45)
                if status == 401:
                    self.log("Session expired while listing files.")
                    return []
                if status != 200:
                    break
                try:
                    payload = json.loads(body.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    break
                batch = parse_file_list(payload)
                if not batch:
                    break
                collected.extend(batch)
                cursor = None
                if isinstance(payload, dict):
                    cursor = payload.get("next_cursor") or payload.get("cursor") or (
                        payload.get("pagination") or {}
                    ).get("next_cursor")
                if not cursor:
                    break
            if collected:
                break
        uniq = {}
        for item in collected:
            uniq[item["path"]] = item
        return list(uniq.values())

    def _list_via_tree(self, page) -> list[str]:
        try:
            page.evaluate(EXPAND_AND_LIST_JS)
            page.wait_for_timeout(800)
            paths = page.evaluate(EXPAND_AND_LIST_JS)
            return [p for p in (paths or []) if isinstance(p, str)]
        except Exception:
            return []

    def _list_via_monaco(self, page) -> list[dict]:
        try:
            models = page.evaluate(MONACO_JS) or []
        except Exception:
            return []
        files = []
        for m in models:
            path = (m.get("path") or "").lstrip("/")
            if path and m.get("value") is not None:
                files.append({"path": path, "value": m["value"]})
        return files

    def _download_files(self, project_id: str, files: list[dict], out_dir: Path, page) -> int:
        self.hub.emit("status", phase="download", detail="Pulling files")
        token_holder = {"token": self.token}
        saved = 0
        errors = 0
        total = len(files)

        def pull(item: dict) -> tuple[str, bool, str]:
            rel = item["path"]
            if item.get("value") is not None:
                try:
                    dest = safe_join(out_dir, rel)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(item["value"], encoding="utf-8")
                    return rel, True, ""
                except Exception as exc:
                    return rel, False, str(exc)
            encoded = urllib.parse.quote(rel, safe="")
            urls = [
                f"{API}/projects/{project_id}/git/file?path={encoded}&ref=main",
                f"{API}/v1/projects/{project_id}/git/file?path={encoded}&ref=main",
            ]
            last_err = "no response"
            for url in urls:
                status, body = http_get(url, token_holder["token"])
                if status == 401:
                    return rel, False, "401"
                if status == 200:
                    try:
                        dest = safe_join(out_dir, rel)
                    except ValueError:
                        return rel, False, "bad path"
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(coerce_file_body(body))
                    return rel, True, ""
                last_err = f"HTTP {status}"
            return rel, False, last_err

        workers = min(16, max(1, total))
        self.log(f"Using {workers} parallel downloads")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(pull, item): item for item in files}
            for i, fut in enumerate(as_completed(futures), start=1):
                item = futures[fut]
                try:
                    rel, ok, err = fut.result()
                except Exception as exc:
                    rel, ok, err = item["path"], False, str(exc)
                if not ok and err == "401":
                    if page is not None:
                        try:
                            fresh = page.evaluate(READ_TOKEN_JS)
                            if fresh:
                                token_holder["token"] = fresh
                                self.token = fresh
                        except Exception:
                            pass
                        rel, ok, err = pull(item)
                    else:
                        fresh = extract_token_from_chrome_profile()
                        if fresh:
                            token_holder["token"] = fresh
                            self.token = fresh
                            rel, ok, err = pull(item)
                if ok:
                    saved += 1
                    self.hub.emit("file", path=rel, ok=True, done=i, total=total)
                else:
                    errors += 1
                    self.hub.emit("file", path=rel, ok=False, error=err, done=i, total=total)
                    self.log(f"Skipped {rel} ({err})")
        self.log(f"Wrote {saved}/{total} files" + (f" · {errors} skipped" if errors else ""))
        return saved

    def _crawl_editor(self, page, files: list[dict], out_dir: Path) -> int:
        self.log("Walking the editor file tree.")
        saved = 0
        for i, item in enumerate(files, start=1):
            rel = item["path"]
            name = rel.split("/")[-1]
            try:
                node = page.get_by_text(name, exact=True)
                if node.count():
                    node.first.click(timeout=4000)
                    page.wait_for_timeout(350)
                models = page.evaluate(MONACO_JS) or []
                content = None
                for m in models:
                    mp = (m.get("path") or "").replace("\\", "/")
                    if mp.endswith(rel) or mp.endswith(name):
                        content = m.get("value")
                        break
                if content is None and models:
                    content = models[-1].get("value")
                if content is None:
                    continue
                dest = safe_join(out_dir, rel)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content, encoding="utf-8")
                saved += 1
                self.hub.emit("file", path=rel, ok=True, done=i, total=len(files))
            except Exception as exc:
                self.hub.emit("file", path=rel, ok=False, error=str(exc), done=i, total=len(files))
        return saved


def start_job(url: str, dest: str) -> None:
    if not RUN_LOCK.acquire(blocking=False):
        HUB.emit("error", message="A download is already running.")
        return
    HUB.busy = True
    HUB.emit("status", phase="start", detail="Starting")
    try:
        Crawler(HUB).run(url, dest)
    except Exception as exc:
        HUB.emit("error", message=str(exc))
        HUB.emit("log", message=str(exc))
        traceback.print_exc()
    finally:
        HUB.busy = False
        HUB.emit("idle")
        RUN_LOCK.release()


PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lovable Pull</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Archivo+Black&family=Archivo+Narrow:wght@500;700&family=Atkinson+Hyperlegible:wght@400;700&family=Fragment+Mono&display=swap" rel="stylesheet">
<style>
  :root {
    --wall: #9eb7a8;
    --wall-deep: #7f9a8b;
    --ticket: #fff6ea;
    --ink: #17231c;
    --mute: #5c6b62;
    --stamp: #c1121f;
    --stamp-ink: #fff6ea;
    --carbon: #23415f;
    --ribbon: #d7f3c4;
    --line: #c9b8a0;
    --ok: #1f7a4d;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; min-height: 100%; }
  body {
    color: var(--ink);
    font-family: "Atkinson Hyperlegible", system-ui, sans-serif;
    background:
      radial-gradient(circle at 18px 18px, #6d8678 1.5px, transparent 1.7px) 0 0 / 28px 28px,
      linear-gradient(160deg, var(--wall) 0%, var(--wall-deep) 100%);
    min-height: 100vh;
    padding: 28px 18px 40px;
  }
  .stage {
    max-width: 1100px;
    margin: 0 auto;
    display: grid;
    grid-template-columns: minmax(320px, 420px) 1fr;
    gap: 22px;
    align-items: stretch;
  }
  .ticket {
    background: var(--ticket);
    border: 1px solid #1a1a1a;
    box-shadow: 8px 10px 0 #17231c;
    position: relative;
    padding: 28px 26px 26px 38px;
    min-height: 640px;
  }
  .ticket::before {
    content: "";
    position: absolute;
    left: -10px; top: 18px; bottom: 18px; width: 20px;
    background: radial-gradient(circle, var(--wall-deep) 6px, transparent 7px) 0 0 / 20px 28px;
  }
  .kicker {
    font-family: "Archivo Narrow", sans-serif;
    letter-spacing: .22em;
    font-size: 12px;
    font-weight: 700;
    text-transform: uppercase;
    color: var(--mute);
  }
  h1 {
    font-family: "Archivo Black", sans-serif;
    font-size: 42px;
    line-height: .9;
    margin: 10px 0 8px;
    letter-spacing: -0.03em;
  }
  .lede { color: var(--mute); margin: 0 0 28px; max-width: 32ch; }
  label {
    display: block;
    font-family: "Archivo Narrow", sans-serif;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: .14em;
    text-transform: uppercase;
    margin: 0 0 8px;
  }
  .field { margin-bottom: 18px; }
  input[type="text"] {
    width: 100%;
    border: 0;
    border-bottom: 2px dotted var(--ink);
    background: transparent;
    font: 15px "Fragment Mono", ui-monospace, monospace;
    padding: 8px 0 10px;
    color: var(--ink);
    outline: none;
  }
  input[type="text"]:focus { border-bottom-style: solid; }
  .row { display: flex; gap: 8px; align-items: end; }
  .row input { flex: 1; }
  .ghost {
    font-family: "Archivo Narrow", sans-serif;
    font-weight: 700;
    letter-spacing: .08em;
    text-transform: uppercase;
    border: 1px solid var(--ink);
    background: transparent;
    padding: 8px 10px;
    cursor: pointer;
  }
  .ghost:hover { background: #efe4d4; }
  .stamp {
    margin-top: 8px;
    width: 100%;
    font-family: "Archivo Black", sans-serif;
    font-size: 22px;
    letter-spacing: .08em;
    text-transform: uppercase;
    color: var(--stamp-ink);
    background: var(--stamp);
    border: 4px double var(--stamp-ink);
    outline: 3px solid var(--stamp);
    padding: 14px 12px;
    cursor: pointer;
    transform: rotate(-1.4deg);
  }
  .stamp:hover { transform: rotate(0deg); }
  .stamp:disabled { opacity: .45; cursor: wait; transform: none; }
  .hint { margin-top: 16px; color: var(--mute); font-size: 14px; }
  .receipt {
    background: var(--carbon);
    color: var(--ribbon);
    box-shadow: 8px 10px 0 #122033;
    min-height: 640px;
    display: flex;
    flex-direction: column;
    font-family: "Fragment Mono", ui-monospace, monospace;
  }
  .receipt-head {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    padding: 16px 18px;
    border-bottom: 1px dashed #7ea0c2;
    font-size: 11px;
    letter-spacing: .16em;
    text-transform: uppercase;
    color: #9ec0e0;
  }
  .log {
    flex: 1;
    overflow: auto;
    padding: 16px 18px 24px;
    white-space: pre-wrap;
    font-size: 13px;
    line-height: 1.55;
  }
  .log .ok { color: var(--ribbon); }
  .log .bad { color: #ffb4b4; }
  .log .meta { color: #9ec0e0; }
  .bar {
    height: 6px;
    background: #183049;
    width: 100%;
  }
  .bar > span {
    display: block;
    height: 100%;
    width: 0%;
    background: var(--ribbon);
    transition: width .2s linear;
  }
  @media (max-width: 860px) {
    .stage { grid-template-columns: 1fr; }
    .ticket, .receipt { min-height: 0; }
  }
</style>
</head>
<body>
  <div class="stage">
    <section class="ticket">
      <div class="kicker">Job ticket · local crawler</div>
      <h1>LOVABLE<br>PULL</h1>
      <p class="lede">Paste a project link. Pick a folder. Every file lands in its original tree.</p>
      <form id="form">
        <div class="field">
          <label for="url">Project link</label>
          <input id="url" type="text" autocomplete="off" placeholder="https://lovable.dev/projects/…">
        </div>
        <div class="field">
          <label for="dest">Save to</label>
          <div class="row">
            <input id="dest" type="text" autocomplete="off">
            <button class="ghost" type="button" id="browse">Folder</button>
          </div>
        </div>
        <button class="stamp" id="go" type="submit">Pull files</button>
      </form>
      <p class="hint" id="hint">Uses a tab in your real Chrome (the profile you last used). Chrome may restart once so that tab can be controlled; your other tabs come back.</p>
    </section>
    <aside class="receipt">
      <div class="receipt-head">
        <span id="phase">idle</span>
        <span id="count">0 / 0</span>
      </div>
      <div class="bar"><span id="fill"></span></div>
      <div class="log" id="log">waiting for a job…\n</div>
    </aside>
  </div>
<script>
const logEl = document.getElementById("log");
const phaseEl = document.getElementById("phase");
const countEl = document.getElementById("count");
const fillEl = document.getElementById("fill");
const goBtn = document.getElementById("go");
const hint = document.getElementById("hint");
let total = 0, done = 0, busy = false;

function line(text, cls) {
  const span = document.createElement("div");
  if (cls) span.className = cls;
  span.textContent = text;
  logEl.appendChild(span);
  logEl.scrollTop = logEl.scrollHeight;
}
function setBusy(v) {
  busy = v;
  goBtn.disabled = v;
  goBtn.textContent = v ? "Pulling…" : "Pull files";
}
async function api(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body || {})
  });
  return res.json();
}
document.getElementById("dest").value = __DEFAULT_DEST__;
document.getElementById("browse").onclick = async () => {
  const data = await api("/api/browse", {});
  if (data.path) document.getElementById("dest").value = data.path;
};
document.getElementById("form").onsubmit = async (e) => {
  e.preventDefault();
  if (busy) return;
  const url = document.getElementById("url").value.trim();
  const dest = document.getElementById("dest").value.trim();
  if (!url) { hint.textContent = "Paste a Lovable project link first."; return; }
  if (!dest) { hint.textContent = "Choose a folder to save into."; return; }
  logEl.textContent = "";
  total = 0; done = 0;
  countEl.textContent = "0 / 0";
  fillEl.style.width = "0%";
  setBusy(true);
  const data = await api("/api/start", {url, dest});
  if (data.error) {
    line(data.error, "bad");
    setBusy(false);
  }
};
const src = new EventSource("/api/events");
src.onmessage = (ev) => {
  const e = JSON.parse(ev.data);
  if (e.type === "log") line(e.message, "meta");
  if (e.type === "status") {
    phaseEl.textContent = e.phase || "";
    if (e.detail) hint.textContent = e.detail;
  }
  if (e.type === "files") {
    total = e.total;
    countEl.textContent = "0 / " + total;
  }
  if (e.type === "file") {
    done = e.done || done + 1;
    total = e.total || total;
    countEl.textContent = done + " / " + total;
    fillEl.style.width = (total ? (100 * done / total) : 0) + "%";
    line((e.ok ? "ok   " : "fail ") + e.path, e.ok ? "ok" : "bad");
  }
  if (e.type === "done") {
    phaseEl.textContent = "done";
    fillEl.style.width = "100%";
    hint.textContent = e.count + " files → " + e.folder;
    line("────────");
    line("saved " + e.count + " files");
    line(e.folder, "meta");
    setBusy(false);
  }
  if (e.type === "error") {
    phaseEl.textContent = "error";
    hint.textContent = e.message;
    line(e.message, "bad");
    setBusy(false);
  }
  if (e.type === "idle") setBusy(false);
};
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            dest = json.dumps(desktop_dir())
            body = PAGE_HTML.replace("__DEFAULT_DEST__", dest).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            last = 0
            try:
                while True:
                    batch = HUB.wait_after(last, timeout=20)
                    if not batch:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    for event in batch:
                        last = event["id"]
                        chunk = "data: %s\n\n" % json.dumps(event)
                        self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return
        self.send_error(404)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        data = self._read_json()
        if path == "/api/browse":
            chosen = pick_folder()
            self._json(200, {"path": chosen})
            return
        if path == "/api/open":
            open_folder(str(data.get("path") or ""))
            self._json(200, {"ok": True})
            return
        if path == "/api/start":
            url = str(data.get("url") or "").strip()
            dest = str(data.get("dest") or "").strip()
            if not url:
                self._json(400, {"error": "Paste a Lovable project link."})
                return
            if not dest:
                self._json(400, {"error": "Choose a folder to save into."})
                return
            if HUB.busy:
                self._json(409, {"error": "A download is already running."})
                return
            threading.Thread(target=start_job, args=(url, dest), daemon=True).start()
            self._json(200, {"ok": True})
            return
        self.send_error(404)


USAGE = """Lovable Pull

  pip install lovable-pull ; lovable-pull https://lovable.dev/projects/YOUR-PROJECT-ID

Options
  -u, --url URL     Lovable project link
  -d, --dest DIR    Folder to save into (default: current directory)
  -h, --help        Show this help

Stay signed in to lovable.dev in Chrome, then pass the project link.
"""


def parse_cli(argv: list[str]) -> tuple[str | None, str | None]:
    url = None
    dest = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            print(USAGE.strip())
            raise SystemExit(0)
        if arg in ("--url", "-u") and i + 1 < len(argv):
            url = argv[i + 1]
            i += 2
            continue
        if arg in ("--dest", "-d") and i + 1 < len(argv):
            dest = argv[i + 1]
            i += 2
            continue
        if arg.startswith("http"):
            url = arg
            i += 1
            continue
        if url and dest is None and not arg.startswith("-"):
            dest = arg
        i += 1
    return url, dest


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    url, dest = parse_cli(sys.argv[1:])
    bootstrap()
    if url:
        dest = dest or str(Path.cwd())
        print(f"Pulling {url}")
        print(f"Saving under {dest}")
        start_job(url, dest)
        return
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    page = f"http://{HOST}:{PORT}"
    print(f"Lovable Pull -> {page}")
    print("Paste a project link in the page. Chromium will open for Lovable.")
    threading.Timer(0.6, lambda: webbrowser.open(page)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
