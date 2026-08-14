/* spanblend.S -- raw x86-64 raster kernels, no C, no tables-of-tables.
 *
 * The Python rasteriser in this project blends spans with strided
 * bytes.translate() calls against cached 256-byte lookup tables: three
 * "C-level" operations per row.  This file is what that actually is, in
 * the language the machine speaks.  Loaded straight into executable
 * memory by feetbrowser/asmblend.py; no assembler required at runtime.
 *
 * SysV ABI, x86-64:
 *   arg1 = rdi, arg2 = rsi, arg3 = rdx, arg4 = rcx, arg5 = r8
 * rax, rcx, rdx, rsi, rdi and r8-r11 are caller-saved, so those are the
 * only registers touched here; rbx, rbp and r12-r15 belong to whoever
 * called us and would have to be saved and restored to be used at all.
 * Position-independent: only local ('.L') labels, so the blob can live
 * anywhere in a PROT_EXEC page.
 */

    .text

/* ------------------------------------------------------------------ */
/* void translate(unsigned char *dst, const unsigned char *src,
 *                const unsigned char *table, size_t n)
 *
 *   dst[i] = table[src[i]]     -- one byte, one table read, one store.
 * This is the entire body of the bytes.translate() trick, minus Python.
 * ------------------------------------------------------------------ */
    .globl translate
    .type translate,@function
translate:
    xor %eax, %eax              /* i = 0                       */
.Ltranslate_loop:
    movzbl (%rsi,%rax), %r9d    /* load src[i]                 */
    movzbl (%rdx,%r9), %r9d     /* table[src[i]]               */
    movb %r9b, (%rdi,%rax)      /* dst[i] = that               */
    inc %rax                    /* i++                         */
    cmp %rcx, %rax              /* i < n ?                      */
    jb  .Ltranslate_loop
    ret
    .size translate, .-translate

/* ------------------------------------------------------------------ */
/* void blend_rgb(unsigned char *dst, const unsigned char *src,
 *                size_t n, unsigned int alpha)
 *
 * Per-channel 8-bit alpha over a contiguous RGB run, in one pass:
 *   dst = (src*a + dst*(255-a)) >> 8
 * No lookup tables at all -- the maths the tables were hiding.
 * ------------------------------------------------------------------ */
    .globl blend_rgb
    .type blend_rgb,@function
blend_rgb:
    mov %ecx, %r9d              /* a = alpha (low byte)        */
    and $0xFF, %r9d
    mov %r9d, %r10d
    not %r10d                   /* 255-a                       */
    and $0xFF, %r10d            /* 32-bit imul below needs the */
                                /* high bits zero              */
    mov %rdx, %rax              /* n                           */
    xor %ecx, %ecx              /* i = 0                       */
.Lblend_loop:
    movzbl (%rsi,%rcx), %r11d   /* src[i]                      */
    movzbl (%rdi,%rcx), %r8d    /* dst[i]                      */
    imul %r9d, %r11d            /* src * a                     */
    imul %r10d, %r8d            /* dst * (255-a)               */
    add %r8d, %r11d             /* sum                         */
    shr $8, %r11d               /* >> 8                        */
    movb %r11b, (%rdi,%rcx)     /* store                       */
    inc %rcx                    /* i++                         */
    cmp %rax, %rcx              /* i < n ?                     */
    jb  .Lblend_loop
    ret
    .size blend_rgb, .-blend_rgb

/* ------------------------------------------------------------------ */
/* void fill_span(unsigned char *dst, unsigned char value, size_t n)  */
/* Opaque span fill -- the case raster.py does with one slice assign. */
/* ------------------------------------------------------------------ */
    .globl fill_span
    .type fill_span,@function
fill_span:
    mov %sil, %al               /* value                       */
    mov %rdx, %rcx              /* n                           */
    rep stosb                   /* cld + store al, rcx times   */
    ret
    .size fill_span, .-fill_span

    .section .note.GNU-stack,"",@progbits