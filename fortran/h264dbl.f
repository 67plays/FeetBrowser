C     The deblocking filter, clause 8.7.
C
C     This runs as one pass over the whole picture after the last slice
C     has been decoded, in macroblock raster order, all vertical edges of
C     a macroblock before any of its horizontal ones.  It filters in
C     place and each edge sees the results of the edges before it, which
C     is not an implementation shortcut -- it is what 8.7 specifies, and
C     filtering into a copy would give different pixels.
C
C     The boundary strength is where inter prediction shows up in this
C     file.  In an I picture it is trivial -- 4 on the outside of a
C     macroblock, 3 inside it, because every macroblock is intra.  In a P
C     picture the strength varies along a single edge, so each edge is
C     filtered as four groups of four lines rather than as sixteen lines
C     with one strength, and each group asks about the two 4x4 blocks it
C     separates: their coefficients, the pictures they predicted from,
C     and how far apart their vectors are.

      SUBROUTINE H2DBLK
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER A
      DO 10 A = 0, MBN - 1
         CALL H2DBMB(A)
   10 CONTINUE
      RETURN
      END

C     8.7.2.2: the alpha and beta table indices for one edge.  qP is the
C     average of the two macroblocks' quantisers, which is why an edge
C     between a coarsely and a finely quantised macroblock is filtered
C     with something in between rather than with either.
      SUBROUTINE H2QPI(A, NB, COMP, IA, IB)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER A, NB, COMP, IA, IB
      INTEGER QP, QN, O, QAV
      QP = MQPY(A + 1)
      QN = MQPY(NB + 1)
      IF (COMP .GT. 0) THEN
         O = CQPO
         IF (COMP .EQ. 2) O = CQPO2
         QP = CHQP(MAX(0, MIN(51, QP + O)))
         QN = CHQP(MAX(0, MIN(51, QN + O)))
      END IF
      QAV = (QP + QN + 1) / 2
      IA = MAX(0, MIN(51, QAV + MALP(A + 1)))
      IB = MAX(0, MIN(51, QAV + MBET(A + 1)))
      RETURN
      END

C     One edge.  Q0 is the index of the first sample on the q side, DP
C     the step from q0 towards q1 (so p0 sits at Q0-DP), DL the step from
C     one line of the edge to the next, and NL the number of lines.  A
C     vertical edge and a horizontal edge differ only in those two steps,
C     which is the whole reason this is one routine and not two.
      SUBROUTINE H2EDG(P, Q0, DP, DL, NL, BS, IA, IB, CH)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER P(*), Q0, DP, DL, NL, BS, IA, IB, CH
      INTEGER L, I, ALPHA, BETA, TC0, TC, D, AP, AQ
      INTEGER P0, P1, P2, P3, R0, R1, R2, R3
      ALPHA = ALPHT(IA)
      BETA = BETAT(IB)
      IF (ALPHA .EQ. 0 .OR. BETA .EQ. 0) RETURN
      TC0 = 0
      IF (BS .LT. 4) TC0 = TC0T(IA, BS)
      DO 10 L = 0, NL - 1
         I = Q0 + L * DL
         R0 = P(I)
         R1 = P(I + DP)
         R2 = P(I + 2 * DP)
         R3 = P(I + 3 * DP)
         P0 = P(I - DP)
         P1 = P(I - 2 * DP)
         P2 = P(I - 3 * DP)
         P3 = P(I - 4 * DP)
C     8.7.2.1's filterSampleFlag.  A real edge in the picture has a big
C     step across it and small steps either side of it; blocking has a
C     small step across it too.  These three tests are what tells them
C     apart, and they are why the filter leaves genuine edges alone.
         IF (ABS(P0 - R0) .GE. ALPHA) GOTO 10
         IF (ABS(P1 - P0) .GE. BETA) GOTO 10
         IF (ABS(R1 - R0) .GE. BETA) GOTO 10
         AP = ABS(P2 - P0)
         AQ = ABS(R2 - R0)
         IF (BS .LT. 4) THEN
