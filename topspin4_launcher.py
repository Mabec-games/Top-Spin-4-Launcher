#!/usr/bin/env python3
"""Top Spin 4 Launcher - a Xenia Edge front-end with PC-style settings.

Runs Xenia Edge in a self-contained (portable) storage folder next to this
script, passes graphics settings as command-line cvar overrides, and applies
fixes that are needed for Top Spin 4 to boot reliably:

* Profile sanity check: a profile without a dashboard GPD (FFFE07D1.gpd) makes
  Top Spin 4 hang forever on "Loading..." right after "Press Start" (the game
  enumerates every profile). Such profiles are moved to data/quarantine.
* Stale-instance kill: Xenia Edge ignores SIGTERM, so a previous instance can
  linger after its window was closed. It is killed before launching.

Works with Xenia Edge or Xenia Canary on Linux (*.appimage) and Windows
(*.exe). Edge is the default: Canary never completes the asynchronous
XamEnumerate of Marketplace content that runs after "Press Start", so the game
waits forever. Edge completes it but its guest scheduler can deadlock while
Swing_DLL loads right afterwards (intermittent).
Usage: topspin4_launcher.py              -> GUI
       topspin4_launcher.py --play       -> launch directly with saved settings
       topspin4_launcher.py --dry-run    -> print the Xenia command line only
       topspin4_launcher.py --self-test  -> headless check (used by the builds)
       topspin4_launcher.py --version
Packaged as a Linux AppImage and a Windows .exe; settings.json and data/ live
next to the package.
"""

import glob
import hashlib
import hmac
import json
import os
import platform
import random
import re
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import time
import zlib

VERSION = "1.0"


def _app_dir():
    """Folder that holds settings.json and data/: next to the .AppImage or
    .exe when packaged, next to this script otherwise."""
    if os.environ.get("APPIMAGE"):
        return os.path.dirname(os.path.abspath(os.environ["APPIMAGE"]))
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = _app_dir()
# Art ships inside the package (PyInstaller unpacks to _MEIPASS, the AppImage
# keeps it next to this script); it is optional at runtime.
RESOURCE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
ASSET_DIR = os.path.join(RESOURCE_DIR, "assets")
# Packaged builds carry Xenia Edge here, so players only supply their ISO.
BUNDLED_XENIA_DIR = os.path.join(RESOURCE_DIR, "xenia")
GAME_DIR = os.path.dirname(APP_DIR)
DATA_DIR = os.path.join(APP_DIR, "data")            # Xenia storage_root
QUARANTINE_DIR = os.path.join(DATA_DIR, "quarantine")
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
LOG_FILE = os.path.join(APP_DIR, "xenia.log")
SYSTEM_XENIA_DIR = os.path.expanduser("~/.local/share/Xenia")
IS_WINDOWS = platform.system() == "Windows"
DASHBOARD_ID = "FFFE07D1"
NATIVE_W, NATIVE_H = 1280, 720                      # Top Spin 4 renders 720p

# label -> (draw scale, extra cvars). The game renders 1280x720 natively; the
# draw scale multiplies every render target, so 3x is a true 3840x2160.
# Higher scales used to deadlock right after "Press Start" (Edge guest
# scheduler); with the spin_quanta fix in build_command, automated Start-press
# trials passed at every scale (1x 10/10, 2x 8/8, 3x 8/8, incl. 60 s soaks).
RESOLUTIONS = {
    "720p (1x)": (1, []),
    "1080p (renders at 2x, scaled to your screen)": (2, []),
    "1440p (2x)": (2, []),
    "4K / 2160p (3x)": (3, []),
    "Steam Deck 800p (1280x800, letterboxed)": (1, []),
    "Steam Deck 800p (1280x800 native 16:10, experimental)": (1, [
        "--internal_display_resolution=17",
        "--internal_display_resolution_x=1280",
        "--internal_display_resolution_y=800",
    ]),
}
ANTI_ALIASING = {
    "Off": "none",
    "FXAA": "fxaa",
    "FXAA Extreme": "fxaa_extreme",
}
SCALERS = {
    "Bilinear": "bilinear",
    "AMD CAS (sharpen)": "cas",
    "AMD FSR 1.0 (upscale)": "fsr",
}
GPU_AUTO = "Auto"
D3D12, VULKAN = "Direct3D 12", "Vulkan"
GRAPHICS_APIS = [D3D12, VULKAN] if IS_WINDOWS else [VULKAN]
CANARY, EDGE = "Xenia Canary", "Xenia Edge"
EMULATOR_NAMES = {CANARY: "xenia_canary", EDGE: "xenia_edge"}
AUDIO_XMA_THREAD = "On - dedicated XMA decoder thread"
AUDIO = {
    AUDIO_XMA_THREAD: ["--use_dedicated_xma_thread=true"],
    "On (Xenia default)": [],
    "Off": ["--apu=nop"],
}
PROFILE_AUTO = "Auto (first healthy profile)"
PROFILE_NONE = "None (not signed in)"

DEFAULTS = {
    "emulator_kind": EDGE,
    "emulator": "",
    "iso": "",
    "resolution": "720p (1x)",
    "anti_aliasing": "FXAA",
    "scaler": "Bilinear",
    "fullscreen": True,
    "framerate_limit": 60,
    "gpu": GPU_AUTO,              # "Auto" or "<index>: <name>"
    "graphics_api": GRAPHICS_APIS[0],
    "guest_scheduler": True,
    "sanitize_profiles": True,
    "kill_stale": True,
    "hide_xenia_menu": True,
    "profile": PROFILE_AUTO,
    "gamertag": "",
    "audio": AUDIO_XMA_THREAD,
    "extra_args": "",
}


# --------------------------------------------------------------------------
# Settings / discovery
# --------------------------------------------------------------------------
def load_settings():
    s = dict(DEFAULTS)
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                s.update(json.load(f))
        except (OSError, ValueError):
            pass
    # Option labels change between versions; drop saved ones that no longer exist.
    for key, options in (("resolution", RESOLUTIONS), ("anti_aliasing", ANTI_ALIASING),
                         ("scaler", SCALERS), ("audio", AUDIO),
                         ("graphics_api", GRAPHICS_APIS),
                         ("emulator_kind", EMULATOR_NAMES)):
        if s.get(key) not in options:
            s[key] = DEFAULTS[key]
    if bundled_emulator():
        # The AppImage mount point changes every run; always use the copy inside.
        s["emulator_kind"], s["emulator"] = EDGE, bundled_emulator()
    elif (not s["emulator"] or not os.path.exists(s["emulator"])
            or emulator_kind(s["emulator"]) != s["emulator_kind"]):
        s["emulator"] = find_emulator(s["emulator_kind"])
    if not s["iso"] or not os.path.exists(s["iso"]):
        s["iso"] = find_iso()
    return s


def save_settings(s):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)


def emulator_kind(path):
    return CANARY if "canary" in os.path.basename(path).lower() else EDGE


def bundled_emulator():
    """Xenia Edge shipped inside the AppImage / .exe, if any."""
    return find_emulator(EDGE, dirs=(BUNDLED_XENIA_DIR,))


