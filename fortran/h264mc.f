C     Inter prediction, clause 8.4: where a macroblock's motion vectors
C     come from and what they fetch.
C
C     Two halves.  8.4.1 derives a vector -- the median of three
C     neighbours, corrected by the special cases that 16x8 and 8x16
C     partitions and P_Skip have -- and 8.4.2 uses it to interpolate a
C     block out of a reference picture at quarter-sample resolution.
C
C     Three arithmetic traps live in here, and all three produce a
C     picture rather than an error when they are got wrong:
C
C       * a motion vector is signed and is shifted by two to reach whole
C         samples.  The spec's >> floors; Fortran's ISHFT is a logical
C         shift and its integer division truncates towards zero.  Both
C         are wrong for exactly the negative half of the vectors, which
C         is why every shift below is SHIFTA and every "remainder" is
C         IAND with 3 or 7 rather than MOD.
C       * the six-tap filter's rounding is (x + 16) >> 5 for a half
C         sample and (x + 512) >> 10 for the centre one, on the
C         unclipped intermediate and not on the clipped one.  Clipping
C         early is a difference of one in the last bit, once every few
C         thousand samples, which no eye and no PSNR figure will find.
C       * a vector may point outside the picture, and the samples it
C         asks for then are the edge samples repeated -- clipped
C         per-sample inside the filter's window, so a filter tap that
C         falls off the edge and one that does not are read the same
C         way.

C     One luma sample of reference slot K, with the picture's edges
C     extended outwards for ever.
      INTEGER FUNCTION H2RY(K, X, Y)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER K, X, Y, XC, YC
      XC = MAX(0, MIN(PICW - 1, X))
      YC = MAX(0, MIN(PICH - 1, Y))
      H2RY = IAND(INT(DPY(YC * MXW + XC + 1, K)), 255)
      RETURN
      END

C     The same for one chroma plane; C is 1 for Cb and 2 for Cr.
      INTEGER FUNCTION H2RC(K, C, X, Y)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER K, C, X, Y, XC, YC, I
      XC = MAX(0, MIN(PICW / 2 - 1, X))
      YC = MAX(0, MIN(PICH / 2 - 1, Y))
      I = YC * (MXW / 2) + XC + 1
      IF (C .EQ. 1) THEN
         H2RC = IAND(INT(DPU(I, K)), 255)
      ELSE
         H2RC = IAND(INT(DPV(I, K)), 255)
      END IF
      RETURN
      END

C     The motion of the 4x4 block at (BX, BY), in units of 4x4 blocks
C     relative to the current macroblock: -1 reaches into the macroblock
C     to the left or above, 4 into the one above and to the right.
C
C     AV is availability in the sense 8.4.1.3.1 asks about -- does that
C     partition exist and has it been decoded -- and is not the same
C     question as whether it has a motion vector.  An intra neighbour is
C     available and contributes a zero vector with reference index -1;
C     that distinction is what decides whether B and C fall back to A.
      SUBROUTINE H2GETN(BX, BY, AV, RF, VX, VY)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER BX, BY, AV, RF, VX, VY
      INTEGER MB, NX, NY, I
      AV = 0
      RF = -1
      VX = 0
      VY = 0
      IF (BY .LT. 0) THEN
         IF (BX .LT. 0) THEN
            IF (AVLD .EQ. 0) RETURN
            MB = ADRD
            NX = 3
         ELSE IF (BX .LE. 3) THEN
            IF (AVLB .EQ. 0) RETURN
            MB = ADRB
            NX = BX
         ELSE
            IF (AVLC .EQ. 0) RETURN
            MB = ADRC
            NX = 0
         END IF
         NY = 3
      ELSE IF (BX .LT. 0) THEN
         IF (BY .GT. 3) RETURN
         IF (AVLA .EQ. 0) RETURN
         MB = ADRA
         NX = 3
         NY = BY
      ELSE IF (BX .GT. 3 .OR. BY .GT. 3) THEN
C     To the right of this macroblock and level with it, or below it:
C     nothing there has been decoded.
         RETURN
      ELSE
