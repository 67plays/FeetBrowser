/* pixelops.S -- raw x86-64 image-codec kernels, no C, no tables.
 *
 * The PNG decoder and image resampler in this project move bytes with
 * per-pixel Python loops: scanline filters, palette/truecolour expansion to
 * RGBA, and nearest-neighbour rows.  This file is what those loops are, in
 * the language the machine speaks.  Loaded straight into executable memory
 * by feetbrowser/asmops.py via the shared compiler helper in
 * feetbrowser/asmblend.py; no assembler required at runtime.
 *
 * SysV ABI, x86-64:
 *   arg1 = rdi, arg2 = rsi, arg3 = rdx, arg4 = rcx, arg5 = r8, arg6 = r9
 *   (a seventh argument sits at 8(%rsp), above the return address)
 * rax, rcx, rdx, rsi, rdi and r8-r11 are caller-saved, so those are the
 * only registers touched here; rbx, rbp and r12-r15 belong to whoever
 * called us and would have to be saved and restored to be used at all.
 * Position-independent: only local ('.L') labels, so the blob can live
 * anywhere in a PROT_EXEC page.
 */

    .text

/* ------------------------------------------------------------------ */
/* void unfilter_line(unsigned char *line, const unsigned char *prev,
 *                    size_t n, unsigned int bpp, unsigned int ftype)
 *
 * Reverse one PNG scanline filter in place.  Every output byte is masked
 * to 0xFF exactly as the Python reference masks it; the Average and Paeth
 * predictions do their arithmetic wide enough that the mask is the only
 * wrap, just like the reference.
 * ------------------------------------------------------------------ */
    .globl unfilter_line
    .type unfilter_line,@function
unfilter_line:
    cmp $1, %r8d                /* dispatch on filter type      */
    je  .Lunfilter_sub
    cmp $2, %r8d
    je  .Lunfilter_up
    cmp $3, %r8d
    je  .Lunfilter_avg
    cmp $4, %r8d
    je  .Lunfilter_paeth
    ret                         /* ftype 0 (and junk): no-op    */

/* Sub: line[i] = (line[i] + line[i-bpp]) & 0xFF for i in [bpp, n)     */
.Lunfilter_sub:
    mov %rdx, %rax              /* n                             */
    mov %ecx, %r8d              /* bpp                           */
    cmp %r8, %rax               /* n <= bpp ? nothing to do      */
    jbe .Lunfilter_done
    mov %r8, %r9                /* i = bpp                       */
.Lsub_loop:
    movzbl (%rdi,%r9), %r10d    /* line[i]                       */
    mov %r9, %r11
    sub %r8, %r11               /* i - bpp                       */
    movzbl (%rdi,%r11), %r11d   /* line[i-bpp]                   */
    add %r11d, %r10d
    and $0xFF, %r10d
    movb %r10b, (%rdi,%r9)
    inc %r9
    cmp %rax, %r9
    jb  .Lsub_loop
    ret

/* Up: line[i] = (line[i] + prev[i]) & 0xFF for i in [0, n)           */
.Lunfilter_up:
    mov %rdx, %rax              /* n                             */
    xor %ecx, %ecx              /* i = 0                         */
.Lup_loop:
    movzbl (%rdi,%rcx), %r8d    /* line[i]                       */
    movzbl (%rsi,%rcx), %r9d    /* prev[i]                       */
    add %r9d, %r8d
    and $0xFF, %r8d
    movb %r8b, (%rdi,%rcx)
    inc %rcx
    cmp %rax, %rcx
    jb  .Lup_loop
    ret

/* Average: left = i>=bpp ? line[i-bpp] : 0;                          */
/*          line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF      */
.Lunfilter_avg:
    mov %rdx, %rax              /* n                             */
    mov %ecx, %r8d              /* bpp                           */
    xor %ecx, %ecx              /* i = 0                         */
.Lavg_loop:
    xor %r9d, %r9d              /* left = 0                      */
    cmp %r8, %rcx               /* i >= bpp ?                    */
    jb  .Lavg_no_left
    mov %rcx, %r10
    sub %r8, %r10               /* i - bpp                       */
    movzbl (%rdi,%r10), %r9d    /* left = line[i-bpp]            */
.Lavg_no_left:
    movzbl (%rsi,%rcx), %r10d   /* prev[i]                       */
    add %r9d, %r10d             /* left + prev[i]  (0..510)      */
    shr $1, %r10d               /* >> 1                          */
    movzbl (%rdi,%rcx), %r11d   /* line[i]                       */
    add %r11d, %r10d
    and $0xFF, %r10d
    movb %r10b, (%rdi,%rcx)
    inc %rcx
    cmp %rax, %rcx
    jb  .Lavg_loop
    ret

