# Copyright 2026 Relebogile Nkotswe (babygrandad). Licensed under the Apache License 2.0, see LICENSE and NOTICE.
"""Light Benchmarking logger. Reads RTSS (every frame) and MSI Afterburner (sensors) while you play.

  python rtss_log.py setup      first time: pick your hotkeys, say whether you record video
  python rtss_log.py check      what is installed, running and missing
  python rtss_log.py            log runs (start it BEFORE the game)
  python rtss_log.py label      give runs a game name and preset (when you don't record video)
  python rtss_log.py relink     after renaming recordings: re-match runs and update presets
  python rtss_log.py probe 10   show which games RTSS is sending frames for

Your hotkeys (set in setup; the logger only listens, your other tools keep working):
  start key        start a run. Pressed during a run: restart, the current attempt is thrown away
  stop key         stop and save
  record key       optional: your video recorder's hotkey, used to link runs to recordings

Beeps: one = started, two = saved, three quick = not saved.

Each saved run is one entry in results.json (frames and sensor readings are only kept in memory
during the run and thrown away once the numbers are worked out): game, preset, recording (if any),
resolution, API, RAM setup, avg FPS, 1% low (RTSS integral method), avg frametime, GPU / CPU usage
and temps, RAM, share of the run that was GPU / CPU bound.

Not saved (three quick beeps): runs under the minimum length (5 s), runs where the game closed,
and runs where more than 10% of the time has no frames. A freeze or hitch is not a gap: it is one
long frame and counts.

With video on, each run is linked to the recording that was running at the same time. Game comes
from the recording's folder, preset from its name: everything after the first " - "
("Onimusha - High, DLSS Quality" -> "High, DLSS Quality"). One run per recording: a newer run in the
same recording replaces the older one. Read only: nothing is injected or sent anywhere.
"""
import ctypes
import ctypes.wintypes as wt
import glob
import json
import os
import re
import struct
import subprocess
import sys
import time
import winsound
from datetime import datetime

from bench_setup import Hotkey, check, combo_name, require_config, setup

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, 'results.json')
GPU_BOUND, CPU_GPU_IDLE, CPU_THREAD_BUSY = 95, 90, 90  # % thresholds for the bottleneck label

k32 = ctypes.WinDLL('kernel32', use_last_error=True)
user32 = ctypes.WinDLL('user32', use_last_error=True)
advapi32 = ctypes.WinDLL('advapi32', use_last_error=True)
k32.CreateNamedPipeW.restype = wt.HANDLE
k32.CreateNamedPipeW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_void_p]
k32.ConnectNamedPipe.argtypes = [wt.HANDLE, ctypes.c_void_p]
k32.DisconnectNamedPipe.argtypes = [wt.HANDLE]
k32.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
k32.CloseHandle.argtypes = [wt.HANDLE]
k32.OpenFileMappingW.restype = wt.HANDLE
k32.OpenFileMappingW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
k32.MapViewOfFile.restype = ctypes.c_void_p
k32.MapViewOfFile.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_size_t]
k32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]

PIPE_NAME = r'\\.\PIPE\RTSS_Frametime'
ERROR_BROKEN_PIPE, ERROR_PIPE_CONNECTED = 109, 535
INVALID_HANDLE = ctypes.c_void_p(-1).value
FRAME = 8  # FRAMETIME_PIPE_DATA { DWORD dwApp; DWORD dwFrametime; }  (RTSS SDK, Samples/Pipe)
API = {1: 'OpenGL', 2: 'DirectDraw', 3: 'D3D8', 4: 'D3D9', 5: 'D3D9Ex', 6: 'D3D10', 7: 'D3D11',
       8: 'D3D12', 9: 'D3D12 AFR', 10: 'Vulkan'}
