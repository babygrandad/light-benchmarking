"""Benchmark logger. Listens to your existing hotkeys, reads RTSS (frames) and Afterburner (sensors).

Start it before a session and leave the window open:

  python bench/rtss_log.py            log runs
  python bench/rtss_log.py relink     after renaming clips: update presets and file names
  python bench/rtss_log.py probe 10   show which apps RTSS is sending frames for

Hotkeys (the logger only listens, Afterburner and NVIDIA keep working as before):
  [         start a run. Pressed during a run: restart, the current attempt is thrown away
  ]         stop and save. Runs under 5 s count as a reset and are not saved
  Alt+F9   NVIDIA recording on/off. Runs are linked to the clip a few seconds later

Beeps: one = started, two = saved, three quick = not saved.

Recorded per run, as one entry in results.json (frames and sensor readings are only kept in
memory during the run and thrown away once the numbers are worked out):
  game, preset, clip, where the run sits in the clip, resolution, API, RAM setup,
  avg FPS, 1% low (RTSS integral method), avg frametime, GPU / CPU usage and temps, RAM,
  share of the run that was GPU / CPU bound

Not saved (three quick beeps): runs under 5 s, runs where the game closed, and runs where more
than 10% of the time has no frames. A freeze or hitch is not a gap: it is one long frame and counts.

Game comes from the NVIDIA folder, preset from the clip name: everything after the first " - "
("Onimusha - High, DLSS Quality, No RT" -> "High, DLSS Quality, No RT"). One run per clip: a newer
run in the same clip replaces the older one. Read only: nothing is injected or sent anywhere.
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

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, 'results.json')
VIDEO_ROOT = r'D:\Nvidia Recordings SSD\Game vids'
MIN_RUN_S = 5
MIN_COVERAGE = 0.9  # logged frame time must cover 90% of the clock time between [ and ]
GPU_BOUND, CPU_GPU_IDLE, CPU_THREAD_BUSY = 95, 90, 90  # % thresholds for the bottleneck label
VK_LBRACKET, VK_RBRACKET, VK_MENU, VK_F9 = 0xDB, 0xDD, 0x12, 0x78

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
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short

PIPE_NAME = r'\\.\PIPE\RTSS_Frametime'
ERROR_BROKEN_PIPE, ERROR_PIPE_CONNECTED = 109, 535
INVALID_HANDLE = ctypes.c_void_p(-1).value
FRAME = 8  # FRAMETIME_PIPE_DATA { DWORD dwApp; DWORD dwFrametime; }  (RTSS SDK, Samples/Pipe)
API = {1: 'OpenGL', 2: 'DirectDraw', 3: 'D3D8', 4: 'D3D9', 5: 'D3D9Ex', 6: 'D3D10', 7: 'D3D11',
       8: 'D3D12', 9: 'D3D12 AFR', 10: 'Vulkan'}
# Offsets inside RTSS_SHARED_MEMORY_APP_ENTRY (RTSSSharedMemory.h, v2.21, x64 packing; the
# struct adds up to dwAppEntrySize = 12416 on this PC, which confirms the layout)
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
    """Server end of the RTSS frametime pipe. RTSS connects to it by itself."""

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
        self.connected = False
        self.buf = ctypes.create_string_buffer(FRAME * 512)

    def read(self):
        """Every (app_slot, frametime_us) waiting in the pipe, without blocking."""
        if not self.connected:
            if k32.ConnectNamedPipe(self.h, None) or ctypes.get_last_error() == ERROR_PIPE_CONNECTED:
                self.connected = True
            else:
                return []
        out, n = [], wt.DWORD()
        while k32.ReadFile(self.h, self.buf, len(self.buf), ctypes.byref(n), None) and n.value:
            out.extend(struct.iter_unpack('<II', self.buf.raw[:n.value - n.value % FRAME]))
        if ctypes.get_last_error() == ERROR_BROKEN_PIPE:  # RTSS restarted
            k32.DisconnectNamedPipe(self.h)
            self.connected = False
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
    """FPS math as RTSS does it with this PC's settings (PercentileCalc=0, integral): the 1% low
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


# ---------------------------------------------------------------- files and clips

NV_STAMP = re.compile(r'(\d{4})\.(\d{2})\.(\d{2}) - (\d{2})\.(\d{2})\.(\d{2})\.(\d{2})$')


def clips():
    """[(path, start_unix, end_unix)] for every NVIDIA clip. Start is the timestamp in an untouched
    NVIDIA name, else the file's creation time (Windows keeps it when you rename)."""
    out = []
    for path in glob.glob(os.path.join(VIDEO_ROOT, '**', '*.mp4'), recursive=True):
        st = os.stat(path)
        start = getattr(st, 'st_birthtime', st.st_ctime)
        m = NV_STAMP.search(os.path.splitext(os.path.basename(path))[0])
        if m:
            y, mo, d, h, mi, s, cs = map(int, m.groups())
            start = datetime(y, mo, d, h, mi, s).timestamp() + cs / 100
        out.append((path, start, st.st_mtime))
    return out


def describe(clip_path):
    """Game from the NVIDIA folder, preset from everything after the first " - " in a renamed clip."""
    game = re.sub(r'\s+', ' ', os.path.basename(os.path.dirname(clip_path))).strip()
    stem = os.path.splitext(os.path.basename(clip_path))[0]
    preset = '' if NV_STAMP.search(stem) or ' - ' not in stem else stem.split(' - ', 1)[1].strip()
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


