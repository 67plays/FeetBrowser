C     Everything after the bits: inverse quantisation, noise substitution,
C     the two stereo tools, temporal noise shaping, and the inverse
C     transform that turns 1024 spectral lines back into sound.
C
C     The order is fixed by the standard and by arithmetic: mid/side and
C     intensity stereo are defined on dequantised coefficients, TNS
C     filters what they produce, and only then does the transform run.
C     Doing any two of them the other way round gives an answer that is
C     wrong by a small enough margin to sound almost right, which is the
C     worst kind of wrong there is.

C     4.6.2/4.6.3, inverse quantisation, and 4.6.13, noise substitution.
C
C     x^(4/3) is a table lookup and 2^((sf-100)/4) is another, so the
C     inner loop is a lookup, a multiply and a sign.  Nothing here can
C     index outside a table: the magnitudes were bounded when they were
C     read and the scalefactors when they were decoded.
      SUBROUTINE IPDEQ(CH)
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER CH
      INTEGER G, SFB, W, K, BT, SF, S, E, GB, BASE, Q, R
      DOUBLE PRECISION GN, EN, SC
      DO 10 K = 0, 1023
         SPEC(K,CH) = 0.0D0
   10 CONTINUE
      GB = 0
      DO 90 G = 1, NGRP(CH)
         DO 80 SFB = 0, MAXSFB(CH) - 1
            BT = BTYPE(SFB,G,CH)
            SF = SFAC(SFB,G,CH)
            S = SWBO(SFB,CH)
            E = SWBO(SFB+1,CH)
            DO 70 W = 0, GLEN(G,CH) - 1
               BASE = 128 * (GB + W)
               IF (BT .EQ. 13) THEN
C     Perceptual noise substitution.  The encoder threw the band away
C     and left its energy behind; the decoder puts back noise of that
C     energy.  Which noise is not specified, so no two decoders agree
C     sample for sample and none of them is wrong.  This one draws from
C     the same generator the reference decoder uses, seeded the same
C     way, so that the two can at least be compared.
                  EN = 0.0D0
                  DO 20 K = S, E - 1
                     CALL IPRAND(R)
                     SPEC(BASE+K,CH) = DBLE(R)
                     EN = EN + DBLE(R) * DBLE(R)
   20             CONTINUE
                  IF (EN .GT. 0.0D0) THEN
                     SC = (2.0D0 ** (0.25D0 * DBLE(SF))) / SQRT(EN)
                     DO 30 K = S, E - 1
                        SPEC(BASE+K,CH) = SPEC(BASE+K,CH) * SC
   30                CONTINUE
                  END IF
               ELSE IF (BT .GT. 0 .AND. BT .LT. 13) THEN
                  GN = GAINT(SF)
                  DO 40 K = S, E - 1
                     Q = QSPEC(BASE+K,CH)
                     IF (Q .GE. 0) THEN
                        SPEC(BASE+K,CH) = X43(Q) * GN
                     ELSE
                        SPEC(BASE+K,CH) = -X43(-Q) * GN
                     END IF
   40             CONTINUE
               END IF
   70       CONTINUE
   80    CONTINUE
         GB = GB + GLEN(G,CH)
   90 CONTINUE
      RETURN
      END

C     The reference decoder's linear congruential generator, kept in 32
C     bits by hand because Fortran integer overflow is not defined to
C     wrap.  The state is carried across frames: two bands of noise that
C     came out identical would be audible as a tone.
      SUBROUTINE IPRAND(V)
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER V
      INTEGER*8 X, TWO32, HALF32, MASK
      TWO32 = 65536
      TWO32 = TWO32 * 65536
      HALF32 = TWO32 / 2
      MASK = TWO32 - 1
      X = RNDST
      IF (X .LT. 0) X = X + TWO32
      X = IAND(X * 1664525 + 1013904223, MASK)
      IF (X .GE. HALF32) X = X - TWO32
      RNDST = X
      V = RNDST
      RETURN
      END

