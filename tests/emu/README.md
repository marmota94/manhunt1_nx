# Emulator tests (Ryubing, `tools/emu_run.py`)

Diagnostic only: these runs catch crashes, new errors and broken images. They do not measure console speed.

| Tier | Command | Time | Use |
|---|---|---|---|
| Smoke | `python tools/emu_run.py --game assets/manhunt1_xbe --script tests/emu/first_mission.pad --dump 300 --baseline <summary.json>` | ~4 min | Every change |
| One level | `python tools/emu_run.py --game assets/manhunt1_xbe --level zoo --dump 600 --baseline <summary.json>` | ~4 min | Changes that touch a scene type; `zoo` is the heaviest level seen (median 540–940 draws per frame, 1,190 at the 90th percentile) |
| All levels (advanced) | `python tools/emu_run.py --game assets/manhunt1_xbe --script tests/emu/levels.pad --secs 1158 --grace 240 --env MANHUNT_DEVMENU=1 --reset-save --save-snapshots _work/emu-saves` | ~20 min | Rarely: before a release, or to make the level snapshots |

## How the level runs work

- **`levels.pad`:**
  - reaches the first level, then 12 times opens Rockstar's dev menu (L3+R3) and selects "Complete level";
  - presses A through the results, the save screens and the next intro;
  - logs a `[MH-LEVEL] <folder>` line for each level it loads;
  - the summary gives FPS, draws and vertices per level (`per_level`).
- **`--save-snapshots DIR`:**
  - when a level starts loading, copies the emulated card's save to `DIR/<level>`;
  - keeps only the newest game save, because with several saves the title loads one of its own choosing;
  - leaves out the Xbox disk partition images, about 5 GB, which the port rebuilds at boot.
- **`--level NAME`** loads `_work/emu-saves/NAME` and runs `first_mission.pad`. With a save on the card, the title menu continues it straight away, so the level starts about 25 s in, and the rest of the script walks and turns the camera there.
- Snapshots made from a new game (2026-11-21, one 12-level run): jury_turf, derelict, der2, scrap, scrap2, zoo, zoo2, mall, church2, pharm_wks, asylum, cellblock. A second `levels.pad` run started from the `cellblock` snapshot would add the later levels.

Snapshots and runs are game-derived: they stay in `_work/` (gitignored).

## Per-level load in the 12-level run (emulator, 2026-11-21)

| Level | Median draws | 90th percentile draws | Median vertices |
|---|---:|---:|---:|
| zoo | 940 | 1,204 | 50,672 |
| pharm_wks | 590 | 705 | 43,871 |
| scrap2 | 520 | 562 | 33,737 |
| cellblock | 407 | 410 | 21,966 |
| jury_turf | 364 | 512 | 38,791 |
| derelict | 353 | 375 | 29,405 |
| church2 | 334 | 384 | 31,783 |
| mall | 312 | 713 | 26,210 |
| asylum | 253 | 786 | 29,980 |
| der2 | 188 | 352 | 19,863 |
| zoo2 | 165 | 700 | 30,422 |
| scrap | 83 | 199 | 10,351 |

Draws per frame depend on where the script walks and are a rough guide. On the console, GL time is about 0.03 ms per draw (`reports/PERF_INVESTIGATION_2026-11-16.md`).
