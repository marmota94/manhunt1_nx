# Guest registers as C locals in call-free lifted functions — 2026-11-27

Branch: `pr/reg-locals`, on top of `pr/cpu-guest-regs` (#20).

**Status: off by default; console speed UNVERIFIED.** The step is measured as code size and instruction counts, checked function by function against the original code on the host, and run in the Ryubing emulator. A console A/B is what settles whether it is faster.

## The cost

After #20, each lifted function reaches the x86 registers through one pointer into the TLS block (`_plr->r_eax`). Guest memory accesses are volatile, and the code is built with `-fno-strict-aliasing`. So the compiler must assume any guest store may hit the block. It reloads a register from the block after every guest store, and writes it back after every change.

## The change

**`scripts/post-lift.py`**, step `reglocals`, after `regs` and only with it:

- **Which functions:** only lifted functions that call nothing:
  - no lifted function;
  - no `RECOMP_ABI_CALL`, `RECOMP_ICALL*` or `RECOMP_ITAIL*`;
  - no host function outside a short vetted list.

  The macros and inline helpers of `recomp_types.h` and `recomp_funcs.h` are read, and a helper counts as pure only if everything it calls is pure, checked to a fixpoint (143 pure helpers). The call macros and `RECOMP_TRACE_*` are not pure.

  Vetted host functions:
  - the math functions;
  - `recomp_unimpl` (log only);
  - `xbox_GuestCpuPreempt`, which yields to another guest thread. Each thread has its own TLS block, so the locals stay right.
- **Registers:** eax, ecx, edx, ebx, esi, edi and esp. Before the function, each is `#define`d to a local `_pll_*`, loaded from the block at entry. After the function, the #20 mapping is restored.
- **Write-back:** `_PLL_WB()` stores back only the registers the function writes. It runs before every `return;` (rewritten as `{ _PLL_WB(); return; }`) and at the end of the body. Registers only read are declared `const`, so a write the analysis missed fails the build. All 60 chunks compile.
- **Marking and undo:** every line it adds carries `/* post-lift.py: reglocals */`. `--undo` and switching it off restore the previous text byte for byte, and applying it twice changes nothing.

**Turning it on:** `MANHUNT_REG_LOCALS=1` when running `scripts/game-build.sh`, which passes the variable into the container. It is off by default; with it off, the Switch NRO is byte-identical to the one built without this branch.

## Measurements

Compiled with the Switch flags (devkitA64, `-O2`):

| | Before | After |
|---|---:|---:|
| Functions changed | — | 3,049 of 14,816 (21%) |
| Instructions in those functions | 337,246 | 293,028 (−13.1%) |
| Bytes in those functions | 1,348,984 | 1,172,112 (−13.1%) |
| All lifted code | 15,272,896 B | 15,097,708 B (−1.15%) |
| NRO | 23,151,805 B | 22,987,965 B (−0.7%) |

Per function, the median change is −5.9%: 2,003 shrank, 538 are unchanged and 508 grew.

**Memory accesses** in the changed functions (static count of AArch64 `ld*`/`st*` instructions):

| | Before | After |
|---|---:|---:|
| Loads | 68,171 | 53,561 (−21.4%) |
| Stores | 59,964 | 44,822 (−25.3%) |
| All loads and stores | 128,135 | 98,383 (−23.2%) |

**Where these functions run.** The PGO build from #21 was trained in the emulator on the first mission, zoo and pharm_wks.
- GCC placed 544 lifted functions in `.text.hot` (the most-executed ones).
- 179 of those 544 (33%) are among the changed functions, although the changed functions are only 21% of all of them.
- In those 179 hot functions:

  | | Before | After |
  |---|---:|---:|
  | Instructions | 38,289 | 34,453 (−10.0%) |
  | Loads | 7,822 | 6,555 (−16.2%) |
  | Stores | 6,903 | 5,816 (−15.8%) |

  Per function, the median is −9.1%. The largest drops are `sub_0001AC20` (154 → 73 instructions) and `sub_0001A8A0` (225 → 127). Some grow: the largest growth is `sub_00293F40`, 693 → 766.
- These are static counts of the instructions in the code, not of how often they run. They show that the change reaches code that runs often, not how much time it saves.

## Correctness

**Differential test, `tests/post-lift/reglocals_diff.py`:**
- **Build:** for each chunk, it compiles every changed function twice in one file (the original and the changed one) with host gcc 12 in the devkitA64 image, and links them into a small harness.
- **Each run:** in a forked child with 4 GB of `MAP_NORESERVE` guest memory, it fills the register block and a 1 MB window of guest memory from a seed, then calls the function under a 2 s alarm.
- **What is compared:** the register block, the other TLS globals, and an FNV hash of every resident guest page.
- **Result** (16 seeds per function):

  | | Runs |
  |---|---:|
  | Equal | 48,081 |
  | Different | 0 |
  | Hit the 2 s limit, inconclusive (loops with a random count) | 697 |
  | of which only the original timed out | 62 |
  | of which only the changed function timed out | 1 |

  The one case is `sub_001C64D0` with seed 13: with a 60 s limit (`RL_ALARM=60 RL_ONLY=sub_001C64D0`) all 16 seeds are equal.
- **Coverage:** random inputs reach only some paths of each function. This is evidence, not proof.

**Emulator** (Ryubing, `tools/emu_run.py` from #19): the current NRO and the `reglocals` NRO, run back to back on the same machine.
- `tests/emu/levels.pad`, 1,158 s:
  - both exit on time, with no crash, watchdog stall, GL error or emulator error line;
  - 295 inputs each;
  - the 116 dumped frames differ in mean luminance by a median of 0.24/255, and none is black in one run only;
  - in this pair of runs the route stayed in jury_turf in both builds.
- The 12 saved levels (`--level`, 120 s each: asylum, cellblock, church2, der2, derelict, jury_turf, mall, pharm_wks, scrap, scrap2, zoo, zoo2):
  - all 24 runs exit on time, with no crash, watchdog stall, GL error, magenta texture, unresolved icall or emulator error line;
  - the frames that differ a lot are the game's own static effect, which shows in both builds at different moments, or the same scene a few seconds apart;
  - `audio_silent` across a whole run happens at random in both builds (5 runs of the current build, 4 of the new one): the emulator's audio, not this change.
- Emulator FPS is not a speed measurement.

## Left out

- **Functions with any call (79%):** the registers would have to be stored before each call and reloaded after it, and the call macros read and write `g_esp`/`eax` themselves.
- **Loops inside functions that call:** needs region analysis across the lifter's `goto`s.
- **x87 stack, `df`, the frame bridges:** the FPU helpers are per-function macros over the block; they are left as they are.
- **Diagnostics:** while a changed function runs, the crash handler and the watchdog see the registers it has not written back yet.

## Tests

- `tests/post-lift/test_post_lift.py` checks the step on a synthetic chunk:
  - only the call-free function changes;
  - written registers are loaded and written back, and read-only ones are `const`;
  - every return writes back;
  - the mapping is restored;
  - a function calling an unknown host function, and one with a call macro, are left alone;
  - idempotence; switched off restores the file; it needs `regs`; byte-exact `--undo`.
- `python -I tests/post-lift/reglocals_diff.py <gen> <gen with reglocals> <out> [--trials 16] [--chunks N,...] [--jobs 4]` runs the differential test. It needs Docker and a lifted `game/build/gen`.

## Console A/B wanted

- Build `a` with the defaults and `b` with `MANHUNT_REG_LOCALS=1`, from the same commit.
- `python tools/console_ab.py kit --a a.nro --b b.nro` writes an SD folder that plays each build for 240 s with no one playing. `python tools/console_ab.py compare log-manhunt-a.txt log-manhunt-b.txt` compares the logs.
- Same clocks for both: same mode (handheld or docked), same charger.
- **Expected:**
  - no visible difference;
  - guest-thread CPU the same or slightly lower, since only 21% of functions change.
- **What the result decides:**
  - if `b` is faster in repeated runs, extending the step to functions with calls is worth its risk;
  - if not, the step stays off.