C     4.6.8.1, mid/side stereo: the encoder sent sum and difference, and
C     only for the bands whose mask bit says so.  Bands coded as noise or
C     as intensity are not part of it -- they have no second channel to
C     add to.
      SUBROUTINE IPMSA
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER G, SFB, W, K, GB, BASE
      DOUBLE PRECISION T
      GB = 0
      DO 50 G = 1, NGRP(1)
         DO 40 SFB = 0, MAXSFB(1) - 1
            IF (MSMASK(SFB,G) .NE. 0 .AND. BTYPE(SFB,G,1) .LT. 13
     +          .AND. BTYPE(SFB,G,2) .LT. 13) THEN
               DO 30 W = 0, GLEN(G,1) - 1
                  BASE = 128 * (GB + W)
                  DO 20 K = SWBO(SFB,1), SWBO(SFB+1,1) - 1
                     T = SPEC(BASE+K,1)
                     SPEC(BASE+K,1) = T + SPEC(BASE+K,2)
                     SPEC(BASE+K,2) = T - SPEC(BASE+K,2)
   20             CONTINUE
   30          CONTINUE
            END IF
   40    CONTINUE
         GB = GB + GLEN(G,1)
   50 CONTINUE
      RETURN
      END

C     4.6.8.2, intensity stereo: above some frequency the second channel
C     is not coded at all, only a scale and a sign relative to the first.
C     Codebook 15 keeps the phase and 14 inverts it, and a mid/side mask
C     bit on the same band inverts it again -- the two tools are allowed
C     to disagree and the standard says which wins.
      SUBROUTINE IPISA
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER G, SFB, W, K, GB, BASE, BT, C
      DOUBLE PRECISION SC
      GB = 0
      DO 50 G = 1, NGRP(2)
         DO 40 SFB = 0, MAXSFB(2) - 1
            BT = BTYPE(SFB,G,2)
            IF (BT .EQ. 14 .OR. BT .EQ. 15) THEN
               C = -1 + 2 * (BT - 14)
               IF (MSPRES .NE. 0) C = C * (1 - 2 * MSMASK(SFB,G))
               SC = DBLE(C) * 2.0D0 ** (-0.25D0 * DBLE(SFAC(SFB,G,2)))
               DO 30 W = 0, GLEN(G,2) - 1
                  BASE = 128 * (GB + W)
                  DO 20 K = SWBO(SFB,2), SWBO(SFB+1,2) - 1
                     SPEC(BASE+K,2) = SC * SPEC(BASE+K,1)
   20             CONTINUE
   30          CONTINUE
            END IF
   40    CONTINUE
         GB = GB + GLEN(G,2)
   50 CONTINUE
      RETURN
      END

C     4.6.9, temporal noise shaping: an all-pole filter run across
C     frequency rather than across time, which shapes quantisation noise
C     inside the frame and keeps a transient's pre-echo underneath it.
C
C     The filter starts cold at the edge of its band -- the history is
C     the coefficients it has already produced and nothing before them,
C     which is why the inner loop stops at MIN(M, order).
      SUBROUTINE IPTNSA(CH)
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER CH
      INTEGER W, F, I, M, OR, BOT, TOP, S, E, SZ, INC, P, MMM, LO, HI
      DOUBLE PRECISION A(0:MXORD), B(0:MXORD), ACC
      IF (WSEQ(CH) .EQ. 2) THEN
         MMM = TNSMS(CSRI)
      ELSE
         MMM = TNSML(CSRI)
      END IF
      IF (MMM .GT. MAXSFB(CH)) MMM = MAXSFB(CH)
      IF (MMM .LE. 0) RETURN
      DO 90 W = 0, NWIN(CH) - 1
         BOT = NSWB(CH)
         DO 80 F = 1, TNSNF(W,CH)
            TOP = BOT
            BOT = TOP - TNSLN(F,W,CH)
            IF (BOT .LT. 0) BOT = 0
            OR = TNSOR(F,W,CH)
            IF (OR .GT. 0) THEN
