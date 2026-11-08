# Light Benchmarking

Frame-accurate game benchmarking for Windows, built on the tools PC gamers already use:
**RivaTuner Statistics Server (RTSS)** for frames and **MSI Afterburner** for sensors.

Press your usual benchmark hotkey in game, play the pass, press stop. You get the same
**average FPS and 1% low** your RTSS overlay shows (same math, every frame counted), plus GPU / CPU
usage, temperatures, RAM, and whether the run was **GPU bound or CPU bound**.

Created by **Relebogile Nkotswe (babygrandad)**. Apache 2.0, see [License](#license).

---

## What you need

| Software | Needed for | Install |
|---|---|---|
| Windows 10 or 11 | everything | |
| Python 3.10 or newer | everything | `winget install Python.Python.3.12` |
| **RivaTuner Statistics Server** 7.3+ | **required**: frame data | `winget install Guru3D.RTSS` |
| **MSI Afterburner** | optional: GPU/CPU usage, temps, RAM, bottleneck check | `winget install Guru3D.Afterburner` |
| A video recorder (NVIDIA App, OBS, AMD, Steam...) | optional: link runs to recordings | |
| ffmpeg | optional: overlay OCR | `winget install Gyan.FFmpeg` |
| Windows OCR packages | optional: overlay OCR | `python -m pip install -r requirements.txt` |

Not sure what you have? Run the check (see below). It lists what is installed, running and missing.

**Afterburner sensors:** in Afterburner > Settings > Monitoring, enable *GPU usage, GPU temperature,
CPU usage, CPU temperature, RAM usage* and the per-thread *CPU1 usage, CPU2 usage, ...* graphs.
The per-thread ones are how CPU-bound runs are detected.

## Install

```
git clone https://github.com/babygrandad/light-benchmarking.git
cd light-benchmarking
python rtss_log.py setup
python rtss_log.py check
```

### Setup asks you, nothing is assumed

1. **Your hotkeys.** Press the key you want for *start* and for *stop*. If MSI Afterburner already
   has benchmark hotkeys, setup offers to use the same ones, so one key press does both.
2. **Video, optional.** Do you record while benchmarking? If yes: the folder your recorder saves to
   (NVIDIA's is detected automatically) and, optionally, your record hotkey.

Settings are saved to `config.json` next to the scripts. Run `setup` again any time to change them.

## Use

1. **Start the logger first:** `python rtss_log.py`
2. **Then launch the game.** RTSS only connects to the logger when a game starts, so a game that was
   already running sends nothing. Restart it.
3. In game: **start key** (1 beep) > play the pass > **stop key** (2 beeps).
   Press start again mid-run to restart; the attempt so far is thrown away.

| Sound | Meaning |
|---|---|
| 1 beep | run started / restarted |
| 2 beeps | run saved |
| 3 quick beeps | **not saved**: too short (under 5 s), the game closed, or over 10% of the run had no frames |

A freeze or hitch is *not* a gap: it is one very long frame, it is kept, and it counts against the
1% low, as it should.

### Naming runs

* **With video:** each run is linked to the recording that was running at the same time. Name the
  recording `Game - preset, settings` (e.g. `Cyberpunk - High, DLSS Quality, RT Off.mp4`) and run
  `python rtss_log.py relink`. The preset is everything after the first ` - `. If your recorder keeps
  one folder per game (NVIDIA does), the game name comes from the folder.
  Recording while benchmarking order: **record > start key > pass > stop key > stop recording**.
* **Without video:** `python rtss_log.py label` lists your runs and lets you type the game and preset.

### Commands

| Command | Does |
|---|---|
| `python rtss_log.py setup` | pick hotkeys and video settings |
| `python rtss_log.py check` | what is installed, running and missing |
| `python rtss_log.py` | log runs |
| `python rtss_log.py label` | name runs by hand |
| `python rtss_log.py relink` | re-match runs to recordings after renaming them |
| `python rtss_log.py probe 30` | listen 30 s and show which games RTSS sends frames for |
| `python osd_ocr.py` | read the overlay numbers out of recordings (backup, see below) |

## Results

Everything lands in `results.json`, one entry per run:

* game, preset, recording and where in it the run sits, resolution, graphics API, RAM setup
  (e.g. `32GB DDR5-6000, dual channel`)
* **avg FPS, 1% low**, avg frametime
* GPU / CPU usage (avg), GPU / CPU temperature (avg and peak), RAM (avg and peak)
* `gpu_bound_pct` / `cpu_bound_pct`: share of the run's seconds that were GPU bound
  (GPU at 95%+) or CPU bound (GPU under 90% while one CPU thread is at 90%+)

Frames are only held in memory during the run and thrown away once the numbers are worked out. One
run per recording: a newer run in the same recording replaces the older one. Your `results.json`
and `config.json` are in `.gitignore`, so they never get uploaded.

### How the numbers are calculated

The same way RTSS does with its default settings, so they match your overlay:

* **Average FPS** = frames / total time
* **1% low**, RTSS *integral* mode: sort frames slowest first and take the frame where the slowest
  frames add up to 1% of the run's **time**. (`check` warns you if your RTSS is set to *linear*.)

Verified against a live RTSS overlay: logger 156.3 avg / 111.9 1% low, overlay 156 / 112.

## Overlay OCR (backup)

Forgot to start the logger? If your recording shows the RTSS / Afterburner overlay, `osd_ocr.py`
reads the numbers back out with Windows' built-in OCR (offline). It is a backup: overlay numbers are
rounded and sampled every few seconds, while the logger sees every frame. Results go to
`ocr_results.json`, marked `"source": "overlay OCR"`.

### Calibrating the OCR

Every overlay looks different, so the OCR needs to know where each number sits on yours. The
default in `config.json` (`"ocr"`) is the author's layout and will not match yours.

1. `python osd_ocr.py --calibrate "path\to\recording.mp4" --at 60` prints every word it sees in the
   top half of the frame at 60 s, with positions.
2. In `config.json` under `"ocr"`, set:
   * `box`: `[x, y, width, height]` around your overlay
   * `rows`: the `y` of each overlay line, e.g. `"gpu": 10`
   * `columns`: `[from, to]` ranges for the left edge `x` of each value column
   * `fields`: which `[row, column, lowest, highest]` holds each value: `gpu_usage`, `gpu_temp`,
     `vram_mb`, `cpu_usage`, `cpu_temp`, `ram_mb`, `fps`, `fps_avg`, `fps_1low`. Leave out what your
     overlay does not show.
3. Run `python osd_ocr.py "recording.mp4" --force` and check the CSV in `ocr/`.

An AI coding agent can do this for you; see `AGENTS.md`.

## Troubleshooting

* **3 quick beeps and "no frames from the game":** the logger was started after the game. Close
  the game, keep the logger open, start the game again.
* **No beeps at all when pressing your keys:** the game or Afterburner runs as administrator, so
  Windows hides key presses from normal programs. Start the logger from an administrator terminal.
* **Sensors say "no sensors":** MSI Afterburner is not running, or the graphs are off (see above).
* **"Could not create the pipe, is another logger open?":** only one logger can run at a time.

## Privacy and safety

Read only. The logger reads what RTSS and Afterburner already publish; it injects nothing into
games, changes no settings and sends nothing anywhere. RTSS and MSI Afterburner are separate
programs by their own authors and are not included here.

## License

Copyright 2026 Relebogile Nkotswe (babygrandad). Licensed under the
[Apache License 2.0](LICENSE): free to use, modify and share, including commercially, as long as
you keep the [LICENSE](LICENSE) and [NOTICE](NOTICE) files and the credit to the original author,
and clearly mark files you changed.