# Offsets inside RTSS_SHARED_MEMORY_APP_ENTRY (RTSSSharedMemory.h, v2.21, x64 packing; the struct
# adds up to dwAppEntrySize = 12416 for RTSS 7.3.x, which confirms the layout)
APP_PID, APP_NAME, APP_FLAGS, APP_RES_X, APP_RES_Y = 0, 4, 264, 9224, 9228
# Afterburner sensors (MAHMSharedMemory entry: 5 x char[260] names, then float data)
SENSORS = {'gpu_usage': 'GPU usage', 'gpu_temp': 'GPU temperature', 'cpu_usage': 'CPU usage',
           'cpu_temp': 'CPU temperature', 'ram_mb': 'RAM usage'}
THREAD_USAGE = re.compile(r'CPU\d+ usage$')  # per-thread usage, only used for the bottleneck label


def mapped(name):
    """Read-only view of a named shared memory block: (handle, address), or (None, None)."""
    h = k32.OpenFileMappingW(0x4, False, name)  # FILE_MAP_READ
    if not h:
        return None, None
    return h, k32.MapViewOfFile(h, 0x4, 0, 0, 0)


def unmap(h, p):
    if p:
        k32.UnmapViewOfFile(p)
    if h:
        k32.CloseHandle(h)


def u32(addr):
    return ctypes.c_uint32.from_address(addr).value


# ---------------------------------------------------------------- RTSS

class FramePipe:
    """Server end of the RTSS frametime pipe. RTSS connects to it when a game starts."""

    def __init__(self):
        sd = ctypes.create_string_buffer(64)  # NULL DACL, as in the SDK sample, so RTSS can always connect
        advapi32.InitializeSecurityDescriptor(sd, 1)
        advapi32.SetSecurityDescriptorDacl(sd, True, None, False)

        class SA(ctypes.Structure):
            _fields_ = [('nLength', wt.DWORD), ('lpSecurityDescriptor', ctypes.c_void_p), ('bInheritHandle', wt.BOOL)]

        self._sd = sd
        sa = SA(ctypes.sizeof(SA), ctypes.cast(sd, ctypes.c_void_p), False)
        size = 4096 * FRAME
        # PIPE_ACCESS_DUPLEX, PIPE_TYPE_MESSAGE | PIPE_NOWAIT, unlimited instances
        self.h = k32.CreateNamedPipeW(PIPE_NAME, 0x3, 0x4 | 0x1, 255, size, size, 0, ctypes.byref(sa))
        if self.h in (None, INVALID_HANDLE):
            sys.exit(f'Could not create {PIPE_NAME} (error {ctypes.get_last_error()}). Is another logger open?')
        self.is_connected = False
        self.buf = ctypes.create_string_buffer(FRAME * 512)

    def read(self):
        """Every (app_slot, frametime_us) waiting in the pipe, without blocking."""
        if not self.is_connected:
            if k32.ConnectNamedPipe(self.h, None) or ctypes.get_last_error() == ERROR_PIPE_CONNECTED:
                self.is_connected = True
            else:
                return []
        out, n = [], wt.DWORD()
        while k32.ReadFile(self.h, self.buf, len(self.buf), ctypes.byref(n), None) and n.value:
            out.extend(struct.iter_unpack('<II', self.buf.raw[:n.value - n.value % FRAME]))
        if ctypes.get_last_error() == ERROR_BROKEN_PIPE:  # the game closed or RTSS restarted
            k32.DisconnectNamedPipe(self.h)
            self.is_connected = False
        return out


def app_info(slot):
    """{pid, exe, api, res} of an RTSS app slot, from RTSSSharedMemoryV2."""
    info = {'pid': 0, 'exe': '?', 'api': '?', 'res': None}
    h, p = mapped('RTSSSharedMemoryV2')
    try:
        if not p or u32(p) != 0x52545353 or u32(p + 4) < 0x20000 or slot >= u32(p + 16):  # 'RTSS', v2.x
            return info
        entry_size = u32(p + 8)
        e = p + u32(p + 12) + slot * entry_size  # dwAppArrOffset + slot * dwAppEntrySize
        info['pid'] = u32(e + APP_PID)
        info['exe'] = os.path.basename(ctypes.string_at(e + APP_NAME, 260).split(b'\0')[0].decode('mbcs', 'replace'))
        info['api'] = API.get(u32(e + APP_FLAGS) & 0xFFFF, '?')
        if u32(p + 4) >= 0x20014 and entry_size > APP_RES_Y:  # resolution fields exist from v2.20
            x, y = u32(e + APP_RES_X), u32(e + APP_RES_Y)
            info['res'] = f'{x}x{y}' if x and y else None
        return info
    finally:
        unmap(h, p)