def find_emulator(kind=CANARY, dirs=None):
    n = EMULATOR_NAMES[kind]
    pats = ([n + "*.exe"] if IS_WINDOWS
            else [n + "*.appimage", n + "*.AppImage", n])
    for d in dirs or (BUNDLED_XENIA_DIR, APP_DIR, os.path.join(APP_DIR, "emulator"), GAME_DIR):
        for p in pats:
            hits = sorted(glob.glob(os.path.join(d, p)))
            if hits:
                return hits[0]
    return ""


def find_iso():
    for d in (GAME_DIR, APP_DIR):
        for ext in ("*.iso", "*.ISO", "*.xex", "*.zar"):
            hits = sorted(glob.glob(os.path.join(d, ext)))
            if hits:
                return hits[0]
    return ""


# --------------------------------------------------------------------------
# Profile handling
# --------------------------------------------------------------------------
def profile_dir(content_root, xuid):
    return os.path.join(content_root, xuid, DASHBOARD_ID, "00010000", xuid)


def list_profiles(content_root):
    """Return [(xuid, healthy, gamertag_hint)] for profiles in content_root."""
    out = []
    if not os.path.isdir(content_root):
        return out
    for xuid in sorted(os.listdir(content_root)):
        if not (len(xuid) == 16 and xuid.upper().startswith("E")):
            continue
        pdir = profile_dir(content_root, xuid)
        if not os.path.exists(os.path.join(pdir, "Account")):
            continue
        # Xenia writes the dashboard GPD on a profile's first boot, so a
        # profile this launcher just created is fine without one.
        healthy = (os.path.exists(os.path.join(pdir, DASHBOARD_ID + ".gpd"))
                   or xuid in launcher_profiles())
        out.append((xuid, healthy))
    return out


# --------------------------------------------------------------------------
# Creating profiles (so new players never see Xenia's "No Profiles Found" or
# the game's sign-in prompt)
# --------------------------------------------------------------------------
# Xbox key 0x19 (retail), as used by Xenia's ProfileManager for Account files.
ACCOUNT_KEY = bytes.fromhex("E1BC159C73B1EAE9AB3170F3AD47EBF3")
ACCOUNT_INFO_SIZE = 0x17C          # sizeof(X_XAMACCOUNTINFO)
GAMERTAG_MAX = 15                  # 16 UTF-16 chars including the terminator
LAUNCHER_PROFILES_FILE = os.path.join(DATA_DIR, "launcher_profiles.json")


def launcher_profiles():
    try:
        with open(LAUNCHER_PROFILES_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def _rc4(key, data):
    box = list(range(256))
    j = 0
    for i in range(256):
        j = (j + box[i] + key[i % len(key)]) & 0xFF
        box[i], box[j] = box[j], box[i]
    out, i, j = bytearray(), 0, 0
    for byte in data:
        i = (i + 1) & 0xFF
        j = (j + box[i]) & 0xFF
        box[i], box[j] = box[j], box[i]
        out.append(byte ^ box[(box[i] + box[j]) & 0xFF])
    return bytes(out)


def encrypt_account(gamertag):
    """Account file exactly as Xenia's ProfileManager::EncryptAccountFile
    writes it: HMAC-SHA1 tag, then RC4(confounder + X_XAMACCOUNTINFO)."""
    info = bytearray(ACCOUNT_INFO_SIZE)
    name = gamertag[:GAMERTAG_MAX].encode("utf-16-be")
    info[8:8 + len(name)] = name
    plain = b"\xFD" * 8 + bytes(info)
    mac = lambda d: hmac.new(ACCOUNT_KEY, d, hashlib.sha1).digest()
    tag = mac(plain)[:16]
    return tag + _rc4(mac(tag)[:16], plain)


def read_gamertag(content_root, xuid):
    """Gamertag stored in a profile's Account file, or "" if unreadable."""
    try:
        with open(os.path.join(profile_dir(content_root, xuid), "Account"), "rb") as f:
            raw = f.read()
        mac = lambda d: hmac.new(ACCOUNT_KEY, d, hashlib.sha1).digest()
        plain = _rc4(mac(raw[:16])[:16], raw[16:])
        if mac(plain)[:16] != raw[:16]:
            return ""
        return plain[16:48].decode("utf-16-be", "replace").split("\0")[0]
    except (OSError, IndexError):
        return ""


def profile_label(content_root, xuid):
    tag = read_gamertag(content_root, xuid)
    return f"{tag} ({xuid})" if tag else xuid


def valid_gamertag(name):
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9 ]{0,%d}" % (GAMERTAG_MAX - 1), name or ""))


def create_profile(gamertag, log=print):
    """Create an offline profile in the launcher's storage; returns its XUID."""
    if not valid_gamertag(gamertag):
        raise ValueError(f"Gamertags are 1-{GAMERTAG_MAX} letters, digits or spaces "
                         "and start with a letter.")
    content = os.path.join(DATA_DIR, "content")
    while True:                                   # same scheme as Xenia
        xuid = f"{(0xE03 << 52) + random.randrange(1 << 31):016X}"
        if not os.path.exists(os.path.join(content, xuid)):
            break
    pdir = profile_dir(content, xuid)
    os.makedirs(pdir)
    with open(os.path.join(pdir, "Account"), "wb") as f:
        f.write(encrypt_account(gamertag))
    with open(LAUNCHER_PROFILES_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(launcher_profiles() | {xuid}), f)
    log(f"Created profile '{gamertag}' ({xuid}).")
    return xuid


def ensure_profile(s, log=print):
    """New players: create a profile before the first boot unless they chose
    to play signed out."""
    if s.get("profile") == PROFILE_NONE:
        return
    if not any(ok for _, ok in list_profiles(os.path.join(DATA_DIR, "content"))):
        name = s.get("gamertag") or "Player"
        create_profile(name if valid_gamertag(name) else "Player", log)


def sanitize_profiles(log=print):
    """Move profiles lacking a dashboard GPD out of the content folder."""
    content = os.path.join(DATA_DIR, "content")
    moved = []
    for xuid, healthy in list_profiles(content):
        if healthy:
            continue
        os.makedirs(QUARANTINE_DIR, exist_ok=True)
        dest = os.path.join(QUARANTINE_DIR, xuid)
        if os.path.exists(dest):
            dest += time.strftime("_%Y%m%d%H%M%S")
        shutil.move(os.path.join(content, xuid), dest)
        moved.append(xuid)
        log(f"Quarantined incomplete profile {xuid} (no {DASHBOARD_ID}.gpd) "
            f"- it makes Top Spin 4 hang after 'Press Start'.")
    if moved:
        clear_logged_slots(moved)
    return moved


def signin_xuid(s):
    """XUID to sign in on boot (slot 0), or "" for none. The portable storage
    starts with empty sign-in slots, so without this the game runs signed out
    and cannot save."""
    choice = s.get("profile", PROFILE_AUTO)
    if choice == PROFILE_NONE:
        return ""
    m = re.search(r"\(([0-9A-F]{16})\)$", choice)       # "Gamertag (XUID)"
    choice = m.group(1) if m else choice
    healthy = [x for x, ok in list_profiles(os.path.join(DATA_DIR, "content")) if ok]
    if choice in healthy:
        return choice
    return healthy[0] if healthy else ""


