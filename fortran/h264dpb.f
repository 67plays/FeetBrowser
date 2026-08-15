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
      RL1N = 0
      COLSL = 0
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
C     FrameNumWrap and not by POC.  A B slice reads little else: both of
C     its lists are ordered by it (8.2.4.2.3), temporal direct scales its
C     vectors by differences of it (8.4.1.2.3), the implicit bipred
C     weights are derived from it (8.4.2.3.1), and the container hands
C     pictures out in the order it gives.
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
      INTEGER K, I, N, BEST, BK
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
      CALL H2RMOD(1, RL0, RL0N, ST)
      RETURN
      END

C     8.2.4.2.3 and 8.2.4.2.4: the two reference lists of a B slice.
C
C     A B list is ordered by picture order count and not by frame number,
C     because the point of it is "the nearest picture before me" and "the
C     nearest picture after me", which is a statement about display order.
C     List 0 runs backwards through the past and then forwards through
C     the future; list 1 does the opposite.  When both come out identical
C     -- which happens whenever there is exactly one picture on each side
C     -- 8.2.4.2.3 swaps the first two entries of list 1, so that a
C     macroblock indexing 0 in each list still reaches two pictures.
C
C     The swap is on the initialised list, before it is cut to
C     num_ref_idx_l1_active, because the comparison in the standard is
C     over the initialisation and not over the slice's window into it.
      SUBROUTINE H2BLST(ST)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER ST
      INTEGER K, I, N, SAME, T
      ST = 0
      CALL H2FNW
      N = 0
      CALL H2POCS(1, -1, RL0, N)
      CALL H2POCS(0, 1, RL0, N)
      IF (N .EQ. 0) THEN
         ST = -51
         RETURN
      END IF
      RL0N = N
      N = 0
      CALL H2POCS(0, 1, RL1, N)
      CALL H2POCS(1, -1, RL1, N)
      RL1N = N
      IF (RL1N .GT. 1) THEN
         SAME = 1
         DO 10 I = 0, RL1N - 1
            IF (RL0(I) .NE. RL1(I)) SAME = 0
   10    CONTINUE
         IF (RL0N .NE. RL1N) SAME = 0
         IF (SAME .NE. 0) THEN
            T = RL1(0)
            RL1(0) = RL1(1)
            RL1(1) = T
         END IF
      END IF
      DO 20 I = RL0N, NREF0 - 1
         RL0(I) = RL0(RL0N - 1)
   20 CONTINUE
      DO 30 I = RL1N, NREF1 - 1
         RL1(I) = RL1(RL1N - 1)
   30 CONTINUE
      RL0N = NREF0
      RL1N = NREF1
      CALL H2RMOD(1, RL0, RL0N, ST)
      IF (ST .NE. 0) RETURN
      CALL H2RMOD(2, RL1, RL1N, ST)
      IF (ST .NE. 0) RETURN
C     RefPicList1[0] is the colocated picture of 8.4.1.2.1, and it is the
C     modified list that names it: a slice that reordered list 1 moved
C     the picture its direct macroblocks read their motion out of.
      COLSL = RL1(0)
      RETURN
      END

