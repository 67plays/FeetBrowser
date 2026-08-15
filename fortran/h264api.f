C     The outside of the decoder: Annex B framing, and the handful of
C     C-callable entry points that feetbrowser/h264.py loads with ctypes.
C
C     What decodes today: I and P slices, CABAC, 4:2:0 8-bit, progressive,
C     Baseline, Main and High profile.  Every intra prediction mode, both
C     transform sizes, scaling matrices, quarter-sample motion
C     compensation, weighted prediction, up to four reference frames, and
C     the deblocking filter.
C
C     Refused, each by its own status code so that a report says which:
C     * B, SP and SI slices                                     -43
C     * CAVLC                                                   -31
C     * interlace                                                -4
C     * chroma other than 4:2:0, or more than 8 bits             -3
C     * slice groups                                            -13
C     * pic_order_cnt_type 1                                    -42
C     * long-term references, and the four marking operations
C       and the one reordering operation that name them         -50
C     * more reference frames than MXREF                         -8
C
C     H2DECD keeps its state across calls, because a P slice predicts
C     from the picture the previous call decoded.  Calling h264_reset
C     between two frames of one stream does not slow the decoder down, it
C     breaks it; the caller resets when the stream changes and not
C     otherwise.
C
C     Everything crossing the boundary is an INTEGER or a byte array
C     passed by reference.  Nothing is returned by value, nothing is a
C     struct, and no memory is allocated on this side and freed on the
C     other; the caller hands us a buffer and we fill it or say why we
C     could not.  That is a duller interface than it could be, and it is
C     the reason a mistake in the Python is a wrong number rather than a
C     crash inside a shared library with no debugger attached.

C     One byte of a caller-supplied buffer, unsigned.
      INTEGER FUNCTION H2ABYT(BUF, I)
      IMPLICIT NONE
      INTEGER*1 BUF(*)
      INTEGER I
      H2ABYT = IAND(INT(BUF(I)), 255)
      RETURN
      END

C     Find the next three-byte start code prefix at or after FROM.  Four
C     byte start codes need no special case: the extra zero belongs to
C     the run of trailing zeroes after the previous NAL unit, and those
C     are trimmed off separately.
      SUBROUTINE H2SCAN(BUF, N, FROM, POS)
      IMPLICIT NONE
      INTEGER*1 BUF(*)
      INTEGER N, FROM, POS, I
      POS = -1
      DO 10 I = MAX(FROM, 1), N - 2
         IF (BUF(I) .EQ. 0 .AND. BUF(I+1) .EQ. 0
     +       .AND. BUF(I+2) .EQ. 1) THEN
            POS = I
            RETURN
         END IF
   10 CONTINUE
      RETURN
      END

C     7.4.1.1: copy a NAL unit's payload into RBSP, dropping the
C     emulation prevention bytes.  A 00 00 03 in the coded stream means
C     the 03 was inserted by the encoder so that the two zeroes could not
C     be mistaken for the beginning of a start code; it is not data and
C     the decoder must not see it.
      SUBROUTINE H2LOAD(BUF, S, E, ST)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER*1 BUF(*)
      INTEGER S, E, ST
      INTEGER I, K, Z, B, H2ABYT
      EXTERNAL H2ABYT
      ST = 0
      K = 0
      Z = 0
      DO 10 I = S, E
         B = H2ABYT(BUF, I)
         IF (Z .GE. 2 .AND. B .EQ. 3) THEN
            Z = 0
         ELSE
            K = K + 1
            IF (K .GT. MXBUF) THEN
               ST = -33
               RETURN
            END IF
            RBSP(K) = BUF(I)
            IF (B .EQ. 0) THEN
               Z = Z + 1
            ELSE
               Z = 0
            END IF
         END IF
   10 CONTINUE
      BITN = 8 * K
      BITP = 0
      BITERR = 0
      RETURN
      END

C     Forget every macroblock of the previous picture.  MSLC is the only
C     thing that has to be cleared: availability is answered by comparing
C     it against the current slice number, and slice numbers restart at
C     one for every picture, so a stale zero is a macroblock that does
C     not exist.
      SUBROUTINE H2CLRP
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER I
      DO 10 I = 1, MBN
         MSLC(I) = 0
   10 CONTINUE
      SLID = 0
      RETURN
      END

C     -- the C interface -------------------------------------------------

C     Bump this whenever the meaning of any entry point changes, so that
C     a Python side built against an older library refuses rather than
C     misreads it.
      SUBROUTINE H2VERS(V) BIND(C, NAME='h264_version')
      IMPLICIT NONE
      INTEGER V
      V = 3
      RETURN
      END

C     Throw away all decoder state, including the parameter sets.
      SUBROUTINE H2RSET() BIND(C, NAME='h264_reset')
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      CALL H2INIT
      SPSOK = 0
      PPSOK = 0
      MBW = 0
      MBH = 0
      MBN = 0
      OUTW = 0
      OUTH = 0
      SLID = 0
      BITERR = 0
      CALL H2DCLR
      CALL H2PCLR
      RETURN
      END

