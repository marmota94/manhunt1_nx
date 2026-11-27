/* One TLS block for the guest's x86 registers, and the runtime's old names as
 * aliases inside it (guest_regs.h). Zero-initialised, like the definitions it
 * replaces (patch 0042). */
#include "guest_regs.h"

    .section .tbss,"awT",%nobits
    .balign 16
    .globl g_guest_regs
    .type g_guest_regs, %tls_object
    .size g_guest_regs, GR_SIZE
g_guest_regs:
    .zero GR_SIZE

    .globl g_esp
    .type g_esp, %tls_object
    .size g_esp, 4
    .set g_esp, g_guest_regs + GR_ESP
    .globl g_eax
    .type g_eax, %tls_object
    .size g_eax, 4
    .set g_eax, g_guest_regs + GR_EAX
    .globl g_ecx
    .type g_ecx, %tls_object
    .size g_ecx, 4
    .set g_ecx, g_guest_regs + GR_ECX
    .globl g_edx
    .type g_edx, %tls_object
    .size g_edx, 4
    .set g_edx, g_guest_regs + GR_EDX
    .globl g_ebx
    .type g_ebx, %tls_object
    .size g_ebx, 4
    .set g_ebx, g_guest_regs + GR_EBX
    .globl g_esi
    .type g_esi, %tls_object
    .size g_esi, 4
    .set g_esi, g_guest_regs + GR_ESI
    .globl g_edi
    .type g_edi, %tls_object
    .size g_edi, 4
    .set g_edi, g_guest_regs + GR_EDI
    .globl g_ebp
    .type g_ebp, %tls_object
    .size g_ebp, 4
    .set g_ebp, g_guest_regs + GR_EBP
    .globl g_seh_ebp
    .type g_seh_ebp, %tls_object
    .size g_seh_ebp, 4
    .set g_seh_ebp, g_guest_regs + GR_SEH_EBP
    .globl g_fp_top
    .type g_fp_top, %tls_object
    .size g_fp_top, 4
    .set g_fp_top, g_guest_regs + GR_FP_TOP
    .globl g_df
    .type g_df, %tls_object
    .size g_df, 4
    .set g_df, g_guest_regs + GR_DF
    .globl g_fp_stack
    .type g_fp_stack, %tls_object
    .size g_fp_stack, 64
    .set g_fp_stack, g_guest_regs + GR_FP_STACK
