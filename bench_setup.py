# Copyright 2026 Relebogile Nkotswe (babygrandad). Licensed under the Apache License 2.0, see LICENSE and NOTICE.
"""Settings, first-run setup and the requirements check for Light Benchmarking.

  python rtss_log.py setup    choose hotkeys and whether you record video
  python rtss_log.py check    see what is installed, running and missing

Settings live in config.json next to the scripts. It is personal (hotkeys, folders) and is
never uploaded (.gitignore).
"""
import ctypes
import ctypes.wintypes as wt
import importlib.util
import json
import msvcrt
import os
import platform
import re
import shutil
import sys
import time
import winreg

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, 'config.json')

user32 = ctypes.WinDLL('user32', use_last_error=True)
k32 = ctypes.WinDLL('kernel32', use_last_error=True)
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.MapVirtualKeyW.argtypes = [wt.UINT, wt.UINT]
user32.GetKeyNameTextW.argtypes = [wt.LONG, wt.LPWSTR, ctypes.c_int]
k32.OpenFileMappingW.restype = wt.HANDLE
k32.OpenFileMappingW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
k32.MapViewOfFile.restype = ctypes.c_void_p
k32.MapViewOfFile.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_size_t]
k32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
k32.CloseHandle.argtypes = [wt.HANDLE]

# Example overlay layout for osd_ocr.py: the author's Afterburner OSD at the top center of a
# 1920x1080 recording. Yours is almost certainly different: see README, "Calibrating the OCR".
EXAMPLE_OCR_LAYOUT = {
    'box': [740, 0, 440, 140],  # x, y, w, h of the overlay in the video frame
    'scale': 3,
    'rows': {'gpu': 10, 'mem': 31, 'cpu': 52, 'ram': 73, 'fps': 94},  # centre y of each overlay row
    'columns': [[790, 870], [900, 955], [975, 1035]],                  # left edge x range of value columns
    'fields': {  # name: [row, column, lowest plausible, highest plausible]
        'gpu_usage': ['gpu', 0, 0, 100], 'gpu_temp': ['gpu', 1, 20, 110],
        'vram_mb': ['mem', 0, 100, 65536],
        'cpu_usage': ['cpu', 0, 0, 100], 'cpu_temp': ['cpu', 1, 20, 110],
        'ram_mb': ['ram', 0, 1000, 262144],
        'fps': ['fps', 0, 1, 1000], 'fps_avg': ['fps', 1, 1, 1000], 'fps_1low': ['fps', 2, 1, 1000],
    },
    'start_s': 20,
    'every_s': 5,
}

DEFAULTS = {
    'start_key': None,  # list of virtual-key codes, e.g. [219] for [
    'stop_key': None,
    'video': {'is_enabled': False, 'folder': None, 'record_key': None, 'extensions': ['.mp4', '.mkv', '.mov']},
    'min_run_s': 5,
    'min_coverage': 0.9,
    'ocr': EXAMPLE_OCR_LAYOUT,
}


# ---------------------------------------------------------------- config

def load_config():
    """Saved settings merged over the defaults, or None when setup has not been run yet."""
    if not os.path.exists(CONFIG):
        return None
    with open(CONFIG, encoding='utf-8') as f:
        saved = json.load(f)
    cfg = json.loads(json.dumps(DEFAULTS))
    for key, value in saved.items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key].update(value)
        else:
            cfg[key] = value
    return cfg


def save_config(cfg):
    with open(CONFIG, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2)


# ---------------------------------------------------------------- hotkeys

MODIFIERS = (0x10, 0x11, 0x12, 0x5B, 0x5C)  # Shift, Ctrl, Alt, Win
# Mouse buttons, and the left/right copies of Shift/Ctrl/Alt (the generic codes cover both)
IGNORED = set(range(0x01, 0x07)) | set(range(0xA0, 0xA6))
EXTENDED = set(range(0x21, 0x2F))  # Page Up ... Delete, arrows: need the extended flag to be named


def key_name(vk):
    names = {0x10: 'Shift', 0x11: 'Ctrl', 0x12: 'Alt', 0x5B: 'Win', 0x5C: 'Win'}
    if vk in names:
        return names[vk]
    buf = ctypes.create_unicode_buffer(32)
    lparam = (user32.MapVirtualKeyW(vk, 0) << 16) | ((vk in EXTENDED) << 24)
    return buf.value if user32.GetKeyNameTextW(lparam, buf, 32) else f'key {vk:#04x}'


