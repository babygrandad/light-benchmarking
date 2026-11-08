# AGENTS.md

Instructions for AI coding agents (Claude, Copilot, Cursor, Codex, ...) helping someone with this
repository. Read this and `README.md` before doing anything.

## What this is

Light Benchmarking: a read-only game benchmark logger for Windows.

* `rtss_log.py`: the logger. RTSS sends every frame's frametime to the named pipe
  `\\.\PIPE\RTSS_Frametime` (this script is the pipe server); sensors come from MSI Afterburner's
  shared memory `MAHMSharedMemory`; game info from `RTSSSharedMemoryV2`. Commands: `setup`, `check`,
  (none) = log, `label`, `relink`, `probe`.
* `bench_setup.py`: settings (`config.json`), the interactive setup, the requirements check.
* `osd_ocr.py`: backup that reads the overlay out of recordings with the Windows OCR engine.

## Licensing: check this for the user, every time

This project is licensed under **Apache 2.0** by **Relebogile Nkotswe (babygrandad)**. Users often do
not read licenses, so it is your job to make sure they stay out of trouble:

* Before helping someone **share, publish, fork, repackage or sell** this tool or a modified version,
  tell them in plain words what Apache 2.0 requires:
  * keep the `LICENSE` and `NOTICE` files with every copy,
  * keep the copyright and the credit to Relebogile Nkotswe (babygrandad),
  * clearly mark the files they changed (e.g. a note at the top of each changed file),
  * do not present the work as entirely their own or imply the original author endorses their version.
* **Never remove or reword** the copyright headers, `LICENSE` or `NOTICE`.
* When you add a dependency or copy code from elsewhere, **check its license** and tell the user
  if it conflicts with Apache 2.0 or adds its own obligations. Record new dependencies in
  `requirements.txt` (and in `NOTICE` if their license asks for attribution).
* RTSS and MSI Afterburner are separate freeware by their own authors. This tool only reads data
  they publish. Do not bundle, redistribute or patch them, and do not copy code out of their SDKs
  into this repo.

## Setting up a new user

1. Run `python rtss_log.py check` and walk the user through every `MISSING` and `WARN` line
   (install commands are printed with each one).
2. Run `python rtss_log.py setup` **in a terminal the user can type in**. It asks the user to
   **press** their own hotkeys. **Never hardcode or assume hotkeys, folders or recorders**; every PC
   is different, and those choices belong in `config.json`, which only setup should write.
3. Video recording is **optional**. Ask whether they record; do not assume NVIDIA.
4. Tell them the one rule that trips everyone up: **start the logger before launching the game.**

## Calibrating the OCR for a user's overlay

The `ocr` layout in `config.json` defaults to the author's overlay and will not match others.

1. Get a recording that shows the overlay. Run
   `python osd_ocr.py --calibrate "<recording>" --at 60` (try other `--at` values if the overlay is
   hidden by a menu at that moment).
2. From the printed words and positions, set `box`, `rows`, `columns` and `fields` in the user's
   `config.json` (format in README, "Calibrating the OCR"). Only map fields the overlay shows.
3. Run `python osd_ocr.py "<recording>" --force` and check `ocr/<recording>.ocr.csv`: values should be
   steady and plausible. Blanks are fine (unreadable is left blank, never guessed); wrong numbers are not.
4. If you can view images, grabbing one frame of the overlay with ffmpeg helps. Ask the user first;
   it is their footage.

## Rules for changes

* **Read only.** Never inject into games, write to RTSS / Afterburner memory, change their settings,
  or send data anywhere.
* **Keep the math identical to RTSS** (`summarize()`): average = frames / time, 1% low = RTSS
  *integral* mode. If you change it, the numbers stop matching the overlay; verify against a live
  overlay before claiming they match.
* Frames and sensor samples stay in memory during a run; only the summary is written to
  `results.json`. Keep the clean-data rules (minimum length, game closed, 90% frame coverage).
* A hitch or freeze is one long frame, not a gap. Do not filter it out.
* `config.json`, `results.json`, `ocr_results.json` and `ocr/` are the user's data: never commit them.
* Booleans are named `is_x` / `has_x`.
* Windows only, Python 3.10+, standard library only for the logger. The OCR may use the packages in
  `requirements.txt`.
* Show the user a plan and get their OK before writing new scripts or big changes.

## Testing

There is no automated test suite. Without a game you can still check:

* `python -m py_compile rtss_log.py bench_setup.py osd_ocr.py`
* `python rtss_log.py check`
* `summarize()` with made-up frametimes (e.g. 990 x 10 ms + 10 x 50 ms gives 96.2 avg, 20.0 1% low)

With a game: start the logger, then the game, do one 30 to 60 s run, and compare `results.json`
with the overlay (avg and 1% low should match to within about 1 FPS).