.Lunfilter_done:
    ret

/* Paeth: a = i>=bpp ? line[i-bpp] : 0; b = prev[i];                  */
/*        c = i>=bpp ? prev[i-bpp] : 0;                               */
/*        p = a+b-c; pa,pb,pc = abs(p-a),abs(p-b),abs(p-c);           */
/*        pred = a if (pa<=pb and pa<=pc) else (b if pb<=pc else c);  */
/*        line[i] = (line[i] + pred) & 0xFF                           */
.Lunfilter_paeth:
    sub $16, %rsp               /* scratch slot for c            */
    mov %rdx, %rax              /* n = loop bound (stays in rax) */
    mov %ecx, %r8d              /* bpp                           */
    xor %ecx, %ecx              /* i = 0                         */
.Lpaeth_loop:
    cmp %rax, %rcx              /* i < n ?                       */
    jae .Lunfilter_done_paeth
    xor %r9d, %r9d              /* a = 0                         */
    cmp %r8, %rcx
    jb  .Lp_no_a
    mov %rcx, %r11
    sub %r8, %r11               /* i - bpp                       */
    movzbl (%rdi,%r11), %r9d    /* a = line[i-bpp]               */
.Lp_no_a:
    movzbl (%rsi,%rcx), %r10d   /* b = prev[i]                   */
    xor %r11d, %r11d            /* c = 0                         */
    cmp %r8, %rcx
    jb  .Lp_no_c
    mov %rcx, %r11
    sub %r8, %r11               /* i - bpp                       */
    movzbl (%rsi,%r11), %r11d   /* c = prev[i-bpp]               */
.Lp_no_c:
    mov %r11d, (%rsp)           /* save c                        */
    mov %r9d, %edx              /* p = a + b - c                 */
    add %r10d, %edx
    sub %r11d, %edx
    mov %edx, %r11d             /* pa = |p - a|                  */
    sub %r9d, %r11d
    jns .Lp_pa_pos
    neg %r11d
.Lp_pa_pos:
    mov %r11d, %r9d
    mov %edx, %r11d             /* pb = |p - b|                  */
    sub %r10d, %r11d
    jns .Lp_pb_pos
    neg %r11d
.Lp_pb_pos:
    mov %r11d, %r10d
    mov %edx, %r11d             /* pc = |p - c|                  */
    sub (%rsp), %r11d
    jns .Lp_pc_pos
    neg %r11d
.Lp_pc_pos:
    cmp %r10d, %r9d             /* pa <= pb ?                    */
    ja  .Lp_b_or_c
    cmp %r11d, %r9d             /* pa <= pc ?                    */
    ja  .Lp_b_or_c
    xor %r11d, %r11d            /* pred = a (re-read; a was      */
    cmp %r8, %rcx               /* overwritten by pa)            */
    jb  .Lp_pred_a_zero
    mov %rcx, %r11
    sub %r8, %r11
    movzbl (%rdi,%r11), %r11d
.Lp_pred_a_zero:
    jmp .Lp_apply
.Lp_b_or_c:
    cmp %r11d, %r10d            /* pb <= pc ?                    */
    ja  .Lp_use_c
    movzbl (%rsi,%rcx), %r11d   /* pred = b = prev[i]            */
    jmp .Lp_apply
.Lp_use_c:
    mov (%rsp), %r11d           /* pred = c                      */
.Lp_apply:
    movzbl (%rdi,%rcx), %r9d    /* line[i] + pred, & 0xFF        */
    add %r11d, %r9d
    and $0xFF, %r9d
    movb %r9b, (%rdi,%rcx)
    inc %rcx
    jmp .Lpaeth_loop
.Lunfilter_done_paeth:
    add $16, %rsp
    ret
    .size unfilter_line, .-unfilter_line

/* ------------------------------------------------------------------ */
/* void palette_expand(unsigned char *dst, const unsigned char *src,
 *                     size_t n, const unsigned char *palette,
 *                     unsigned int pal_len, const unsigned char *trns,
 *                     unsigned int trns_len)
 *
 * Indexed samples to RGBA.  For i in [0,n):
 *   idx = src[i]
 *   if idx*3+2 < pal_len: dst[4i..4i+2] = palette[idx*3..idx*3+2]
 *   else:                 dst[4i..4i+2] = 0,0,0
 *   dst[4i+3] = trns[idx] if idx < trns_len else 255
 * ------------------------------------------------------------------ */
    .globl palette_expand
    .type palette_expand,@function