def combo_name(keys):
    return ' + '.join(key_name(k) for k in sorted(keys, key=lambda k: (k not in MODIFIERS, k))) if keys else '(none)'


def keys_down():
    return {vk for vk in range(0x08, 0xFF) if vk not in IGNORED and user32.GetAsyncKeyState(vk) & 0x8000}


def capture_combo(prompt, can_skip=False):
    """Wait for the user to press a key or key combination and return its key codes."""
    print(f"  {prompt} {'(Esc to skip)' if can_skip else ''}")
    while keys_down():
        time.sleep(0.02)
    while True:
        pressed = set()
        while not pressed:
            pressed = keys_down()
            time.sleep(0.01)
        while True:  # collect everything held until all keys are released
            now = keys_down()
            if not now:
                break
            pressed |= now
            time.sleep(0.01)
        while msvcrt.kbhit():  # the keys also typed into the console; drop them
            msvcrt.getwch()
        if pressed == {0x1B}:
            if can_skip:
                print('    skipped')
                return None
            print('    Esc cannot be used here, press another key')
            continue
        if pressed == {0x0D}:
            print('    Enter cannot be used here, press another key')
            continue
        print(f'    -> {combo_name(pressed)}')
        return sorted(pressed)


class Hotkey:
    """Edge detector for a key combination: pressed() is True once per press."""

    def __init__(self, keys):
        self.keys = keys or []
        self.was_down = False

    def pressed(self):
        is_down = bool(self.keys) and all(user32.GetAsyncKeyState(k) & 0x8000 for k in self.keys)
        hit = is_down and not self.was_down
        self.was_down = is_down
        return hit


# ---------------------------------------------------------------- what is installed

def install_dir(subkey):
    """InstallDir of a program from HKLM\\SOFTWARE\\WOW6432Node\\<subkey>, or None."""
    for view in (r'SOFTWARE\WOW6432Node', 'SOFTWARE'):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf'{view}\{subkey}') as k:
                return winreg.QueryValueEx(k, 'InstallDir')[0]
        except OSError:
            pass
    return None


def registry_value(subkey, name, root=winreg.HKEY_LOCAL_MACHINE):
    try:
        with winreg.OpenKey(root, subkey) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def afterburner_hotkeys():
    """Afterburner's benchmark begin/end hotkeys as key-code lists, if set."""
    folder = install_dir(r'MSI\Afterburner')
    path = folder and os.path.join(folder, 'Profiles', 'MSIAfterburner.cfg')
    if not path or not os.path.exists(path):
        return None, None
    with open(path, encoding='latin-1') as f:
        text = f.read()

    def decode(name):  # stored as (HOTKEYF_* modifiers << 16) | virtual key
        m = re.search(rf'^{name}=([0-9A-Fa-f]+)h', text, re.M)
        value = int(m.group(1), 16) if m else 0
        vk, mods = value & 0xFF, value >> 16
        if not vk:
            return None
        return [k for bit, k in ((1, 0x10), (2, 0x11), (4, 0x12)) if mods & bit] + [vk]

    return decode('BeginRecordHotkey'), decode('EndRecordHotkey')


def nvidia_video_folder():
    raw = registry_value(r'Software\NVIDIA Corporation\Global\ShadowPlay\NVSPCAPS', 'DefaultPathW', winreg.HKEY_CURRENT_USER)
    if isinstance(raw, bytes):
        path = raw.decode('utf-16-le', 'ignore').rstrip('\0')
        return path if os.path.isdir(path) else None
    return None


def shared_memory(name):
    """(signature, version) of a named shared memory block, or None when the program is not running."""
    h = k32.OpenFileMappingW(0x4, False, name)  # FILE_MAP_READ
    if not h:
        return None
    p = k32.MapViewOfFile(h, 0x4, 0, 0, 0)
    try:
        return (ctypes.c_uint32.from_address(p).value, ctypes.c_uint32.from_address(p + 4).value) if p else None
    finally:
        if p:
            k32.UnmapViewOfFile(p)
        k32.CloseHandle(h)


