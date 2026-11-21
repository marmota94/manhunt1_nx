"""Focused test for scripts/post-lift.py (host, no game data).

    python -I tests/post-lift/test_post_lift.py

Checks on a synthetic chunk in the lifter's shape:
  - applying both edits adds one local per function and the two headers;
  - applying twice changes nothing (idempotent);
  - each edit can be left out on its own (MANHUNT_MEMBASE_LOCAL=0, MANHUNT_GUEST_REGS=0);
  - --undo restores the input byte for byte;
  - negative control: a chunk without the recomp_funcs.h include is refused.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/post-lift.py"
CHUNK = """/**
 * Manhunt - Recompiled code chunk 0
 */

#define RECOMP_GENERATED_CODE
#include "recomp_funcs.h"
#include <math.h>

void sub_00010000(void)
{
    uint32_t ebp = 0;
    PUSH32(esp, esi);
    eax = MEM32(esp + 0x8u);
}

void sub_00010010(void)
{
    int64_t _r = 0;
    ecx = (uint32_t)_r;
}

void xbe_entry_point(void)
{
    sub_00010000();
}
"""
fails = 0


def check(cond, what):
    global fails
    if not cond:
        print("FAIL", what)
        fails += 1


def run(gen, *args, **env):
    e = dict(os.environ)
    e.pop("MANHUNT_MEMBASE_LOCAL", None)
    e.pop("MANHUNT_GUEST_REGS", None)
    e.update(env)
    return subprocess.run([sys.executable, "-I", str(SCRIPT), str(gen), *args], env=e, capture_output=True, text=True)


with tempfile.TemporaryDirectory() as d:
    gen = Path(d)
    chunk = gen / "recomp_0000.c"
    chunk.write_text(CHUNK, encoding="utf-8", newline="\n")

    check(run(gen).returncode == 0, "apply runs")
    text = chunk.read_text(encoding="utf-8")
    check(text.count("const uintptr_t _plmb = (uintptr_t)g_xbox_mem_offset;") == 3, "one memory-base local per function")
    check(text.count("RecompGuestRegs *const _plr = &g_guest_regs;") == 3, "one register pointer per function")
    check("#define XBOX_PTR(addr) ((uintptr_t)(uint32_t)(addr) + _plmb)" in text, "XBOX_PTR redefined")
    check("#define esp (_plr->r_esp)" in text and '#include "guest_regs.h"' in text, "registers mapped")
    check("int64_t _r = 0;" in text, "the lifter's own _r is left alone")

    run(gen)
    check(chunk.read_text(encoding="utf-8") == text, "applying twice changes nothing")

    run(gen, MANHUNT_GUEST_REGS="0")
    only_mb = chunk.read_text(encoding="utf-8")
    check("_plr" not in only_mb and only_mb.count("_plmb = ") == 3, "MANHUNT_GUEST_REGS=0 leaves the memory base only")

    run(gen, MANHUNT_MEMBASE_LOCAL="0")
    only_regs = chunk.read_text(encoding="utf-8")
    check("_plmb" not in only_regs and only_regs.count("_plr = ") == 3, "MANHUNT_MEMBASE_LOCAL=0 leaves the registers only")

    run(gen, "--undo")
    check(chunk.read_bytes() == CHUNK.encode(), "--undo restores the lifter's output byte for byte")

    bad = gen / "recomp_0001.c"
    bad.write_text(CHUNK.replace('#include "recomp_funcs.h"\n', ""), encoding="utf-8", newline="\n")
    check(run(gen).returncode != 0, "negative control: a chunk without the include is refused")

print("FAIL" if fails else "PASS", f"post-lift ({fails} failures)")
sys.exit(1 if fails else 0)