def foreground_pid():
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), ctypes.byref(pid))
    return pid.value


def is_running(pid):
    """False only when the process is really gone. Games we may not open (anti-cheat, admin)
    count as running."""
    h = k32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not h:
        return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER: no such process
    try:
        return k32.WaitForSingleObject(h, 0) == 0x102  # WAIT_TIMEOUT: still running
    finally:
        k32.CloseHandle(h)


# ---------------------------------------------------------------- Afterburner and Windows

def read_sensors():
    """Current SENSORS values from MSI Afterburner plus the bottleneck label, None where unavailable."""
    out = dict.fromkeys(SENSORS)
    out['bound'] = None
    busiest_thread = None
    h, p = mapped('MAHMSharedMemory')
    try:
        if not p or u32(p) != 0x4D41484D:  # 'MAHM'
            return out
        header, count, size = u32(p + 8), u32(p + 12), u32(p + 16)
        wanted = {v: k for k, v in SENSORS.items()}
        for i in range(count):
            e = p + header + i * size
            name = ctypes.string_at(e, 260).split(b'\0')[0].decode('mbcs', 'replace')
            value = ctypes.c_float.from_address(e + 5 * 260).value
            if value >= 1e30:  # FLT_MAX means no data
                continue
            key = wanted.get(name)
            if key and out[key] is None:
                out[key] = round(value, 1)
            elif THREAD_USAGE.match(name):
                busiest_thread = max(busiest_thread or 0, value)
    finally:
        unmap(h, p)

    gpu = out['gpu_usage']
    if gpu is not None:
        if gpu >= GPU_BOUND:
            out['bound'] = 'gpu'
        elif gpu < CPU_GPU_IDLE and busiest_thread is not None and busiest_thread >= CPU_THREAD_BUSY:
            out['bound'] = 'cpu'
        else:
            out['bound'] = 'other'  # FPS cap, V-Sync, engine limit, loading / streaming
    return out


def ram_setup():
    """e.g. "32GB DDR5-6000, dual channel", from the RAM sticks Windows reports."""
    try:
        cmd = ('Get-CimInstance Win32_PhysicalMemory | Select-Object Capacity, BankLabel, DeviceLocator, '
               'ConfiguredClockSpeed, SMBIOSMemoryType | ConvertTo-Json')
        sticks = json.loads(subprocess.run(['powershell', '-NoProfile', '-Command', cmd],
                                           capture_output=True, text=True, timeout=15).stdout)
    except Exception:
        return None
    sticks = sticks if isinstance(sticks, list) else [sticks]
    gb = round(sum(s['Capacity'] for s in sticks) / 2**30)
    kind = {26: 'DDR4', 34: 'DDR5'}.get(sticks[0].get('SMBIOSMemoryType'), 'RAM')
    speed = max(s.get('ConfiguredClockSpeed') or 0 for s in sticks)
    channels = set()
    for s in sticks:  # "P0 CHANNEL A" / "DIMMA1": the letter is the channel
        m = re.search(r'CHANNEL\s*([A-Z])', s.get('BankLabel') or '', re.I) or \
            re.search(r'DIMM\s*([A-Z])', s.get('DeviceLocator') or '', re.I)
        channels.add(m.group(1).upper() if m else s.get('DeviceLocator'))
    mode = {1: 'single channel', 2: 'dual channel', 4: 'quad channel'}.get(len(channels), f'{len(channels)} channels')
    return f"{gb}GB {kind}-{speed}, {mode} ({len(sticks)} stick{'s' if len(sticks) != 1 else ''})"


# ---------------------------------------------------------------- stats

