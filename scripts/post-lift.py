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

reglocals (experimental, off unless MANHUNT_REG_LOCALS=1; needs regs): in functions that call nothing, the x86
  registers live in C locals between entry and each return. See apply_reglocals below.

Usage: python -I scripts/post-lift.py <gen dir>          apply both; MANHUNT_MEMBASE_LOCAL=0 or MANHUNT_GUEST_REGS=0
                                                         in the environment leaves that edit out (undoes it);
                                                         MANHUNT_REG_LOCALS=1 adds reglocals
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

# reglocals (opt-in, MANHUNT_REG_LOCALS=1; needs regs): in functions that call nothing, keep the x86 registers they use
# in C locals, loaded at entry and written back before every return and at the end of the body. With volatile guest
# accesses and -fno-strict-aliasing, any guest store may alias _plr->r_*, so the compiler reloads and re-stores the
# register block around nearly every guest access; locals cannot alias guest memory. Only functions with no call of
# any kind are changed (no RECOMP_ABI_CALL, indirect call or tail call, no sub_ name, no host function outside a
# vetted list), so nothing can read or write the register block while the locals hold the values. The x87 state
# (g_fp_*), g_df and the frame-pointer bridges stay in the block.
LMARK = "/* post-lift.py: reglocals */"
LREGS = ["eax", "ecx", "edx", "ebx", "esi", "edi", "esp"]
LRET = "{ _PLL_WB(); return; }"
CALL_MACROS = re.compile(r"^(RECOMP_ABI_CALL|RECOMP_ICALL\w*|RECOMP_ITAIL\w*)$")
PURE_HOST = {"memcpy", "memset", "memmove", "fabs", "fabsf", "sqrt", "sqrtf", "atan2", "atan2f", "log2", "expm1",
             "isfinite", "isnan", "isinf", "signbit", "floor", "ceil", "sin", "cos", "tan", "exp", "log", "pow", "fmod",
             "frexp", "ldexp", "copysign", "copysignf", "remquo", "scalbn", "trunc", "nearbyint", "fpclassify",
             "INT64_C", "UINT64_C",
             # host functions that never read or write the guest register block (read 2026-11-26):
             "recomp_unimpl",          # logs an untranslated instruction (recomp_manual.c)
             "xbox_GuestCpuPreempt"}   # hands the guest CPU to another thread; each thread has its own TLS block
KEYWORDS = {"if", "while", "for", "switch", "sizeof", "return", "defined", "else", "volatile"}
PURE_PREFIX = re.compile(r"^(_mm_|__builtin_|__sync_|__atomic_)")
COMMENT = re.compile(r"/\*.*?\*/", re.S)
CALLEE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")


def header_helpers(gen):
    """Names of the macros and static inline functions recomp_types.h / recomp_funcs.h define that call nothing
    outside themselves: the call macros are out, and so is any helper whose body calls a function that is not
    another such helper or a vetted host function (RECOMP_UNIMPL calls recomp_unimpl, for one)."""
    text = "".join((gen / h).read_text(encoding="utf-8") for h in ("recomp_types.h", "recomp_funcs.h")
                   if (gen / h).exists())
    text = COMMENT.sub("", re.sub(r"\\\n", " ", text))
    bodies = {}
    for m in re.finditer(r"(?m)^\s*#\s*define\s+(\w+)\(([^)]*)\)(.*)$", text):
        bodies[m.group(1)] = (m.group(3), {p.strip() for p in m.group(2).split(",")})
    for m in re.finditer(r"static inline [\w\s*]+?\b(\w+)\s*\(([^)]*)\)\s*\{", text):
        depth, i = 1, m.end()
        while i < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        bodies[m.group(1)] = (text[m.end():i], set(re.findall(r"\w+", m.group(2))))
    pure = {n for n in bodies if not CALL_MACROS.match(n)}
    while True:   # drop helpers that call anything outside the pure set, until nothing changes
        drop = {n for n in pure if any(c not in pure and c not in KEYWORDS and c not in PURE_HOST
                                       and c not in bodies[n][1] and not is_lane_helper(c)
                                       for c in CALLEE.findall(bodies[n][0]))}
        if not drop:
            return pure
        pure -= drop


def is_lane_helper(name):
    """MMX/XMM lane helpers generated from the header's templates (they compute; XMM_STORE writes guest memory), and
    compiler intrinsics and builtins."""
    return re.match(r"^(MMX|XMM)_[A-Z0-9_]+$", name) is not None or PURE_PREFIX.match(name) is not None


def calls_nothing(body, helpers):
    code = COMMENT.sub("", body)
    local = set(re.findall(r"#\s*define\s+(\w+)", code))
    for name in set(CALLEE.findall(code)):
        if name in KEYWORDS or name in local or name in PURE_HOST or name in helpers or is_lane_helper(name):
            continue
        return False
    return True


