"""Differential check of post-lift.py's reglocals edit: each changed function against its original, from the same state.

    python -I tests/post-lift/reglocals_diff.py <gen dir without reglocals> <gen dir with reglocals> <out dir>
                                                [--trials 16] [--chunks 0010,0011] [--jobs 4]

For every function reglocals changed, the harness builds both versions into one host program (x86-64 gcc in the pinned
devkitA64 image, the same headers and macros), and for each trial:
  - maps a fresh 4 GB guest memory (lazily backed), fills a 1 MB window with random bytes, sets the register block,
    the x87 stack, the TLS globals the header declares (fs base, eflags, xmm, mmx, ...) from one seed;
  - runs the original in one child process and the changed version in another (2 s limit each);
  - compares the register block, those TLS globals, and every guest page either run touched.
A trial where both runs fault the same way counts as equal; one where either run hits the time limit (a long loop
driven by random memory) is counted apart as inconclusive. Random inputs reach only some paths, so
this is evidence, not proof. Needs the game's generated code: run it locally, never in CI.
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "devkitpro/devkita64@sha256:1fc388c3a0d34bd2045a6dadcb1020e069d5f876a187fd705de14b4440c00282"
LMARK = "/* post-lift.py: reglocals */"
FUNC = re.compile(r"^void (sub_[0-9A-F]+)\(void\)\n\{\n", re.M)
NL = "\n"

HARNESS = r"""
#define _GNU_SOURCE
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <unistd.h>
#define RECOMP_GENERATED_CODE
#include "recomp_types.h"
#include "guest_regs.h"

/* The two host functions reglocals lets a changed function call; neither touches the register block. */
void recomp_unimpl(const char *text, uint32_t va) { (void)text; (void)va; }
void xbox_GuestCpuPreempt(uint32_t va) { (void)va; }

typedef void (*fn_t)(void);
struct pair { const char *name; fn_t o, t; };
extern const struct pair g_pairs[];
extern const int g_npairs;

/* What a run leaves behind: the register block and the TLS globals the header declares. */
struct state {
    RecompGuestRegs regs;
    uint32_t fs_base, eflags, mxcsr;
    uint16_t fp_control_word, fp_cc;
    int fp_cmp;
    RecompXmm xmm[8];
    RecompMmx mm[8];
};

#define GUEST_SIZE (1ull << 32)
#define WIN_LO 0x00100000u
#define WIN_SIZE 0x00100000u

static uint64_t rng;
static uint32_t rnd(void) { rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17; return (uint32_t)rng; }

static void seed_state(uint64_t seed, uint8_t *mem)
{
    rng = seed * 0x9E3779B97F4A7C15ull + 1;
    for (uint32_t i = 0; i < WIN_SIZE; i += 4) *(uint32_t *)(mem + WIN_LO + i) = rnd();
    uint32_t *r = &g_guest_regs.r_esp;
    for (int i = 0; i < 9; i++)   /* esp ... seh_ebp: mostly inside the window, sometimes anything */
        r[i] = (rnd() & 7) ? WIN_LO + 0x1000 + (rnd() % (WIN_SIZE - 0x2000)) : rnd();
    g_guest_regs.r_esp = WIN_LO + WIN_SIZE / 2 + (rnd() & 0xFFC);
    g_guest_regs.r_fp_top = rnd() & 7;
    g_guest_regs.r_df = 0;
    for (int i = 0; i < 8; i++) g_guest_regs.r_fp_stack[i] = (double)(int32_t)rnd() / 65536.0;
    g_fs_base = WIN_LO + 0x800;
    g_eflags = 0x202; g_mxcsr = 0x1F80; g_fp_control_word = 0x27F; g_fp_cc = 0; g_fp_cmp = 0;
    RecompXmm *x[8] = {&g_xmm0, &g_xmm1, &g_xmm2, &g_xmm3, &g_xmm4, &g_xmm5, &g_xmm6, &g_xmm7};
    RecompMmx *m[8] = {&g_mm0, &g_mm1, &g_mm2, &g_mm3, &g_mm4, &g_mm5, &g_mm6, &g_mm7};
    for (int i = 0; i < 8; i++) {
        for (unsigned j = 0; j < sizeof(RecompXmm) / 4; j++) ((uint32_t *)x[i])[j] = rnd();
        for (unsigned j = 0; j < sizeof(RecompMmx) / 4; j++) ((uint32_t *)m[i])[j] = rnd();
    }
}