def summarize(ft_us, samples):
    """FPS math as RTSS does it with its default settings (PercentileCalc=0, integral): the 1% low
    is the frame where the slowest frames add up to 1% of the run's time."""
    n, total = len(ft_us), sum(ft_us)
    acc, low = 0, ft_us[0]
    for ft in sorted(ft_us, reverse=True):
        if acc + ft > total / 100:
            low = ft
            break
        acc += ft
    out = {
        'frames': n,
        'duration_s': round(total / 1e6, 1),
        'avg_fps': round(1e6 * n / total, 1),
        'p1_low_fps': round(1e6 / low, 1),
        'avg_frametime_ms': round(total / n / 1000, 2),
    }
    for key in SENSORS:
        vals = [s[key] for s in samples if s[key] is not None]
        out[f'{key}_avg'] = round(sum(vals) / len(vals), 1) if vals else None
        if key in ('gpu_temp', 'cpu_temp', 'ram_mb'):
            out[f'{key}_max'] = max(vals) if vals else None
    labels = [s['bound'] for s in samples if s['bound']]
    for b in ('gpu', 'cpu', 'other'):
        out[f'{b}_bound_pct'] = round(100 * labels.count(b) / len(labels)) if labels else None
    return out


# ---------------------------------------------------------------- recordings

NV_STAMP = re.compile(r'(\d{4})\.(\d{2})\.(\d{2}) - (\d{2})\.(\d{2})\.(\d{2})\.(\d{2})$')


def clips(cfg):
    """[(path, start_unix, end_unix)] for every recording in the video folder. Start is the timestamp
    in an untouched NVIDIA name, else the file's creation time (Windows keeps it when you rename)."""
    video = cfg['video']
    if not video['is_enabled'] or not video['folder']:
        return []
    out = []
    for ext in video['extensions']:
        for path in glob.glob(os.path.join(video['folder'], '**', f'*{ext}'), recursive=True):
            st = os.stat(path)
            start = getattr(st, 'st_birthtime', st.st_ctime)
            m = NV_STAMP.search(os.path.splitext(os.path.basename(path))[0])
            if m:
                y, mo, d, h, mi, s, cs = map(int, m.groups())
                start = datetime(y, mo, d, h, mi, s).timestamp() + cs / 100
            out.append((path, start, st.st_mtime))
    return out


def describe(clip_path, video_folder):
    """(game, preset) from a recording. Game from its folder when the recorder sorts recordings into
    one folder per game (NVIDIA does), preset from everything after the first " - " in its name."""
    parent = os.path.dirname(clip_path)
    is_in_game_folder = os.path.normcase(os.path.abspath(parent)) != os.path.normcase(os.path.abspath(video_folder))
    game = re.sub(r'\s+', ' ', os.path.basename(parent)).strip() if is_in_game_folder else None
    stem = os.path.splitext(os.path.basename(clip_path))[0]
    preset = None if NV_STAMP.search(stem) or ' - ' not in stem else stem.split(' - ', 1)[1].strip()
    return game, preset


def load():
    if os.path.exists(RESULTS):
        with open(RESULTS, encoding='utf-8') as f:
            return json.load(f)
    return {'runs': []}


def store(data):
    tmp = RESULTS + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, RESULTS)


def relink(data, cfg, quiet=False):
    """Match each run to the recording made during it and keep the newest run per recording.
    Safe to run any time, also after renaming recordings. Does nothing with video off."""
    all_clips = clips(cfg)
    for run in data['runs']:
        best, best_overlap = None, 0.0
        for c in all_clips:
            overlap = min(c[2], run['ended_unix']) - max(c[1], run['started_unix'])
            if overlap > best_overlap:
                best, best_overlap = c, overlap
        if best:
            path, c0, c1 = best
            game, preset = describe(path, cfg['video']['folder'])
            run.update(clip=os.path.basename(path), clip_path=path,
                       clip_from_s=round(run['started_unix'] - c0, 1), clip_to_s=round(run['ended_unix'] - c0, 1),
                       is_fully_on_clip=c0 <= run['started_unix'] and run['ended_unix'] <= c1 + 2)
            if game:
                run['game'] = game
            if preset:
                run['preset'] = preset

    newest = {}
    for run in data['runs']:
        if run.get('clip_path') and run['started_unix'] > newest.get(run['clip_path'], {}).get('started_unix', -1):
            newest[run['clip_path']] = run
    kept = []
    for run in data['runs']:
        if run.get('clip_path') and newest[run['clip_path']] is not run:
            if not quiet:
                print(f"\n  replaced the older run in {run['clip']}")
            continue
        if run.get('clip'):
            run['id'] = os.path.splitext(run['clip'])[0]
        kept.append(run)
    data['runs'] = kept
    store(data)
    return data


