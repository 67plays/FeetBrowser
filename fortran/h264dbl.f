C     The deblocking filter, clause 8.7.
C
C     This runs as one pass over the whole picture after the last slice
C     has been decoded, in macroblock raster order, all vertical edges of
C     a macroblock before any of its horizontal ones.  It filters in
C     place and each edge sees the results of the edges before it, which
C     is not an implementation shortcut -- it is what 8.7 specifies, and
C     filtering into a copy would give different pixels.
C
C     Intra pictures make the boundary strength trivial.  8.7.2.1 gives
C     bS = 4 when either side is intra and the edge is a macroblock edge,
C     and bS = 3 when either side is intra otherwise; in an I picture
C     every macroblock is intra, so the strength is 4 on the outside of a
C     macroblock and 3 inside it, with no motion vectors or coefficient
C     counts to consult.  When P slices arrive this is the routine that
C     grows a real bS derivation.

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

C     All the edges of one macroblock.
      SUBROUTINE H2DBMB(A)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER A
      INTEGER MX, MY, E, BS, NB, X, Y, IA, IB, LEFT, TOP, SC, T8
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
      DO 10 E = 0, 3
         IF (T8 .NE. 0 .AND. MOD(E, 2) .NE. 0) GOTO 10
         IF (E .EQ. 0) THEN
            IF (LEFT .EQ. 0) GOTO 10
            NB = A - 1
            BS = 4
         ELSE
            NB = A
            BS = 3
         END IF
         CALL H2QPI(A, NB, 0, IA, IB)
         X = MX * 16 + E * 4
         CALL H2EDG(PY, (MY * 16) * MXW + X + 1, 1, MXW, 16,
     +              BS, IA, IB, 0)
   10 CONTINUE
      DO 20 E = 0, 1
         IF (E .EQ. 0) THEN
            IF (LEFT .EQ. 0) GOTO 20
            NB = A - 1
            BS = 4
         ELSE
            NB = A
            BS = 3
         END IF
         X = MX * 8 + E * 4
         CALL H2QPI(A, NB, 1, IA, IB)
         CALL H2EDG(PU, (MY * 8) * SC + X + 1, 1, SC, 8,
     +              BS, IA, IB, 1)
         CALL H2QPI(A, NB, 2, IA, IB)
         CALL H2EDG(PV, (MY * 8) * SC + X + 1, 1, SC, 8,
     +              BS, IA, IB, 1)
   20 CONTINUE

C     Horizontal edges, top to bottom.
      DO 30 E = 0, 3
         IF (T8 .NE. 0 .AND. MOD(E, 2) .NE. 0) GOTO 30
         IF (E .EQ. 0) THEN
            IF (TOP .EQ. 0) GOTO 30
            NB = A - MBW
            BS = 4
         ELSE
            NB = A
            BS = 3
         END IF
         CALL H2QPI(A, NB, 0, IA, IB)
         Y = MY * 16 + E * 4
         CALL H2EDG(PY, Y * MXW + MX * 16 + 1, MXW, 1, 16,
     +              BS, IA, IB, 0)
   30 CONTINUE
      DO 40 E = 0, 1
         IF (E .EQ. 0) THEN
            IF (TOP .EQ. 0) GOTO 40
            NB = A - MBW
            BS = 4
         ELSE
            NB = A
            BS = 3
         END IF
         Y = MY * 8 + E * 4
         CALL H2QPI(A, NB, 1, IA, IB)
         CALL H2EDG(PU, Y * SC + MX * 8 + 1, SC, 1, 8,
     +              BS, IA, IB, 1)
         CALL H2QPI(A, NB, 2, IA, IB)
         CALL H2EDG(PV, Y * SC + MX * 8 + 1, SC, 1, 8,
     +              BS, IA, IB, 1)
   40 CONTINUE
      RETURN
      END