def clear_logged_slots(xuids):
    for name in ("xenia-edge.config.toml", "xenia-canary.config.toml"):
        cfg = os.path.join(DATA_DIR, name)
        if not os.path.exists(cfg):
            continue
        with open(cfg, encoding="utf-8") as f:
            text = f.read()
        for x in xuids:
            text = text.replace(f'"{x}"', '""')
        with open(cfg, "w", encoding="utf-8") as f:
            f.write(text)


def import_profiles(src_root=SYSTEM_XENIA_DIR, log=print):
    """Copy healthy profiles (and their game saves) from another Xenia
    storage folder into the launcher's portable storage."""
    src_content = os.path.join(src_root, "content")
    dst_content = os.path.join(DATA_DIR, "content")
    imported = []
    for xuid, healthy in list_profiles(src_content):
        if not healthy:
            log(f"Skipped incomplete profile {xuid} (would hang the game).")
            continue
        dst = os.path.join(dst_content, xuid)
        if os.path.exists(dst):
            log(f"Profile {xuid} already present - not overwritten.")
            continue
        shutil.copytree(os.path.join(src_content, xuid), dst)
        imported.append(xuid)
        log(f"Imported profile {xuid}.")
    return imported


# --------------------------------------------------------------------------
# Launching
# --------------------------------------------------------------------------
def kill_stale(emulator, log=print):
    # Either emulator would hold the same storage folder, so kill both.
    names = {os.path.basename(e) for e in
             (emulator, find_emulator(CANARY), find_emulator(EDGE)) if e}
    if IS_WINDOWS:
        for name in names:
            subprocess.run(["taskkill", "/F", "/IM", name],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    # Match on the executable (argv[0]) only - "pgrep -f" would also hit
    # shells/editors that merely mention the name in their arguments.
    me = os.getpid()
    for cmdline in glob.glob("/proc/[0-9]*/cmdline"):
        pid = int(cmdline.split("/")[2])
        try:
            with open(cmdline, "rb") as f:
                exe = f.read().split(b"\0", 1)[0].decode(errors="replace")
        except OSError:
            continue
        if pid == me or os.path.basename(exe) not in names:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            log(f"Killed stale Xenia process {pid}.")
        except OSError:
            pass


def child_env():
    """Environment for programs we start. The AppImage's AppRun points Python
    and Tcl/Tk at the bundled copies (listed in TS4_BUNDLE_ENV); a system
    Python started by a child must not inherit that."""
    env = dict(os.environ)
    for key in env.pop("TS4_BUNDLE_ENV", "").split():
        env.pop(key, None)
    return env


def bool_arg(v):
    return "true" if v else "false"


def gpu_index(label):
    """'1: NVIDIA ...' (or the older 'GPU 1') -> 1; 'Auto' -> -1."""
    m = re.match(r"(?:GPU )?(\d+)", label or "")
    return int(m.group(1)) if m else -1


def gpu_args(s):
    idx = gpu_index(s.get("gpu"))
    if not IS_WINDOWS:
        return [f"--vulkan_device={idx}"]
    if s.get("graphics_api") == VULKAN:
        return ["--gpu=vulkan", f"--vulkan_device={idx}"]
    return ["--gpu=d3d12", f"--d3d12_adapter={idx}"]


def _vulkan_gpu_names():
    """Physical devices in vkEnumeratePhysicalDevices order, which is the
    index Xenia's vulkan_device uses."""
    import ctypes as C
    lib = C.CDLL("vulkan-1.dll" if IS_WINDOWS else "libvulkan.so.1")

    class AppInfo(C.Structure):
        _fields_ = [("sType", C.c_int), ("pNext", C.c_void_p), ("pApplicationName", C.c_char_p),
                    ("applicationVersion", C.c_uint32), ("pEngineName", C.c_char_p),
                    ("engineVersion", C.c_uint32), ("apiVersion", C.c_uint32)]

    class InstanceInfo(C.Structure):
        _fields_ = [("sType", C.c_int), ("pNext", C.c_void_p), ("flags", C.c_uint32),
                    ("pApplicationInfo", C.POINTER(AppInfo)), ("enabledLayerCount", C.c_uint32),
                    ("ppEnabledLayerNames", C.c_void_p), ("enabledExtensionCount", C.c_uint32),
                    ("ppEnabledExtensionNames", C.c_void_p)]

    app = AppInfo(0, None, b"TopSpin4Launcher", 1, None, 0, (1 << 22) | (1 << 12))  # 1.1
    info = InstanceInfo(1, None, 0, C.pointer(app), 0, None, 0, None)
    inst = C.c_void_p()
    if lib.vkCreateInstance(C.byref(info), None, C.byref(inst)) != 0:
        raise RuntimeError("vkCreateInstance failed")
    try:
        count = C.c_uint32()
        lib.vkEnumeratePhysicalDevices(inst, C.byref(count), None)
        devices = (C.c_void_p * count.value)()
        lib.vkEnumeratePhysicalDevices(inst, C.byref(count), devices)
        kinds = {1: "integrated", 2: "discrete", 3: "virtual", 4: "CPU"}
        names = []
        for dev in devices[:count.value]:
            props = C.create_string_buffer(4096)       # VkPhysicalDeviceProperties
            lib.vkGetPhysicalDeviceProperties(C.c_void_p(dev), props)
            kind = int.from_bytes(props.raw[16:20], "little")
            name = props.raw[20:276].split(b"\0")[0].decode("utf-8", "replace")
            names.append(f"{name} ({kinds[kind]})" if kind in kinds else name)
        return names
    finally:
        lib.vkDestroyInstance(inst, None)


def _dxgi_gpu_names():
    """Hardware adapters in IDXGIFactory1::EnumAdapters1 order, which is the
    index Xenia's d3d12_adapter uses. Windows only."""
    import ctypes as C
    from ctypes import wintypes as W

    class GUID(C.Structure):
        _fields_ = [("a", C.c_uint32), ("b", C.c_uint16), ("c", C.c_uint16), ("d", C.c_ubyte * 8)]

    class Desc1(C.Structure):          # DXGI_ADAPTER_DESC1
        _fields_ = [("Description", C.c_wchar * 128), ("VendorId", W.UINT), ("DeviceId", W.UINT),
                    ("SubSysId", W.UINT), ("Revision", W.UINT), ("DedicatedVideoMemory", C.c_size_t),
                    ("DedicatedSystemMemory", C.c_size_t), ("SharedSystemMemory", C.c_size_t),
                    ("AdapterLuid", C.c_int64), ("Flags", W.UINT)]

    iid = GUID(0x770AAE78, 0xF26F, 0x4DBA, (C.c_ubyte * 8)(0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87))
    factory = C.c_void_p()
    if C.windll.dxgi.CreateDXGIFactory1(C.byref(iid), C.byref(factory)) != 0:
        raise RuntimeError("CreateDXGIFactory1 failed")

    def method(obj, index, *argtypes):
        vtbl = C.cast(obj, C.POINTER(C.POINTER(C.c_void_p))).contents
        return C.WINFUNCTYPE(C.c_long, C.c_void_p, *argtypes)(vtbl[index])

    names, i = [], 0
    while True:
        adapter = C.c_void_p()
        # IDXGIFactory1::EnumAdapters1 = vtable slot 12
        if method(factory, 12, W.UINT, C.POINTER(C.c_void_p))(factory, i, C.byref(adapter)) != 0:
            break
        desc = Desc1()
        method(adapter, 10, C.POINTER(Desc1))(adapter, C.byref(desc))   # GetDesc1
        method(adapter, 2)(adapter)                                     # Release
        if not desc.Flags & 2:                                          # skip software (WARP)
            names.append(desc.Description)
        i += 1
    method(factory, 2)(factory)
    return names


def list_gpus(api):
    """GPU names for the chosen API, read in a child process so a misbehaving
    driver cannot take the launcher down. [] if unavailable."""
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        subprocess.run(self_command("--list-gpus", api, path), timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    finally:
        os.remove(path)


def build_command(s):
    scale, res_extra = RESOLUTIONS.get(s["resolution"], (1, []))
    edge = emulator_kind(s["emulator"]) == EDGE
    args = [
        s["emulator"],
        f"--storage_root={DATA_DIR}",
        f"--log_file={LOG_FILE}",
        # Graphics
        f"--draw_resolution_scale_x={scale}",
        f"--draw_resolution_scale_y={scale}",
        f"--postprocess_antialiasing={ANTI_ALIASING.get(s['anti_aliasing'], 'none')}",
        f"--postprocess_scaling_and_sharpening={SCALERS.get(s['scaler'], 'bilinear')}",
        f"--fullscreen={bool_arg(s['fullscreen'])}",
        f"--framerate_limit={int(s['framerate_limit'])}",
        *gpu_args(s),
        "--discord=false",
        f"--logged_profile_slot_0_xuid={signin_xuid(s)}",
    ] + AUDIO.get(s.get("audio"), [])
    # Xenia's overlay menu (Exit Fullscreen / Profiles / Quit...) otherwise
    # pops up on the controller's Guide button and on right-click.
    if s.get("hide_xenia_menu"):
        args.append("--guide_button=false")
    # Canary quits on flags it doesn't know, so Edge-only cvars go here.
    if edge:
        args += [f"--guest_scheduler={bool_arg(s['guest_scheduler'])}"] + res_extra
        if s["guest_scheduler"]:
            # Right after "Press Start" two game threads spin-wait on each
            # other and starve the fiber they need. Running starved threads
            # after one timeslice, with 250 us slices, avoids the deadlock
            # (automated Start-press trials: 1/4 -> 4/4 at 720p).
            args += ["--guest_scheduler_spin_quanta=1", "--guest_scheduler_quantum_us=250"]
        if s.get("hide_xenia_menu"):
            args.append("--disable_game_window_mouse=true")
    if s.get("extra_args"):
        args += shlex.split(s["extra_args"], posix=not IS_WINDOWS)
    args.append(s["iso"])
    return args


def launch(s, log=print, wait=False):
    if not s["emulator"] or not os.path.exists(s["emulator"]):
        raise FileNotFoundError(s["emulator_kind"] + " executable not found.")
    if not s["iso"] or not os.path.exists(s["iso"]):
        raise FileNotFoundError("Top Spin 4 ISO not found.")
    os.makedirs(DATA_DIR, exist_ok=True)
    if not IS_WINDOWS and not os.access(s["emulator"], os.X_OK):
        try:
            os.chmod(s["emulator"], os.stat(s["emulator"]).st_mode | 0o111)
        except OSError:
            pass    # read-only (e.g. inside the AppImage); Popen reports it
    if s["kill_stale"]:
        kill_stale(s["emulator"], log)
    if s["sanitize_profiles"]:
        sanitize_profiles(log)
    ensure_profile(s, log)
    cmd = build_command(s)
    log("Launching: " + " ".join(f'"{a}"' if " " in a else a for a in cmd))
    try:
        os.remove(LOG_FILE)     # one log per session
    except OSError:
        pass
    env = child_env()
    # With Steam running, Steam Input adds a virtual pad (28de:11ff) for the
    # same physical controller. Start then arrives from two slots, the game
    # signs the profile into the second one too and hangs. Steam sets this
    # hint itself for games it launches, so only fill it in when unset.
    if "SDL_GAMECONTROLLER_IGNORE_DEVICES" not in env:
        env["SDL_GAMECONTROLLER_IGNORE_DEVICES"] = "0x28de/0x11ff"
    proc = subprocess.Popen(cmd, cwd=os.path.dirname(s["emulator"]), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    if wait:
        proc.wait()
    return proc


def play(s, log=print, wait=True):
    """Launch the game; with wait, return when the player closes it. Never
    restarts on its own: closing the window always ends the session."""
    proc = launch(s, log)
    if wait:
        proc.wait()


def self_command(*args):
    """Command line that starts this launcher (AppImage, .exe or script)."""
    if os.environ.get("APPIMAGE"):
        return [os.environ["APPIMAGE"], *args]
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, os.path.abspath(__file__), *args]


def create_desktop_entry():
    """Linux: add 'Top Spin 4' to the application menu."""
    apps = os.path.expanduser("~/.local/share/applications")
    os.makedirs(apps, exist_ok=True)
    path = os.path.join(apps, "topspin4-launcher.desktop")
    q = lambda a: '"' + a.replace("\\", "\\\\").replace('"', '\\"') + '"'
    exec_ = lambda *args: " ".join(q(a) for a in self_command(*args))
    with open(path, "w", encoding="utf-8") as f:
        f.write("[Desktop Entry]\nType=Application\nName=Top Spin 4\n"
                "Comment=Play Top Spin 4 with Xenia Edge\n"
                f"Exec={exec_('--play', '--wait')}\nCategories=Game;\nTerminal=false\n"
                "Actions=settings;\n\n[Desktop Action settings]\n"
                f"Name=Launcher settings\nExec={exec_()}\n")
    return path


# --------------------------------------------------------------------------
# Steam shortcut (needed for Remote Play Together)
# --------------------------------------------------------------------------
STEAM_APP_NAME = "Top Spin 4"


def steam_root():
    if IS_WINDOWS:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
                return os.path.normpath(winreg.QueryValueEx(k, "SteamPath")[0])
        except OSError:
            return ""
    for d in ("~/.local/share/Steam", "~/.steam/steam",
              "~/.var/app/com.valvesoftware.Steam/.local/share/Steam"):
        d = os.path.expanduser(d)
        if os.path.isdir(os.path.join(d, "userdata")):
            return d
    return ""


def steam_running():
    if IS_WINDOWS:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq steam.exe"],
                             capture_output=True, text=True).stdout
        return "steam.exe" in out.lower()
    for comm in glob.glob("/proc/[0-9]*/comm"):
        try:
            with open(comm) as f:
                if f.read().strip() == "steam":
                    return True
        except OSError:
            pass
    return False


def steam_account_ids(root):
    """Account folder(s) under userdata/: the most recent login if known."""
    ids = [d for d in os.listdir(os.path.join(root, "userdata"))
           if d.isdigit() and d != "0"]
    try:
        with open(os.path.join(root, "config", "loginusers.vdf"), encoding="utf-8") as f:
            text = f.read()
        newest = (-1, None)
        for sid, body in re.findall(r'"(\d{17})"\s*\{([^}]*)\}', text):
            acc = str(int(sid) - 76561197960265728)
            if acc not in ids:
                continue
            if re.search(r'"MostRecent"\s*"1"', body):
                return [acc]
            # Newer Steam drops MostRecent; the latest login has the newest timestamp.
            ts = re.search(r'"timestamp"\s*"(\d+)"', body, re.I)
            if ts and int(ts.group(1)) > newest[0]:
                newest = (int(ts.group(1)), acc)
        if newest[1]:
            return [newest[1]]
    except OSError:
        pass
    return ids


def vdf_load(data):
    """Parse Steam's binary VDF (shortcuts.vdf)."""
    pos = 0

    def cstr():
        nonlocal pos
        end = data.index(b"\0", pos)
        s, pos = data[pos:end].decode("utf-8", "replace"), end + 1
        return s

    def node():
        nonlocal pos
        d = {}
        while True:
            kind = data[pos]; pos += 1
            if kind == 8:
                return d
            key = cstr()
            if kind == 0:
                d[key] = node()
            elif kind == 1:
                d[key] = cstr()
            elif kind == 2:
                d[key] = struct.unpack_from("<i", data, pos)[0]; pos += 4
            elif kind == 3:
                d[key] = struct.unpack_from("<f", data, pos)[0]; pos += 4
            elif kind == 7:
                d[key] = ("u64", struct.unpack_from("<Q", data, pos)[0]); pos += 8
            else:
                raise ValueError(f"unknown VDF type {kind} at {pos - 1}")

    return node()


def vdf_dump(d):
    out = bytearray()
    for key, v in d.items():
        k = key.encode() + b"\0"
        if isinstance(v, dict):
            out += b"\0" + k + vdf_dump(v)
        elif isinstance(v, str):
            out += b"\1" + k + v.encode() + b"\0"
        elif isinstance(v, bool) or isinstance(v, int):
            out += b"\2" + k + struct.pack("<i", int(v))
        elif isinstance(v, float):
            out += b"\3" + k + struct.pack("<f", v)
        else:                                   # ("u64", n)
            out += b"\7" + k + struct.pack("<Q", v[1])
    return bytes(out + b"\x08")


def install_steam_art(cfg, appid):
    """Copy the library art into Steam's grid folder; returns the icon path."""
    grid = os.path.join(cfg, "grid")
    os.makedirs(grid, exist_ok=True)
    icon = ""
    for src, dst in (("steam_grid.png", f"{appid}p.png"), ("steam_wide.png", f"{appid}.png"),
                     ("steam_hero.png", f"{appid}_hero.png"), ("icon.png", f"{appid}_icon.png")):
        path = os.path.join(ASSET_DIR, src)
        if os.path.exists(path):
            shutil.copyfile(path, os.path.join(grid, dst))
            if src == "icon.png":
                icon = os.path.join(grid, dst)
    return icon


def add_to_steam(log=print):
    """Add (or update) a 'Top Spin 4' non-Steam shortcut that starts the game
    directly. Steam rewrites shortcuts.vdf on exit, so it must be closed."""
    root = steam_root()
    if not root:
        raise FileNotFoundError("Steam installation not found.")
    if steam_running():
        raise RuntimeError("Close Steam completely first (Steam > Exit), then try again.")
    cmd = self_command("--play", "--wait")
    exe = '"' + cmd[0] + '"'
    options = " ".join(f'"{a}"' if " " in a else a for a in cmd[1:])
    crc = zlib.crc32((exe + STEAM_APP_NAME).encode()) | 0x80000000
    entry_base = {
        "appid": crc - (1 << 32),               # stored as signed int32
        "AppName": STEAM_APP_NAME,
        "Exe": exe,
        "StartDir": '"' + APP_DIR + '"',
        "icon": "",
        "ShortcutPath": "",
        "LaunchOptions": options,
        "IsHidden": 0, "AllowDesktopConfig": 1, "AllowOverlay": 1,
        "OpenVR": 0, "Devkit": 0, "DevkitGameID": "", "DevkitOverrideAppID": 0,
        "LastPlayTime": 0, "FlatpakAppID": "",
        "tags": {},
    }
    written = []
    for acc in steam_account_ids(root):
        cfg = os.path.join(root, "userdata", acc, "config")
        os.makedirs(cfg, exist_ok=True)
        entry_base["icon"] = install_steam_art(cfg, crc)
        path = os.path.join(cfg, "shortcuts.vdf")
        data = {"shortcuts": {}}
        if os.path.exists(path):
            with open(path, "rb") as f:
                data = vdf_load(f.read())
            shutil.copy2(path, path + time.strftime(".bak-%Y%m%d%H%M%S"))
        shortcuts = data.setdefault("shortcuts", {})
        slot = next((k for k, v in shortcuts.items()
                     if isinstance(v, dict) and v.get("AppName") == STEAM_APP_NAME),
                    str(len(shortcuts)))
        entry = dict(shortcuts.get(slot, {}))
        entry.update({k: v for k, v in entry_base.items()
                      if k not in entry or k in ("appid", "Exe", "StartDir", "LaunchOptions", "icon")})
        shortcuts[slot] = entry
        with open(path, "wb") as f:
            f.write(vdf_dump(data))
        written.append(path)
        log(f"Steam shortcut '{STEAM_APP_NAME}' written to {path}")
    if not written:
        raise FileNotFoundError("No Steam account found under userdata/.")
    return written


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
ONLINE_TEXT = """\
Top Spin 4 has no System Link / LAN mode - its online play used Xbox LIVE,
which was shut down. Xenia's System Link tunnel therefore cannot connect two
copies of this game.

The reliable way to play 1v1 online is local 2-player (Player 2 on a second
controller) streamed to your friend, who only needs a client - no game copy:

  1. Steam Remote Play Together (easiest)
     Close Steam, press "Add to Steam" on the Game tab, start Steam again.
     Launch "Top Spin 4" from your Steam library, open the Steam overlay
     (Shift+Tab) > Remote Play Together > invite your friend. (Not yet
     tested with two devices.)

  2. Parsec  (low latency, works with any app; friend connects as guest)

  3. Sunshine (host) + Moonlight (client) - open source, best quality.

Tip: set Frame-rate limit to 60 and use 1440p with FXAA - streaming
compresses the image, so higher internal resolution keeps lines clean.
"""


# Colours picked from the key art: stadium navy, red and blue floodlights,
# tennis-ball green for the main action.
THEME = {
    "bg": "#0a0f1e", "panel": "#121a33", "field": "#1b2547", "line": "#2a3766",
    "fg": "#e8ecf8", "muted": "#8a95b8",
    "red": "#e3262f", "blue": "#2f7bff",
    "ball": "#c8f031", "ball_hi": "#dcff5a", "ball_fg": "#0a0f1e",
}


def apply_theme(root):
    """Dark 'stadium' ttk theme; returns the font family used."""
    import tkinter.font as tkfont
    from tkinter import ttk
    c = THEME
    families = set(tkfont.families(root))
    family = next((f for f in (("Segoe UI",) if IS_WINDOWS else ())
                   + ("Inter", "Noto Sans", "Cantarell", "DejaVu Sans") if f in families),
                  "TkDefaultFont")
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        tkfont.nametofont(name).configure(family=family, size=10)
    mono = next((f for f in ("Cascadia Mono", "Consolas", "JetBrains Mono",
                             "DejaVu Sans Mono", "Noto Sans Mono") if f in families), "TkFixedFont")

    root.configure(bg=c["bg"])
    # Combobox drop-down lists are plain Tk listboxes.
    root.option_add("*TCombobox*Listbox.background", c["field"])
    root.option_add("*TCombobox*Listbox.foreground", c["fg"])
    root.option_add("*TCombobox*Listbox.selectBackground", c["blue"])
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
    root.option_add("*TCombobox*Listbox.font", (family, 10))

    st = ttk.Style(root)
    st.theme_use("clam")
    st.configure(".", background=c["bg"], foreground=c["fg"], fieldbackground=c["field"],
                 bordercolor=c["line"], lightcolor=c["line"], darkcolor=c["line"],
                 troughcolor=c["panel"], focuscolor=c["blue"], insertcolor=c["fg"],
                 selectbackground=c["blue"], selectforeground="#ffffff", font=(family, 10))
    st.configure("TFrame", background=c["panel"])
    st.configure("Bg.TFrame", background=c["bg"])
    st.configure("TLabel", background=c["panel"], foreground=c["fg"])
    st.configure("Muted.TLabel", foreground=c["muted"])
    st.configure("Brand.TLabel", background=c["bg"], foreground=c["muted"], font=(family, 9))
    st.configure("BrandName.TLabel", background=c["bg"], foreground=c["fg"],
                 font=(family, 9, "bold"))

    st.configure("TNotebook", background=c["bg"], borderwidth=0, tabmargins=(0, 6, 0, 0))
    st.configure("TNotebook.Tab", background=c["bg"], foreground=c["muted"],
                 padding=(18, 8), borderwidth=0, font=(family, 10, "bold"))
    st.map("TNotebook.Tab",
           background=[("selected", c["panel"]), ("active", c["field"])],
           foreground=[("selected", c["fg"]), ("active", c["fg"])],
           lightcolor=[("selected", c["red"])], bordercolor=[("selected", c["red"])])

    for w in ("TCombobox", "TEntry", "TSpinbox"):
        st.configure(w, fieldbackground=c["field"], foreground=c["fg"], background=c["field"],
                     arrowcolor=c["fg"], bordercolor=c["line"], padding=5)
        st.map(w, fieldbackground=[("readonly", c["field"]), ("disabled", c["panel"])],
               foreground=[("readonly", c["fg"])], bordercolor=[("focus", c["blue"])],
               lightcolor=[("focus", c["blue"])], arrowcolor=[("active", c["ball"])])

    st.configure("TCheckbutton", background=c["panel"], foreground=c["fg"],
                 indicatorbackground=c["field"], indicatorforeground=c["ball"],
                 indicatormargin=(0, 0, 8, 0), padding=2)
    st.map("TCheckbutton", background=[("active", c["panel"])],
           indicatorbackground=[("selected", c["field"])], foreground=[("active", c["ball"])])

    st.configure("TButton", background=c["field"], foreground=c["fg"], padding=(12, 6),
                 bordercolor=c["line"], lightcolor=c["field"], darkcolor=c["field"])
    st.map("TButton", background=[("pressed", c["red"]), ("active", c["blue"])],
           lightcolor=[("active", c["blue"])], darkcolor=[("active", c["blue"])],
           foreground=[("active", "#ffffff")])

    st.configure("Play.TButton", background=c["ball"], foreground=c["ball_fg"],
                 font=(family, 16, "bold"), padding=(38, 10), bordercolor=c["ball"],
                 lightcolor=c["ball_hi"], darkcolor=c["ball"])
    st.map("Play.TButton", background=[("pressed", c["ball"]), ("active", c["ball_hi"])],
           foreground=[("active", c["ball_fg"])], lightcolor=[("active", c["ball_hi"])],
           darkcolor=[("active", c["ball_hi"])], bordercolor=[("active", c["ball_hi"])])
    return family, mono


def accent_stripe(parent, height=4):
    """Red-to-blue floodlight gradient bar."""
    import tkinter as tk
    cv = tk.Canvas(parent, height=height, highlightthickness=0, bg=THEME["bg"])
    a = tuple(int(THEME["red"][i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(THEME["blue"][i:i + 2], 16) for i in (1, 3, 5))

    def draw(event):
        cv.delete("all")
        steps = max(event.width // 4, 1)
        for i in range(steps):
            f = i / max(steps - 1, 1)
            col = "#%02x%02x%02x" % tuple(round(a[k] + (b[k] - a[k]) * f) for k in range(3))
            x0 = event.width * i / steps
            cv.create_rectangle(x0, 0, event.width * (i + 1) / steps + 1, height,
                                fill=col, outline="")

    cv.bind("<Configure>", draw)
    return cv


def run_gui():
    import threading
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, simpledialog

    s = load_settings()
    root = tk.Tk()
    root.title(f"Top Spin 4 Launcher {VERSION}")
    root.minsize(700, 560)
    family, mono = apply_theme(root)
    c = THEME
    images = {}                     # Tk drops images that aren't referenced
    try:
        images["icon"] = tk.PhotoImage(file=os.path.join(ASSET_DIR, "icon.png"))
        root.iconphoto(True, images["icon"])
        header = tk.PhotoImage(file=os.path.join(ASSET_DIR, "header.png"))
        if root.winfo_screenheight() < 900:      # e.g. Steam Deck 1280x800
            header = header.subsample(2, 2)
        images["header"] = header
        tk.Label(root, image=header, borderwidth=0, bg=c["bg"]).pack(side="top")
    except tk.TclError:
        tk.Label(root, text="TOP SPIN 4", bg=c["bg"], fg=c["fg"],
                 font=(family, 28, "bold")).pack(side="top", pady=(18, 10))
    accent_stripe(root).pack(side="top", fill="x")

    vars_ = {
        k: (tk.BooleanVar(value=v) if isinstance(v, bool)
            else tk.IntVar(value=v) if isinstance(v, int)
            else tk.StringVar(value=v))
        for k, v in s.items() if k in DEFAULTS
    }

    def collect():
        for k, v in vars_.items():
            s[k] = v.get()
        return s

    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=12, pady=(4, 8))

    # --- Log box (shared) ---
    logbox = tk.Text(root, height=6, wrap="word", state="disabled", relief="flat",
                     bg=c["panel"], fg=c["muted"], font=(mono, 9), padx=8, pady=6,
                     highlightthickness=1, highlightbackground=c["line"],
                     highlightcolor=c["line"], insertbackground=c["fg"])
    logbox.pack(fill="x", padx=12)

    def log(msg):
        logbox.configure(state="normal")
        logbox.insert("end", msg + "\n")
        logbox.see("end")
        logbox.configure(state="disabled")
        root.update_idletasks()

    def row(parent, r, label, widget):
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", padx=(14, 10), pady=5)
        widget.grid(row=r, column=1, sticky="ew", padx=(0, 14), pady=5)

    def tab(text):
        f = ttk.Frame(nb, padding=(0, 10))
        nb.add(f, text=text.upper())
        f.columnconfigure(1, weight=1)
        return f

    # --- Graphics tab ---
    g = tab("Graphics")
    row(g, 0, "Internal resolution", ttk.Combobox(
        g, textvariable=vars_["resolution"], values=list(RESOLUTIONS), state="readonly"))
    row(g, 1, "Anti-aliasing", ttk.Combobox(
        g, textvariable=vars_["anti_aliasing"], values=list(ANTI_ALIASING), state="readonly"))
    row(g, 2, "Output scaling / sharpening", ttk.Combobox(
        g, textvariable=vars_["scaler"], values=list(SCALERS), state="readonly"))
    row(g, 3, "Frame-rate limit (0 = off)", ttk.Spinbox(
        g, from_=0, to=240, increment=30, textvariable=vars_["framerate_limit"]))
    gpu_box = ttk.Combobox(g, textvariable=vars_["gpu"], values=[GPU_AUTO], state="readonly")
    row(g, 4, "GPU", gpu_box)

    def refresh_gpus(*_):
        api = vars_["graphics_api"].get()
        gpu_box.configure(values=[GPU_AUTO, "(detecting graphics cards...)"])

        def work():
            names = list_gpus(api)
            labels = [f"{i}: {n}" for i, n in enumerate(names)] or ["0: GPU 0", "1: GPU 1"]
            root.after(0, apply, labels)

        def apply(labels):
            gpu_box.configure(values=[GPU_AUTO] + labels)
            cur = vars_["gpu"].get()
            idx = gpu_index(cur)
            if cur != GPU_AUTO:      # keep the index, show its real name
                vars_["gpu"].set(next((l for l in labels if gpu_index(l) == idx), GPU_AUTO))

        threading.Thread(target=work, daemon=True).start()

    if IS_WINDOWS:
        api_box = ttk.Combobox(g, textvariable=vars_["graphics_api"], values=GRAPHICS_APIS,
                               state="readonly")
        row(g, 7, "Graphics API", api_box)
        api_box.bind("<<ComboboxSelected>>", refresh_gpus)
    refresh_gpus()
    ttk.Checkbutton(g, text="Fullscreen", variable=vars_["fullscreen"]).grid(
        row=5, column=1, sticky="w", padx=6)
    ttk.Label(g, style="Muted.TLabel", justify="left", wraplength=620, text=(
        "Pick the entry that matches your screen. 720p runs on any GPU and suits "
        "Steam Deck; 4K needs a strong GPU. FXAA removes the shimmer on menus/UI. "
        "On laptops pick the discrete GPU.")).grid(row=6, column=0, columnspan=2,
                                               sticky="w", padx=14, pady=10)

    # --- Game / paths tab ---
    p = tab("Game")

    def browse(var, title, types):
        f = filedialog.askopenfilename(title=title, filetypes=types,
                                       initialdir=GAME_DIR)
        if f:
            var.set(f)

    builtin = bool(bundled_emulator())
    if builtin:
        row(p, 0, "Emulator", ttk.Label(p, text="Xenia Edge (built in)", style="Muted.TLabel"))
    else:
        kind_box = ttk.Combobox(p, textvariable=vars_["emulator_kind"],
                                values=[CANARY, EDGE], state="readonly")
        row(p, 0, "Emulator", kind_box)
        kind_box.bind("<<ComboboxSelected>>", lambda e: vars_["emulator"].set(
            find_emulator(vars_["emulator_kind"].get())))

    for r, (key, label, types) in enumerate([
        ("emulator", "Emulator path", [("Xenia", "*.appimage *.AppImage *.exe"), ("All", "*")]),
        ("iso", "Top Spin 4 image", [("Game image", "*.iso *.xex *.zar"), ("All", "*")]),
    ], start=1):
        if key == "emulator" and builtin:
            continue
        row(p, r, label, ttk.Entry(p, textvariable=vars_[key]))
        ttk.Button(p, text="...", width=3,
                   command=lambda v=vars_[key], t=label, ty=types: browse(v, t, ty)
                   ).grid(row=r, column=2, padx=(0, 14))

    def do_import():
        n = import_profiles(log=log)
        log(f"Imported {len(n)} profile(s) from {SYSTEM_XENIA_DIR}.")
        refresh_profiles()

    content = os.path.join(DATA_DIR, "content")

    def show_profiles():
        profs = list_profiles(content)
        return ", ".join(profile_label(content, x) + ("" if ok else " (BROKEN)")
                         for x, ok in profs) or "none yet"

    prof_lbl = ttk.Label(p, style="Muted.TLabel", wraplength=620, justify="left")
    prof_lbl.grid(row=4, column=0, columnspan=3, sticky="w", padx=14, pady=(2, 8))
    prof_box = ttk.Combobox(p, textvariable=vars_["profile"], state="readonly")
    row(p, 3, "Sign in on boot", prof_box)

    def refresh_profiles():
        prof_lbl.configure(text="Profiles: " + show_profiles())
        healthy = [profile_label(content, x) for x, ok in list_profiles(content) if ok]
        prof_box.configure(values=[PROFILE_AUTO, PROFILE_NONE] + healthy)

    def new_profile():
        name = simpledialog.askstring(
            "Create your profile",
            f"Choose a gamertag (1-{GAMERTAG_MAX} letters, digits or spaces):",
            initialvalue=vars_["gamertag"].get() or "Player", parent=root)
        if name is None:
            return
        try:
            xuid = create_profile(name.strip(), log)
        except ValueError as e:
            messagebox.showerror("Create profile", str(e), parent=root)
            return new_profile()
        vars_["gamertag"].set(name.strip())
        vars_["profile"].set(profile_label(content, xuid))
        refresh_profiles()
        collect(); save_settings(s)

    refresh_profiles()
    ttk.Button(p, text="New profile…", command=new_profile).grid(
        row=5, column=1, sticky="w", pady=4)
    if not IS_WINDOWS:
        ttk.Button(p, text="Import from ~/.local/share/Xenia",
                   command=do_import).grid(row=5, column=0, sticky="w", padx=14, pady=4)
    # First run: ask for a gamertag instead of letting Xenia/the game prompt.
    if not any(ok for _, ok in list_profiles(content)):
        root.after(400, new_profile)

    def open_folder(path):
        os.makedirs(path, exist_ok=True)
        if IS_WINDOWS:
            os.startfile(path)  # noqa
        else:
            subprocess.Popen(["xdg-open", path], env=child_env())

    ttk.Button(p, text="Open data folder", command=lambda: open_folder(DATA_DIR)).grid(
        row=6, column=0, sticky="w", padx=14, pady=4)

    def clear_cache():
        for sub in ("cache_host", "cache", "cache0", "cache1"):
            shutil.rmtree(os.path.join(DATA_DIR, sub), ignore_errors=True)
        log("Shader/code cache cleared.")

    ttk.Button(p, text="Clear shader/code cache", command=clear_cache).grid(
        row=6, column=1, sticky="w", pady=4)

    def do_shortcut():
        log("Created menu entry: " + create_desktop_entry())

    if not IS_WINDOWS:
        ttk.Button(p, text="Add to application menu", command=do_shortcut).grid(
            row=7, column=0, sticky="w", padx=14, pady=4)

    def do_steam():
        try:
            add_to_steam(log=log)
            log("Start Steam again - 'Top Spin 4' is in your library.")
        except Exception as e:  # noqa
            messagebox.showerror("Add to Steam", str(e))

    ttk.Button(p, text="Add to Steam", command=do_steam).grid(
        row=7, column=1, sticky="w", pady=4)

    # --- Advanced tab ---
    a = tab("Advanced")
    ttk.Checkbutton(a, text="Quarantine broken profiles before launch (fixes Press-Start hang)",
                    variable=vars_["sanitize_profiles"]).grid(row=0, column=0, columnspan=2, sticky="w", padx=14, pady=3)
    ttk.Checkbutton(a, text="Kill leftover Xenia processes before launch",
                    variable=vars_["kill_stale"]).grid(row=1, column=0, columnspan=2, sticky="w", padx=14, pady=3)
    ttk.Checkbutton(a, text="Hide Xenia's overlay menu (Guide button / right-click); quit with Alt+F4",
                    variable=vars_["hide_xenia_menu"]).grid(row=4, column=0, columnspan=2, sticky="w", padx=14, pady=3)
    ttk.Checkbutton(a, text="Edge cooperative guest scheduler (Xenia Edge only)",
                    variable=vars_["guest_scheduler"]).grid(row=2, column=0, columnspan=2, sticky="w", padx=14, pady=3)
    row(a, 5, "Audio", ttk.Combobox(
        a, textvariable=vars_["audio"], values=list(AUDIO), state="readonly"))
    row(a, 6, "Extra Xenia arguments", ttk.Entry(a, textvariable=vars_["extra_args"]))

    # --- Online tab ---
    o = tab("Online 1v1")
    t = tk.Text(o, wrap="word", height=16, relief="flat", bg=c["panel"], fg=c["fg"],
                font=(mono, 9), padx=10, pady=4, highlightthickness=0)
    t.insert("1.0", ONLINE_TEXT)
    t.configure(state="disabled")
    t.grid(row=0, column=0, columnspan=2, sticky="nsew", padx=6)
    o.rowconfigure(0, weight=1)

    # --- Buttons ---
    bar = ttk.Frame(root, style="Bg.TFrame"); bar.pack(fill="x", padx=12, pady=12)

    def on_play():
        collect(); save_settings(s)
        # Tk isn't thread-safe: hand log lines from the watcher to the UI loop.
        tlog = lambda m: root.after(0, log, m)

        def run():
            try:
                play(dict(s), log=tlog)
            except Exception as e:  # noqa
                root.after(0, messagebox.showerror, "Launch failed", str(e))

        threading.Thread(target=run, daemon=True).start()

    def on_save():
        collect(); save_settings(s); log("Settings saved.")

    ttk.Button(bar, text="▶  PLAY", style="Play.TButton", command=on_play).pack(side="right")
    ttk.Button(bar, text="Save", command=on_save).pack(side="right", padx=10)
    ttk.Button(bar, text="Open log", command=lambda: open_folder(APP_DIR)).pack(side="left")
    brand = ttk.Frame(bar, style="Bg.TFrame"); brand.pack(side="left", padx=14)
    ttk.Label(brand, text="MABEC GAMES", style="BrandName.TLabel").pack(anchor="w")
    ttk.Label(brand, text=f"Launcher v{VERSION} · Xenia Edge"
              + (" built in" if bundled_emulator() else ""), style="Brand.TLabel").pack(anchor="w")

    root.mainloop()


def self_test(report=None):
    """Headless check that the packaged runtime works; no files are written
    except the optional report (a windowed .exe has no console)."""
    import tkinter  # noqa: F401  (fails if Tcl/Tk isn't bundled correctly)
    s = load_settings()
    s.setdefault("emulator", "")
    cmd = build_command(dict(s, emulator=s["emulator"] or "xenia_edge", iso=s["iso"] or "game.iso"))
    vdf = {"shortcuts": {"0": {"appid": -5, "AppName": "x", "tags": {"0": "a"},
                               "LastPlayTime": 0, "f": ("u64", 7)}}}
    assert vdf_load(vdf_dump(vdf)) == vdf, "VDF round-trip failed"
    raw = encrypt_account("Player")
    mac = lambda d: hmac.new(ACCOUNT_KEY, d, hashlib.sha1).digest()
    plain = _rc4(mac(raw[:16])[:16], raw[16:])
    assert len(raw) == 0x194 and mac(plain)[:16] == raw[:16], "Account encryption failed"
    lines = [f"Top Spin 4 Launcher {VERSION} self-test OK",
             f"  python {platform.python_version()}, tk {tkinter.TkVersion}, app dir {APP_DIR}",
             f"  bundled xenia: {bundled_emulator() or 'no'}",
             f"  emulator: {s['emulator'] or 'not found'}",
             f"  game image: {s['iso'] or 'not found'}",
             f"  command: {len(cmd)} arguments"]
    print("\n".join(lines))
    if report:
        with open(report, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def main():
    if "--list-gpus" in sys.argv:          # child mode used by list_gpus()
        api, out = sys.argv[sys.argv.index("--list-gpus") + 1:][:2]
        try:
            names = _dxgi_gpu_names() if api == D3D12 else _vulkan_gpu_names()
        except Exception:  # noqa
            import traceback
            names = []
            with open(out + ".err", "w", encoding="utf-8") as f:   # for CI
                f.write(traceback.format_exc())
        with open(out, "w", encoding="utf-8") as f:
            json.dump(names, f)
        return
    if "--version" in sys.argv:
        print(VERSION)
        return
    if "--self-test" in sys.argv:
        # Exit code only: a windowed .exe would otherwise show an error dialog
        # and block the build script.
        argv = sys.argv
        report = argv[argv.index("--report") + 1] if "--report" in argv else None
        try:
            self_test(report)
        except Exception as e:  # noqa
            print(f"self-test FAILED: {e!r}")
            sys.exit(1)
        return
    if "--dry-run" in sys.argv:
        print(shlex.join(build_command(load_settings())))
        return
    if "--play" in sys.argv:
        s = load_settings()
        save_settings(s)
        play(s, wait="--wait" in sys.argv)
        return
    run_gui()


if __name__ == "__main__":
    main()
