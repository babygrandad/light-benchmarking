# Copyright 2026 Relebogile Nkotswe (babygrandad). Licensed under the Apache License 2.0, see LICENSE and NOTICE.
"""Read the RTSS / Afterburner overlay out of recordings with the Windows OCR engine.

A backup for runs recorded without the logger: samples a frame every few seconds, crops the
overlay, reads it locally and writes what it found. Nothing leaves the PC.

  python osd_ocr.py                        every recording in your video folder not read yet
  python osd_ocr.py "Game - High.mp4"      just these recordings (name or full path)
  python osd_ocr.py --every 2 --force      denser sampling, redo recordings already read
  python osd_ocr.py --calibrate "Game - High.mp4" --at 60
                                           print every word the OCR sees at 60 s, with its
                                           position, to set up the layout for YOUR overlay

Writes:
  ocr/<recording>.ocr.csv   one row per sample: time + every value read (blank = unreadable)
  ocr_results.json          per recording: last avg / 1% low on the overlay, averages of the rest,
                            marked "source": "overlay OCR" so it is never mixed up with logger runs

The overlay layout (where each number sits) lives in config.json under "ocr". The default is the
author's overlay and will not match yours until calibrated: see README, "Calibrating the OCR".
If your overlay shows Afterburner's "Framerate Avg" and "Framerate 1% Low", they reset with the
benchmark start key, so the last readable values are that pass's result.
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

from bench_setup import require_config
from rtss_log import clips, describe

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, 'ocr')
RESULTS = os.path.join(HERE, 'ocr_results.json')

# Clean-ups tried in order; later ones only fill values the earlier ones missed. On the author's
# overlay the first alone read 95% of values, all four 98%, and wherever two passes both read a
# value they agreed.
PASSES = [
    'format=gray,eq=contrast=1.8:brightness=-0.15',
    '',
    'format=gray,eq=contrast=2.5:brightness=-0.25',
    'format=gray,eq=contrast=1.8:brightness=-0.15,negate',
]
NAMES = ['gpu_usage', 'gpu_temp', 'vram_mb', 'cpu_usage', 'cpu_temp', 'ram_mb', 'fps', 'fps_avg', 'fps_1low']
LOOKALIKE = str.maketrans({'o': '0', 'O': '0', 'D': '0', 'g': '9', 'q': '9', 'l': '1', 'I': '1', '|': '1',
                           'i': '1', 'S': '5', 's': '5', 'B': '8', 'Z': '2', 'z': '2', ',': '.'})
NUMBER = re.compile(r'^(\d+(?:\.\d+)?)')


def duration(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', path],
                         capture_output=True, text=True).stdout
    return float(out.strip() or 0)


def grab(path, t, box, scale, clean=''):
    """PNG bytes of the overlay box at t seconds, cleaned up and upscaled."""
    x, y, w, h = box
    vf = ','.join(f for f in (f'crop={w}:{h}:{x}:{y}', clean, f'scale=iw*{scale}:ih*{scale}:flags=lanczos') if f)
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


def words(result, box, scale):
    """(text, x, y) of every OCR word, x = left edge and y = vertical centre in video pixels."""
    for line in result.lines:
        for word in line.words:
            r = word.bounding_rect
            yield word.text, box[0] + r.x / scale, box[1] + (r.y + r.height / 2) / scale


def parse(result, layout):
    """Map OCR words to overlay fields by position. Unreadable or implausible values stay None."""
    by_cell = {(row, col): (name, lo, hi) for name, (row, col, lo, hi) in layout['fields'].items()}
    values = dict.fromkeys(NAMES)
    for text, x, y in words(result, layout['box'], layout['scale']):
        row = next((k for k, cy in layout['rows'].items() if abs(y - cy) <= 6), None)
        col = next((i for i, (a, b) in enumerate(layout['columns']) if a <= x <= b), None)
        field = by_cell.get((row, col))
        m = NUMBER.match(text.translate(LOOKALIKE))
        if not field or not m:
            continue
        name, lo, hi = field
        value = float(m.group(1))
        if lo <= value <= hi and values.get(name) is None:
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
        # an overlay has no per-thread CPU usage, so only GPU bound vs not can be told apart
        'gpu_bound_pct': round(100 * sum(g >= 95 for g in gpu) / len(gpu)) if gpu else None,
    }


async def read_clip(engine, path, layout, start, every):
    end = duration(path)
    times = [start + i * every for i in range(int((end - 1 - start) // every) + 1)]
    if end - 1 > start and (not times or end - 1 - times[-1] > 0.5):
        times.append(end - 1)  # always look at the very end, where avg / 1% low are final
    rows = []
    for i, t in enumerate(times):
        values = dict.fromkeys(NAMES)
        for clean in PASSES:
            png = grab(path, t, layout['box'], layout['scale'], clean)
            if not png:
                break
            for name, value in parse(await ocr(engine, png), layout).items():
                if values[name] is None:
                    values[name] = value
            if all(values[n] is not None for n in layout['fields']):
                break
        rows.append({'time_s': round(t, 1), **values})
        print(f'\r  {i + 1}/{len(times)}', end='', flush=True)
    print()
    return rows


async def calibrate(engine, path, at):
    """Print every word in the top half of the frame with its position, for setting up the layout."""
    probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                            'stream=width,height', '-of', 'csv=p=0', path], capture_output=True, text=True).stdout
    w, h = (int(v) for v in probe.strip().split(','))
    box, scale = (0, 0, w, h // 2), 2
    result = await ocr(engine, grab(path, at, box, scale, PASSES[0]))
    print(f'{os.path.basename(path)} at {at}s ({w}x{h}). Words in the top half, as  text @ x (left edge), y (centre):')
    for text, x, y in sorted(words(result, box, scale), key=lambda t: (round(t[2] / 8), t[1])):
        print(f'  {text:14s} @ x {x:6.0f}, y {y:5.0f}')
    print('\nPut these into config.json "ocr": "box" around the overlay, "rows" = the y of each line, '
          '"columns" = x ranges of the value columns, "fields" = which row/column holds which value. '
          'See README, "Calibrating the OCR".')


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('clips', nargs='*', help='recording names or paths (default: every recording in your video folder)')
    ap.add_argument('--every', type=float, help='seconds between samples (default from config, 5)')
    ap.add_argument('--start', type=float, help='first sample, skips menus at the start (default from config, 20)')
    ap.add_argument('--force', action='store_true', help='read recordings again even if already done')
    ap.add_argument('--calibrate', action='store_true', help='print the words the OCR sees, to set up the layout')
    ap.add_argument('--at', type=float, default=60, help='with --calibrate: second of the recording to look at')
    args = ap.parse_args()

    cfg = require_config()
    layout = cfg['ocr']
    engine = OcrEngine.try_create_from_user_profile_languages()
    if not engine:
        sys.exit('Windows OCR has no language installed (Settings > Time & language > add English).')

    all_paths = [c[0] for c in clips(cfg)]
    paths = [p for p in all_paths if not args.clips or os.path.basename(p) in args.clips or p in args.clips]
    paths += [c for c in args.clips if os.path.isfile(c) and c not in paths]
    if not paths:
        sys.exit('No recordings found. Pass a file path, or turn video on in: python rtss_log.py setup')

    if args.calibrate:
        return await calibrate(engine, paths[0], args.at)

    results = {}
    if os.path.exists(RESULTS):
        with open(RESULTS, encoding='utf-8') as f:
            results = json.load(f)
    os.makedirs(OUT_DIR, exist_ok=True)
    start = args.start if args.start is not None else layout.get('start_s', 20)
    every = args.every if args.every is not None else layout.get('every_s', 5)

    for path in sorted(paths):
        name = os.path.basename(path)
        if name in results and not args.force:
            print(f'{name}: already read (use --force to redo)')
            continue
        print(name)
        rows = await read_clip(engine, path, layout, start, every)
        stem = os.path.splitext(name)[0]
        with open(os.path.join(OUT_DIR, f'{stem}.ocr.csv'), 'w', newline='') as f:
            w = csv.DictWriter(f, ['time_s', *NAMES])
            w.writeheader()
            w.writerows(rows)
        game, preset = describe(path, cfg['video']['folder'] or os.path.dirname(path))
        s = summarize(rows)
        results[name] = {'game': game or stem, 'preset': preset or '', 'csv': f'ocr/{stem}.ocr.csv', **s}
        print(f"  avg {s['osd_avg_fps']} | 1% low {s['osd_1low_fps']} (overlay at {s['osd_values_at_s']}s) | "
              f"GPU {s['gpu_usage_avg']}% | CPU {s['cpu_usage_avg']}% | {s['readable_pct']}% of samples readable")
        if s['readable_pct'] == 0:
            print('  nothing readable: no overlay in this recording, or the layout needs calibrating (--calibrate)')
        with open(RESULTS, 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2)


if __name__ == '__main__':
    asyncio.run(main())