def label():
    """Type a game name and preset for saved runs (for runs without a recording to name them)."""
    data = load()
    if not data['runs']:
        print('No saved runs yet.')
        return
    for i, r in enumerate(data['runs'], 1):
        print(f"{i:3}. {r['started']}  {r['game']:30.30s} {r.get('preset') or '(no preset)':30.30s} "
              f"{r['avg_fps']} avg / {r['p1_low_fps']} 1% low")
    print('\nPick a run number (Enter to finish). Press Enter at a prompt to keep the current value.')
    while True:
        pick = input('\nRun #: ').strip()
        if not pick:
            break
        if not pick.isdigit() or not 1 <= int(pick) <= len(data['runs']):
            print('  No run with that number')
            continue
        run = data['runs'][int(pick) - 1]
        run['game'] = input(f"  Game [{run['game']}]: ").strip() or run['game']
        run['preset'] = input(f"  Preset [{run.get('preset') or ''}]: ").strip() or run.get('preset', '')
        store(data)
        print(f"  saved: {run['game']} | {run['preset']}")


# ---------------------------------------------------------------- main loop

def probe(pipe, seconds):
    print(f'Listening for {seconds:g}s. Start or restart a game now: RTSS connects when a game launches.')
    counts, t0 = {}, time.time()
    while time.time() - t0 < seconds:
        for slot, _ in pipe.read():
            counts[slot] = counts.get(slot, 0) + 1
        time.sleep(0.01)
    if not pipe.is_connected and not counts:
        print('No frames. RTSS only connects at game launch: keep this running, then start the game.')
    for slot, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        a = app_info(slot)
        print(f"  {a['exe']} (pid {a['pid']}, {a['api']}, {a['res'] or '?'}): {n} frames")
    s = read_sensors()
    print('Afterburner sensors:', ', '.join(f'{k}={v}' for k, v in s.items()) if s['gpu_usage'] is not None
          else 'not available (is MSI Afterburner running?)')
    print('RAM:', ram_setup() or 'unknown')


def beep_not_saved():
    for _ in range(3):
        winsound.Beep(660, 70)


def save(run, now, ram, cfg):
    logged, clock = sum(run['ft']) / 1e6, now - run['start']
    if logged < cfg['min_run_s']:
        print(f"\n[RESET] {logged:.1f}s, not saved (under {cfg['min_run_s']}s)")
        beep_not_saved()
        return
    if logged < cfg['min_coverage'] * clock:
        print(f'\n[DISCARDED] only {logged:.0f}s of {clock:.0f}s had frames, not saved')
        beep_not_saved()
        return
    a = app_info(run['slot'])
    stamp = datetime.fromtimestamp(run['start']).strftime('%Y%m%d-%H%M%S')
    entry = {
        'id': f"{os.path.splitext(a['exe'])[0]}_{stamp}", 'game': os.path.splitext(a['exe'])[0],
        'preset': '', 'clip': None, 'exe': a['exe'], 'api': a['api'], 'res': a['res'], 'ram_setup': ram,
        'started': datetime.fromtimestamp(run['start']).isoformat(timespec='seconds'),
        'started_unix': run['start'], 'ended_unix': now,
        **summarize(run['ft'], run['samples']),
    }
    data = load()
    data['runs'].append(entry)
    r = next(x for x in relink(data, cfg)['runs'] if x['started_unix'] == run['start'])
    if not cfg['video']['is_enabled']:
        where = 'name it later: python rtss_log.py label'
    elif r.get('clip'):
        where = f"{r['clip']} @ {r['clip_from_s']:.0f}s-{r['clip_to_s']:.0f}s"
    else:
        where = 'no recording found yet (python rtss_log.py relink)'
    bound = (f"GPU bound {r['gpu_bound_pct']}% / CPU bound {r['cpu_bound_pct']}%"
             if r['gpu_bound_pct'] is not None else 'no sensors')
    print(f"\n[SAVED] {r['avg_fps']} avg | {r['p1_low_fps']} 1% low | {bound} | {r['duration_s']:.0f}s | {where}")
    winsound.Beep(1320, 90)
    winsound.Beep(1320, 90)


