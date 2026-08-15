C     The bitstream: raw bits, exp-Golomb, and the CABAC arithmetic
C     decoder.
C
C     Two readers share one buffer and one cursor.  H2U1/H2UN/H2UE/H2SE
C     read the descriptors of clause 7.2, and H2DEC/H2BYP/H2TRM read
C     bins through the arithmetic decoder of 9.3.3.2 -- but a slice
C     starts in the first and switches to the second at
C     cabac_alignment_one_bit, and I_PCM switches back and then forward
C     again, so they cannot each keep their own idea of where they are.
C
C     Overrun is a decode error, not a crash.  Every syntax read past
C     the end of the buffer sets BITERR and returns zero, and every
C     caller that loops checks it; that is the whole defence against a
C     file that says it has 8160 macroblocks and stops after two.

C     One byte of the RBSP, unsigned.  INTEGER*1 is signed in Fortran,
C     so the mask is not decoration.
      INTEGER FUNCTION H2BYT(I)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER I
      H2BYT = IAND(INT(RBSP(I)), 255)
      RETURN
      END

C     One bit for the arithmetic decoder.  Past the end it reads zeroes
C     and says nothing: CABAC legitimately runs up to nine bits beyond
C     the last byte of a slice, and treating that as an error would
C     reject streams that are perfectly well formed.  A stream that is
C     not well formed is stopped by the macroblock count instead.
      INTEGER FUNCTION H2RAW()
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER H2BYT
      EXTERNAL H2BYT
      INTEGER B
      IF (BITP .GE. BITN) THEN
         H2RAW = 0
      ELSE
         B = H2BYT(BITP / 8 + 1)
         H2RAW = IAND(ISHFT(B, -(7 - MOD(BITP, 8))), 1)
         BITP = BITP + 1
      END IF
      RETURN
      END

C     u(1).
      INTEGER FUNCTION H2U1()
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER H2BYT
      EXTERNAL H2BYT
      INTEGER B
      IF (BITP .GE. BITN) THEN
         BITERR = 1
         H2U1 = 0
      ELSE
         B = H2BYT(BITP / 8 + 1)
         H2U1 = IAND(ISHFT(B, -(7 - MOD(BITP, 8))), 1)
         BITP = BITP + 1
      END IF
      RETURN
      END

C     u(n), most significant bit first, for n up to 31.
      INTEGER FUNCTION H2UN(N)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER N, I, V, H2U1
      EXTERNAL H2U1
      V = 0
      DO 10 I = 1, N
         V = IOR(ISHFT(V, 1), H2U1())
   10 CONTINUE
      H2UN = V
      RETURN
      END

C     ue(v): 9.1, the exp-Golomb code.  The leading-zero count is capped
C     at 31 because a run of zeroes long enough to overflow the shift is
C     a corrupt stream, and the cap turns it into an error rather than
C     into an integer whose sign has quietly changed.  31 and not 32:
C     shifting a 32-bit one left by 32 places is not a large number, it
C     is zero.
      INTEGER FUNCTION H2UE()
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER LZ, H2U1, H2UN
      EXTERNAL H2U1, H2UN
      LZ = 0
   10 IF (BITERR .NE. 0) GOTO 20
      IF (H2U1() .NE. 0) GOTO 20
         LZ = LZ + 1
         IF (LZ .GT. 31) THEN
            BITERR = 1
            GOTO 20
         END IF
         GOTO 10
   20 CONTINUE
      IF (BITERR .NE. 0) THEN
         H2UE = 0
      ELSE
         H2UE = ISHFT(1, LZ) - 1 + H2UN(LZ)
      END IF
      RETURN
      END

C     se(v): 9.1.1, the same code read as a signed value.
      INTEGER FUNCTION H2SE()
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER K, H2UE
      EXTERNAL H2UE
      K = H2UE()
      IF (IAND(K, 1) .EQ. 1) THEN
         H2SE = (K + 1) / 2
      ELSE
         H2SE = -(K / 2)
      END IF
      RETURN
      END

C     more_rbsp_data(): true when anything but the stop bit and its
C     padding is left.  BITN has already been shortened to exclude the
C     trailing zero bits and the rbsp_stop_one_bit by H2TRIM, so this is
C     a compare.
      INTEGER FUNCTION H2MORE()
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      IF (BITERR .EQ. 0 .AND. BITP .LT. BITN) THEN
         H2MORE = 1
      ELSE
         H2MORE = 0
      END IF
      RETURN
      END

C     Shorten BITN to the last bit before rbsp_trailing_bits, so that
C     more_rbsp_data() is a comparison rather than a search.  A NAL
C     whose payload is all zeroes has no stop bit at all; that is a
C     malformed NAL and BITN goes to zero, which every reader then
C     treats as overrun.
      SUBROUTINE H2TRIM
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER I, B, K, H2BYT
      EXTERNAL H2BYT
      I = BITN / 8
   10 IF (I .LT. 1) GOTO 40
         B = H2BYT(I)
         IF (B .NE. 0) GOTO 20
         I = I - 1
         GOTO 10
   20 CONTINUE
      DO 30 K = 0, 7
         IF (IAND(ISHFT(B, -K), 1) .EQ. 1) THEN
            BITN = (I - 1) * 8 + (7 - K)
            RETURN
         END IF
   30 CONTINUE
   40 BITN = 0
      RETURN
      END

