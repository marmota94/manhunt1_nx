# Guest memory base and registers in the lifted code — 2026-11-21

Branch: `pr/cpu-guest-regs`.

**Status: console speed UNVERIFIED.** The changes are measured as code size and instruction counts, and checked for faults in the Ryubing emulator. A console A/B is what settles whether they are faster. This implements items 2 and 5 (option A) of `PERF_INVESTIGATION_2026-11-16.md`.

## The two costs

Disassembly of `recomp_0010.o` at v1.0.0, sample function `sub_00069D70`:

- **Memory base.** `XBOX_PTR` adds the global `g_xbox_mem_offset` to every guest address. Guest accesses are volatile and the code is built with `-fno-strict-aliasing`, so the base is reloaded from memory before nearly every access (`ldr x2, [x20]`, then the access). That is two dependent loads per guest access.
- **Registers.**
  - `eax`…`edi`, `ebp`, `seh_ebp`, the x87 stack and its top, and DF are separate `__thread` globals.
  - With `-mtp=soft`, a function calls `__aarch64_read_tp`, 5.8 times per function on average in that chunk (1,466 calls over 251 functions).
  - It also holds one callee-saved register per TLS address: the sample function saves 8.

## The changes

**`scripts/post-lift.py`** edits the generated chunks after the lift, in place and reversibly. `scripts/game-build.sh` and `game-build-native.sh` run it after the lift.

- **membase:** each lifted function reads the base once, `const uintptr_t _plmb = (uintptr_t)g_xbox_mem_offset;`, and each chunk redefines `XBOX_PTR` to use it.
  - The base is written only at boot, before the guest entry point (`xbox_memory_layout.c`, `main_switch.c`, `main.c`).
  - Inline helpers in the headers keep the global, because they are expanded where the header defines them.
- **regs:** each function takes `RecompGuestRegs *const _plr = &g_guest_regs;`, and each chunk maps `eax`, `esp`, …, `g_ebp`, `g_seh_ebp`, `g_fp_top`, `g_fp_stack` and `g_df` to members of `*_plr`.
  - The names `_plr` and `_plmb` are used because the lifter already uses `_r` for its own temporaries.

**The register block:**
- **`game/src/guest_regs.S`:** one zero-initialised `.tbss` block, `g_guest_regs`, laid out by `game/src/guest_regs.h` (with static asserts on every offset). The old names (`g_eax`, …) are defined as aliases at fixed offsets inside it, so every existing `extern` declaration in the runtime keeps working, and the header helpers address the same storage.
- **Patch 0042:** under `RECOMP_GUEST_REGS_EXTERNAL`, the runtime declares those registers `extern` instead of defining them.
- **`game/CMakeLists.txt`:**
  - Switch: the target gets `RECOMP_GUEST_REGS_EXTERNAL` and `guest_regs.S`.
  - Host: `xbox_kernel` gets the define and the executable gets `guest_regs.S`, because the host and Switch builds share the lifted chunks.

**Turning them off:**
- `MANHUNT_MEMBASE_LOCAL=0` and/or `MANHUNT_GUEST_REGS=0` when running `scripts/game-build.sh` leave that edit out (and undo it).
- `python -I scripts/post-lift.py game/build/gen --undo` restores the lifter's output byte for byte.
- Patch 0042 changes the lift cache key, so the first build after it lifts again (about 10 minutes). The lift output is the same.

## Measurements

| Build | `recomp_0010.o` instructions | Whole `.text` (Switch) |
|---|---:|---:|
| v1.0.0 | 83,314 | 25.85 MB |
| + membase | 78,897 (−5.3%) | 24.84 MB (−3.9%) |
| + regs | 63,401 (−24%) | 22.56 MB (−12.7%) |

- **The sample function after both edits:**
  - the prologue saves 2 registers instead of 8;
  - every register access is one load or store at an offset from one base register;
  - every guest access uses the base from a register.
- **Emulator (Ryubing), no new error kind and the same scenes in the dumped frames:**
  - a 240 s first-mission run;
  - two 19.5-minute runs of `tests/emu/levels.pad`, v1.0.0 code against both edits, which between them loaded der2, scrap, scrap2, zoo, zoo2, mall, pharm_wks, asylum, prison, ramirez and journo_streets;
  - one-level runs of zoo, the heaviest level seen.
  - Guest-thread CPU in the emulator's `[perf]` report went from 76% to 73%. The emulator varies more than that between runs of one build (`reports/EMULATOR_TESTS_2026-11-21.md`), so this is not a measurement.
- **Where a gain can show:**
  - Only the guest thread runs lifted code.
  - In the heaviest scenes, the GL thread's per-draw driver cost is the limit (about 0.03 ms per draw, `PERF_INVESTIGATION_2026-11-16.md`), and these edits do not touch it.
  - So a gain, if any, is expected where the guest thread is the limit rather than in the heaviest draw-bound scenes.

## Host build

- The host build (`scripts/game-build.sh release`) builds and links with both edits.
- Run for 60 s under Xvfb, it stops with `[CRASH] signal 11 at host address 0x29d8e4` (guest `eax=0029D8CC`, `esp=00F7F244`) right after `[D3D] BlockOnTime: fence 11 not reached in 250 ms`.
- **That crash is not from these changes:**
  - v1.0.0 built from `main` with the same lifted code, without patch 0042, `guest_regs.S` or the post-lift edits, stops at the same address with the same registers;
  - so does this branch with the post-lift edits undone.
- Not looked into further.

## Tests

- `tests/post-lift/test_post_lift.py` checks, on a synthetic chunk:
  - one local per function;
  - idempotence;
  - each edit on its own;
  - byte-exact undo;
  - a negative control (a chunk without the include is refused).

## Console A/B wanted

- Build `a` with `MANHUNT_MEMBASE_LOCAL=0 MANHUNT_GUEST_REGS=0` and `b` with the defaults, from the same commit.
- Compare in heavy gameplay at fixed clocks: median FPS and the `[perf]` guest-thread CPU, within the same draws-per-frame bucket.
- `tools/console_ab.py` (from `pr/emulator-tests`) writes an SD folder for that and compares the two logs.