def main(cfg):
    pipe = FramePipe()
    start_key, stop_key = Hotkey(cfg['start_key']), Hotkey(cfg['stop_key'])
    record_key = Hotkey(cfg['video']['record_key'] if cfg['video']['is_enabled'] else None)
    ram = ram_setup()
    print(f'RAM: {ram or "unknown"}')
    print(f"Logger ready.  {combo_name(cfg['start_key'])} start/restart   {combo_name(cfg['stop_key'])} save"
          f"   Ctrl+C quits")
    print('Start your game now (after this window): RTSS connects to the logger when a game launches.')
    run, relink_at, waiting_msg = None, 0, time.time()
    while True:
        frames = pipe.read()
        now = time.time()
        is_start, is_stop, is_record = start_key.pressed(), stop_key.pressed(), record_key.pressed()

        if is_record:
            relink_at = now + 4  # give the recorder a moment to create or finish the file
        if relink_at and now >= relink_at and run is None:
            relink_at = 0
            relink(load(), cfg)

        if is_start:
            print('\n[REC] restarted' if run else f'\n[REC] started {datetime.now():%H:%M:%S}')
            run = {'pid': foreground_pid(), 'slot': None, 'ft': [], 'samples': [], 'start': now, 'tick': now}
            winsound.Beep(880, 120)
            continue

        if run is None:
            if not pipe.is_connected and now - waiting_msg > 30:
                waiting_msg = now
                print('waiting for a game (RTSS connects when a game launches)...')
            time.sleep(0.005)
            continue

        for slot, us in frames:
            if run['slot'] is None:  # lock onto the game that was in front when the start key was pressed
                if app_info(slot)['pid'] != run['pid']:
                    continue
                run['slot'] = slot
            if slot == run['slot'] and us:
                run['ft'].append(us)
        if now - run['tick'] >= 1:
            run['tick'] = now
            if run['slot'] is not None and not is_running(run['pid']):
                print('\n[DISCARDED] the game closed during the run, not saved')
                beep_not_saved()
                run = None
                continue
            run['samples'].append({'t': round(now - run['start']), **read_sensors()})
            if run['ft']:
                recent = run['ft'][-120:]
                print(f"\r  {now - run['start']:5.0f}s  {len(run['ft']):7d} frames  "
                      f"~{1e6 * len(recent) / sum(recent):5.1f} fps", end='', flush=True)
        if is_stop:
            if run['slot'] is None:
                print('\n[RESET] no frames from the game in front. Was the logger started before the game? '
                      '(try: python rtss_log.py probe 30)')
                beep_not_saved()
            else:
                save(run, now, ram, cfg)
            run = None
        time.sleep(0.005)


if __name__ == '__main__':
    command = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        if command == 'setup':
            setup()
        elif command == 'check':
            check()
        elif command == 'label':
            label()
        elif command == 'relink':
            for r in relink(load(), require_config())['runs']:
                print(f"{r.get('clip') or '(no recording)':55s} {r['avg_fps']:6} avg {r['p1_low_fps']:6} 1% low")
        elif command == 'probe':
            probe(FramePipe(), float(sys.argv[2]) if len(sys.argv) > 2 else 10)
        elif command is None:
            main(require_config())
        else:
            sys.exit(__doc__)
    except KeyboardInterrupt:
        print('\nbye')
