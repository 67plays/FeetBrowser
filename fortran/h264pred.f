C     Intra prediction: clauses 8.3.1 through 8.3.4.
C
C     Every predictor here works from two small arrays and a corner
C     sample rather than from the picture, because the picture is the
C     wrong shape to ask questions of.  H2GATH copies the row above and
C     the column to the left of a block into PT and PL, substituting for
C     whatever is not available, and from that point the nine directional
C     modes are pure arithmetic with no edge cases in them.
C
C     The 4x4 and 8x8 predictors are the same nine formulas at two sizes.
C     The spec writes them out twice, in 8.3.1.2 and 8.3.2.2, and the two
C     copies differ in exactly three places: the rounding shift for DC,
C     the corner of diagonal-down-left, and the threshold in
C     horizontal-up.  Writing them twice here would have been writing the
C     other six modes twice for no reason, so N carries the size and the
C     three differences are spelled out where they occur.

C     Copy a block's neighbours out of a plane.  The four availability
C     flags are set by the caller, because they come from macroblock
C     bookkeeping this routine has no way to redo.  Unavailable samples
C     become 128 -- a conforming stream never predicts from them, and a
C     non-conforming one gets grey instead of whatever was in memory.
      SUBROUTINE H2GATH(P, STR, X, Y, N, NTR)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER P(*), STR, X, Y, N, NTR
      INTEGER K, R
      IF (PTOK .NE. 0) THEN
         R = (Y - 1) * STR + X
         DO 10 K = 0, N - 1
            PT(K) = P(R + K + 1)
   10    CONTINUE
      ELSE
         DO 20 K = 0, N - 1
            PT(K) = 128
   20    CONTINUE
      END IF
C     8.3.1.2 and 8.3.2: when the four (or eight) samples above and to
C     the right are missing but the ones above are not, the rightmost
C     sample above is repeated across them.  Doing it here rather than in
C     each predictor is why diagonal-down-left needs no availability test.
      IF (NTR .NE. 0) THEN
         IF (PTROK .NE. 0) THEN
            R = (Y - 1) * STR + X + N
            DO 30 K = 0, N - 1
               PT(N + K) = P(R + K + 1)
   30       CONTINUE
         ELSE
            DO 40 K = N, 2 * N - 1
               PT(K) = PT(N - 1)
   40       CONTINUE
         END IF
      END IF
      IF (PLOK .NE. 0) THEN
         DO 50 K = 0, N - 1
            PL(K) = P((Y + K) * STR + X)
   50    CONTINUE
      ELSE
         DO 60 K = 0, N - 1
            PL(K) = 128
   60    CONTINUE
      END IF
      IF (PDOK .NE. 0) THEN
         PT(-1) = P((Y - 1) * STR + X)
      ELSE
         PT(-1) = 128
      END IF
      PL(-1) = PT(-1)
      RETURN
      END

C     8.3.2.2's reference sample filtering, which Intra_8x8 applies to its
C     neighbours before predicting and Intra_4x4 does not apply at all.
C     The filtered corner is computed from the unfiltered edges and the
C     filtered edges from the unfiltered corner, so the originals are
C     copied aside first; doing it in place would feed each result into
C     the next.
      SUBROUTINE H2F8
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER T(-1:15), L(-1:7), D, K, V
      DO 10 K = -1, 15
         T(K) = PT(K)
   10 CONTINUE
      DO 20 K = -1, 7
         L(K) = PL(K)
   20 CONTINUE
      D = PT(-1)
      IF (PTOK .NE. 0) THEN
         IF (PDOK .NE. 0) THEN
            PT(0) = ISHFT(D + 2 * T(0) + T(1) + 2, -2)
         ELSE
            PT(0) = ISHFT(3 * T(0) + T(1) + 2, -2)
         END IF
         DO 30 K = 1, 14
            PT(K) = ISHFT(T(K - 1) + 2 * T(K) + T(K + 1) + 2, -2)
   30    CONTINUE
         PT(15) = ISHFT(T(14) + 3 * T(15) + 2, -2)
      END IF
      IF (PLOK .NE. 0) THEN
         IF (PDOK .NE. 0) THEN
            PL(0) = ISHFT(D + 2 * L(0) + L(1) + 2, -2)
         ELSE
            PL(0) = ISHFT(3 * L(0) + L(1) + 2, -2)
         END IF
         DO 40 K = 1, 6
            PL(K) = ISHFT(L(K - 1) + 2 * L(K) + L(K + 1) + 2, -2)
   40    CONTINUE
         PL(7) = ISHFT(L(6) + 3 * L(7) + 2, -2)
      END IF
      IF (PDOK .NE. 0) THEN
         IF (PTOK .NE. 0 .AND. PLOK .NE. 0) THEN
            V = ISHFT(T(0) + 2 * D + L(0) + 2, -2)
         ELSE IF (PLOK .EQ. 0) THEN
            V = ISHFT(3 * D + T(0) + 2, -2)
         ELSE
            V = ISHFT(3 * D + L(0) + 2, -2)
         END IF
         PT(-1) = V
         PL(-1) = V
      END IF
      RETURN
      END