C     Reflection coefficients to filter coefficients, the step-up
C     recursion of 4.6.9.3.
               A(0) = 1.0D0
               DO 30 M = 1, OR
                  DO 10 I = 1, M - 1
                     B(I) = A(I) + TNSCF(M,F,W,CH) * A(M-I)
   10             CONTINUE
                  DO 20 I = 1, M - 1
                     A(I) = B(I)
   20             CONTINUE
                  A(M) = TNSCF(M,F,W,CH)
   30          CONTINUE
               LO = BOT
               HI = TOP
               IF (LO .GT. MMM) LO = MMM
               IF (HI .GT. MMM) HI = MMM
               S = SWBO(LO,CH)
               E = SWBO(HI,CH)
               SZ = E - S
               IF (SZ .GT. 0) THEN
                  INC = 1
                  IF (TNSDR(F,W,CH) .NE. 0) THEN
                     INC = -1
                     S = E - 1
                  END IF
                  S = S + 128 * W
                  DO 60 M = 0, SZ - 1
                     P = S + M * INC
                     ACC = SPEC(P,CH)
                     DO 50 I = 1, MIN(M, OR)
                        ACC = ACC - A(I) * SPEC(P - I * INC, CH)
   50                CONTINUE
                     SPEC(P,CH) = ACC
   60             CONTINUE
               END IF
            END IF
   80    CONTINUE
   90 CONTINUE
      RETURN
      END

C     -- the inverse transform --------------------------------------------

C     A complex FFT of N points, N a power of two, decimation in time,
C     from a bit reversal table and a twiddle table the caller supplies.
C     Two sizes are ever asked for -- 512 and 64 -- which is why the
C     tables are passed in rather than chosen here.
      SUBROUTINE IPFFTC(ZR, ZI, N, TR, TI, BR, NT)
      IMPLICIT NONE
      INTEGER N, NT
      DOUBLE PRECISION ZR(0:*), ZI(0:*), TR(0:*), TI(0:*)
      INTEGER BR(0:*)
      INTEGER I, J, K, LEN, H, STEP, P, Q
      DOUBLE PRECISION T, WR, WI, UR, UI, VR, VI
      DO 10 I = 0, N - 1
         J = BR(I)
         IF (J .GT. I) THEN
            T = ZR(I)
            ZR(I) = ZR(J)
            ZR(J) = T
            T = ZI(I)
            ZI(I) = ZI(J)
            ZI(J) = T
         END IF
   10 CONTINUE
      LEN = 2
   20 IF (LEN .GT. N) GOTO 60
         H = LEN / 2
         STEP = NT / H
         DO 50 K = 0, N - 1, LEN
            DO 40 J = 0, H - 1
               WR = TR(J*STEP)
               WI = TI(J*STEP)
               P = K + J
               Q = P + H
               UR = ZR(P)
               UI = ZI(P)
               VR = ZR(Q) * WR - ZI(Q) * WI
               VI = ZR(Q) * WI + ZI(Q) * WR
               ZR(P) = UR + VR
               ZI(P) = UI + VI
               ZR(Q) = UR - VR
               ZI(Q) = UI - VI
   40       CONTINUE
   50    CONTINUE
         LEN = LEN * 2
         GOTO 20
   60 CONTINUE
      RETURN
      END

C     A DCT-IV of M points as a complex FFT of M/2, with a rotation by
C     -pi(k + 1/8)/M before and after it.  The eighth turn is not a
C     typo: the DCT-IV's half-sample offsets in both index and frequency
C     split evenly between the two rotations, and a quarter turn there
C     -- the obvious guess -- gives an answer that is wrong by a few
C     parts in ten, which is quite loud enough to hear.
      SUBROUTINE IPDCT4(X, M, U)
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER M
      DOUBLE PRECISION X(0:*), U(0:*)
      INTEGER L, I
      DOUBLE PRECISION ZR(0:511), ZI(0:511), PR(0:511), PI(0:511)
      DOUBLE PRECISION RE, IM
      L = M / 2
      IF (M .EQ. 1024) THEN
         DO 10 I = 0, L - 1
            PR(I) = PTLR(I)
            PI(I) = PTLI(I)
   10    CONTINUE
      ELSE
         DO 20 I = 0, L - 1
            PR(I) = PTSR(I)
            PI(I) = PTSI(I)
   20    CONTINUE
      END IF
      DO 30 I = 0, L - 1
         RE = X(2*I)
         IM = X(M - 1 - 2*I)
         ZR(I) = RE * PR(I) - IM * PI(I)
         ZI(I) = RE * PI(I) + IM * PR(I)
   30 CONTINUE
      IF (M .EQ. 1024) THEN
         CALL IPFFTC(ZR, ZI, L, TWLR, TWLI, BRL, L/2)
      ELSE
         CALL IPFFTC(ZR, ZI, L, TWSR, TWSI, BRS, L/2)
      END IF
      DO 40 I = 0, L - 1
         RE = ZR(I) * PR(I) - ZI(I) * PI(I)
         IM = ZR(I) * PI(I) + ZI(I) * PR(I)
         U(2*I) = RE
         U(M - 1 - 2*I) = -IM
   40 CONTINUE
      RETURN
      END

