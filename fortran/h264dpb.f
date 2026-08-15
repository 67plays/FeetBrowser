C     The decoded picture buffer: picture order counts (8.2.1), reference
C     picture lists (8.2.4) and reference picture marking (8.2.5).
C
C     This is the bookkeeping that turns a decoder of pictures into a
C     decoder of video.  None of it touches a sample except at the very
C     end, where a finished picture is copied into a slot; all of it is
C     about which of the four slots a reference index means, and which
C     slot the next picture is allowed to take.
C
C     Four slots, and MXREF says why.  A stream that declares more
C     reference frames than that is refused in H2SPSP rather than decoded
C     against whichever picture happened to survive, because a decoder
C     that silently substitutes a reference produces a picture that looks
C     almost right, which is the worst thing a decoder can produce.
C
C     Field coding is not here at all.  frame_mbs_only_flag is checked in
C     the sequence parameter set, so every picture is a frame, PicNum is
C     FrameNumWrap rather than twice it plus one, and the four "if this
C     is a field" branches of 8.2.4.1 collapse.

C     Empty the buffer.  Called when the decoder is reset and at every
C     IDR picture, which is 8.2.5.1: an IDR says that nothing before it
C     is ever referred to again.
      SUBROUTINE H2DCLR
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER K
      DO 10 K = 1, MXREF
         DPUSE(K) = 0
         DPFN(K) = 0
         DPFNW(K) = 0
         DPPOC(K) = 0
         DPID(K) = -1
   10 CONTINUE
      RL0N = 0
      RETURN
      END

C     Everything the picture order count carries from one picture to the
C     next, back to its start-of-stream value.
      SUBROUTINE H2PCLR
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      PRVMSB = 0
      PRVLSB = 0
      PRVFNO = 0
      PRVFN = 0
      CURPOC = 0
      CURID = 0
      NXTID = 1
      RETURN
      END

C     8.2.1, the picture order count of the picture now being decoded.
C
C     Nothing in a P slice reads it -- 8.2.4.2.1 orders a P list by
C     FrameNumWrap and not by POC, and we hand pictures out in decoding
C     order because there is no reordering without B slices.  It is
C     computed and stored anyway because it is the number that says which
C     picture is which when there is one, and because a decoder that
C     computes it only when it needs it computes it wrong the first time
C     it needs it.
      SUBROUTINE H2CPOC
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER MSB, FNO
      IF (POCTYP .EQ. 0) THEN
C     8.2.1.1.  The count arrives as its low bits only, and the high bits
C     are carried forward from the last reference picture; a lsb that has
C     dropped by more than half its range has wrapped.
         IF (IDRF .NE. 0) THEN
            PRVMSB = 0
            PRVLSB = 0
         END IF
         IF (POCLSB .LT. PRVLSB .AND.
     +       (PRVLSB - POCLSB) .GE. MXPOCL / 2) THEN
            MSB = PRVMSB + MXPOCL
         ELSE IF (POCLSB .GT. PRVLSB .AND.
     +            (POCLSB - PRVLSB) .GT. MXPOCL / 2) THEN
            MSB = PRVMSB - MXPOCL
         ELSE
            MSB = PRVMSB
         END IF
         CURPOC = MSB + POCLSB
C     Only a reference picture moves the carry forward, which is what
C     lets a stream drop a non-reference picture without every count
C     after it changing.
         IF (NALRI .NE. 0) THEN
            PRVMSB = MSB
            PRVLSB = POCLSB
         END IF
      ELSE
C     8.2.1.3, pic_order_cnt_type 2: the count is twice the frame number,
C     which is only expressible when decoding order and output order are
C     the same.  Streams that say 2 are saying they have no B slices.
         IF (IDRF .NE. 0) THEN
            FNO = 0
         ELSE IF (FRNUM .LT. PRVFN) THEN
            FNO = PRVFNO + MXFNUM
         ELSE
            FNO = PRVFNO
         END IF
         CURPOC = 2 * (FNO + FRNUM)
         IF (NALRI .EQ. 0) CURPOC = CURPOC - 1
         PRVFNO = FNO
         PRVFN = FRNUM
      END IF
      RETURN
      END