def afterburner_sensor_names():
    h = k32.OpenFileMappingW(0x4, False, 'MAHMSharedMemory')
    if not h:
        return set()
    p = k32.MapViewOfFile(h, 0x4, 0, 0, 0)
    try:
        u32 = lambda a: ctypes.c_uint32.from_address(a).value
        if not p or u32(p) != 0x4D41484D:
            return set()
        header, count, size = u32(p + 8), u32(p + 12), u32(p + 16)
        return {ctypes.string_at(p + header + i * size, 260).split(b'\0')[0].decode('mbcs', 'replace')
                for i in range(count)}
    finally:
        if p:
            k32.UnmapViewOfFile(p)
        k32.CloseHandle(h)


# ---------------------------------------------------------------- setup

def ask_yes(question, default):
    hint = '[Y/n]' if default else '[y/N]'
    answer = input(f'  {question} {hint} ').strip().lower()
    return default if not answer else answer.startswith('y')


def setup():
    cfg = load_config() or json.loads(json.dumps(DEFAULTS))
    print('\nLight Benchmarking setup. Press the keys you actually use; nothing is assumed.\n')

    print('1. Run hotkeys (start / restart a run, and stop + save it)')
    ab_start, ab_stop = afterburner_hotkeys()
    if ab_start and ab_stop and ask_yes(
            f'MSI Afterburner uses {combo_name(ab_start)} / {combo_name(ab_stop)} for its benchmark. '
            f'Use the same keys?', True):
        cfg['start_key'], cfg['stop_key'] = ab_start, ab_stop
    else:
        cfg['start_key'] = capture_combo('Press the key to START a run:')
        while True:
            cfg['stop_key'] = capture_combo('Press the key to STOP and save a run:')
            if cfg['stop_key'] != cfg['start_key']:
                break
            print('    That is the start key, pick a different one')

    print('\n2. Video (optional)')
    video = cfg['video']
    video['is_enabled'] = ask_yes('Do you record video while benchmarking?', bool(video['is_enabled']))
    if video['is_enabled']:
        suggestion = video['folder'] or nvidia_video_folder()
        while True:
            hint = f' [{suggestion}]' if suggestion else ''
            folder = input(f'  Folder your recordings are saved in{hint}: ').strip().strip('"') or suggestion
            if folder and os.path.isdir(folder):
                video['folder'] = folder
                break
            print('    Folder not found, try again')
        print('  Runs are linked to the recording that was running at the same time.')
        video['record_key'] = capture_combo('Press your record on/off hotkey, if you use one:', can_skip=True)
    else:
        video['folder'] = None
        video['record_key'] = None

    save_config(cfg)
    print(f"\nSaved to {CONFIG}")
    print(f"  start / restart : {combo_name(cfg['start_key'])}")
    print(f"  stop + save     : {combo_name(cfg['stop_key'])}")
    print(f"  video           : {video['folder'] if video['is_enabled'] else 'off'}")
    if video['is_enabled']:
        print(f"  record hotkey   : {combo_name(video['record_key'])}")
    print('\nNext: python rtss_log.py check')
    return cfg


def require_config():
    cfg = load_config()
    if cfg and cfg.get('start_key') and cfg.get('stop_key'):
        return cfg
    print('No settings yet, starting setup first.')
    return setup()


# ---------------------------------------------------------------- check