static void take_state(struct state *s)
{
    memset(s, 0, sizeof *s);
    s->regs = g_guest_regs;
    s->fs_base = g_fs_base; s->eflags = g_eflags; s->mxcsr = g_mxcsr;
    s->fp_control_word = g_fp_control_word; s->fp_cc = g_fp_cc; s->fp_cmp = g_fp_cmp;
    s->xmm[0] = g_xmm0; s->xmm[1] = g_xmm1; s->xmm[2] = g_xmm2; s->xmm[3] = g_xmm3;
    s->xmm[4] = g_xmm4; s->xmm[5] = g_xmm5; s->xmm[6] = g_xmm6; s->xmm[7] = g_xmm7;
    s->mm[0] = g_mm0; s->mm[1] = g_mm1; s->mm[2] = g_mm2; s->mm[3] = g_mm3;
    s->mm[4] = g_mm4; s->mm[5] = g_mm5; s->mm[6] = g_mm6; s->mm[7] = g_mm7;
}

/* One run in a child: writes the state, then (page, FNV hash) for every resident guest page, to fd. */
static unsigned g_alarm = 2;   /* RL_ALARM=<seconds> */
static int run_child(fn_t fn, uint64_t seed, int fd)
{
    uint8_t *mem = mmap(NULL, GUEST_SIZE + 0x10000, PROT_READ | PROT_WRITE,
                        MAP_PRIVATE | MAP_ANONYMOUS | MAP_NORESERVE, -1, 0);
    if (mem == MAP_FAILED) return 90;
    g_xbox_mem_offset = (ptrdiff_t)mem;
    seed_state(seed, mem);
    alarm(g_alarm);
    fn();
    alarm(0);
    struct state s;
    take_state(&s);
    if (write(fd, &s, sizeof s) != sizeof s) return 91;
    static unsigned char vec[(GUEST_SIZE >> 12) + 16];
    if (mincore(mem, GUEST_SIZE, vec)) return 92;
    for (uint64_t p = 0; p < (GUEST_SIZE >> 12); p++) {
        if (!(vec[p] & 1)) continue;
        uint64_t h = 1469598103934665603ull;
        const uint8_t *q = mem + (p << 12);
        int zero = 1;
        for (int i = 0; i < 4096; i++) { h = (h ^ q[i]) * 1099511628211ull; zero &= q[i] == 0; }
        if (zero) continue;   /* a page only read reads as zero in both */
        uint64_t rec[2] = {p, h};
        if (write(fd, rec, sizeof rec) != sizeof rec) return 93;
    }
    return 0;
}

/* Runs fn in a child; returns a malloc'd dump and its size, and the child's status. */
static unsigned char *run(fn_t fn, uint64_t seed, size_t *len, int *status)
{
    int p[2];
    if (pipe(p)) exit(2);
    pid_t pid = fork();
    if (pid == 0) { close(p[0]); _exit(run_child(fn, seed, p[1])); }
    close(p[1]);
    size_t cap = 1 << 16, n = 0;
    unsigned char *buf = malloc(cap);
    for (;;) {
        if (n == cap) buf = realloc(buf, cap *= 2);
        ssize_t r = read(p[0], buf + n, cap - n);
        if (r <= 0) break;
        n += (size_t)r;
    }
    close(p[0]);
    waitpid(pid, status, 0);
    *len = n;
    return buf;
}