C     Inside the macroblock being decoded.  A partition that comes later
C     in decoding order than this one has no motion yet, which is the
C     "not available" of 6.4.11.7 rather than a special case of it.
         I = 1 + BX + 4 * BY
         IF (CMVOK(I) .EQ. 0) RETURN
         AV = 1
         RF = CREF(1 + BX / 2 + 2 * (BY / 2))
         VX = CMVX(I)
         VY = CMVY(I)
         IF (CINTR .NE. 0) THEN
            RF = -1
            VX = 0
            VY = 0
         END IF
         RETURN
      END IF
      AV = 1
      IF (MINT(MB + 1) .NE. 0) RETURN
      I = 1 + NX + 4 * NY
      RF = MREF(1 + NX / 2 + 2 * (NY / 2), MB + 1)
      VX = MMVX(I, MB + 1)
      VY = MMVY(I, MB + 1)
      RETURN
      END

C     The absolute mvd of a neighbouring 4x4 block, for 9.3.3.1.1.7's
C     context.  A block that is missing, intra or skipped contributes
C     nothing, which is the same answer as a block whose vector was
C     exactly predicted.
      SUBROUTINE H2GETD(BX, BY, DX, DY)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER BX, BY, DX, DY
      INTEGER MB, NX, NY, I
      DX = 0
      DY = 0
      IF (BY .LT. 0) THEN
         IF (BX .LT. 0) RETURN
         IF (BX .GT. 3) RETURN
         IF (AVLB .EQ. 0) RETURN
         MB = ADRB
         NX = BX
         NY = 3
      ELSE IF (BX .LT. 0) THEN
         IF (BY .GT. 3) RETURN
         IF (AVLA .EQ. 0) RETURN
         MB = ADRA
         NX = 3
         NY = BY
      ELSE IF (BX .GT. 3 .OR. BY .GT. 3) THEN
         RETURN
      ELSE
         I = 1 + BX + 4 * BY
         IF (CMVOK(I) .EQ. 0) RETURN
         DX = CMDX(I)
         DY = CMDY(I)
         RETURN
      END IF
      IF (MINT(MB + 1) .NE. 0) RETURN
      I = 1 + NX + 4 * NY
      DX = MMDX(I, MB + 1)
      DY = MMDY(I, MB + 1)
      RETURN
      END

C     8.4.1.3: the predicted motion vector for a partition whose top left
C     4x4 block is (BX, BY) and which is PBW blocks wide, predicting from
C     reference index RI.  MODE picks the directional special cases: 1
C     and 2 are the two halves of a 16x8, 3 and 4 the two halves of an
C     8x16, and 0 is everything else, which is the median.
      SUBROUTINE H2MVPR(BX, BY, PBW, RI, MODE, PVX, PVY)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER BX, BY, PBW, RI, MODE, PVX, PVY
      INTEGER AVA, RFA, VXA, VYA, AVB, RFB, VXB, VYB
      INTEGER AVC, RFC, VXC, VYC, N
      CALL H2GETN(BX - 1, BY, AVA, RFA, VXA, VYA)
      CALL H2GETN(BX, BY - 1, AVB, RFB, VXB, VYB)
      CALL H2GETN(BX + PBW, BY - 1, AVC, RFC, VXC, VYC)
      IF (AVC .EQ. 0) THEN
C     8.4.1.3: when the block above and to the right is missing, the one
C     above and to the left stands in for it.
         CALL H2GETN(BX - 1, BY - 1, AVC, RFC, VXC, VYC)
      END IF
      IF (AVB .EQ. 0 .AND. AVC .EQ. 0 .AND. AVA .NE. 0) THEN
         RFB = RFA
         VXB = VXA
         VYB = VYA
         RFC = RFA
         VXC = VXA
         VYC = VYA
      END IF
      IF (MODE .EQ. 1 .AND. RFB .EQ. RI) THEN
         PVX = VXB
         PVY = VYB
         RETURN
      ELSE IF (MODE .EQ. 2 .AND. RFA .EQ. RI) THEN
         PVX = VXA
         PVY = VYA
         RETURN
      ELSE IF (MODE .EQ. 3 .AND. RFA .EQ. RI) THEN
         PVX = VXA
         PVY = VYA
         RETURN
      ELSE IF (MODE .EQ. 4 .AND. RFC .EQ. RI) THEN
         PVX = VXC
         PVY = VYC
         RETURN
      END IF
