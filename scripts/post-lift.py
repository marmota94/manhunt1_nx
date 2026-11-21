"""Post-lift edits of the generated chunks (game/build/gen/recomp_NNNN.c), applied in place and reversible.

membase: hold the guest memory base in a local per function.
  XBOX_PTR (recomp_types.h) adds the global g_xbox_mem_offset to every guest address. All guest accesses are
  volatile and the code is built with -fno-strict-aliasing, so the compiler reloads the global from memory before
  nearly every access: two dependent loads per guest access instead of one. The base is written only at boot,
  before the guest entry point (xbox_memory_layout.c, main_switch.c), so each function can read it once:
      const uintptr_t _plmb = (uintptr_t)g_xbox_mem_offset;
  and each chunk redefines XBOX_PTR to use _plmb. Inline helpers in the headers keep the global (they are expanded
  where the header defines them). reports/PERF_INVESTIGATION_2026-11-16.md, item 5, option A.

regs: address the guest's x86 registers through one pointer per function.
  The runtime keeps each register as its own thread-local; with -mtp=soft every function calls __aarch64_read_tp
  and holds one callee-saved register per TLS address. game/src/guest_regs.S puts them in one block,
  g_guest_regs, with the old names as aliases inside it; each function takes
      RecompGuestRegs *const _plr = &g_guest_regs;
  and each chunk maps eax, esp, ..., g_ebp, g_seh_ebp, g_fp_top, g_fp_stack, g_df to members of *_plr.
  Header helpers keep the alias names, which are the same storage. Needs the Switch build
  (RECOMP_GUEST_REGS_EXTERNAL). Item 2 there. The names _plr and _plmb are used because the lifter already
  uses _r for its own temporaries.

Usage: python -I scripts/post-lift.py <gen dir>          apply both; MANHUNT_MEMBASE_LOCAL=0 or MANHUNT_GUEST_REGS=0
                                                         in the environment leaves that edit out (undoes it)
       python -I scripts/post-lift.py <gen dir> --undo   restore the lifter's output
Only files whose text changes are rewritten, so incremental builds recompile only what changed.
"""
import os
import re
import sys
from pathlib import Path

NL = "\n"
FUNC = re.compile(r"^(void (?:sub_[0-9A-F]+|xbe_entry_point)\(void\)\n\{\n)", re.M)
INCLUDE = '#include "recomp_funcs.h"' + NL

MARK = "/* post-lift.py: membase */"
HEADER = MARK + NL + "#undef XBOX_PTR" + NL + "#define XBOX_PTR(addr) ((uintptr_t)(uint32_t)(addr) + _plmb)" + NL
LOCAL = "    const uintptr_t _plmb = (uintptr_t)g_xbox_mem_offset; " + MARK + NL

RMARK = "/* post-lift.py: regs */"
RMAP = [("eax", "r_eax"), ("ecx", "r_ecx"), ("edx", "r_edx"), ("esp", "r_esp"), ("ebx", "r_ebx"), ("esi", "r_esi"),
        ("edi", "r_edi"), ("g_ebp", "r_ebp"), ("g_seh_ebp", "r_seh_ebp"), ("g_fp_top", "r_fp_top"),
        ("g_fp_stack", "r_fp_stack"), ("g_df", "r_df")]
RHEADER = RMARK + NL + '#include "guest_regs.h"' + NL + "".join(
    f"#undef {a}{NL}#define {a} (_plr->{b}){NL}" for a, b in RMAP)
RLOCAL = "    RecompGuestRegs *const _plr = &g_guest_regs; " + RMARK + NL

# (environment switch, header, per-function line, name); applied in this order, each undone on its own.
EDITS = [("MANHUNT_MEMBASE_LOCAL", HEADER, LOCAL, "membase"), ("MANHUNT_GUEST_REGS", RHEADER, RLOCAL, "regs")]


def apply(text, header, local):
    if header in text:
        return text
    if INCLUDE not in text:
        raise ValueError("no recomp_funcs.h include")
    text = text.replace(INCLUDE, INCLUDE + header, 1)
    return FUNC.sub(lambda m: m.group(1) + local, text)


def undo(text, header, local):
    return text.replace(header, "").replace(local, "")


def main():
    gen = Path(sys.argv[1])
    undo_all = "--undo" in sys.argv[2:]
    wanted = {name: not undo_all and os.environ.get(env) != "0" for env, _, _, name in EDITS}
    files = sorted(gen.glob("recomp_[0-9][0-9][0-9][0-9].c"))
    if not files:
        sys.exit(f"post-lift: no chunks in {gen}")
    changed, counts = 0, {name: 0 for _, _, _, name in EDITS}
    for f in files:
        old = f.read_text(encoding="utf-8")
        new = old
        for env, header, local, name in EDITS:          # strip every edit, then put back the wanted ones in order,
            new = undo(new, header, local)               # so the result does not depend on what was there before
        for env, header, local, name in EDITS:
            if wanted[name]:
                new = apply(new, header, local)
        for env, header, local, name in EDITS:
            counts[name] += new.count(local)
        if new != old:
            f.write_text(new, encoding="utf-8", newline=NL)
            changed += 1
    state = ", ".join(f"{n} {'on' if wanted[n] else 'off'} ({counts[n]} functions)" for n in counts)
    print(f"post-lift: {state}; {changed} of {len(files)} chunks rewritten")


if __name__ == "__main__":
    main()