C     The cropped size of the last picture decoded, or zero if there has
C     not been one.
      SUBROUTINE H2DIMS(W, H) BIND(C, NAME='h264_dims')
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER W, H
      W = 0
      H = 0
      IF (SPSOK .NE. 0) THEN
         W = OUTW
         H = OUTH
      END IF
      RETURN
      END

C     Decode one access unit of Annex B bytes into the picture planes.
C     Parameter sets carry across calls, which is what lets a caller hand
C     us the SPS and PPS once and then a frame at a time.
      SUBROUTINE H2DECD(BUF, N, ST) BIND(C, NAME='h264_decode')
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER*1 BUF(*)
      INTEGER N, ST
      INTEGER P, S, E, NT, NR, GOTPIC, NNAL, H2ABYT
      EXTERNAL H2ABYT

      ST = 0
      GOTPIC = 0
      NNAL = 0
      P = 1

   10 CONTINUE
      CALL H2SCAN(BUF, N, P, S)
      IF (S .LT. 0) GOTO 90
      S = S + 3
      CALL H2SCAN(BUF, N, S, E)
      IF (E .LT. 0) THEN
         E = N
      ELSE
         E = E - 1
      END IF
C     Trailing zero bytes belong to the framing, not to the NAL unit.
C     Every well formed NAL ends in an rbsp_stop_one_bit, so its last
C     byte is never zero and this trim can never eat real payload.
   20 IF (E .GE. S) THEN
         IF (BUF(E) .EQ. 0) THEN
            E = E - 1
            GOTO 20
         END IF
      END IF
      NNAL = NNAL + 1
      IF (NNAL .GT. 4096) GOTO 90
      IF (E .LT. S) GOTO 80

      NT = IAND(H2ABYT(BUF, S), 31)
      NR = ISHFT(IAND(H2ABYT(BUF, S), 96), -5)
      IF (NT .EQ. 7 .OR. NT .EQ. 8 .OR. NT .EQ. 1 .OR. NT .EQ. 5) THEN
         CALL H2LOAD(BUF, S + 1, E, ST)
         IF (ST .NE. 0) RETURN
C     Parameter sets get their trailing bits trimmed; slices must not.
C     The picture parameter set ends in an optional block guarded by
C     more_rbsp_data(), and without the trim that predicate mistakes the
C     rbsp_stop_one_bit for payload: a Baseline or Main PPS, which has no
C     such block, then reads a transform_size_8x8 flag off the end of
C     itself and fails.
C
C     A CABAC slice is the opposite case.  9.3.4.6 flushes the encoder by
C     writing the low bits of codILow and then a one, and that one is the
C     rbsp_stop_one_bit -- the stop bit is the last bit of the arithmetic
C     codeword, not framing after it.  Trimming it away feeds the
C     decoder a zero where the encoder wrote a one, and the final
C     end_of_slice_flag reads as "more data" on the slices where the
C     bit happened to matter.
         IF (NT .EQ. 7 .OR. NT .EQ. 8) CALL H2TRIM
      END IF

      IF (NT .EQ. 7) THEN
         CALL H2SPSP(ST)
         IF (ST .NE. 0) RETURN
      ELSE IF (NT .EQ. 8) THEN
         CALL H2PPSP(ST)
         IF (ST .NE. 0) RETURN
      ELSE IF (NT .EQ. 1 .OR. NT .EQ. 5) THEN
         IF (SPSOK .EQ. 0 .OR. PPSOK .EQ. 0) THEN
            ST = -30
            RETURN
         END IF
         IF (GOTPIC .EQ. 0) THEN
            CALL H2CLRP
            GOTPIC = 1
         END IF
         SLID = SLID + 1
         CALL H2SHDR(NT, NR, ST)
         IF (ST .NE. 0) RETURN
         IF (SLID .EQ. 1) THEN
C     8.2.1 once per picture, not once per slice: every slice of a
C     picture carries the same frame number and order count, and the
C     count's carried-forward high bits would be advanced twice by a
C     second slice that recomputed them.
            CALL H2CPOC
            CURID = NXTID
            NXTID = NXTID + 1
         END IF
         CALL H2WSCL
         IF (ECMODE .EQ. 0) THEN
C     CAVLC.  This decoder reads CABAC only; saying so here is better
C     than decoding the slice header and then producing a grey picture.
            ST = -31
            RETURN
         END IF
         RL0N = 0
         IF (SLTYPE .EQ. 0) THEN
C     8.2.4, per slice and not per picture: two P slices of one picture
C     may reorder the same set of references differently.
            CALL H2RLST(ST)
            IF (ST .NE. 0) RETURN
         END IF
         CALL H2ALGN
         CALL H2CINI(SLQPY)
         CALL H2SLIC(ST)
         IF (ST .NE. 0) RETURN
      END IF

   80 P = E + 1
      GOTO 10

   90 CONTINUE
      IF (GOTPIC .EQ. 0) THEN
         ST = -32
         RETURN
      END IF
      CALL H2DBLK