palette_expand:
    sub $16, %rsp               /* scratch slot; args shift up   */
    mov %rcx, %r11              /* palette base                  */
    mov %r8d, %r10d             /* pal_len                       */
    xor %eax, %eax              /* i = 0                         */
.Lpal_loop:
    cmp %rdx, %rax              /* i < n ?                       */
    jae .Lpal_done
    movzbl (%rsi,%rax), %r8d    /* idx                           */
    mov %r8d, (%rsp)            /* spill idx for the alpha step  */
    lea (%r8,%r8,2), %ecx       /* idx*3                         */
    lea 2(%rcx), %r8d           /* idx*3 + 2                     */
    cmp %r10d, %r8d             /* idx*3+2 < pal_len ?           */
    jae .Lpal_zero
    movzbl (%r11,%rcx), %r8d    /* palette[idx*3+0]              */
    movb %r8b, (%rdi,%rax,4)
    movzbl 1(%r11,%rcx), %r8d   /* palette[idx*3+1]              */
    movb %r8b, 1(%rdi,%rax,4)
    movzbl 2(%r11,%rcx), %r8d   /* palette[idx*3+2]              */
    movb %r8b, 2(%rdi,%rax,4)
    jmp .Lpal_alpha
.Lpal_zero:
    movb $0, (%rdi,%rax,4)
    movb $0, 1(%rdi,%rax,4)
    movb $0, 2(%rdi,%rax,4)
.Lpal_alpha:
    mov 24(%rsp), %r8d          /* trns_len (shifted arg)        */
    cmp %r8d, (%rsp)            /* idx < trns_len ?              */
    jae .Lpal_alpha255
    mov (%rsp), %r8d            /* idx                           */
    movzbl (%r9,%r8), %r8d      /* trns[idx]                     */
    movb %r8b, 3(%rdi,%rax,4)
    inc %rax
    jmp .Lpal_loop
.Lpal_alpha255:
    movb $0xFF, 3(%rdi,%rax,4)
    inc %rax
    jmp .Lpal_loop
.Lpal_done:
    add $16, %rsp
    ret
    .size palette_expand, .-palette_expand

/* ------------------------------------------------------------------ */
/* void grey_expand(unsigned char *dst, const unsigned char *src,
 *                  size_t n, unsigned int scale, unsigned int key,
 *                  unsigned int has_key)
 *
 * Greyscale samples to RGBA.  For i in [0,n):
 *   v = (src[i] * scale) & 0xFF
 *   dst[4i..4i+2] = v,v,v
 *   dst[4i+3] = 0 if (has_key and src[i] == key) else 255
 * ------------------------------------------------------------------ */
    .globl grey_expand
    .type grey_expand,@function
grey_expand:
    mov %ecx, %r10d             /* scale                        */
    xor %eax, %eax              /* i = 0                        */
.Lgrey_loop:
    cmp %rdx, %rax              /* i < n ?                      */
    jae .Lgrey_done
    movzbl (%rsi,%rax), %ecx    /* src[i]                       */
    mov %ecx, %r11d             /* keep src[i] for the key test */
    imul %r10d, %ecx            /* src[i] * scale               */
    and $0xFF, %ecx             /* v                            */
    movb %cl, (%rdi,%rax,4)
    movb %cl, 1(%rdi,%rax,4)
    movb %cl, 2(%rdi,%rax,4)
    test %r9d, %r9d             /* has_key ?                    */
    jz  .Lgrey_alpha255
    cmp %r8d, %r11d             /* src[i] == key ?              */
    jne .Lgrey_alpha255
    movb $0, 3(%rdi,%rax,4)
    inc %rax
    jmp .Lgrey_loop
.Lgrey_alpha255:
    movb $0xFF, 3(%rdi,%rax,4)
    inc %rax
    jmp .Lgrey_loop
.Lgrey_done:
    ret
    .size grey_expand, .-grey_expand

/* ------------------------------------------------------------------ */
/* void grey_alpha_expand(unsigned char *dst, const unsigned char *src,
 *                        size_t n)
 *
 * Grey + alpha samples to RGBA.  For i in [0,n): s = i*2;
 * dst[4i..4i+2] = src[s],src[s],src[s]; dst[4i+3] = src[s+1].
 * ------------------------------------------------------------------ */
    .globl grey_alpha_expand
    .type grey_alpha_expand,@function
grey_alpha_expand:
    xor %eax, %eax              /* i = 0                        */
