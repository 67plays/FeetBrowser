// nearest_offset(adv, n, x, pen)
//
// The nearest character boundary to pixel x, as a 0-based offset.
//
//   arguments:  rdi = adv      double* of per-character advances
//               rsi = n        number of advances
//               xmm0 = x       pixel x
//               xmm1 = pen     pen at the start of the run
//   returns:    rax            the best offset (a run-local index)
//
// Walks the advances exactly like the Python loop it replaces:
//
//     best, best_d = 0, abs(x - pen)
//     for i in range(n):
//         pen += adv[i]
//         d = abs(x - pen)
//         if d < best_d:
//             best, best_d = i + 1, d
//     return best
//
// The comparison is strict <: on a tie the earlier boundary wins, which is
// what the Python code did (it only replaces best when the new distance is
// strictly smaller). The kernel never looks at the text itself, so there is
// no encoding question -- the advances array is what it is.

    .text
    .globl nearest_offset
    .type nearest_offset, @function
nearest_offset:
    movq .Labs_mask(%rip), %xmm5    // sign-mask for |x|
    movsd %xmm0, %xmm2              // best_d = x - pen
    subsd %xmm1, %xmm2
    andpd %xmm5, %xmm2              // best_d = |x - pen|
    xor %eax, %eax                  // best = 0
    xor %ecx, %ecx                  // i = 0
    test %rsi, %rsi
    jz  .Ldone                      // n == 0
.Lloop:
    movsd (%rdi,%rcx,8), %xmm3      // d = x - (pen += adv[i])
    addsd %xmm3, %xmm1              // pen += adv[i]
    movsd %xmm0, %xmm3
    subsd %xmm1, %xmm3
    andpd %xmm5, %xmm3              // d = |x - pen|
    ucomisd %xmm2, %xmm3            // d - best_d (CF=1 iff d < best_d)
    jae .Lnext                      // d >= best_d: no change (strict <)
    lea 1(%rcx), %eax               // best = i + 1
    movsd %xmm3, %xmm2              // best_d = d
.Lnext:
    inc %rcx
    cmp %rsi, %rcx
    jb  .Lloop
.Ldone:
    ret
    .balign 16
.Labs_mask:
    .quad 0x7FFFFFFFFFFFFFFF
    .size nearest_offset, .-nearest_offset

    .section .note.GNU-stack,"",@progbits