"""Read the RTSS / Afterburner overlay out of recorded clips with the Windows OCR engine.

For clips recorded without the logger: samples a frame every few seconds, crops the overlay box
(top center), reads it locally and writes what it found. Nothing leaves the PC.

  python bench/osd_ocr.py                      every clip in the NVIDIA folder not read yet
  python bench/osd_ocr.py "Onimusha - High.mp4" just these clips (name or full path)
  python bench/osd_ocr.py --every 2 --force    denser sampling, redo clips already read

Writes:
  ocr/<clip>.ocr.csv     one row per sample: time in clip + every value read (blank = unreadable)
  ocr_results.json       per clip: last avg / 1% low shown on the overlay, averages of the rest,
                         marked "source": "overlay OCR" so it is never mixed up with logger runs

Overlay layout this reads (Afterburner OSD, top center, 1920x1080):
  GPU  usage %  temp C  clock  power      MEM  VRAM MB      CPU  usage %  temp C  clock  power
  RAM  MB       D3D12  current FPS  avg FPS  1% low FPS
The avg and 1% low reset when [ is pressed, so the last readable values are that pass's result.
"""
import argparse
import asyncio
import csv
import json
import os
import re
import subprocess
import sys

from winrt.windows.graphics.imaging import BitmapDecoder
from winrt.windows.media.ocr import OcrEngine
from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

from rtss_log import VIDEO_ROOT, clips, describe

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, 'ocr')
RESULTS = os.path.join(HERE, 'ocr_results.json')

BOX = (740, 0, 440, 140)  # x, y, w, h of the overlay box in a 1920x1080 frame
SCALE = 3                 # upscale before OCR, the overlay font is small
# Clean-ups tried in order; later ones only fill values the earlier ones missed. Tested on
# "Onimusha - RT Ultra": 95% of values from the first alone, 98% with all four, and wherever
# two passes both read a value they agreed.
PASSES = [
    'format=gray,eq=contrast=1.8:brightness=-0.15',
    '',
    'format=gray,eq=contrast=2.5:brightness=-0.25',
    'format=gray,eq=contrast=1.8:brightness=-0.15,negate',
]
ROWS = {'gpu': 10, 'mem': 31, 'cpu': 52, 'ram': 73, 'fps': 94}  # row centre y, frame pixels
COLS = [(790, 870), (900, 955), (975, 1035)]                    # left edge x of value columns 1-3
FIELDS = {  # (row, column): name, plausible range
    ('gpu', 0): ('gpu_usage', 0, 100), ('gpu', 1): ('gpu_temp', 20, 110),
    ('mem', 0): ('vram_mb', 100, 12288),
    ('cpu', 0): ('cpu_usage', 0, 100), ('cpu', 1): ('cpu_temp', 20, 110),
    ('ram', 0): ('ram_mb', 1000, 65536),
    ('fps', 0): ('fps', 1, 1000), ('fps', 1): ('fps_avg', 1, 1000), ('fps', 2): ('fps_1low', 1, 1000),
}
NAMES = [name for name, _, _ in FIELDS.values()]
LOOKALIKE = str.maketrans({'o': '0', 'O': '0', 'D': '0', 'g': '9', 'q': '9', 'l': '1', 'I': '1', '|': '1',
                           'i': '1', 'S': '5', 's': '5', 'B': '8', 'Z': '2', 'z': '2', ',': '.'})
NUMBER = re.compile(r'^(\d+(?:\.\d+)?)')


def duration(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', path],
                         capture_output=True, text=True).stdout
    return float(out.strip() or 0)


def grab(path, t, clean=''):
    """PNG bytes of the overlay box at t seconds, cleaned up and upscaled."""
    x, y, w, h = BOX
    vf = ','.join(f for f in (f'crop={w}:{h}:{x}:{y}', clean, f'scale=iw*{SCALE}:ih*{SCALE}:flags=lanczos') if f)
    return subprocess.run(
        ['ffmpeg', '-v', 'error', '-ss', f'{t:.2f}', '-i', path, '-frames:v', '1',
         '-vf', vf, '-f', 'image2pipe', '-c:v', 'png', '-'],
        capture_output=True).stdout


async def ocr(engine, png):
    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(png)
    await writer.store_async()
    await writer.flush_async()
    stream.seek(0)
    bitmap = await (await BitmapDecoder.create_async(stream)).get_software_bitmap_async()
    return await engine.recognize_async(bitmap)


def parse(result):
    """Map OCR words to overlay fields by position. Unreadable or implausible values stay None."""
    values = dict.fromkeys(NAMES)
    for line in result.lines:
        for word in line.words:
            r = word.bounding_rect
            x = BOX[0] + r.x / SCALE
            y = BOX[1] + (r.y + r.height / 2) / SCALE
            row = next((k for k, cy in ROWS.items() if abs(y - cy) <= 6), None)
            col = next((i for i, (a, b) in enumerate(COLS) if a <= x <= b), None)
            field = FIELDS.get((row, col))
            m = NUMBER.match(word.text.translate(LOOKALIKE))
            if not field or not m:
                continue
            name, lo, hi = field
            value = float(m.group(1))
            if lo <= value <= hi and values[name] is None:
                values[name] = value
    return values