C     Append the reference slots whose POC is on one side of the current
C     picture's, in one direction.  SIDE 1 means "before me", 0 means
C     "after me"; DIR -1 sorts descending and 1 ascending.  Between them
C     the four calls of H2BLST spell out both halves of both lists.
      SUBROUTINE H2POCS(SIDE, DIR, LST, N)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER SIDE, DIR, N
      INTEGER LST(0:31)
      INTEGER K, I, BK, BEST, WANT
      DO 20 I = 1, MXREF
         BK = 0
         BEST = 0
         DO 10 K = 1, MXREF
            IF (DPUSE(K) .GT. 0) THEN
               WANT = 0
               IF (SIDE .EQ. 1 .AND. DPPOC(K) .LT. CURPOC) WANT = 1
               IF (SIDE .EQ. 0 .AND. DPPOC(K) .GT. CURPOC) WANT = 1
               IF (WANT .NE. 0) THEN
                  IF (BK .EQ. 0) THEN
                     BK = K
                     BEST = DPPOC(K)
                  ELSE IF (DIR .LT. 0 .AND. DPPOC(K) .GT. BEST) THEN
                     BK = K
                     BEST = DPPOC(K)
                  ELSE IF (DIR .GT. 0 .AND. DPPOC(K) .LT. BEST) THEN
                     BK = K
                     BEST = DPPOC(K)
                  END IF
               END IF
            END IF
   10    CONTINUE
         IF (BK .EQ. 0) GOTO 30
         IF (N .GT. 31) GOTO 30
         LST(N) = BK
         N = N + 1
         DPUSE(BK) = -DPUSE(BK)
   20 CONTINUE
   30 CONTINUE
      DO 40 K = 1, MXREF
         IF (DPUSE(K) .LT. 0) DPUSE(K) = -DPUSE(K)
   40 CONTINUE
      RETURN
      END

C     8.2.4.3.1, the modification commands the slice header recorded.
C     Shared by both lists and by both slice types, because the
C     derivation is written once in the standard and differs only in
C     which list it is handed.
      SUBROUTINE H2RMOD(L, LST, LN, ST)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER L, LN, ST
      INTEGER LST(0:31)
      INTEGER K, I, J, PRED, NOWRAP, PICX, IDX, SLOT, M
      INTEGER TMP(0:31)
      ST = 0
      IF (NRMOP(L) .LE. 0) RETURN
      PRED = FRNUM
      IDX = 0
      DO 90 J = 1, NRMOP(L)
         IF (RMOP(J,L) .EQ. 0) THEN
            NOWRAP = PRED - RMVAL(J,L)
            IF (NOWRAP .LT. 0) NOWRAP = NOWRAP + MXFNUM
         ELSE
            NOWRAP = PRED + RMVAL(J,L)
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
         DO 70 I = 0, LN - 1
            TMP(I) = LST(I)
   70    CONTINUE
         IF (IDX .GT. LN - 1) THEN
            ST = -52
            RETURN
         END IF
         LST(IDX) = SLOT
         M = IDX + 1
         DO 80 I = IDX, LN - 1
            IF (TMP(I) .NE. SLOT) THEN
               IF (M .LE. LN - 1) LST(M) = TMP(I)
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
      INTEGER K, X, Y, R, SC, V, MB, L, B, Q, RI
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
C     The motion field goes with the samples.  8.4.1.2.1 reads it out of
C     this picture again later, when some future B slice makes it
C     RefPicList1[0], and by then the working arrays have been overwritten
C     several times over.  The vectors are clamped into INTEGER*2 rather
C     than truncated into it: Annex A bounds them well inside the range,
C     so the clamp only ever fires on a stream that was already lying,
C     and a clamped vector predicts from the wrong place where a wrapped
C     one predicts from the wrong place *and* reads out of bounds.
      DO 70 MB = 1, MBN
         CLINT(MB, K) = MINT(MB)
         DO 60 L = 1, 2
            DO 50 B = 1, 16
               V = MMVX(B, L, MB)
               IF (V .LT. -32768) V = -32768
               IF (V .GT. 32767) V = 32767
               CLMVX(B, L, MB, K) = V
               V = MMVY(B, L, MB)
               IF (V .LT. -32768) V = -32768
               IF (V .GT. 32767) V = 32767
               CLMVY(B, L, MB, K) = V
   50       CONTINUE
            DO 55 Q = 1, 4
               RI = MREF(Q, L, MB)
               IF (RI .LT. -1) RI = -1
               IF (RI .GT. 31) RI = 31
               CLREF(Q, L, MB, K) = RI
               CLPIC(Q, L, MB, K) = MRPI(Q, L, MB)
   55       CONTINUE
   60    CONTINUE
   70 CONTINUE
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