C     8.4.1.3.1: exactly one neighbour using this reference picture makes
C     the choice by itself; otherwise the three vote and the median wins.
      N = 0
      IF (RFA .EQ. RI) N = N + 1
      IF (RFB .EQ. RI) N = N + 1
      IF (RFC .EQ. RI) N = N + 1
      IF (N .EQ. 1) THEN
         IF (RFA .EQ. RI) THEN
            PVX = VXA
            PVY = VYA
         ELSE IF (RFB .EQ. RI) THEN
            PVX = VXB
            PVY = VYB
         ELSE
            PVX = VXC
            PVY = VYC
         END IF
         RETURN
      END IF
      PVX = VXA + VXB + VXC - MIN(VXA, VXB, VXC) - MAX(VXA, VXB, VXC)
      PVY = VYA + VYB + VYC - MIN(VYA, VYB, VYC) - MAX(VYA, VYB, VYC)
      RETURN
      END

C     8.4.1.1, P_Skip: the macroblock has no syntax of its own at all, so
C     its vector is the 16x16 prediction from reference index zero --
C     except at the top or left edge of a slice, and except when either
C     neighbour is itself sitting still on the same picture, where it is
C     zero.  That exception is what makes a static background cost one
C     bin per macroblock.
      SUBROUTINE H2SKMV(VX, VY)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER VX, VY
      INTEGER AV, RF, X, Y
      VX = 0
      VY = 0
      IF (AVLA .EQ. 0 .OR. AVLB .EQ. 0) RETURN
      CALL H2GETN(-1, 0, AV, RF, X, Y)
      IF (RF .EQ. 0 .AND. X .EQ. 0 .AND. Y .EQ. 0) RETURN
      CALL H2GETN(0, -1, AV, RF, X, Y)
      IF (RF .EQ. 0 .AND. X .EQ. 0 .AND. Y .EQ. 0) RETURN
      CALL H2MVPR(0, 0, 4, 0, 0, VX, VY)
      RETURN
      END