C     Skip to the next byte boundary.
      SUBROUTINE H2ALGN
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      IF (MOD(BITP, 8) .NE. 0) BITP = BITP + (8 - MOD(BITP, 8))
      RETURN
      END

C     -- the arithmetic decoder ----------------------------------------

C     9.3.1.1: turn the (m, n) pair of each context into a state and an
C     MPS for this slice's QP.  Called once per slice, 1024 times, which
C     is cheap next to everything it makes possible.
C
C     SHIFTA and not ISHFT.  Fortran's ISHFT is a logical shift; more
C     than half the m values in Table 9-12 onwards are negative, and a
C     logical shift turns those into eight-figure positives that the
C     clip below quietly flattens onto the 126 rail.  The clip would
C     hide the bug rather than catch it, and the decoder would produce
C     noise from the first macroblock with nothing pointing back here.
C     Integer division by 16 is wrong for the same values in the other
C     direction: Fortran truncates towards zero where the spec's >>
C     floors.
      SUBROUTINE H2CINI(QP)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER QP, I, Q, PRE, H2UN
      EXTERNAL H2UN
      Q = QP
      IF (Q .LT. 0) Q = 0
      IF (Q .GT. 51) Q = 51
C     CMODEL is the column the slice header chose: 0 for an I slice, and
C     1 + cabac_init_idc for a P one.  It is read from COMMON rather than
C     passed because I_PCM restarts the engine from inside the macroblock
C     layer, and a second parameter to thread through that would be a
C     second place to get it wrong.
      DO 10 I = 0, 1023
         PRE = SHIFTA(CTXM(I,CMODEL) * Q, 4) + CTXN(I,CMODEL)
         IF (PRE .LT. 1) PRE = 1
         IF (PRE .GT. 126) PRE = 126
         IF (PRE .LE. 63) THEN
            CST(I) = 63 - PRE
            CMP(I) = 0
         ELSE
            CST(I) = PRE - 64
            CMP(I) = 1
         END IF
   10 CONTINUE
C     9.3.1.2: the engine starts with a range of 510 and nine bits of
C     the stream, which the caller has already byte-aligned for us.
      CRNG = 510
      COFF = H2UN(9)
      RETURN
      END

C     9.3.3.2.1, DecodeDecision.  This is the loop the whole codec is
C     shaped around: one table lookup, one subtract, one compare, and a
C     renormalisation that shifts in at most seven bits.
      INTEGER FUNCTION H2DEC(C)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER C, S, M, Q, RL, B, H2RAW
      EXTERNAL H2RAW
      S = CST(C)
      M = CMP(C)
      Q = IAND(ISHFT(CRNG, -6), 3)
      RL = RLPS(S, Q)
      CRNG = CRNG - RL
      IF (COFF .LT. CRNG) THEN
         B = M
         CST(C) = TMPS(S)
      ELSE
         COFF = COFF - CRNG
         CRNG = RL
         B = 1 - M
         IF (S .EQ. 0) CMP(C) = 1 - M
         CST(C) = TLPS(S)
      END IF
   10 IF (CRNG .GE. 256) GOTO 20
         CRNG = ISHFT(CRNG, 1)
         COFF = IOR(ISHFT(COFF, 1), H2RAW())
         GOTO 10
   20 H2DEC = B
      RETURN
      END

C     9.3.3.2.3, DecodeBypass: no context, no state, one bit in one bit
C     out.
      INTEGER FUNCTION H2BYP()
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER H2RAW
      EXTERNAL H2RAW
      COFF = IOR(ISHFT(COFF, 1), H2RAW())
      IF (COFF .GE. CRNG) THEN
         COFF = COFF - CRNG
         H2BYP = 1
      ELSE
         H2BYP = 0
      END IF
      RETURN
      END

C     9.3.3.2.4, DecodeTerminate.  Returns 1 for the last bin of the
C     slice (or for I_PCM), and unlike the other two it does not
C     renormalise when it does, because there is nothing after it to
C     renormalise for.
      INTEGER FUNCTION H2TRM()
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER H2RAW
      EXTERNAL H2RAW
      CRNG = CRNG - 2
      IF (COFF .GE. CRNG) THEN
         H2TRM = 1
         RETURN
      END IF
   10 IF (CRNG .GE. 256) GOTO 20
         CRNG = ISHFT(CRNG, 1)
         COFF = IOR(ISHFT(COFF, 1), H2RAW())
         GOTO 10
   20 H2TRM = 0
      RETURN
      END