C     The nine Intra_4x4 / Intra_8x8 modes, at N = 4 or N = 8.  PT(-1)
C     and PL(-1) are the same corner sample under two names, which is what
C     lets diagonal-down-right, vertical-right and horizontal-down index
C     one step off the near end of an array and land on it.
      SUBROUTINE H2PNN(N, MODE, PRD)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER N, MODE, PRD(0:7,0:7)
      INTEGER X, Y, S, K, LG, V, Z, H

      GOTO (100, 200, 300, 400, 500, 600, 700, 800, 900), MODE + 1
      RETURN

C     Mode 0, vertical.
  100 DO 120 Y = 0, N - 1
         DO 110 X = 0, N - 1
            PRD(X, Y) = PT(X)
  110    CONTINUE
  120 CONTINUE
      RETURN

C     Mode 1, horizontal.
  200 DO 220 Y = 0, N - 1
         DO 210 X = 0, N - 1
            PRD(X, Y) = PL(Y)
  210    CONTINUE
  220 CONTINUE
      RETURN

C     Mode 2, DC.  The one mode that is defined when neighbours are
C     missing, and the reason the availability flags survive this far.
  300 LG = 2
      IF (N .EQ. 8) LG = 3
      S = 0
      IF (PTOK .NE. 0 .AND. PLOK .NE. 0) THEN
         DO 310 K = 0, N - 1
            S = S + PT(K) + PL(K)
  310    CONTINUE
         V = ISHFT(S + N, -(LG + 1))
      ELSE IF (PLOK .NE. 0) THEN
         DO 320 K = 0, N - 1
            S = S + PL(K)
  320    CONTINUE
         V = ISHFT(S + N / 2, -LG)
      ELSE IF (PTOK .NE. 0) THEN
         DO 330 K = 0, N - 1
            S = S + PT(K)
  330    CONTINUE
         V = ISHFT(S + N / 2, -LG)
      ELSE
         V = 128
      END IF
      DO 350 Y = 0, N - 1
         DO 340 X = 0, N - 1
            PRD(X, Y) = V
  340    CONTINUE
  350 CONTINUE
      RETURN

C     Mode 3, diagonal down left.  The bottom right sample is the one
C     place the 4x4 and 8x8 forms differ, and only because the run of
C     samples above ends there.
  400 DO 420 Y = 0, N - 1
         DO 410 X = 0, N - 1
            IF (X .EQ. N - 1 .AND. Y .EQ. N - 1) THEN
               PRD(X, Y) = ISHFT(PT(2 * N - 2)
     +                           + 3 * PT(2 * N - 1) + 2, -2)
            ELSE
               PRD(X, Y) = ISHFT(PT(X + Y) + 2 * PT(X + Y + 1)
     +                           + PT(X + Y + 2) + 2, -2)
            END IF
  410    CONTINUE
  420 CONTINUE
      RETURN

C     Mode 4, diagonal down right.
  500 DO 520 Y = 0, N - 1
         DO 510 X = 0, N - 1
            IF (X .GT. Y) THEN
               Z = X - Y
               PRD(X, Y) = ISHFT(PT(Z - 2) + 2 * PT(Z - 1)
     +                           + PT(Z) + 2, -2)
            ELSE IF (X .LT. Y) THEN
               Z = Y - X
               PRD(X, Y) = ISHFT(PL(Z - 2) + 2 * PL(Z - 1)
     +                           + PL(Z) + 2, -2)
            ELSE
               PRD(X, Y) = ISHFT(PT(0) + 2 * PT(-1) + PL(0) + 2, -2)
            END IF
  510    CONTINUE
  520 CONTINUE
      RETURN