C     8.4.2.2.1: a W by H luma block at quarter-sample position, from
C     slot K, into the macroblock's prediction array at (XA, YA).
C
C     The three intermediate arrays are the whole of the arithmetic.  HZ
C     is the six-tap filter run across every row the block can see, VT
C     the same run down every column, and J1 the centre position, which
C     is HZ filtered a second time vertically and therefore carries ten
C     bits of gain rather than five.  Everything in Table 8-12 is one of
C     those three, a whole sample, or the average of two of them.
      SUBROUTINE H2MCY(K, XA, YA, W, H, MX, MY, RI, PRD)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER K, XA, YA, W, H, MX, MY, RI
      INTEGER PRD(0:15,0:15)
      INTEGER REF(0:21,0:21), HZ(0:15,0:21), VT(0:21,0:15)
      INTEGER J1(0:15,0:15)
      INTEGER XI, YI, XF, YF, I, J, V, GG, HH, MM, BB, SS, HV, MV, JJ
      INTEGER DOJ, H2RY
      EXTERNAL H2RY
      XF = IAND(MX, 3)
      YF = IAND(MY, 3)
      XI = CMBX * 16 + XA + SHIFTA(MX, 2)
      YI = CMBY * 16 + YA + SHIFTA(MY, 2)
      DO 20 J = 0, H + 5
         DO 10 I = 0, W + 5
            REF(I, J) = H2RY(K, XI + I - 2, YI + J - 2)
   10    CONTINUE
   20 CONTINUE
      DOJ = 0
      IF (XF .EQ. 2 .AND. YF .NE. 0) DOJ = 1
      IF (YF .EQ. 2 .AND. XF .NE. 0) DOJ = 1
      IF (XF .NE. 0) THEN
         DO 40 J = 0, H + 5
            DO 30 I = 0, W - 1
               HZ(I, J) = REF(I, J) - 5 * REF(I+1, J) + 20 * REF(I+2, J)
     +                    + 20 * REF(I+3, J) - 5 * REF(I+4, J)
     +                    + REF(I+5, J)
   30       CONTINUE
   40    CONTINUE
      END IF
      IF (YF .NE. 0) THEN
         DO 60 J = 0, H - 1
            DO 50 I = 0, W + 5
               VT(I, J) = REF(I, J) - 5 * REF(I, J+1) + 20 * REF(I, J+2)
     +                    + 20 * REF(I, J+3) - 5 * REF(I, J+4)
     +                    + REF(I, J+5)
   50       CONTINUE
   60    CONTINUE
      END IF
      IF (DOJ .NE. 0) THEN
         DO 80 J = 0, H - 1
            DO 70 I = 0, W - 1
               J1(I, J) = HZ(I, J) - 5 * HZ(I, J+1) + 20 * HZ(I, J+2)
     +                    + 20 * HZ(I, J+3) - 5 * HZ(I, J+4)
     +                    + HZ(I, J+5)
   70       CONTINUE
   80    CONTINUE
      END IF

      DO 100 J = 0, H - 1
         DO 90 I = 0, W - 1
            GG = REF(I+2, J+2)
            HH = REF(I+3, J+2)
            MM = REF(I+2, J+3)
            BB = 0
            SS = 0
            HV = 0
            MV = 0
            JJ = 0
            IF (XF .NE. 0) THEN
               BB = MAX(0, MIN(255, SHIFTA(HZ(I, J+2) + 16, 5)))
               SS = MAX(0, MIN(255, SHIFTA(HZ(I, J+3) + 16, 5)))
            END IF
            IF (YF .NE. 0) THEN
               HV = MAX(0, MIN(255, SHIFTA(VT(I+2, J) + 16, 5)))
               MV = MAX(0, MIN(255, SHIFTA(VT(I+3, J) + 16, 5)))
            END IF
            IF (DOJ .NE. 0) THEN
               JJ = MAX(0, MIN(255, SHIFTA(J1(I, J) + 512, 10)))
            END IF
C     Table 8-12, read as four rows of four.
            IF (YF .EQ. 0) THEN
               IF (XF .EQ. 0) THEN
                  V = GG
               ELSE IF (XF .EQ. 1) THEN
                  V = SHIFTA(GG + BB + 1, 1)
               ELSE IF (XF .EQ. 2) THEN
                  V = BB
               ELSE
                  V = SHIFTA(HH + BB + 1, 1)
               END IF
            ELSE IF (YF .EQ. 1) THEN
               IF (XF .EQ. 0) THEN
                  V = SHIFTA(GG + HV + 1, 1)
               ELSE IF (XF .EQ. 1) THEN
                  V = SHIFTA(BB + HV + 1, 1)
               ELSE IF (XF .EQ. 2) THEN
                  V = SHIFTA(BB + JJ + 1, 1)
               ELSE
                  V = SHIFTA(BB + MV + 1, 1)
               END IF
            ELSE IF (YF .EQ. 2) THEN
               IF (XF .EQ. 0) THEN
                  V = HV
               ELSE IF (XF .EQ. 1) THEN
                  V = SHIFTA(HV + JJ + 1, 1)
               ELSE IF (XF .EQ. 2) THEN
                  V = JJ
               ELSE
                  V = SHIFTA(JJ + MV + 1, 1)
               END IF
            ELSE
               IF (XF .EQ. 0) THEN
                  V = SHIFTA(MM + HV + 1, 1)
               ELSE IF (XF .EQ. 1) THEN
                  V = SHIFTA(HV + SS + 1, 1)
               ELSE IF (XF .EQ. 2) THEN
                  V = SHIFTA(JJ + SS + 1, 1)
               ELSE
                  V = SHIFTA(MV + SS + 1, 1)
               END IF
            END IF
            IF (WPRED .NE. 0) THEN
               IF (LOGWL .GE. 1) THEN
                  V = SHIFTA(V * WPL(RI) + ISHFT(1, LOGWL - 1), LOGWL)
     +                + WOL(RI)
               ELSE
                  V = V * WPL(RI) + WOL(RI)
               END IF
               V = MAX(0, MIN(255, V))
            END IF
            PRD(XA + I, YA + J) = V
   90    CONTINUE
  100 CONTINUE
      RETURN
      END