C     8.7.2.3, the normal filter: nudge the two samples either side of
C     the edge towards each other by no more than tC.
            IF (CH .NE. 0) THEN
               TC = TC0 + 1
            ELSE
               TC = TC0
               IF (AP .LT. BETA) TC = TC + 1
               IF (AQ .LT. BETA) TC = TC + 1
            END IF
            D = SHIFTA(ISHFT(R0 - P0, 2) + (P1 - R1) + 4, 3)
            D = MAX(-TC, MIN(TC, D))
            P(I - DP) = MAX(0, MIN(255, P0 + D))
            P(I) = MAX(0, MIN(255, R0 - D))
            IF (CH .EQ. 0) THEN
               IF (AP .LT. BETA) THEN
                  D = SHIFTA(P2 + SHIFTA(P0 + R0 + 1, 1)
     +                       - ISHFT(P1, 1), 1)
                  P(I - 2 * DP) = P1 + MAX(-TC0, MIN(TC0, D))
               END IF
               IF (AQ .LT. BETA) THEN
                  D = SHIFTA(R2 + SHIFTA(P0 + R0 + 1, 1)
     +                       - ISHFT(R1, 1), 1)
                  P(I + DP) = R1 + MAX(-TC0, MIN(TC0, D))
               END IF
            END IF
         ELSE IF (CH .NE. 0) THEN
C     8.7.2.4 for chroma: two samples, no choice of filter width.
            P(I - DP) = SHIFTA(2 * P1 + P0 + R1 + 2, 2)
            P(I) = SHIFTA(2 * R1 + R0 + P1 + 2, 2)
         ELSE
C     8.7.2.4 for luma: across a macroblock edge in a flat area the
C     filter reaches three samples deep, because that is where the
C     blocking of a whole macroblock's worth of quantisation shows.
C     Where the area is not flat it falls back to two samples.
            D = 0
            IF (ABS(P0 - R0) .LT. SHIFTA(ALPHA, 2) + 2) D = 1
            IF (AP .LT. BETA .AND. D .NE. 0) THEN
               P(I - DP) = SHIFTA(P2 + 2 * P1 + 2 * P0
     +                            + 2 * R0 + R1 + 4, 3)
               P(I - 2 * DP) = SHIFTA(P2 + P1 + P0 + R0 + 2, 2)
               P(I - 3 * DP) = SHIFTA(2 * P3 + 3 * P2 + P1
     +                                + P0 + R0 + 4, 3)
            ELSE
               P(I - DP) = SHIFTA(2 * P1 + P0 + R1 + 2, 2)
            END IF
            IF (AQ .LT. BETA .AND. D .NE. 0) THEN
               P(I) = SHIFTA(R2 + 2 * R1 + 2 * R0
     +                       + 2 * P0 + P1 + 4, 3)
               P(I + DP) = SHIFTA(R2 + R1 + R0 + P0 + 2, 2)
               P(I + 2 * DP) = SHIFTA(2 * R3 + 3 * R2 + R1
     +                                + R0 + P0 + 4, 3)
            ELSE
               P(I) = SHIFTA(2 * R1 + R0 + P1 + 2, 2)
            END IF
         END IF
   10 CONTINUE
      RETURN
      END

C     Does the transform block containing 4x4 block (BX, BY) of
C     macroblock A hold any non-zero coefficients?
C
C     MNZ is per 4x4 block because CAVLC needs it that way -- each 4x4's
C     TotalCoeff is the next block's nC -- so when the 8x8 transform is
C     in use the four counts of an 8x8 are four different numbers and the
C     question has to be asked of all four.  Under CABAC an 8x8 is one
C     coded block and all four slots hold its count, so the loop finds
C     the same answer it would have found from any one of them.
      INTEGER FUNCTION H2NZQ(A, BX, BY)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER A, BX, BY, I, K
      H2NZQ = 0
      IF (MT8(A + 1) .EQ. 0) THEN
         IF (MNZ(ZORD(BX, BY) + 1, A + 1) .GT. 0) H2NZQ = 1
         RETURN
      END IF
      K = 4 * (BX / 2 + 2 * (BY / 2))
      DO 10 I = 1, 4
         IF (MNZ(K + I, A + 1) .GT. 0) H2NZQ = 1
   10 CONTINUE
      RETURN
      END