C     Mode 5, vertical right.
  600 DO 620 Y = 0, N - 1
         DO 610 X = 0, N - 1
            Z = 2 * X - Y
            H = X - ISHFT(Y, -1)
            IF (Z .GE. 0 .AND. MOD(Z, 2) .EQ. 0) THEN
               PRD(X, Y) = ISHFT(PT(H - 1) + PT(H) + 1, -1)
            ELSE IF (Z .GE. 0) THEN
               PRD(X, Y) = ISHFT(PT(H - 2) + 2 * PT(H - 1)
     +                           + PT(H) + 2, -2)
            ELSE IF (Z .EQ. -1) THEN
               PRD(X, Y) = ISHFT(PL(0) + 2 * PT(-1) + PT(0) + 2, -2)
            ELSE
               K = Y - 2 * X - 1
               PRD(X, Y) = ISHFT(PL(K) + 2 * PL(K - 1)
     +                           + PL(K - 2) + 2, -2)
            END IF
  610    CONTINUE
  620 CONTINUE
      RETURN

C     Mode 6, horizontal down: mode 5 with the axes exchanged.
  700 DO 720 Y = 0, N - 1
         DO 710 X = 0, N - 1
            Z = 2 * Y - X
            H = Y - ISHFT(X, -1)
            IF (Z .GE. 0 .AND. MOD(Z, 2) .EQ. 0) THEN
               PRD(X, Y) = ISHFT(PL(H - 1) + PL(H) + 1, -1)
            ELSE IF (Z .GE. 0) THEN
               PRD(X, Y) = ISHFT(PL(H - 2) + 2 * PL(H - 1)
     +                           + PL(H) + 2, -2)
            ELSE IF (Z .EQ. -1) THEN
               PRD(X, Y) = ISHFT(PL(0) + 2 * PT(-1) + PT(0) + 2, -2)
            ELSE
               K = X - 2 * Y - 1
               PRD(X, Y) = ISHFT(PT(K) + 2 * PT(K - 1)
     +                           + PT(K - 2) + 2, -2)
            END IF
  710    CONTINUE
  720 CONTINUE
      RETURN

C     Mode 7, vertical left.
  800 DO 820 Y = 0, N - 1
         DO 810 X = 0, N - 1
            H = X + ISHFT(Y, -1)
            IF (MOD(Y, 2) .EQ. 0) THEN
               PRD(X, Y) = ISHFT(PT(H) + PT(H + 1) + 1, -1)
            ELSE
               PRD(X, Y) = ISHFT(PT(H) + 2 * PT(H + 1)
     +                           + PT(H + 2) + 2, -2)
            END IF
  810    CONTINUE
  820 CONTINUE
      RETURN

C     Mode 8, horizontal up.  Past 2N-3 the ray has run off the bottom of
C     the column to the left and the prediction is just its last sample.
  900 DO 920 Y = 0, N - 1
         DO 910 X = 0, N - 1
            Z = X + 2 * Y
            H = Y + ISHFT(X, -1)
            IF (Z .GT. 2 * N - 3) THEN
               PRD(X, Y) = PL(N - 1)
            ELSE IF (Z .EQ. 2 * N - 3) THEN
               PRD(X, Y) = ISHFT(PL(N - 2) + 3 * PL(N - 1) + 2, -2)
            ELSE IF (MOD(Z, 2) .EQ. 0) THEN
               PRD(X, Y) = ISHFT(PL(H) + PL(H + 1) + 1, -1)
            ELSE
               PRD(X, Y) = ISHFT(PL(H) + 2 * PL(H + 1)
     +                           + PL(H + 2) + 2, -2)
            END IF
  910    CONTINUE
  920 CONTINUE
      RETURN
      END

C     8.3.3, the four Intra_16x16 modes.
      SUBROUTINE H2P16(MODE, PRD)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER MODE, PRD(0:15,0:15)
      INTEGER X, Y, S, K, V, A, B, C, HH, VV
      IF (MODE .EQ. 0) THEN
         DO 20 Y = 0, 15
            DO 10 X = 0, 15
               PRD(X, Y) = PT(X)
   10       CONTINUE
   20    CONTINUE
      ELSE IF (MODE .EQ. 1) THEN
         DO 40 Y = 0, 15
            DO 30 X = 0, 15
               PRD(X, Y) = PL(Y)
   30       CONTINUE
   40    CONTINUE
      ELSE IF (MODE .EQ. 2) THEN
         S = 0
         IF (PTOK .NE. 0 .AND. PLOK .NE. 0) THEN
            DO 50 K = 0, 15
               S = S + PT(K) + PL(K)
   50       CONTINUE
            V = ISHFT(S + 16, -5)
         ELSE IF (PLOK .NE. 0) THEN
            DO 60 K = 0, 15
               S = S + PL(K)
   60       CONTINUE
            V = ISHFT(S + 8, -4)
         ELSE IF (PTOK .NE. 0) THEN
            DO 70 K = 0, 15
               S = S + PT(K)
   70       CONTINUE
            V = ISHFT(S + 8, -4)
         ELSE
            V = 128
         END IF
         DO 90 Y = 0, 15
            DO 80 X = 0, 15
               PRD(X, Y) = V
   80       CONTINUE
   90    CONTINUE
      ELSE