C     8.4.2.2.2: chroma, where the vector has three fractional bits
C     because the plane is half the size, and where the filter is a
C     bilinear one over four samples rather than a six-tap one.  Both
C     planes are done at once because they share every weight.
      SUBROUTINE H2MCC(K, XA, YA, W, H, MX, MY, RI, PU8, PV8)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER K, XA, YA, W, H, MX, MY, RI
      INTEGER PU8(0:7,0:7), PV8(0:7,0:7)
      INTEGER XI, YI, XF, YF, I, J, C, V, W00, W10, W01, W11
      INTEGER A, B, CC, D, H2RC
      EXTERNAL H2RC
      XF = IAND(MX, 7)
      YF = IAND(MY, 7)
      XI = CMBX * 8 + XA + SHIFTA(MX, 3)
      YI = CMBY * 8 + YA + SHIFTA(MY, 3)
      W00 = (8 - XF) * (8 - YF)
      W10 = XF * (8 - YF)
      W01 = (8 - XF) * YF
      W11 = XF * YF
      DO 30 C = 1, 2
         DO 20 J = 0, H - 1
            DO 10 I = 0, W - 1
               A = H2RC(K, C, XI + I, YI + J)
               B = H2RC(K, C, XI + I + 1, YI + J)
               CC = H2RC(K, C, XI + I, YI + J + 1)
               D = H2RC(K, C, XI + I + 1, YI + J + 1)
               V = SHIFTA(W00 * A + W10 * B + W01 * CC + W11 * D + 32,
     +                    6)
               IF (WPRED .NE. 0) THEN
                  IF (C .EQ. 1) THEN
                     IF (LOGWC .GE. 1) THEN
                        V = SHIFTA(V * WPCB(RI)
     +                             + ISHFT(1, LOGWC - 1), LOGWC)
     +                      + WOCB(RI)
                     ELSE
                        V = V * WPCB(RI) + WOCB(RI)
                     END IF
                  ELSE
                     IF (LOGWC .GE. 1) THEN
                        V = SHIFTA(V * WPCR(RI)
     +                             + ISHFT(1, LOGWC - 1), LOGWC)
     +                      + WOCR(RI)
                     ELSE
                        V = V * WPCR(RI) + WOCR(RI)
                     END IF
                  END IF
                  V = MAX(0, MIN(255, V))
               END IF
               IF (C .EQ. 1) THEN
                  PU8(XA + I, YA + J) = V
               ELSE
                  PV8(XA + I, YA + J) = V
               END IF
   10       CONTINUE
   20    CONTINUE
   30 CONTINUE
      RETURN
      END

