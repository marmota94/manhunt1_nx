/*
 * The guest's per-thread x86 registers in one TLS block (Switch build).
 *
 * The runtime declares each register as its own thread-local (g_eax, g_esp, ...).
 * With -mtp=soft every lifted function then calls __aarch64_read_tp and keeps one
 * callee-saved register per TLS address it uses. guest_regs.S defines one block,
 * g_guest_regs, and the old names as aliases at fixed offsets inside it, so the runtime's
 * declarations keep working unchanged (patch 0042 drops its own definitions under
 * RECOMP_GUEST_REGS_EXTERNAL). scripts/post-lift.py makes each lifted function take
 * &g_guest_regs once and address the registers through it.
 * reports/PERF_INVESTIGATION_2026-11-16.md, item 2.
 *
 * Member names must not be eax etc.: the generated code #defines those names.
 */
#ifndef MANHUNT_GUEST_REGS_H
#define MANHUNT_GUEST_REGS_H

#define GR_ESP      0
#define GR_EAX      4
#define GR_ECX      8
#define GR_EDX      12
#define GR_EBX      16
#define GR_ESI      20
#define GR_EDI      24
#define GR_EBP      28
#define GR_SEH_EBP  32
#define GR_FP_TOP   36
#define GR_DF       40
#define GR_FP_STACK 48
#define GR_SIZE     112

#ifndef __ASSEMBLER__
#include <stddef.h>
#include <stdint.h>

typedef struct RecompGuestRegs {
    uint32_t r_esp, r_eax, r_ecx, r_edx, r_ebx, r_esi, r_edi, r_ebp, r_seh_ebp;
    int r_fp_top, r_df;
    uint32_t r_pad;
    double r_fp_stack[8];
} RecompGuestRegs;

_Static_assert(offsetof(RecompGuestRegs, r_esp) == GR_ESP, "guest_regs.S layout");
_Static_assert(offsetof(RecompGuestRegs, r_eax) == GR_EAX, "guest_regs.S layout");
_Static_assert(offsetof(RecompGuestRegs, r_ecx) == GR_ECX, "guest_regs.S layout");
_Static_assert(offsetof(RecompGuestRegs, r_edx) == GR_EDX, "guest_regs.S layout");
_Static_assert(offsetof(RecompGuestRegs, r_ebx) == GR_EBX, "guest_regs.S layout");
_Static_assert(offsetof(RecompGuestRegs, r_esi) == GR_ESI, "guest_regs.S layout");
_Static_assert(offsetof(RecompGuestRegs, r_edi) == GR_EDI, "guest_regs.S layout");
_Static_assert(offsetof(RecompGuestRegs, r_ebp) == GR_EBP, "guest_regs.S layout");
_Static_assert(offsetof(RecompGuestRegs, r_seh_ebp) == GR_SEH_EBP, "guest_regs.S layout");
_Static_assert(offsetof(RecompGuestRegs, r_fp_top) == GR_FP_TOP, "guest_regs.S layout");
_Static_assert(offsetof(RecompGuestRegs, r_df) == GR_DF, "guest_regs.S layout");
_Static_assert(offsetof(RecompGuestRegs, r_fp_stack) == GR_FP_STACK, "guest_regs.S layout");
_Static_assert(sizeof(RecompGuestRegs) == GR_SIZE, "guest_regs.S layout");

extern __thread RecompGuestRegs g_guest_regs;
#endif
#endif