C     8.7.2.1, for a frame-coded P or I picture with one reference list.
C     A and NB are the macroblocks on the q and p sides; MBEDG says the
C     edge between them is a macroblock edge; the four block coordinates
C     name the 4x4 block on each side, in raster order within its own
C     macroblock.
C
C     MRPI and not MREF: two slices of one picture can reach the same
C     reference picture through different indices, and 8.7.2.1 asks
C     whether the pictures are the same, not whether the numbers are.
      SUBROUTINE H2BS(A, NB, MBEDG, PBX, PBY, QBX, QBY, BS)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER A, NB, MBEDG, PBX, PBY, QBX, QBY, BS
      INTEGER PI, QI, PQ, QQ, H2NZQ
      EXTERNAL H2NZQ
      BS = 0
      IF (MINT(NB + 1) .NE. 0 .OR. MINT(A + 1) .NE. 0) THEN
         BS = 3
         IF (MBEDG .NE. 0) BS = 4
         RETURN
      END IF
C     8.7.2.1 asks about the transform block containing the 4x4, which
C     is the 4x4 itself unless the 8x8 transform is in use.
      IF (H2NZQ(NB, PBX, PBY) .NE. 0 .OR. H2NZQ(A, QBX, QBY) .NE. 0)
     +   THEN
         BS = 2
         RETURN
      END IF
      PI = 1 + PBX + 4 * PBY
      QI = 1 + QBX + 4 * QBY
      PQ = 1 + PBX / 2 + 2 * (PBY / 2)
      QQ = 1 + QBX / 2 + 2 * (QBY / 2)
      IF (MRPI(PQ, NB + 1) .NE. MRPI(QQ, A + 1)) THEN
         BS = 1
         RETURN
      END IF
C     One full luma sample of disagreement, which is four quarter-sample
C     units, is where a seam becomes visible.
      IF (ABS(MMVX(PI, NB + 1) - MMVX(QI, A + 1)) .GE. 4) BS = 1
      IF (ABS(MMVY(PI, NB + 1) - MMVY(QI, A + 1)) .GE. 4) BS = 1
      RETURN
      END

C     All the edges of one macroblock.
      SUBROUTINE H2DBMB(A)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER A
      INTEGER MX, MY, E, BS, NB, X, Y, IA, IB, LEFT, TOP, SC, T8
      INTEGER G, ME, PBX, PBY, LE
      MX = MOD(A, MBW)
      MY = A / MBW
      IF (MDBI(A + 1) .EQ. 1) RETURN
      T8 = MT8(A + 1)
      SC = MXW / 2
C     A macroblock edge is filtered unless it is the edge of the picture,
C     or the slice asked for its own edges to be left alone.
      LEFT = 0
      IF (MX .GT. 0) THEN
         LEFT = 1
         IF (MDBI(A + 1) .EQ. 2) THEN
            IF (MSLC(A) .NE. MSLC(A + 1)) LEFT = 0
         END IF
      END IF
      TOP = 0
      IF (MY .GT. 0) THEN
         TOP = 1
         IF (MDBI(A + 1) .EQ. 2) THEN
            IF (MSLC(A + 1 - MBW) .NE. MSLC(A + 1)) TOP = 0
         END IF
      END IF