def relink(data, quiet=False):
    """Match each run to the clip recording during it and keep the newest run per clip.
    Safe to run any time, also after renaming clips."""
    all_clips = clips()
    for run in data['runs']:
        best, best_overlap = None, 0.0
        for c in all_clips:
            overlap = min(c[2], run['ended_unix']) - max(c[1], run['started_unix'])
            if overlap > best_overlap:
                best, best_overlap = c, overlap
        if best:
            path, c0, c1 = best
            game, preset = describe(path)
            run.update(clip=os.path.basename(path), clip_path=path, game=game, preset=preset,
                       clip_from_s=round(run['started_unix'] - c0, 1), clip_to_s=round(run['ended_unix'] - c0, 1),
                       fully_on_clip=c0 <= run['started_unix'] and run['ended_unix'] <= c1 + 2)

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


# ---------------------------------------------------------------- main loop

def probe(pipe, seconds):
    print(f'Listening for {seconds:g}s. Have a game (or any 3D app RTSS hooks) on screen...')
    counts, t0 = {}, time.time()
    while time.time() - t0 < seconds:
        for slot, _ in pipe.read():
            counts[slot] = counts.get(slot, 0) + 1
        time.sleep(0.01)
    if not pipe.connected:
        print('No frames: RTSS sends them from inside a running game, so start one (with RTSS running) and probe again.')
    elif not counts:
        print('RTSS connected, but no frames arrived. Is a hooked 3D app on screen?')
    for slot, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        a = app_info(slot)
        print(f"  {a['exe']} (pid {a['pid']}, {a['api']}, {a['res'] or '?'}): {n} frames, ~{n / seconds:.0f} fps")
    s = read_sensors()
    print('Afterburner sensors:', ', '.join(f'{k}={v}' for k, v in s.items()) if s['gpu_usage'] is not None
          else 'not available (is Afterburner running?)')
    print('RAM:', ram_setup() or 'unknown')


def beep_not_saved():
    for _ in range(3):
        winsound.Beep(660, 70)


def save(run, now, ram):
    logged, clock = sum(run['ft']) / 1e6, now - run['start']
    if logged < MIN_RUN_S:
        print(f'\n[RESET] {logged:.1f}s, not saved (under {MIN_RUN_S}s)')
        beep_not_saved()
        return
    if logged < MIN_COVERAGE * clock:
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
    r = next(x for x in relink(data)['runs'] if x['started_unix'] == run['start'])
    where = (f"{r['clip']} @ {r['clip_from_s']:.0f}s-{r['clip_to_s']:.0f}s" if r.get('clip')
             else 'no clip yet (links after Alt+F9)')
    bound = (f"GPU bound {r['gpu_bound_pct']}% / CPU bound {r['cpu_bound_pct']}%"
             if r['gpu_bound_pct'] is not None else 'no sensors')
    print(f"\n[SAVED] {r['avg_fps']} avg | {r['p1_low_fps']} 1% low | {bound} | {r['duration_s']:.0f}s | {where}")
    winsound.Beep(1320, 90)
    winsound.Beep(1320, 90)


def main():
    pipe = FramePipe()
    if sys.argv[1:2] == ['probe']:
        return probe(pipe, float(sys.argv[2]) if len(sys.argv) > 2 else 10)

    ram = ram_setup()
    print(f'RAM: {ram or "unknown"}')
    print('Logger ready.   [ start/restart    ] save    Alt+F9 recording    Ctrl+C quits')
    keys = dict.fromkeys((VK_LBRACKET, VK_RBRACKET, VK_F9), False)
    run, relink_at, waiting_msg = None, 0, 0
    while True:
        frames = pipe.read()
        now = time.time()
        pressed = set()
        for vk in keys:
            down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
            if down and not keys[vk]:
                pressed.add(vk)
            keys[vk] = down

        if VK_F9 in pressed and user32.GetAsyncKeyState(VK_MENU) & 0x8000:
            relink_at = now + 4  # give NVIDIA a moment to create or finish the file
        if relink_at and now >= relink_at and run is None:
            relink_at = 0
            relink(load())

        if VK_LBRACKET in pressed:
            print('\n[REC] restarted' if run else f'\n[REC] started {datetime.now():%H:%M:%S}')
            run = {'pid': foreground_pid(), 'slot': None, 'ft': [], 'samples': [], 'start': now, 'tick': now}
            winsound.Beep(880, 120)
            continue

        if run is None:
            if not pipe.connected and now - waiting_msg > 10:
                waiting_msg = now
                print('waiting for RTSS to connect...')
            time.sleep(0.005)
            continue

        for slot, us in frames:
            if run['slot'] is None:  # lock onto the game that was in front when [ was pressed
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
        if VK_RBRACKET in pressed:
            if run['slot'] is None:
                print('\n[RESET] no frames from the game in front. Is it hooked by RTSS? (try: probe 10)')
                beep_not_saved()
            else:
                save(run, now, ram)
            run = None
        time.sleep(0.005)


if __name__ == '__main__':
    try:
        if sys.argv[1:2] == ['relink']:
            for r in relink(load())['runs']:
                print(f"{r.get('clip') or '(no clip)':55s} {r['avg_fps']:6} avg {r['p1_low_fps']:6} 1% low")
        else:
            main()
    except KeyboardInterrupt:
        print('\nbye')