C     8.2.4.1: FrameNumWrap for every reference in the buffer, which is
C     the frame number of a picture from before the last wrap of
C     frame_num expressed as a negative number, so that "most recent"
C     is "largest" without a modular comparison.
      SUBROUTINE H2FNW
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER K
      DO 10 K = 1, MXREF
         IF (DPUSE(K) .NE. 0) THEN
            IF (DPFN(K) .GT. FRNUM) THEN
               DPFNW(K) = DPFN(K) - MXFNUM
            ELSE
               DPFNW(K) = DPFN(K)
            END IF
         END IF
   10 CONTINUE
      RETURN
      END

C     8.2.4.2.1 and 8.2.4.3.1: the reference picture list this P slice
C     will index, initialised by descending FrameNumWrap and then put
C     through whatever reordering the slice header asked for.
      SUBROUTINE H2RLST(ST)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER ST
      INTEGER K, I, J, N, BEST, BK, PRED, NOWRAP, PICX, IDX, SLOT
      INTEGER TMP(0:31), M
      ST = 0
      CALL H2FNW
      N = 0
      DO 20 I = 1, MXREF
         BEST = 0
         BK = 0
         DO 10 K = 1, MXREF
            IF (DPUSE(K) .GT. 0) THEN
               IF (BK .EQ. 0) THEN
                  BK = K
                  BEST = DPFNW(K)
               ELSE IF (DPFNW(K) .GT. BEST) THEN
                  BK = K
                  BEST = DPFNW(K)
               END IF
            END IF
   10    CONTINUE
         IF (BK .EQ. 0) GOTO 30
C     Sorting by picking the largest and then hiding it: MXREF is four,
C     so a real sort would be more code than the loop it replaced.
         RL0(N) = BK
         DPUSE(BK) = -DPUSE(BK)
         N = N + 1
   20 CONTINUE
   30 CONTINUE
      DO 40 K = 1, MXREF
         IF (DPUSE(K) .LT. 0) DPUSE(K) = -DPUSE(K)
   40 CONTINUE
      IF (N .EQ. 0) THEN
C     A P slice with nothing to predict from: a stream joined after its
C     IDR, or one whose IDR we refused.  Every macroblock in it would
C     read from a picture that does not exist.
         ST = -51
         RETURN
      END IF
C     A list shorter than the slice says it is repeats its last entry.
C     7.4.3 forbids a stream from doing this, so the only thing that
C     reaches here is a damaged one, and repeating a picture keeps the
C     indices in range while the picture stays wrong in the way the
C     damage made it wrong.
      DO 50 I = N, NREF0 - 1
         RL0(I) = RL0(N - 1)
   50 CONTINUE
      RL0N = NREF0

      IF (NRMOP .LE. 0) RETURN
      PRED = FRNUM
      IDX = 0
      DO 90 J = 1, NRMOP
         IF (RMOP(J) .EQ. 0) THEN
            NOWRAP = PRED - RMVAL(J)
            IF (NOWRAP .LT. 0) NOWRAP = NOWRAP + MXFNUM
         ELSE
            NOWRAP = PRED + RMVAL(J)
            IF (NOWRAP .GE. MXFNUM) NOWRAP = NOWRAP - MXFNUM
         END IF
         PRED = NOWRAP
         PICX = NOWRAP
         IF (PICX .GT. FRNUM) PICX = PICX - MXFNUM
         SLOT = 0
         DO 60 K = 1, MXREF
            IF (DPUSE(K) .NE. 0 .AND. DPFNW(K) .EQ. PICX) SLOT = K
   60    CONTINUE
         IF (SLOT .EQ. 0) THEN
            ST = -52
            RETURN
         END IF
C     8-38: put the named picture at the current index, push everything
C     from there down one place, and drop the copy of it that was
C     already somewhere further down the list.
         DO 70 I = 0, RL0N - 1
            TMP(I) = RL0(I)
   70    CONTINUE
         IF (IDX .GT. RL0N - 1) THEN
            ST = -52
            RETURN
         END IF
         RL0(IDX) = SLOT
         M = IDX + 1
         DO 80 I = IDX, RL0N - 1
            IF (TMP(I) .NE. SLOT) THEN
               IF (M .LE. RL0N - 1) RL0(M) = TMP(I)
               M = M + 1
            END IF
   80    CONTINUE
         IDX = IDX + 1
   90 CONTINUE
      RETURN
      END

