C     The Huffman coded spectrum, and turning it back into numbers.
C
C     A granule's coefficients come in three parts.  The big values are
C     pairs, coded with one of 32 tables chosen per region of the
C     spectrum, where the largest table entry means "and now some more
C     bits"; the count1 region is quadruples of values that are only ever
C     -1, 0 or 1, coded with one of two tables; and the rest of the
C     granule is zero and is not coded at all.  The decoder has to find
C     the boundary between the second and third parts by running out of
C     bits, which is why part2_3_length matters as much as it does.

C     The big values and the count1 quadruples of one granule and
C     channel, into IS in the order the bitstream sends them.
      SUBROUTINE BLSPEC(GR, CH, GEND)
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER GR, CH, GEND
      INTEGER I, K, T, TR, N, R0, R1, IDX, V, XL, YL, X, Y, LB
      INTEGER BLHCW, BLUN, BLU1
      EXTERNAL BLHCW, BLUN, BLU1

      DO 10 I = 0, MXSMP - 1
         IS(I, CH) = 0
   10 CONTINUE

      CALL BLRGN(GR, CH, R0, R1)
      N = BIGVAL(GR, CH) * 2
      I = 0
   20 IF (I .GE. N) GOTO 40
         IF (I .LT. R0) THEN
            K = 1
         ELSE IF (I .LT. R1) THEN
            K = 2
         ELSE
            K = 3
         END IF
         T = TBLSEL(K, GR, CH)
         IF (T .LT. 0 .OR. T .GE. MXTBL) THEN
            BERR = 1
            GOTO 40
         END IF
         UTBL(T) = UTBL(T) + 1
C        Table 0 codes nothing: the pair is a pair of zeros and not one
C        bit is spent on it.  Tables 4 and 14 do not exist, and a stream
C        that selects one is corrupt rather than silent.
         IF (T .EQ. 0) THEN
            IS(I, CH) = 0
            IS(I + 1, CH) = 0
            I = I + 2
            GOTO 20
         END IF
         TR = HWHICH(T)
         IF (TR .LT. 0) THEN
            BERR = 1
            GOTO 40
         END IF
         XL = HXLEN(TR)
         YL = HYLEN(TR)
         LB = HLINB(T)
         IDX = BLHCW(TR)
         IF (BERR .NE. 0) GOTO 40
         X = IDX / YL
         Y = MOD(IDX, YL)
C        The top row and column of a table with linbits mean "at least
C        15", and the remainder follows as a plain field.  Without
C        linbits 15 is simply 15.
         IF (X .EQ. XL - 1 .AND. LB .GT. 0) X = X + BLUN(LB)
         IF (X .NE. 0) THEN
            IF (BLU1() .NE. 0) X = -X
         END IF
         IF (Y .EQ. YL - 1 .AND. LB .GT. 0) Y = Y + BLUN(LB)
         IF (Y .NE. 0) THEN
            IF (BLU1() .NE. 0) Y = -Y
         END IF
         IF (IABS(X) .GT. MXQNT .OR. IABS(Y) .GT. MXQNT) THEN
            BERR = 1
            GOTO 40
         END IF
         IS(I, CH) = X
         IS(I + 1, CH) = Y
         I = I + 2
         IF (BERR .NE. 0) GOTO 40
         GOTO 20
   40 CONTINUE

C     The count1 region runs until the granule's bits are used up.  There
C     is no count of quadruples anywhere in the bitstream: the encoder
C     wrote as many as fitted, and the decoder stops when the next one
C     would start past the end.  A quadruple that would run off the end
C     of the granule is not decoded at all, which is why the test is
C     before the codeword and not after it.
      IDX = I
      T = 16 + CNT1TS(GR, CH)
   50 IF (IDX .GT. MXSMP - 4) GOTO 60
         IF (BPOS .GE. GEND) GOTO 60
         IF (BERR .NE. 0) GOTO 60
         UCT1(CNT1TS(GR, CH)) = UCT1(CNT1TS(GR, CH)) + 1
         V = BLHCW(T)
         IF (BERR .NE. 0) GOTO 60
         DO 55 K = 0, 3
            X = IAND(ISHFT(V, -(3 - K)), 1)
            IF (X .NE. 0) THEN
               IF (BLU1() .NE. 0) X = -X
            END IF
            IS(IDX + K, CH) = X
   55    CONTINUE
         IDX = IDX + 4
         GOTO 50
   60 CONTINUE