def summarize(rows):
    def col(name):
        return [r[name] for r in rows if r[name] is not None]

    def avg(name):
        v = col(name)
        return round(sum(v) / len(v), 1) if v else None

    # last readable avg / 1% low: what the overlay showed at the end of the pass
    last = next((r for r in reversed(rows) if r['fps_avg'] is not None and r['fps_1low'] is not None), None)
    gpu = col('gpu_usage')
    readable = sum(1 for r in rows if r['fps'] is not None)
    return {
        'source': 'overlay OCR',
        'samples': len(rows),
        'readable_pct': round(100 * readable / len(rows)) if rows else 0,
        'osd_avg_fps': last['fps_avg'] if last else None,
        'osd_1low_fps': last['fps_1low'] if last else None,
        'osd_values_at_s': last['time_s'] if last else None,
        'sampled_fps_avg': avg('fps'),
        'gpu_usage_avg': avg('gpu_usage'), 'cpu_usage_avg': avg('cpu_usage'),
        'gpu_temp_avg': avg('gpu_temp'), 'gpu_temp_max': max(col('gpu_temp'), default=None),
        'cpu_temp_avg': avg('cpu_temp'), 'cpu_temp_max': max(col('cpu_temp'), default=None),
        'ram_mb_avg': avg('ram_mb'), 'ram_mb_max': max(col('ram_mb'), default=None),
        'vram_mb_avg': avg('vram_mb'), 'vram_mb_max': max(col('vram_mb'), default=None),
        # the overlay has no per-thread CPU usage, so only GPU bound vs not can be told apart
        'gpu_bound_pct': round(100 * sum(g >= 95 for g in gpu) / len(gpu)) if gpu else None,
    }


async def read_clip(engine, path, start, every):
    end = duration(path)
    times = [start + i * every for i in range(int((end - 1 - start) // every) + 1)]
    if end - 1 > start and (not times or end - 1 - times[-1] > 0.5):
        times.append(end - 1)  # always look at the very end, where avg / 1% low are final
    rows = []
    for i, t in enumerate(times):
        values = dict.fromkeys(NAMES)
        for clean in PASSES:
            png = grab(path, t, clean)
            if not png:
                break
            for name, value in parse(await ocr(engine, png)).items():
                if values[name] is None:
                    values[name] = value
            if None not in values.values():
                break
        rows.append({'time_s': round(t, 1), **values})
        print(f'\r  {i + 1}/{len(times)}', end='', flush=True)
    print()
    return rows


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('clips', nargs='*', help='clip names or paths (default: every clip in the NVIDIA folder)')
    ap.add_argument('--every', type=float, default=5, help='seconds between samples (default 5)')
    ap.add_argument('--start', type=float, default=20, help='first sample, skips menus at the start (default 20)')
    ap.add_argument('--force', action='store_true', help='read clips again even if already done')
    args = ap.parse_args()

    all_paths = [c[0] for c in clips()]
    if args.clips:
        paths = [p for p in all_paths if os.path.basename(p) in args.clips or p in args.clips]
        paths += [c for c in args.clips if os.path.isfile(c) and c not in paths]
    else:
        paths = all_paths
    if not paths:
        sys.exit(f'No clips found (looked in {VIDEO_ROOT}).')

    results = {}
    if os.path.exists(RESULTS):
        with open(RESULTS, encoding='utf-8') as f:
            results = json.load(f)
    engine = OcrEngine.try_create_from_user_profile_languages()
    os.makedirs(OUT_DIR, exist_ok=True)

    for path in sorted(paths):
        name = os.path.basename(path)
        if name in results and not args.force:
            print(f'{name}: already read (use --force to redo)')
            continue
        print(name)
        rows = await read_clip(engine, path, args.start, args.every)
        stem = os.path.splitext(name)[0]
        with open(os.path.join(OUT_DIR, f'{stem}.ocr.csv'), 'w', newline='') as f:
            w = csv.DictWriter(f, ['time_s', *NAMES])
            w.writeheader()
            w.writerows(rows)
        game, preset = describe(path)
        s = summarize(rows)
        results[name] = {'game': game, 'preset': preset, 'csv': f'ocr/{stem}.ocr.csv', **s}
        print(f"  avg {s['osd_avg_fps']} | 1% low {s['osd_1low_fps']} (overlay at {s['osd_values_at_s']}s) | "
              f"GPU {s['gpu_usage_avg']}% | CPU {s['cpu_usage_avg']}% | {s['readable_pct']}% of samples readable")
        with open(RESULTS, 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2)


if __name__ == '__main__':
    asyncio.run(main())