# header macros that assign to some of their arguments: name -> indexes of those arguments (recomp_types.h)
WRITING_MACROS = {"PUSH32": (0,), "POP32": (0, 1), "SET_LO8": (0,), "SET_HI8": (0,), "SET_LO16": (0,),
                  "SET_HI16": (0,)}


def macro_args(code, at):
    """The top-level arguments of the call whose '(' ends just before index at."""
    args, depth, start, i = [], 1, at, at
    while i < len(code) and depth:
        c = code[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "," and depth == 1:
            args.append(code[start:i])
            start = i + 1
        i += 1
    args.append(code[start:i - 1])
    return args


def writes(code, r):
    """Whether the body may write register r: an assignment, ++/--, its address taken, or an argument a writing macro
    assigns to. Erring towards 'written' only costs a store; a miss fails the build through the const declaration."""
    if re.search(rf"\b{r}\s*(=(?!=)|\+=|-=|\*=|/=|%=|&=|\|=|\^=|<<=|>>=|\+\+|--)|(\+\+|--)\s*{r}\b|&\s*\(?\s*{r}\b", code):
        return True
    for m in re.finditer(r"\b(" + "|".join(WRITING_MACROS) + r")\s*\(", code):
        args = macro_args(code, m.end())
        if any(i < len(args) and re.search(rf"\b{r}\b", args[i]) for i in WRITING_MACROS[m.group(1)]):
            return True
    return False


def apply_reglocals(text, helpers):
    """Returns (text, functions changed)."""
    out, pos, changed = [], 0, 0
    for m in FUNC.finditer(text):
        start, body_at = m.start(), m.end()
        end = text.find(NL + "}" + NL, body_at)
        if end < 0:
            continue
        body = text[body_at:end + 1]
        if RLOCAL not in body or LRET in body or not calls_nothing(body, helpers):
            continue
        code = COMMENT.sub("", body)
        used = [r for r in LREGS if re.search(r"\b" + r + r"\b", code)]
        if not used:
            continue
        written = [r for r in used if writes(code, r)]
        pre = "".join(f"#undef {r} {LMARK}{NL}#define {r} _pll_{r} {LMARK}{NL}" for r in used)
        wb = ", ".join(f"_plr->r_{r} = _pll_{r}" for r in written) or "(void)0"
        pre += f"#define _PLL_WB() ({wb}) {LMARK}{NL}"
        post = "".join(f"#undef {r} {LMARK}{NL}#define {r} (_plr->r_{r}) {LMARK}{NL}" for r in used)
        post += f"#undef _PLL_WB {LMARK}{NL}"
        # Registers judged read-only are const: a write the analysis missed fails the build instead of being lost.
        decl = "".join(f"    {'' if r in written else 'const '}uint32_t _pll_{r} = _plr->r_{r}; {LMARK}{NL}"
                       for r in used)
        new_body = body.replace(RLOCAL, RLOCAL + decl, 1)
        new_body = re.sub(r"\breturn;", LRET, new_body)
        new_body += f"    _PLL_WB(); {LMARK}{NL}"
        out += [text[pos:start], pre, m.group(1), new_body, "}" + NL, post]
        pos = end + 3
        changed += 1
    out.append(text[pos:])
    return "".join(out), changed


def undo_reglocals(text):
    if LMARK not in text:
        return text
    text = NL.join(l for l in text.split(NL) if LMARK not in l)
    return text.replace(LRET, "return;")


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
    wanted["reglocals"] = not undo_all and wanted["regs"] and os.environ.get("MANHUNT_REG_LOCALS") == "1"
    files = sorted(gen.glob("recomp_[0-9][0-9][0-9][0-9].c"))
    if not files:
        sys.exit(f"post-lift: no chunks in {gen}")
    helpers = header_helpers(gen) if wanted["reglocals"] else set()
    changed, counts = 0, {name: 0 for name in wanted}
    for f in files:
        old = f.read_text(encoding="utf-8")
        new = undo_reglocals(old)
        for env, header, local, name in EDITS:          # strip every edit, then put back the wanted ones in order,
            new = undo(new, header, local)               # so the result does not depend on what was there before
        for env, header, local, name in EDITS:
            if wanted[name]:
                new = apply(new, header, local)
        for env, header, local, name in EDITS:
            counts[name] += new.count(local)
        if wanted["reglocals"]:
            new, n = apply_reglocals(new, helpers)
            counts["reglocals"] += n
        if new != old:
            f.write_text(new, encoding="utf-8", newline=NL)
            changed += 1
    state = ", ".join(f"{n} {'on' if wanted[n] else 'off'} ({counts[n]} functions)" for n in counts)
    print(f"post-lift: {state}; {changed} of {len(files)} chunks rewritten")


if __name__ == "__main__":
    main()