C     8.2.5 after the filter, because the picture later pictures predict
C     from is the filtered one.
      CALL H2MARK(ST)
      IF (ST .NE. 0) RETURN
      ST = 0
      RETURN
      END

C     Copy the decoded picture out as planar I420, cropped to the size
C     the SPS asked for.  The caller owns the buffer and tells us how big
C     it is; a buffer that is too small is an error and not a partial
C     copy, because a partial picture is worse than no picture.
      SUBROUTINE H2I420(DST, CAP, ST) BIND(C, NAME='h264_i420')
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER*1 DST(*)
      INTEGER CAP, ST
      INTEGER NEED, X, Y, K, SC, CW, CH, V
      ST = 0
      IF (SPSOK .EQ. 0 .OR. OUTW .LT. 1 .OR. OUTH .LT. 1) THEN
         ST = -1
         RETURN
      END IF
      CW = OUTW / 2
      CH = OUTH / 2
      NEED = OUTW * OUTH + 2 * CW * CH
      IF (CAP .LT. NEED) THEN
         ST = -2
         RETURN
      END IF
      SC = MXW / 2
      K = 0
      DO 20 Y = 0, OUTH - 1
         DO 10 X = 0, OUTW - 1
            K = K + 1
            V = PY((CRPT + Y) * MXW + CRPL + X + 1)
            IF (V .GT. 127) V = V - 256
            DST(K) = V
   10    CONTINUE
   20 CONTINUE
      DO 40 Y = 0, CH - 1
         DO 30 X = 0, CW - 1
            K = K + 1
            V = PU((CRPT / 2 + Y) * SC + CRPL / 2 + X + 1)
            IF (V .GT. 127) V = V - 256
            DST(K) = V
   30    CONTINUE
   40 CONTINUE
      DO 60 Y = 0, CH - 1
         DO 50 X = 0, CW - 1
            K = K + 1
            V = PV((CRPT / 2 + Y) * SC + CRPL / 2 + X + 1)
            IF (V .GT. 127) V = V - 256
            DST(K) = V
   50    CONTINUE
   60 CONTINUE
      ST = NEED
      RETURN
      END

C     The same picture as RGBA, which is the only form the rest of the
C     browser has any use for.
C
C     This lives here rather than in Python because it is the one part of
C     the job that is pure arithmetic over every sample: a 352x288 frame
C     is a hundred thousand pixels, and a Python loop over them costs
C     several times what decoding the frame did.  Writing it twice would
C     be a shame; writing it in the slower of the two languages would be
C     a waste of the faster one.
C
C     BT.601 studio swing, the matrix in ITU-R BT.601 and Table E-3's
C     default when a stream says nothing, with the coefficients scaled by
C     256.  Chroma is upsampled by repetition rather than interpolation:
C     4:2:0 chroma siting is a display question, the answer differs
C     between the specification and every player that ships, and picking
C     the simple one keeps this reversible.
      SUBROUTINE H2RGBA(DST, CAP, ST) BIND(C, NAME='h264_rgba')
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER*1 DST(*)
      INTEGER CAP, ST
      INTEGER NEED, X, Y, K, SC, CI, YV, R, G, B, CR, CB, LY
      ST = 0
      IF (SPSOK .EQ. 0 .OR. OUTW .LT. 1 .OR. OUTH .LT. 1) THEN
         ST = -1
         RETURN
      END IF
      NEED = OUTW * OUTH * 4
      IF (CAP .LT. NEED) THEN
         ST = -2
         RETURN
      END IF
      SC = MXW / 2
      K = 0
      DO 20 Y = 0, OUTH - 1
         DO 10 X = 0, OUTW - 1
            YV = PY((CRPT + Y) * MXW + CRPL + X + 1)
            CI = (CRPT + Y) / 2 * SC + (CRPL + X) / 2 + 1
            LY = 298 * (YV - 16)
            CB = PU(CI) - 128
            CR = PV(CI) - 128
            R = SHIFTA(LY + 409 * CR + 128, 8)
            G = SHIFTA(LY - 100 * CB - 208 * CR + 128, 8)
            B = SHIFTA(LY + 516 * CB + 128, 8)
            R = MAX(0, MIN(255, R))
            G = MAX(0, MIN(255, G))
            B = MAX(0, MIN(255, B))
            IF (R .GT. 127) R = R - 256
            IF (G .GT. 127) G = G - 256
            IF (B .GT. 127) B = B - 256
            DST(K + 1) = R
            DST(K + 2) = G
            DST(K + 3) = B
            DST(K + 4) = -1
            K = K + 4
   10    CONTINUE
   20 CONTINUE
      ST = NEED
      RETURN
      END
