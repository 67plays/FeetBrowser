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

/* ------------------------------------------------------------------ */
/* void blit_cov(unsigned char *dst, const unsigned char *cov,
 *               size_t n, unsigned char r, unsigned char g,
 *               unsigned char b)
 *
 * Composite an n-pixel coverage run in a solid colour over an RGB dst
 * (3 contiguous bytes per pixel), the coverage bytes per pixel:
 *   a == 0:   pixel untouched
 *   a == 255: pixel becomes (r, g, b)
 *   else:     (c*a + dst*(255-a)) >> 8 per channel
 * ------------------------------------------------------------------ */
    .globl blit_cov
    .type blit_cov,@function
blit_cov:
    test %rdx, %rdx             /* n == 0 ?                     */
    jz  .Lblitcov_done
.Lblitcov_loop:
    movzbl (%rsi), %eax         /* a = cov[i]                   */
    test %eax, %eax             /* a == 0 ?                     */
    jz  .Lblitcov_next          /* yes: leave pixel untouched   */
    cmp $255, %eax
    je  .Lblitcov_solid         /* a == 255: write solid colour */
    mov $255, %r10d
    sub %eax, %r10d             /* inv = 255 - a                */
    /* red channel: (r*a + dst[0]*inv) >> 8 */
    movzbl (%rsi), %eax         /* a                            */
    movzbl %cl, %r11d           /* r                            */
    imul %eax, %r11d            /* r * a                        */
    movzbl (%rdi), %eax         /* dst[0]                       */
    imul %r10d, %eax            /* dst[0] * inv                 */
    add %r11d, %eax             /* sum                          */
    shr $8, %eax                /* >> 8                         */
    movb %al, (%rdi)            /* store                        */
    /* green channel: (g*a + dst[1]*inv) >> 8 */
    movzbl (%rsi), %eax         /* a                            */
    movzbl %r8b, %r11d          /* g                            */
    imul %eax, %r11d            /* g * a                        */
    movzbl 1(%rdi), %eax        /* dst[1]                       */
    imul %r10d, %eax            /* dst[1] * inv                 */
    add %r11d, %eax
    shr $8, %eax
    movb %al, 1(%rdi)           /* store                        */
    /* blue channel: (b*a + dst[2]*inv) >> 8 */
    movzbl (%rsi), %eax         /* a                            */
    movzbl %r9b, %r11d          /* b                            */
    imul %eax, %r11d            /* b * a                        */
    movzbl 2(%rdi), %eax        /* dst[2]                       */
    imul %r10d, %eax            /* dst[2] * inv                 */
    add %r11d, %eax
    shr $8, %eax
    movb %al, 2(%rdi)           /* store                        */
    jmp .Lblitcov_next
.Lblitcov_solid:
    movb %cl, (%rdi)            /* dst[0] = r                   */
    movb %r8b, 1(%rdi)          /* dst[1] = g                   */
    movb %r9b, 2(%rdi)          /* dst[2] = b                   */
.Lblitcov_next:
    add $3, %rdi                /* 3 bytes per dst pixel        */
    inc %rsi                    /* 1 byte per cov pixel         */
    dec %rdx                    /* i++                          */
    jnz .Lblitcov_loop
.Lblitcov_done:
    ret
    .size blit_cov, .-blit_cov

/* ------------------------------------------------------------------ */
/* void blend_rgba(unsigned char *dst, const unsigned char *src,
 *                 size_t n)
 *
 * Composite an n-pixel RGBA run over an RGB dst (3 bytes per pixel);
 * src advances 4 bytes per pixel:
 *   a == 0:   pixel untouched
 *   a == 255: copy src's three colour bytes
 *   else:     (srcc*a + dst*(255-a)) >> 8 per channel
 * ------------------------------------------------------------------ */
    .globl blend_rgba
    .type blend_rgba,@function
blend_rgba:
    test %rdx, %rdx             /* n == 0 ?                     */
    jz  .Lblendrgba_done
.Lblendrgba_loop:
    movzbl 3(%rsi), %eax        /* a = src[3]                   */
    test %eax, %eax             /* a == 0 ?                     */
    jz  .Lblendrgba_next        /* yes: skip pixel              */
    cmp $255, %eax
    je  .Lblendrgba_solid       /* a == 255: copy colour bytes  */
    mov $255, %r10d
    sub %eax, %r10d             /* inv = 255 - a                */
    /* red channel: (src[0]*a + dst[0]*inv) >> 8 */
    movzbl 3(%rsi), %eax        /* a                            */
    movzbl (%rsi), %r11d        /* src[0]                       */
    imul %eax, %r11d            /* src[0] * a                   */
    movzbl (%rdi), %eax         /* dst[0]                       */
    imul %r10d, %eax            /* dst[0] * inv                 */
    add %r11d, %eax             /* sum                          */
    shr $8, %eax                /* >> 8                         */
    movb %al, (%rdi)            /* store                        */
    /* green channel: (src[1]*a + dst[1]*inv) >> 8 */
    movzbl 3(%rsi), %eax        /* a                            */
    movzbl 1(%rsi), %r11d       /* src[1]                       */
    imul %eax, %r11d            /* src[1] * a                   */
    movzbl 1(%rdi), %eax        /* dst[1]                       */
    imul %r10d, %eax            /* dst[1] * inv                 */
    add %r11d, %eax
    shr $8, %eax
    movb %al, 1(%rdi)           /* store                        */
    /* blue channel: (src[2]*a + dst[2]*inv) >> 8 */
    movzbl 3(%rsi), %eax        /* a                            */
    movzbl 2(%rsi), %r11d       /* src[2]                       */
    imul %eax, %r11d            /* src[2] * a                   */
    movzbl 2(%rdi), %eax        /* dst[2]                       */
    imul %r10d, %eax            /* dst[2] * inv                 */
    add %r11d, %eax
    shr $8, %eax
    movb %al, 2(%rdi)           /* store                        */
    jmp .Lblendrgba_next
.Lblendrgba_solid:
    movzbl (%rsi), %eax         /* src[0]                       */
    movb %al, (%rdi)
    movzbl 1(%rsi), %eax        /* src[1]                       */
    movb %al, 1(%rdi)
    movzbl 2(%rsi), %eax        /* src[2]                       */
    movb %al, 2(%rdi)
.Lblendrgba_next:
    add $4, %rsi                /* 4 bytes per src pixel        */
    add $3, %rdi                /* 3 bytes per dst pixel        */
    dec %rdx                    /* i++                          */
    jnz .Lblendrgba_loop
.Lblendrgba_done:
    ret
    .size blend_rgba, .-blend_rgba

    .section .note.GNU-stack,"",@progbits