C     The inverse MDCT of 4.6.11.2:
C
C       y(n) = 2/N sum_k spec(k) cos(2pi/N (n + 1/2 + N/4)(k + 1/2))
C
C     for n = 0 .. N-1, N = 2M.  Written out that way it is M multiplies
C     for each of 2M outputs, two million of them per long window, which
C     is most of a decoder's time.  Written as a DCT-IV of M points plus
C     the fold below -- even about -1/2, odd about M-1/2, and negated
C     every 2M -- it is one FFT of M/2 points, and the decoder runs
C     about forty times faster.
      SUBROUTINE IPIMDC(X, M, Y)
      IMPLICIT NONE
      INTEGER M
      DOUBLE PRECISION X(0:*), Y(0:*)
      DOUBLE PRECISION U(0:1023), SC, V
      INTEGER N, I, J, HALF
      N = 2 * M
      HALF = M / 2
      SC = 2.0D0 / DBLE(N)
      CALL IPDCT4(X, M, U)
      DO 10 I = 0, N - 1
         J = I + HALF
         IF (J .LT. M) THEN
            V = U(J)
         ELSE IF (J .LT. 2*M) THEN
            V = -U(2*M - 1 - J)
         ELSE
            V = -U(J - 2*M)
         END IF
         Y(I) = SC * V
   10 CONTINUE
      RETURN
      END

C     The same transform written as its definition, at M multiplies per
C     output.  Nothing calls this to decode anything; it exists so that
C     the test suite can hold the fast one to the formula it claims to
C     compute, on real coefficients, to fifteen figures.
      SUBROUTINE IPIMDS(X, M, Y)
      IMPLICIT NONE
      INTEGER M
      DOUBLE PRECISION X(0:*), Y(0:*)
      DOUBLE PRECISION PI, S, A
      INTEGER N, I, K
      N = 2 * M
      PI = 4.0D0 * ATAN(1.0D0)
      DO 20 I = 0, N - 1
         S = 0.0D0
         DO 10 K = 0, M - 1
            A = 2.0D0 * PI / DBLE(N)
     +          * (DBLE(I) + 0.5D0 + DBLE(N) / 4.0D0)
     +          * (DBLE(K) + 0.5D0)
            S = S + X(K) * COS(A)
   10    CONTINUE
         Y(I) = 2.0D0 / DBLE(N) * S
   20 CONTINUE
      RETURN
      END

C     4.6.11.3 and 4.6.11.4: window the transform's output and add it to
C     what the last frame left behind.
C
C     Every window's left half is built from the *previous* frame's
C     window shape and its right half from this one's, because the two
C     halves that overlap have to be the two halves of one window.  That
C     is the whole reason a decoder cannot start in the middle of a
C     stream: the first frame it sees has nothing to add to and comes
C     out half right.
      SUBROUTINE IPWOVL(CH, OUT)
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER CH
      DOUBLE PRECISION OUT(0:*)
      DOUBLE PRECISION WLFT(0:1023), WRGT(0:1023)
      DOUBLE PRECISION Y(0:2047), T(0:2047), XS(0:127), YS(0:255)
      DOUBLE PRECISION RS(0:127), FS(0:127), SC
      INTEGER I, W, B
C     The standard's decoder produces samples on the scale of a 16 bit
C     integer, because in 1997 that was what a sample was; everything
C     downstream of here wants them in [-1, 1].  The factor belongs at
C     the transform's output rather than in the scalefactor table so
C     that the coefficients the test hooks expose stay on the scale the
C     specification talks about them in.
      SC = 1.0D0 / 32768.0D0
      IF (WSEQ(CH) .NE. 2) THEN
