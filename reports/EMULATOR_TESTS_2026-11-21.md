# Emulator test flow — 2026-11-21

Branch: `pr/emulator-tests`.

**Status: emulator results only.** Ryubing 1.3.3 (a Ryujinx fork) on Windows. The emulator catches crashes, new errors and broken frames. It says nothing about console speed. Nothing here changes what runs on the console by default.

## Clean build

`scripts/game-build.sh switch` was run on 2026-11-20 from a fresh clone of `main` (6f6c837) on Windows with Git Bash and Docker Desktop 29.9.2. The Docker VM had 7.3 GB and 12 CPUs. The game files matched `reports/ASSET_MANIFEST.sha256` (836 of 836 files).

- Analysis, lift (14,800 functions) and NRO took about 10 minutes with `CPUS=4`.
- The clone had been made with `core.autocrlf=true`, which put CRLF in the scripts and patches and broke them. A checkout made after `git config core.autocrlf false` and `git config core.eol lf`, as the README says, built cleanly.
- `CPUS` defaults to every core. With 12 jobs and a 7.3 GB VM, memory was tight; it was not seen to run out, because the runs used 4 jobs.

## Runner: `tools/emu_run.py`

```sh
python tools/emu_run.py --game assets/manhunt1_xbe --script tests/emu/first_mission.pad --dump 300 \
    [--baseline <earlier run>/summary.json] [--build]
```

- Runs a portable Ryubing copy, by default `_work/emu/ryubing-1.3.3/Ryujinx.exe` (`_work/` is gitignored). It runs headless, handheld, with the OpenGL backend and no vsync. A homebrew NRO needs no firmware or keys.
- Links the game folder into the emulated SD card as a directory junction, with no copy.
- Writes `manhunt.env` from `tests/emu/emulator.env` plus `MANHUNT_PAD_SCRIPT`, `MANHUNT_AUTO_EXIT_SECS` and `MANHUNT_LOG_FILE`, and puts back any earlier `manhunt.env` afterwards.
- Stops on `[MH-EXIT]`, `[MH-CRASH]`, the emulator exiting, or a time limit. Stops only the emulator process it started.
- Writes `summary.json` with:
  - the result;
  - median `[perf]` FPS, overall and over samples above 20,000 vertices;
  - counts of error kinds: crash, watchdog dump, GL error, magenta texture, unresolved call, refused contiguous memory, new underruns, silent audio;
  - the levels loaded, and FPS, draws and vertices per level;
  - with `--dump N`, each dumped frame's mean luminance.
- `--baseline` reports a changed result, new error kinds, new emulator error lines, levels the baseline reached and the run did not, and an FPS drop above 15%.
- `--build` runs `scripts/game-build.sh switch` first through Git Bash. It uses `CPUS=4`, set with `--jobs`.

## Tests by tier (`tests/emu/README.md`)

| Tier | Time | What |
|---|---|---|
| Smoke | ~4 min | `first_mission.pad`: intros and menus skipped with A, then walking and camera turns in the first level |
| One level | ~4 min | `--level <name>`: start from a save snapshot of that level |
| All levels | ~20 min | `levels.pad`: 12 levels in a row through Rockstar's dev menu ("Complete level"), with `MANHUNT_DEVMENU=1` |

- **`[MH-LEVEL] <folder>`** (`movie_trace.c`) is printed on the first file open under a new `levels\<folder>\`, so a log shows which levels were loaded.
- **`--save-snapshots DIR`** copies the emulated card's save folder to `DIR/<level>` when each level starts loading. The copy leaves out the Xbox disk partition images (about 5 GB, rebuilt at boot) and keeps only the newest game save: with several saves, the title continued an older one.
- **`--level`:** with a save on the card, the title menu continues it at once, so the level starts about 25 s in.
- **`MANHUNT_PAD_SCRIPT`** now takes up to 512 steps (it was 128), because `levels.pad` has 296.

**zoo is the heaviest level seen:**
- 940 median and 1,204 90th-percentile draws per frame in the 12-level run;
- 540 median and 1,190 90th percentile in a one-level run;
- pharm_wks is next, with 590 median.
- Per-level figures are in `tests/emu/README.md`.

## Ryubing returns a quarter of each row on readback

- **Symptom:** every frame dumped with `RECOMP_GL_DUMP` held pixels only in columns 0–159 of 640, in every frame. That is the width counted in bytes instead of RGBA8 pixels.
- **Isolation:**
  - the emulator window showed the full frame;
  - `glReadPixels` as `GL_BGRA`, as `GL_RGBA` and into a pixel-pack buffer all cut at the same column.
  - So it is the emulator's tiled-to-linear copy, not the port. Where exactly in the emulator was not looked for.
- **Workaround:** `RECOMP_GL_DUMP_READ=wide` (`nv2a_gl.c`, `dump_read`) blits the frame into the left quarter of a target four times as wide, reads the whole target and compacts the rows.
  - A correct reader returns the same frame.
  - 48 of 48 frames of a 240 s run came out full width.
  - It is off by default; `tests/emu/emulator.env` turns it on.

## Console A/B tool: `tools/console_ab.py`

- **`{nro}` in `MANHUNT_LOG_FILE`** is replaced by the running NRO's file name without `.nro` (from `argv[0]`). Two builds on one card then keep separate logs with one `manhunt.env`. The boot line prints `nro=<name>`.
  - When `argv[0]` is empty, as in Ryubing, the name is `manhunt`.
  - What the console's loader passes is UNVERIFIED.
- **`kit --a <nro> --b <nro>`** writes an SD folder with:
  - `manhunt-a.nro` and `manhunt-b.nro`;
  - a test `manhunt.env`: the release settings, `MANHUNT_PERF_LOG=2`, the scripted pad and an auto exit after 240 s;
  - `manhunt.env.play` with the release settings alone;
  - instructions.
- **`compare <log a> <log b>`** prints, over samples above 20,000 vertices: median FPS, guest, GL and APU thread CPU, GL time per draw, and the share of slow frames.
- **UNVERIFIED on the console:** whether the scripted pad reaches the level with the console's boot timing.

## Emulator noise

The same build on the same level varied:
- from 335 to 573 median draws per frame (the scripted path drifts);
- from 419 to 448 ms/s of APU voice work.

FPS and per-thread CPU from the emulator are therefore not comparable between builds.

## Not explained

- **Audio:** in Ryubing, most `[perf]` periods print `played 0/s`, but not in every run. Not looked into.
- **Levels with no snapshot yet:** after cellblock. A second `levels.pad` run started from the cellblock snapshot would add them.