C     8.3.3.4, plane prediction: a least-squares tilted plane through the
C     samples above and to the left.  The two sums weight each sample by
C     its distance from the middle of the edge, which is what makes this
C     a gradient rather than an average.
         HH = 0
         VV = 0
         DO 100 K = 0, 7
            HH = HH + (K + 1) * (PT(8 + K) - PT(6 - K))
            VV = VV + (K + 1) * (PL(8 + K) - PL(6 - K))
  100    CONTINUE
         A = 16 * (PL(15) + PT(15))
         B = SHIFTA(5 * HH + 32, 6)
         C = SHIFTA(5 * VV + 32, 6)
         DO 120 Y = 0, 15
            DO 110 X = 0, 15
               V = SHIFTA(A + B * (X - 7) + C * (Y - 7) + 16, 5)
               PRD(X, Y) = MAX(0, MIN(255, V))
  110       CONTINUE
  120    CONTINUE
      END IF
      RETURN
      END

C     8.3.4, the four chroma modes for one 8x8 chroma block.  They are
C     numbered differently from the luma modes -- DC is 0 here and 2
C     there -- because Table 7-16 says so and not for any reason visible
C     from the prediction itself.
      SUBROUTINE H2PCH(MODE, PRD)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER MODE, PRD(0:7,0:7)
      INTEGER X, Y, K, V, A, B, C, HH, VV, BX, BY, ST, SL, TOK, LOK
      IF (MODE .EQ. 1) THEN
         DO 20 Y = 0, 7
            DO 10 X = 0, 7
               PRD(X, Y) = PL(Y)
   10       CONTINUE
   20    CONTINUE
      ELSE IF (MODE .EQ. 2) THEN
         DO 40 Y = 0, 7
            DO 30 X = 0, 7
               PRD(X, Y) = PT(X)
   30       CONTINUE
   40    CONTINUE
      ELSE IF (MODE .EQ. 3) THEN
         HH = 0
         VV = 0
         DO 50 K = 0, 3
            HH = HH + (K + 1) * (PT(4 + K) - PT(2 - K))
            VV = VV + (K + 1) * (PL(4 + K) - PL(2 - K))
   50    CONTINUE
         A = 16 * (PL(7) + PT(7))
         B = SHIFTA(34 * HH + 32, 6)
         C = SHIFTA(34 * VV + 32, 6)
         DO 70 Y = 0, 7
            DO 60 X = 0, 7
               V = SHIFTA(A + B * (X - 3) + C * (Y - 3) + 16, 5)
               PRD(X, Y) = MAX(0, MIN(255, V))
   60       CONTINUE
   70    CONTINUE
      ELSE
C     8.3.4.1, chroma DC.  Each of the four 4x4 quadrants gets its own
C     average, and the three quadrants that are not the top left prefer
C     the edge they are nearer: the top right averages the samples above
C     it if it can, the bottom left the samples to its left, and only
C     falls back to the other edge when its own is missing.
         DO 120 BY = 0, 1
            DO 110 BX = 0, 1
               ST = 0
               SL = 0
               DO 80 K = 0, 3
                  ST = ST + PT(4 * BX + K)
                  SL = SL + PL(4 * BY + K)
   80          CONTINUE
               TOK = PTOK
               LOK = PLOK
               IF (BX .EQ. 1 .AND. BY .EQ. 0) THEN
                  IF (TOK .NE. 0) LOK = 0
               ELSE IF (BX .EQ. 0 .AND. BY .EQ. 1) THEN
                  IF (LOK .NE. 0) TOK = 0
               END IF
               IF (TOK .NE. 0 .AND. LOK .NE. 0) THEN
                  V = ISHFT(ST + SL + 4, -3)
               ELSE IF (TOK .NE. 0) THEN
                  V = ISHFT(ST + 2, -2)
               ELSE IF (LOK .NE. 0) THEN
                  V = ISHFT(SL + 2, -2)
               ELSE
                  V = 128
               END IF
               DO 100 Y = 0, 3
                  DO 90 X = 0, 3
                     PRD(4 * BX + X, 4 * BY + Y) = V
   90             CONTINUE
  100          CONTINUE
  110       CONTINUE
  120    CONTINUE
      END IF
      RETURN
      END