C     The left half: a rising long window, or -- coming out of a run of
C     short windows -- 448 zeroes, a rising short window and 448 ones.
         IF (WSEQ(CH) .EQ. 3) THEN
            DO 10 I = 0, 447
               WLFT(I) = 0.0D0
   10       CONTINUE
            DO 20 I = 0, 127
               IF (PWSHP(CH) .EQ. 0) THEN
                  WLFT(448+I) = WSINS(I)
               ELSE
                  WLFT(448+I) = WKBDS(I)
               END IF
   20       CONTINUE
            DO 30 I = 576, 1023
               WLFT(I) = 1.0D0
   30       CONTINUE
         ELSE
            DO 40 I = 0, 1023
               IF (PWSHP(CH) .EQ. 0) THEN
                  WLFT(I) = WSINL(I)
               ELSE
                  WLFT(I) = WKBDL(I)
               END IF
   40       CONTINUE
         END IF
C     The right half: a falling long window, or -- going into a run of
C     short ones -- 448 ones, a falling short window and 448 zeroes.
         IF (WSEQ(CH) .EQ. 1) THEN
            DO 50 I = 0, 447
               WRGT(I) = 1.0D0
   50       CONTINUE
            DO 60 I = 0, 127
               IF (WSHP(CH) .EQ. 0) THEN
                  WRGT(448+I) = WSINS(127-I)
               ELSE
                  WRGT(448+I) = WKBDS(127-I)
               END IF
   60       CONTINUE
            DO 70 I = 576, 1023
               WRGT(I) = 0.0D0
   70       CONTINUE
         ELSE
            DO 80 I = 0, 1023
               IF (WSHP(CH) .EQ. 0) THEN
                  WRGT(I) = WSINL(1023-I)
               ELSE
                  WRGT(I) = WKBDL(1023-I)
               END IF
   80       CONTINUE
         END IF
         CALL IPIMDC(SPEC(0,CH), 1024, Y)
         DO 90 I = 0, 1023
            OUT(I) = OVLP(I,CH) + SC * Y(I) * WLFT(I)
   90    CONTINUE
         DO 100 I = 0, 1023
            OVLP(I,CH) = SC * Y(1024+I) * WRGT(I)
  100    CONTINUE
      ELSE
C     Eight short windows, each of 256 points, overlapped 128 apart and
C     laid down starting 448 samples into the frame -- which is what
C     makes LONG_START's flat 448 and zero 448 the shape they are.
         DO 110 I = 0, 2047
            T(I) = 0.0D0
  110    CONTINUE
         DO 160 W = 0, 7
            DO 120 I = 0, 127
               XS(I) = SPEC(128*W+I,CH)
  120       CONTINUE
            CALL IPIMDC(XS, 128, YS)
            DO 130 I = 0, 127
               IF (W .EQ. 0) THEN
                  IF (PWSHP(CH) .EQ. 0) THEN
                     RS(I) = WSINS(I)
                  ELSE
                     RS(I) = WKBDS(I)
                  END IF
               ELSE
                  IF (WSHP(CH) .EQ. 0) THEN
                     RS(I) = WSINS(I)
                  ELSE
                     RS(I) = WKBDS(I)
                  END IF
               END IF
               IF (WSHP(CH) .EQ. 0) THEN
                  FS(I) = WSINS(127-I)
               ELSE
                  FS(I) = WKBDS(127-I)
               END IF
  130       CONTINUE
            B = 448 + 128 * W
            DO 140 I = 0, 127
               T(B+I) = T(B+I) + SC * YS(I) * RS(I)
  140       CONTINUE
            DO 150 I = 0, 127
               T(B+128+I) = T(B+128+I) + SC * YS(128+I) * FS(I)
  150       CONTINUE
  160    CONTINUE
         DO 170 I = 0, 1023
            OUT(I) = OVLP(I,CH) + T(I)
  170    CONTINUE
         DO 180 I = 0, 1023
            OVLP(I,CH) = T(1024+I)
  180    CONTINUE
      END IF
      PWSHP(CH) = WSHP(CH)
      PWSEQ(CH) = WSEQ(CH)
      RETURN
      END