int main(int argc, char **argv)
{
    int trials = argc > 1 ? atoi(argv[1]) : 16;
    const char *only = getenv("RL_ONLY");   /* RL_ONLY=<function name>: run one pair */
    if (getenv("RL_ALARM")) g_alarm = (unsigned)atoi(getenv("RL_ALARM"));
    int equal = 0, differ = 0, faulted = 0, functions_differ = 0, timeouts = 0, faster = 0, slower = 0;
    for (int f = 0; f < g_npairs; f++) {
        if (only && strcmp(only, g_pairs[f].name)) continue;
        int bad = 0;
        for (int t = 0; t < trials; t++) {
            uint64_t seed = (uint64_t)f * 1000003u + (uint64_t)t + 1;
            size_t la, lb; int sa, sb;
            unsigned char *a = run(g_pairs[f].o, seed, &la, &sa);
            unsigned char *b = run(g_pairs[f].t, seed, &lb, &sb);
            int same;
            int ta = WIFSIGNALED(sa) && WTERMSIG(sa) == SIGALRM, tb = WIFSIGNALED(sb) && WTERMSIG(sb) == SIGALRM;
            if (ta || tb) {
                timeouts++; faster += ta && !tb; slower += tb && !ta;
                if (tb && !ta) printf("SLOWER %s trial %d (only the changed one timed out)\n", g_pairs[f].name, t);
                free(a); free(b); continue;
            }
            if (sa != sb) same = 0;
            else if (!(WIFEXITED(sa) && WEXITSTATUS(sa) == 0)) { same = 1; faulted++; }
            else same = la == lb && memcmp(a, b, la) == 0;
            if (same) equal++;
            else {
                differ++;
                if (!bad) printf("DIFF %s trial %d (status %d / %d, %zu / %zu bytes)\n", g_pairs[f].name, t, sa, sb, la, lb);
                bad = 1;
            }
            free(a); free(b);
        }
        functions_differ += bad;
    }
    printf("timeouts %d (only the original timed out: %d, only the changed one: %d)\n", timeouts, faster, slower);
    printf("functions %d, trials %d: equal %d (both faulted the same way %d), different %d; functions with a difference %d\n",
           g_npairs, trials, equal, faulted, differ, functions_differ);
    return functions_differ ? 1 : 0;
}
"""


def functions(text):
    """name -> (start, end) of each function, end just past its closing brace line."""
    out = {}
    for m in FUNC.finditer(text):
        end = text.find(NL + "}" + NL, m.end())
        out[m.group(1)] = (m.start(), end + 3)
    return out


def prelude(text):
    m = FUNC.search(text)
    head = text[:m.start()]
    return head[:head.rfind("/**")] if "/**" in head[-400:] else head


def build_tu(base_text, rl_text):
    base_f, rl_f = functions(base_text), functions(rl_text)
    parts, names = [prelude(base_text)], []
    for name, (s, e) in rl_f.items():
        pre_start = rl_text.rfind(NL, 0, s - 1)
        block = rl_text[:s]
        if not block.rstrip(NL).endswith(LMARK):
            continue                                   # not changed by reglocals
        # the marked lines just above the function, and the ones just below it
        lines_before = []
        for line in reversed(block.rstrip(NL).split(NL)):
            if LMARK not in line:
                break
            lines_before.insert(0, line)
        after, rest = [], rl_text[e:].split(NL)
        for line in rest:
            if LMARK not in line:
                break
            after.append(line)
        bs, be = base_f[name]
        parts.append(base_text[bs:be].replace(f"void {name}(void)", f"void o_{name}(void)", 1))
        parts.append(NL.join(lines_before) + NL + rl_text[s:e].replace(f"void {name}(void)", f"void t_{name}(void)", 1)
                     + NL.join(after) + NL)
        names.append(name)
    parts.append("struct pair { const char *name; void (*o)(void), (*t)(void); };\nconst struct pair g_pairs[] = {\n")
    parts += [f'    {{"{n}", o_{n}, t_{n}}},\n' for n in names]
    parts.append("};\nconst int g_npairs = %d;\n" % len(names))
    return "".join(parts), len(names)


def globals_c(types_h):
    """Definitions for every extern variable the header declares (TLS or not)."""
    text = Path(types_h).read_text(encoding="utf-8")
    out = ['#define RECOMP_GENERATED_CODE', '#include "recomp_types.h"', '#include "guest_regs.h"', "__thread RecompGuestRegs g_guest_regs;"]
    alias = {"g_eax", "g_ecx", "g_edx", "g_esp", "g_ebx", "g_esi", "g_edi", "g_ebp", "g_seh_ebp", "g_fp_top",
             "g_fp_stack", "g_df"}
    for m in re.finditer(r"(?m)^extern\s+((?:RECOMP_TLS\s+)?(?:volatile\s+)?[A-Za-z_]\w*)\s+([^;(]+);", text):
        kind, names = m.group(1), [n.strip() for n in m.group(2).split(",")]
        names = [n for n in names if re.sub(r"\[.*", "", n) not in alias]
        if names:
            out.append(f"{kind} {', '.join(names)};")
    return NL.join(out) + NL


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("base", type=Path)
    ap.add_argument("rl", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--trials", type=int, default=16)
    ap.add_argument("--chunks", help="comma-separated chunk numbers (default: all)")
    ap.add_argument("--jobs", type=int, default=4)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    chunks = sorted(p.stem for p in a.rl.glob("recomp_[0-9][0-9][0-9][0-9].c"))
    if a.chunks:
        chunks = [f"recomp_{int(c):04d}" for c in a.chunks.split(",")]
    total = 0
    for c in chunks:
        tu, n = build_tu((a.base / f"{c}.c").read_text(encoding="utf-8"), (a.rl / f"{c}.c").read_text(encoding="utf-8"))
        if n:
            (a.out / f"{c}_pairs.c").write_text(tu, encoding="utf-8", newline=NL)
            total += n
    (a.out / "harness.c").write_text(HARNESS, encoding="utf-8", newline=NL)
    (a.out / "globals.c").write_text(globals_c(a.base / "recomp_types.h"), encoding="utf-8", newline=NL)
    print(f"{total} function pairs in {len(chunks)} chunks")
    inc = "-I/gen -I/repo/game/src -I/repo/game/build/xboxrecomp/include -I/repo/game/build/xboxrecomp/src"
    flags = f"-O2 -std=gnu11 -fno-strict-aliasing -ffp-contract=off -w -DRECOMP_GUEST_REGS_EXTERNAL=1 {inc}"
    script = (f"set -e; cd /out; ls *_pairs.c | xargs -P{a.jobs} -I{{}} gcc {flags} -c {{}}; "
              f"for f in *_pairs.o; do gcc {flags} harness.c globals.c $f -lm -o run_${{f%_pairs.o}} || echo LINKFAIL $f; done; "
              f"for r in run_*; do echo \"== $r\"; ./$r {a.trials} || true; done")
    cmd = ["docker", "run", "--rm", "--network", "none", "--cpus", str(a.jobs), "--memory", "8g",
           "--mount", f"type=bind,src={ROOT},dst=/repo,readonly", "--mount", f"type=bind,src={a.base.resolve()},dst=/gen,readonly",
           "--mount", f"type=bind,src={a.out.resolve()},dst=/out", IMAGE, "bash", "-c", script]
    r = subprocess.run(cmd, env=dict(os.environ, MSYS_NO_PATHCONV="1"), capture_output=True, text=True)
    (a.out / "result.txt").write_text(r.stdout + r.stderr, encoding="utf-8")
    lines = r.stdout.splitlines()
    for l in lines:
        if l.startswith(("DIFF", "SLOWER", "functions", "timeouts", "LINKFAIL")):
            print(l)
    if r.returncode:
        print(r.stderr[-3000:])
    sums = [re.findall(r"\d+", l) for l in lines if l.startswith("functions")]
    if sums:
        f = sum(int(s[0]) for s in sums); d = sum(int(s[-1]) for s in sums)
        print(f"TOTAL: {f} functions checked, {d} with a difference")
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