C     Where the coded part of the granule ends.  Everything above it is
C     zero, and the stereo tools need to know because intensity stereo
C     only applies above the last band the second channel actually coded.
      IF (IDX .GT. MXSMP) IDX = MXSMP
      RZERO(CH) = IDX
      DO 70 I = IDX, MXSMP - 1
         IS(I, CH) = 0
   70 CONTINUE
      RETURN
      END

C     Requantisation, ISO/IEC 11172-3 2.4.3.4.
C
C     Every coefficient is its magnitude to the four thirds, signed, and
C     scaled by two to a quarter power that the global gain, the
C     scalefactor of its band and -- for a short block -- the gain of its
C     window between them decide.  The exponent is an integer count of
C     quarter powers all the way through, which is what keeps this exact
C     enough to compare against the formula rather than approximately
C     right: nothing here is a float until the very last multiply.
C
C     The coefficients stay in the order the bitstream sent them.  A
C     short block's are reordered later, because the stereo tools want
C     them in this order and the transform wants them in the other.
      SUBROUTINE BLDEQ(GR, CH)
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER GR, CH
      INTEGER I, J, L, K, W, E, GAIN, SH, LEN, PRE
      INTEGER GW(0:2)
      DOUBLE PRECISION V

      DO 10 I = 0, MXSMP - 1
         XRQ(I, CH) = 0.0D0
   10 CONTINUE

      GAIN = GGAIN(GR, CH) - 210
      SH = SFSCAL(GR, CH) + 1
      J = 0

      DO 30 I = 0, LONGE(CH) - 1
         LEN = SBL(I + 1, HSFI) - SBL(I, HSFI)
         PRE = 0
         IF (PREFLG(GR, CH) .EQ. 1 .AND. I .LE. 20) PRE = PRETB(I)
C        Band 21 carries no scalefactor of its own; the standard gives
C        it band 20's.
         K = I
         IF (K .GT. 20) K = 20
         E = GAIN - ISHFT(SCF(K, CH) + PRE, SH)
C        Every legal combination of gains and scalefactors lands well
C        inside the table; the clamp is against a corrupt granule
C        indexing outside it, which would be a memory fault rather than
C        a quiet wrong number.
         IF (E .LT. -512) E = -512
         IF (E .GT. 511) E = 511
         DO 20 L = 1, LEN
            IF (J .GE. MXSMP) GOTO 30
            IF (IS(J, CH) .NE. 0) THEN
               V = X43(IABS(IS(J, CH))) * QPOW(E + 512)
               IF (IS(J, CH) .LT. 0) V = -V
               XRQ(J, CH) = V
            END IF
            J = J + 1
   20    CONTINUE
   30 CONTINUE

      IF (SHRTS(CH) .LT. 13) THEN
         DO 40 W = 0, 2
            GW(W) = GAIN - ISHFT(SBGAIN(W + 1, GR, CH), 3)
   40    CONTINUE
         K = LONGE(CH)
         DO 70 I = SHRTS(CH), 12
            LEN = SBS(I + 1, HSFI) - SBS(I, HSFI)
            DO 60 W = 0, 2
               E = GW(W) - ISHFT(SCF(K, CH), SH)
               IF (E .LT. -512) E = -512
               IF (E .GT. 511) E = 511
               K = K + 1
               DO 50 L = 1, LEN
                  IF (J .GE. MXSMP) GOTO 70
                  IF (IS(J, CH) .NE. 0) THEN
                     V = X43(IABS(IS(J, CH))) * QPOW(E + 512)
                     IF (IS(J, CH) .LT. 0) V = -V
                     XRQ(J, CH) = V
                  END IF
                  J = J + 1
   50          CONTINUE
   60       CONTINUE
   70    CONTINUE
      END IF
      RETURN
      END