C     Copy the finished picture into slot K.  This happens after the
C     deblocking filter has run, because 8.7 filters the picture that
C     later pictures predict from and not just the one on screen; a
C     decoder that stored the unfiltered samples would drift a little
C     further from the encoder with every frame.
      SUBROUTINE H2STOR(K)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER K, X, Y, R, SC, V
      SC = MXW / 2
      DO 20 Y = 0, PICH - 1
         R = Y * MXW
         DO 10 X = 0, PICW - 1
            V = PY(R + X + 1)
            IF (V .GT. 127) V = V - 256
            DPY(R + X + 1, K) = V
   10    CONTINUE
   20 CONTINUE
      DO 40 Y = 0, PICH / 2 - 1
         R = Y * SC
         DO 30 X = 0, PICW / 2 - 1
            V = PU(R + X + 1)
            IF (V .GT. 127) V = V - 256
            DPU(R + X + 1, K) = V
            V = PV(R + X + 1)
            IF (V .GT. 127) V = V - 256
            DPV(R + X + 1, K) = V
   30    CONTINUE
   40 CONTINUE
      DPFN(K) = FRNUM
      DPPOC(K) = CURPOC
      DPID(K) = CURID
      DPUSE(K) = 1
      RETURN
      END

C     8.2.5, marking: what the picture just decoded does to the buffer,
C     and where it goes in it.
      SUBROUTINE H2MARK(ST)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER ST
      INTEGER J, K, N, FREE, WORST, WK, PICX
      ST = 0
      IF (NALRI .EQ. 0) RETURN
      IF (IDRF .NE. 0) THEN
C     8.2.5.1: an IDR empties the buffer and is the only picture in it.
         CALL H2DCLR
         CALL H2STOR(1)
         RETURN
      END IF
      CALL H2FNW
      IF (ADAPTF .NE. 0) THEN
C     8.2.5.4.  Only the two operations that a stream without long-term
C     references can use are here; the other four are refused in the
C     slice header, where the refusal can still name itself.
         DO 20 J = 1, NAMOP
            IF (AMOP(J) .EQ. 1) THEN
               PICX = FRNUM - AMV(J)
               DO 10 K = 1, MXREF
                  IF (DPUSE(K) .NE. 0 .AND. DPFNW(K) .EQ. PICX)
     +               DPUSE(K) = 0
   10          CONTINUE
            ELSE IF (AMOP(J) .EQ. 5) THEN
C     Memory management control operation 5 says "forget everything and
C     start counting again", which also resets this picture's own frame
C     number and order count -- it becomes the origin the next pictures
C     are measured from.
               CALL H2DCLR
               FRNUM = 0
               CURPOC = 0
               PRVMSB = 0
               PRVLSB = 0
               PRVFNO = 0
               PRVFN = 0
            END IF
   20    CONTINUE
      ELSE
C     8.2.5.3, the sliding window: when the buffer is as full as the
C     sequence said it would get, the oldest picture in it leaves.
         N = 0
         WK = 0
         WORST = 0
         DO 30 K = 1, MXREF
            IF (DPUSE(K) .NE. 0) THEN
               N = N + 1
               IF (WK .EQ. 0) THEN
                  WK = K
                  WORST = DPFNW(K)
               ELSE IF (DPFNW(K) .LT. WORST) THEN
                  WK = K
                  WORST = DPFNW(K)
               END IF
            END IF
   30    CONTINUE
         IF (N .GE. MAX(NUMREF, 1) .AND. WK .NE. 0) DPUSE(WK) = 0
      END IF
      FREE = 0
      DO 40 K = MXREF, 1, -1
         IF (DPUSE(K) .EQ. 0) FREE = K
   40 CONTINUE
      IF (FREE .EQ. 0) THEN
C     Every slot still in use with a new picture to store.  A conforming
C     stream cannot reach this, because the sliding window above ran
C     first; a stream whose marking commands emptied nothing can.
         ST = -53
         RETURN
      END IF
      CALL H2STOR(FREE)
      RETURN
      END
