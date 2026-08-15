/* x11pack.S -- raw x86-64 X11 framebuffer-packing kernel, no C, no tables
 * of tables.
 *
 * An X server whose TrueColor visual is depth 15 or 16 (or carries channel
 * masks that do not line up with whole bytes) is presented through
 * feetbrowser/x11.py `_pack_generic`: one pixel at a time, each channel
 * scaled through a 256-entry lookup table and the three pieces OR'd into a
 * value that is then written little- or big-endian, with per-row padding on
 * top.  This file is what that loop is, in the language the machine speaks.
 * Loaded straight into executable memory by feetbrowser/asmx11.py via the
 * shared compiler helper in feetbrowser/asmlib.py; no assembler required at
 * runtime.
 *
 * SysV ABI, x86-64:
 *   arg1  = rdi, arg2 = rsi, arg3 = rdx, arg4 = rcx, arg5 = r8, arg6 = r9
 *   (the seventh argument sits at 8(%rsp), above the return address)
 * rax, rcx, rdx, rsi, rdi and r8-r11 are caller-saved, so those are the
 * only registers touched here; rbx, rbp and r12-r15 belong to whoever
 * called us and would have to be saved and restored to be used at all.
 * Position-independent: only local ('.L') labels, so the blob can live
 * anywhere in a PROT_EXEC page.
 */

    .text

/* ------------------------------------------------------------------ */
/* void pack_rows(const unsigned char *src, unsigned char *dst,
 *                size_t width, size_t height,
 *                size_t src_stride, size_t dst_stride,
 *                const unsigned int *red, const unsigned int *green,
 *                const unsigned int *blue, unsigned int pixel_bytes,
 *                unsigned int big_endian)
 *
 * Pack one framebuffer from packed RGB (3 bytes per pixel) into the
 * server's Z-format, exactly as the Python reference does.  For each row
 * (advancing `src` by src_stride and `dst` by dst_stride) and each pixel:
 *   value = red[src[0]] | green[src[1]] | blue[src[2]]
 * and write `pixel_bytes` bytes of value to dst, least significant first
 * when big_endian is 0 and most significant first when it is not.  Each
 * lookup table is 256 32-bit little-endian entries holding the channel
 * value already scaled into its bit position (a channel needs up to four
 * bytes at depth 32), so a pixel is a four-byte read per channel, an OR,
 * and a store, nothing else.
 *
 * Stack frame: five 8-byte locals for what must survive a whole row of
 * pixels, then the six incoming arguments pushed up past them.
 *   8(%rsp)  row src pointer        (inits to arg1)
 *   16(%rsp) row dst pointer        (inits to arg2)
 *   24(%rsp) src_stride
 *   32(%rsp) dst_stride
 *   40(%rsp) rows remaining         (inits to height)
 *   56(%rsp) red table    (was arg7 at 8)
 *   64(%rsp) green table  (was arg8)
 *   72(%rsp) blue table   (was arg9)
 *   80(%rsp) pixel_bytes  (was arg10)
 *   88(%rsp) big_endian   (was arg11)
 * ------------------------------------------------------------------ */
    .globl pack_rows
    .type pack_rows,@function
pack_rows:
    sub $48, %rsp
    mov %rdi, 8(%rsp)               /* row src                       */
    mov %rsi, 16(%rsp)              /* row dst                       */
    mov %r8, 24(%rsp)               /* src_stride                    */
    mov %r9, 32(%rsp)               /* dst_stride                    */
    mov %rcx, 40(%rsp)              /* rows remaining = height       */
    test %rdx, %rdx                 /* width == 0 ?                  */
    jz  .Lpack_done
    cmpq $0, 40(%rsp)               /* height == 0 ?                 */
    jz  .Lpack_done
    cmpl $0, 80(%rsp)               /* pixel_bytes == 0 ? (junk arg) */
    je  .Lpack_done
    cmpl $0, 88(%rsp)               /* big_endian ?                  */
    jne .Lrow_be

/* Little-endian rows: store the low byte first and shift the value down. */
.Lrow_le:
    mov 8(%rsp), %rsi               /* row src                       */
    mov 16(%rsp), %rdi              /* row dst                       */
    mov 56(%rsp), %r9               /* red                           */
    mov 64(%rsp), %r10              /* green                         */
    mov 72(%rsp), %r11              /* blue                          */
    xor %eax, %eax                  /* col = 0                       */
.Lcol_le:
    cmp %rdx, %rax                  /* col < width ?                 */
    jae .Lrow_next
    movzbl (%rsi), %r8d             /* src[0]                        */
    mov (%r9,%r8,4), %ecx           /* value = red[src[0]]           */
    movzbl 1(%rsi), %r8d            /* src[1]                        */
    or  (%r10,%r8,4), %ecx          /* |= green[src[1]]              */
    movzbl 2(%rsi), %r8d            /* src[2]                        */
    or  (%r11,%r8,4), %ecx          /* |= blue[src[2]]               */
    mov 80(%rsp), %r8d              /* pixel_bytes                  */
.Lwrite_le:
    mov %cl, (%rdi)                 /* one low byte of value        */
    shr $8, %ecx
    inc %rdi
    dec %r8d
    jnz .Lwrite_le
    add $3, %rsi                    /* 3 bytes in, pixel_bytes out  */
    inc %rax
    jmp .Lcol_le

/* Big-endian rows: walk dst backwards so the most significant byte lands
 * first, which is exactly int.to_bytes(pixel_bytes, "big"). */
.Lrow_be:
    mov 8(%rsp), %rsi
    mov 16(%rsp), %rdi
    mov 56(%rsp), %r9
    mov 64(%rsp), %r10
    mov 72(%rsp), %r11
    xor %eax, %eax
.Lcol_be:
    cmp %rdx, %rax
    jae .Lrow_next
    movzbl (%rsi), %r8d
    mov (%r9,%r8,4), %ecx
    movzbl 1(%rsi), %r8d
    or  (%r10,%r8,4), %ecx
    movzbl 2(%rsi), %r8d
    or  (%r11,%r8,4), %ecx
    mov 80(%rsp), %r8d              /* pixel_bytes                  */
    lea (%rdi,%r8), %rdi            /* dst + pixel_bytes            */
.Lwrite_be:
    dec %rdi
    mov %cl, (%rdi)                 /* one byte, high to low        */
    shr $8, %ecx
    dec %r8d
    jnz .Lwrite_be
    mov 80(%rsp), %r8d
    add %r8, %rdi                   /* dst + pixel_bytes again      */
    add $3, %rsi
    inc %rax
    jmp .Lcol_be

.Lrow_next:
    mov 8(%rsp), %rsi
    add 24(%rsp), %rsi
    mov %rsi, 8(%rsp)
    mov 16(%rsp), %rdi
    add 32(%rsp), %rdi
    mov %rdi, 16(%rsp)
    decq 40(%rsp)
    jz  .Lpack_done
    cmpl $0, 88(%rsp)               /* which endianness we are in    */
    jne .Lrow_be
    jmp .Lrow_le

.Lpack_done:
    add $48, %rsp
    ret
    .size pack_rows, .-pack_rows

    .section .note.GNU-stack,"",@progbits