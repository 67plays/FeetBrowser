C     The inverse transform, the overlap, the frequency inversion and the
C     polyphase synthesis filterbank -- everything from a spectrum to
C     samples.
C
C     Both transforms here are written as the sums the standard defines
C     them by rather than as fast algorithms.  An 18-point inverse MDCT
C     costs 648 multiplies done directly and a 32-band synthesis costs
C     2560; over a whole second of stereo audio that is about six million
C     of them, which is nothing on any machine that can run a browser,
C     and it buys a transform a test can check against the standard's own
C     summation term for term.  The fast versions are where transform
C     bugs live.

C     The inverse MDCT and the overlap for one granule and channel.  SB
C     comes back as eighteen samples for each of the 32 subbands.
C
C     A long block is one 36-point transform of 18 coefficients, windowed
C     and split in half: the first half is added to the tail the last
C     granule left, and the second half is kept as the tail for the next.
C     A short block is three 12-point transforms of 6 coefficients each,
C     windowed and laid down overlapping by six samples inside the same
C     36-point frame, so the arithmetic after them is identical.
C
C     A mixed block is a short block whose lowest two subbands are long,
C     and they take the normal window rather than the start window --
C     there is no window switching going on within them, only within the
C     subbands above.
      SUBROUTINE BLIMDC(GR, CH, SB)
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER GR, CH
      DOUBLE PRECISION SB(0:17,0:31)
      INTEGER SBI, I, K, W, BT, WI, LEND
      DOUBLE PRECISION Z(0:35), R(0:11), S

      BT = BLKTYP(GR, CH)
      LEND = MXSB
      IF (BT .EQ. 2) THEN
         IF (MIXBLK(GR, CH) .EQ. 1) THEN
            LEND = 2
         ELSE
            LEND = 0
         END IF
      END IF

      DO 90 SBI = 0, MXSB - 1
         DO 10 I = 0, 35
            Z(I) = 0.0D0
   10    CONTINUE
         IF (SBI .LT. LEND) THEN
            WI = BT
            IF (BT .EQ. 2) WI = 0
            DO 30 I = 0, 35
               S = 0.0D0
               DO 20 K = 0, 17
                  S = S + XR(18 * SBI + K, CH) * CIM(I, K)
   20          CONTINUE
               Z(I) = S * IMDW(I, WI)
   30       CONTINUE
         ELSE IF (BT .EQ. 2) THEN
            DO 60 W = 0, 2
               DO 50 I = 0, 11
                  S = 0.0D0
                  DO 40 K = 0, 5
                     S = S + XR(18 * SBI + 3 * K + W, CH) * CIS(I, K)
   40             CONTINUE
                  R(I) = S * IMDW(I, 2)
   50          CONTINUE
               DO 55 I = 0, 11
                  Z(6 * W + 6 + I) = Z(6 * W + 6 + I) + R(I)
   55          CONTINUE
   60       CONTINUE
         ELSE
            DO 80 I = 0, 35
               S = 0.0D0
               DO 70 K = 0, 17
                  S = S + XR(18 * SBI + K, CH) * CIM(I, K)
   70          CONTINUE
               Z(I) = S * IMDW(I, BT)
   80       CONTINUE
         END IF

         DO 85 I = 0, 17
            SB(I, SBI) = Z(I) + OVER(18 * SBI + I, CH)
            OVER(18 * SBI + I, CH) = Z(18 + I)
   85    CONTINUE

C        Frequency inversion.  Every other subband comes out of the
C        analysis filterbank with its spectrum flipped, and the flip is
C        undone by negating every other sample rather than by anything
C        more expensive.
         IF (MOD(SBI, 2) .EQ. 1) THEN
            DO 88 I = 1, 17, 2
               SB(I, SBI) = -SB(I, SBI)
   88       CONTINUE
         END IF
   90 CONTINUE
      RETURN
      END

C     The polyphase synthesis filterbank, ISO/IEC 11172-3 Annex B, figure
C     A.2: 32 subband samples in, 32 audio samples out, eighteen times a
C     granule.
C
C     The V vector is a thousand-and-twenty-four sample history that
C     every output depends on, so it is carried across granules and
C     across frames like the transform's overlap.  It is held as a ring
C     rather than shifted by 64 each time -- the shift is what the
C     standard's flowchart draws, and doing it literally would move eight
C     kilobytes eighteen times per granule per channel for no reason.
      SUBROUTINE BLSYNT(CH, SB, BASE)
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER CH, BASE
      DOUBLE PRECISION SB(0:17,0:31)
      INTEGER T, I, J, K, P
      DOUBLE PRECISION S

      DO 60 T = 0, 17
         VPOS(CH) = MOD(VPOS(CH) + 1024 - 64, 1024)
         DO 20 I = 0, 63
            S = 0.0D0
            DO 10 K = 0, 31
               S = S + NMAT(I, K) * SB(T, K)
   10       CONTINUE
            VBUF(MOD(VPOS(CH) + I, 1024), CH) = S
   20    CONTINUE
         DO 40 J = 0, 31
            S = 0.0D0
            DO 30 I = 0, 15
               P = J + 32 * I
               S = S + VBUF(MOD(VPOS(CH) + UMAP(P), 1024), CH) * SYNW(P)
   30       CONTINUE
            PCMO(BASE + (T * 32 + J) * NOUTCH + CH - 1) = S
   40    CONTINUE
   60 CONTINUE
      RETURN
      END