C     The partitions of the macroblock being decoded, in decoding order,
C     measured in 4x4 blocks.  Table 7-13 gives the four macroblock
C     shapes and Table 7-17 the four sub-macroblock shapes; between them
C     a P macroblock is between one and sixteen rectangles.
C
C     MODE is which of 8.4.1.3's special cases the partition's predictor
C     takes: the two halves of a 16x8 look up and left respectively, the
C     two halves of an 8x16 look left and up-right, and everything else
C     takes the median.  Carrying it here rather than working it out
C     again in the predictor keeps the shape of a macroblock described in
C     exactly one place.
      SUBROUTINE H2PLST(NP, BX, BY, BW, BH, MODE)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER NP, BX(16), BY(16), BW(16), BH(16), MODE(16)
      INTEGER I, K, S, X8, Y8
      NP = 0
      IF (CPTYP .EQ. 31) THEN
         NP = 2
         BX(1) = 0
         BY(1) = 0
         BW(1) = 4
         BH(1) = 2
         MODE(1) = 1
         BX(2) = 0
         BY(2) = 2
         BW(2) = 4
         BH(2) = 2
         MODE(2) = 2
      ELSE IF (CPTYP .EQ. 32) THEN
         NP = 2
         BX(1) = 0
         BY(1) = 0
         BW(1) = 2
         BH(1) = 4
         MODE(1) = 3
         BX(2) = 2
         BY(2) = 0
         BW(2) = 2
         BH(2) = 4
         MODE(2) = 4
      ELSE IF (CPTYP .EQ. 33) THEN
         DO 20 K = 0, 3
            X8 = MOD(K, 2) * 2
            Y8 = (K / 2) * 2
            S = CSUB(K + 1)
            IF (S .EQ. 0) THEN
               NP = NP + 1
               BX(NP) = X8
               BY(NP) = Y8
               BW(NP) = 2
               BH(NP) = 2
            ELSE IF (S .EQ. 1) THEN
               DO 5 I = 0, 1
                  NP = NP + 1
                  BX(NP) = X8
                  BY(NP) = Y8 + I
                  BW(NP) = 2
                  BH(NP) = 1
    5          CONTINUE
            ELSE IF (S .EQ. 2) THEN
               DO 10 I = 0, 1
                  NP = NP + 1
                  BX(NP) = X8 + I
                  BY(NP) = Y8
                  BW(NP) = 1
                  BH(NP) = 2
   10          CONTINUE
            ELSE
               DO 15 I = 0, 3
                  NP = NP + 1
                  BX(NP) = X8 + MOD(I, 2)
                  BY(NP) = Y8 + I / 2
                  BW(NP) = 1
                  BH(NP) = 1
   15          CONTINUE
            END IF
   20    CONTINUE
         DO 30 I = 1, NP
            MODE(I) = 0
   30    CONTINUE
      ELSE
         NP = 1
         BX(1) = 0
         BY(1) = 0
         BW(1) = 4
         BH(1) = 4
         MODE(1) = 0
      END IF
      RETURN
      END

C     Predict every partition of the inter macroblock now being decoded
C     and add its residual.
C
C     Each partition is interpolated on its own, which is not merely
C     allowed but required: two partitions of one macroblock may point at
C     different pictures, and even when they point at the same one their
C     filter windows overlap and each has to see the reference picture
C     rather than its neighbour's output.
      SUBROUTINE H2PMB(ST)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER ST
      INTEGER PRD(0:15,0:15), PU8(0:7,0:7), PV8(0:7,0:7)
      INTEGER NP, BX(16), BY(16), BW(16), BH(16), MODE(16)
      INTEGER I, BI, RI, SLOT
      ST = 0
      CALL H2PLST(NP, BX, BY, BW, BH, MODE)
      DO 10 I = 1, NP
         BI = 1 + BX(I) + 4 * BY(I)
         RI = CREF(1 + BX(I) / 2 + 2 * (BY(I) / 2))
         IF (RI .LT. 0 .OR. RI .GE. RL0N) THEN
            ST = -54
            RETURN
         END IF
         SLOT = RL0(RI)
         IF (SLOT .LT. 1 .OR. SLOT .GT. MXREF) THEN
            ST = -54
            RETURN
         END IF
         IF (DPUSE(SLOT) .EQ. 0) THEN
            ST = -54
            RETURN
         END IF
         CALL H2MCY(SLOT, 4 * BX(I), 4 * BY(I), 4 * BW(I), 4 * BH(I),
     +              CMVX(BI), CMVY(BI), RI, PRD)
         CALL H2MCC(SLOT, 2 * BX(I), 2 * BY(I), 2 * BW(I), 2 * BH(I),
     +              CMVX(BI), CMVY(BI), RI, PU8, PV8)
   10 CONTINUE

      CALL H2ADDY(PRD)
      CALL H2ADDC(PU, 1, PU8)
      CALL H2ADDC(PV, 2, PV8)
      RETURN
      END