.Lgalpha_loop:
    cmp %rdx, %rax              /* i < n ?                      */
    jae .Lgalpha_done
    movzbl (%rsi,%rax,2), %ecx  /* v = src[2i]                  */
    movzbl 1(%rsi,%rax,2), %r8d /* a = src[2i+1]                */
    movb %cl, (%rdi,%rax,4)
    movb %cl, 1(%rdi,%rax,4)
    movb %cl, 2(%rdi,%rax,4)
    movb %r8b, 3(%rdi,%rax,4)
    inc %rax
    jmp .Lgalpha_loop
.Lgalpha_done:
    ret
    .size grey_alpha_expand, .-grey_alpha_expand

/* ------------------------------------------------------------------ */
/* void rgb_expand(unsigned char *dst, const unsigned char *src,
 *                 size_t n, unsigned int kr, unsigned int kg,
 *                 unsigned int kb, unsigned int has_key)
 *
 * Truecolour samples to RGBA.  For i in [0,n): s = i*3;
 * dst[4i..4i+2] = src[s],src[s+1],src[s+2];
 * dst[4i+3] = 0 if (has_key and src[s]==kr and src[s+1]==kg
 *                   and src[s+2]==kb) else 255.
 * ------------------------------------------------------------------ */
    .globl rgb_expand
    .type rgb_expand,@function
rgb_expand:
    mov %ecx, %r10d             /* kr                           */
    mov %r8d, %r11d             /* kg                           */
    xor %eax, %eax              /* i = 0                        */
.Lrgb_loop:
    cmp %rdx, %rax              /* i < n ?                      */
    jae .Lrgb_done
    lea (%rax,%rax,2), %rcx     /* s = i*3                      */
    movzbl (%rsi,%rcx), %r8d    /* src[3i]                      */
    movb %r8b, (%rdi,%rax,4)
    movzbl 1(%rsi,%rcx), %r8d   /* src[3i+1]                    */
    movb %r8b, 1(%rdi,%rax,4)
    movzbl 2(%rsi,%rcx), %r8d   /* src[3i+2]                    */
    movb %r8b, 2(%rdi,%rax,4)
    mov 8(%rsp), %r8d           /* has_key                      */
    test %r8d, %r8d
    jz  .Lrgb_a255
    movzbl 2(%rsi,%rcx), %r8d   /* b == kb ?                    */
    cmp %r9d, %r8d
    jne .Lrgb_a255
    movzbl 1(%rsi,%rcx), %r8d   /* g == kg ?                    */
    cmp %r11d, %r8d
    jne .Lrgb_a255
    movzbl (%rsi,%rcx), %r8d    /* r == kr ?                    */
    cmp %r10d, %r8d
    jne .Lrgb_a255
    movb $0, 3(%rdi,%rax,4)
    inc %rax
    jmp .Lrgb_loop
.Lrgb_a255:
    movb $0xFF, 3(%rdi,%rax,4)
    inc %rax
    jmp .Lrgb_loop
.Lrgb_done:
    ret
    .size rgb_expand, .-rgb_expand

/* ------------------------------------------------------------------ */
/* void resize_row(unsigned char *dst, const unsigned char *src,
 *                 const unsigned int *xmap, size_t n)
 *
 * One nearest-neighbour output row.  For i in [0,n): copy the 4 bytes
 * at src + xmap[i] (a byte offset) to dst + i*4.
 * ------------------------------------------------------------------ */
    .globl resize_row
    .type resize_row,@function
resize_row:
    xor %eax, %eax              /* i = 0                        */
.Lrr_loop:
    cmp %rcx, %rax              /* i < n ?                      */
    jae .Lrr_done
    mov (%rdx,%rax,4), %r8d     /* xmap[i]                      */
    movzbl (%rsi,%r8), %r9d     /* src[xmap[i]+0]               */
    movb %r9b, (%rdi,%rax,4)
    movzbl 1(%rsi,%r8), %r9d    /* src[xmap[i]+1]               */
    movb %r9b, 1(%rdi,%rax,4)
    movzbl 2(%rsi,%r8), %r9d    /* src[xmap[i]+2]               */
    movb %r9b, 2(%rdi,%rax,4)
    movzbl 3(%rsi,%r8), %r9d    /* src[xmap[i]+3]               */
    movb %r9b, 3(%rdi,%rax,4)
    inc %rax
    jmp .Lrr_loop
.Lrr_done:
    ret
    .size resize_row, .-resize_row

    .section .note.GNU-stack,"",@progbits