def check():
    rows = []

    def add(state, what, hint=''):
        rows.append((state, what, hint))

    def ok(what):
        add('OK', what)

    def missing(what, hint):
        add('MISSING', what, hint)

    def warn(what, hint):
        add('WARN', what, hint)

    print('\nLight Benchmarking requirements check\n')

    # system
    if sys.platform == 'win32':
        ok(f'Windows {platform.release()} ({platform.version()})')
    else:
        missing('Windows', 'This tool only runs on Windows 10 or 11.')
    if sys.version_info >= (3, 10):
        ok(f'Python {platform.python_version()}')
    else:
        missing(f'Python 3.10 or newer (found {platform.python_version()})', 'winget install Python.Python.3.12')

    # RTSS (required)
    rtss_dir = install_dir(r'Unwinder\RTSS')
    if rtss_dir:
        version = registry_value(r'SOFTWARE\WOW6432Node\Unwinder\RTSS', 'ProductVersion') or ''
        ok(f'RivaTuner Statistics Server {version}'.strip())
        mem = shared_memory('RTSSSharedMemoryV2')
        if not mem:
            missing('RTSS running', 'Start RTSS (it usually starts with MSI Afterburner or Windows).')
        elif mem[1] < 0x20014:
            warn('RTSS running, but older than v7.3', 'Update RTSS to read the game resolution.')
        else:
            ok('RTSS running')
        global_profile = os.path.join(rtss_dir, 'Profiles', 'Global')
        if os.path.exists(global_profile):
            with open(global_profile, encoding='latin-1') as f:
                m = re.search(r'^PercentileCalc=(\d+)', f.read(), re.M)
            if m and m.group(1) != '0':
                warn('RTSS 1% low mode is "linear"', 'The logger uses RTSS\'s default "integral" mode, so its 1% lows '
                     'will differ slightly from your overlay. Set RTSS > Setup > percentile calculation to integral.')
    else:
        missing('RivaTuner Statistics Server (RTSS)', 'winget install Guru3D.RTSS  (or guru3d.com, search "RTSS download")')

    # Afterburner (optional, sensors)
    if install_dir(r'MSI\Afterburner'):
        ok('MSI Afterburner installed')
        names = afterburner_sensor_names()
        if not names:
            warn('MSI Afterburner not running', 'Start it for GPU/CPU usage, temperatures, RAM and the bottleneck check.')
        else:
            want = ['GPU usage', 'GPU temperature', 'CPU usage', 'CPU temperature', 'RAM usage']
            gone = [n for n in want if n not in names]
            if gone:
                warn(f"Afterburner sensors off: {', '.join(gone)}",
                     'Afterburner > Settings > Monitoring: tick these graphs.')
            else:
                ok('Afterburner sensors: GPU/CPU usage and temperature, RAM')
            if not any(re.fullmatch(r'CPU\d+ usage', n) for n in names):
                warn('Afterburner per-thread CPU usage off', 'Tick "CPU1 usage" ... in Monitoring, or CPU-bound '
                     'runs cannot be detected (GPU-bound still works).')
    else:
        warn('MSI Afterburner (optional)', 'For sensors: winget install Guru3D.Afterburner')

    # settings
    cfg = load_config()
    if not cfg or not cfg.get('start_key'):
        missing('Settings', 'Run: python rtss_log.py setup')
    else:
        ok(f"Hotkeys: start {combo_name(cfg['start_key'])}, stop {combo_name(cfg['stop_key'])}")
        video = cfg['video']
        if not video['is_enabled']:
            ok('Video linking off (label runs with: python rtss_log.py label)')
        elif video['folder'] and os.path.isdir(video['folder']):
            ok(f"Video folder: {video['folder']}")
        else:
            missing(f"Video folder {video['folder']}", 'Run setup again and pick an existing folder.')

    # overlay OCR (optional)
    for tool in ('ffmpeg', 'ffprobe'):
        if shutil.which(tool):
            ok(f'{tool} (for the overlay OCR)')
        else:
            warn(f'{tool} not found (only needed for the overlay OCR)', 'winget install Gyan.FFmpeg, then open a new terminal')
    if importlib.util.find_spec('winrt') and importlib.util.find_spec('winrt.windows.media.ocr'):
        try:
            from winrt.windows.media.ocr import OcrEngine
            engine = OcrEngine.try_create_from_user_profile_languages()
            if engine:
                ok(f'Windows OCR ({engine.recognizer_language.language_tag})')
            else:
                warn('Windows OCR has no language', 'Windows Settings > Time & language > add an English language pack')
        except Exception as e:
            warn(f'Windows OCR failed to load ({e})', 'python -m pip install -r requirements.txt')
    else:
        warn('Windows OCR packages (only needed for the overlay OCR)', 'python -m pip install -r requirements.txt')

    for state, what, hint in rows:
        print(f'  [{state:7}] {what}')
        if hint:
            print(f'            -> {hint}')
    required = [r for r in rows if r[0] == 'MISSING']
    print('\nReady to log runs.' if not required else f'\n{len(required)} required item(s) missing, see above.')
    print('Tip: start the logger BEFORE the game. RTSS only connects to it when a game launches.')