C     Vertical edges, left to right.  With an 8x8 transform the two
C     edges at four and twelve are inside a transform block and are not
C     filtered at all.
      DO 15 E = 0, 3
         IF (T8 .NE. 0 .AND. MOD(E, 2) .NE. 0) GOTO 15
         IF (E .EQ. 0) THEN
            IF (LEFT .EQ. 0) GOTO 15
            NB = A - 1
            ME = 1
            PBX = 3
         ELSE
            NB = A
            ME = 0
            PBX = E - 1
         END IF
         CALL H2QPI(A, NB, 0, IA, IB)
         X = MX * 16 + E * 4
         DO 10 G = 0, 3
            CALL H2BS(A, NB, ME, PBX, G, E, G, BS)
            IF (BS .EQ. 0) GOTO 10
            CALL H2EDG(PY, (MY * 16 + 4 * G) * MXW + X + 1, 1, MXW, 4,
     +                 BS, IA, IB, 0)
   10    CONTINUE
   15 CONTINUE
C     Chroma reuses the luma strengths: a chroma edge is a luma edge seen
C     at half the resolution, so two chroma lines share one luma group.
      DO 25 E = 0, 1
         LE = 2 * E
         IF (E .EQ. 0) THEN
            IF (LEFT .EQ. 0) GOTO 25
            NB = A - 1
            ME = 1
            PBX = 3
         ELSE
            NB = A
            ME = 0
            PBX = LE - 1
         END IF
         X = MX * 8 + E * 4
         DO 20 G = 0, 3
            CALL H2BS(A, NB, ME, PBX, G, LE, G, BS)
            IF (BS .EQ. 0) GOTO 20
            CALL H2QPI(A, NB, 1, IA, IB)
            CALL H2EDG(PU, (MY * 8 + 2 * G) * SC + X + 1, 1, SC, 2,
     +                 BS, IA, IB, 1)
            CALL H2QPI(A, NB, 2, IA, IB)
            CALL H2EDG(PV, (MY * 8 + 2 * G) * SC + X + 1, 1, SC, 2,
     +                 BS, IA, IB, 1)
   20    CONTINUE
   25 CONTINUE

C     Horizontal edges, top to bottom.
      DO 35 E = 0, 3
         IF (T8 .NE. 0 .AND. MOD(E, 2) .NE. 0) GOTO 35
         IF (E .EQ. 0) THEN
            IF (TOP .EQ. 0) GOTO 35
            NB = A - MBW
            ME = 1
            PBY = 3
         ELSE
            NB = A
            ME = 0
            PBY = E - 1
         END IF
         CALL H2QPI(A, NB, 0, IA, IB)
         Y = MY * 16 + E * 4
         DO 30 G = 0, 3
            CALL H2BS(A, NB, ME, G, PBY, G, E, BS)
            IF (BS .EQ. 0) GOTO 30
            CALL H2EDG(PY, Y * MXW + MX * 16 + 4 * G + 1, MXW, 1, 4,
     +                 BS, IA, IB, 0)
   30    CONTINUE
   35 CONTINUE
      DO 45 E = 0, 1
         LE = 2 * E
         IF (E .EQ. 0) THEN
            IF (TOP .EQ. 0) GOTO 45
            NB = A - MBW
            ME = 1
            PBY = 3
         ELSE
            NB = A
            ME = 0
            PBY = LE - 1
         END IF
         Y = MY * 8 + E * 4
         DO 40 G = 0, 3
            CALL H2BS(A, NB, ME, G, PBY, G, LE, BS)
            IF (BS .EQ. 0) GOTO 40
            CALL H2QPI(A, NB, 1, IA, IB)
            CALL H2EDG(PU, Y * SC + MX * 8 + 2 * G + 1, SC, 1, 2,
     +                 BS, IA, IB, 1)
            CALL H2QPI(A, NB, 2, IA, IB)
            CALL H2EDG(PV, Y * SC + MX * 8 + 2 * G + 1, SC, 1, 2,
     +                 BS, IA, IB, 1)
   40    CONTINUE
   45 CONTINUE
      RETURN
      END
