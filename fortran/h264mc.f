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
C     L is the reference picture list being predicted, 1 or 2.  A
C     partition that did not use that list answers with reference index
C     -1 and a zero vector, which is deliberately the same answer an
C     intra neighbour gives: 8.4.1.3.2 makes no distinction between them.
      SUBROUTINE H2GETN(L, BX, BY, AV, RF, VX, VY)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER L, BX, BY, AV, RF, VX, VY
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
         IF (CMVOK(I, L) .EQ. 0) RETURN
         AV = 1
         RF = CREF(1 + BX / 2 + 2 * (BY / 2), L)
         VX = CMVX(I, L)
         VY = CMVY(I, L)
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
      RF = MREF(1 + NX / 2 + 2 * (NY / 2), L, MB + 1)
      VX = MMVX(I, L, MB + 1)
      VY = MMVY(I, L, MB + 1)
      IF (RF .LT. 0) THEN
         VX = 0
         VY = 0
      END IF
      RETURN
      END

C     The absolute mvd of a neighbouring 4x4 block, for 9.3.3.1.1.7's
C     context.  A block that is missing, intra or skipped contributes
C     nothing, which is the same answer as a block whose vector was
C     exactly predicted.
      SUBROUTINE H2GETD(L, BX, BY, DX, DY)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER L, BX, BY, DX, DY
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
         IF (CMVOK(I, L) .EQ. 0) RETURN
         DX = CMDX(I, L)
         DY = CMDY(I, L)
         RETURN
      END IF
      IF (MINT(MB + 1) .NE. 0) RETURN
      I = 1 + NX + 4 * NY
      DX = MMDX(I, L, MB + 1)
      DY = MMDY(I, L, MB + 1)
      RETURN
      END

C     8.4.1.3: the predicted motion vector for a partition whose top left
C     4x4 block is (BX, BY) and which is PBW blocks wide, predicting from
C     reference index RI.  MODE picks the directional special cases: 1
C     and 2 are the two halves of a 16x8, 3 and 4 the two halves of an
C     8x16, and 0 is everything else, which is the median.
      SUBROUTINE H2MVPR(L, BX, BY, PBW, RI, MODE, PVX, PVY)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER L, BX, BY, PBW, RI, MODE, PVX, PVY
      INTEGER AVA, RFA, VXA, VYA, AVB, RFB, VXB, VYB
      INTEGER AVC, RFC, VXC, VYC, N
      CALL H2GETN(L, BX - 1, BY, AVA, RFA, VXA, VYA)
      CALL H2GETN(L, BX, BY - 1, AVB, RFB, VXB, VYB)
      CALL H2GETN(L, BX + PBW, BY - 1, AVC, RFC, VXC, VYC)
      IF (AVC .EQ. 0) THEN
C     8.4.1.3: when the block above and to the right is missing, the one
C     above and to the left stands in for it.
         CALL H2GETN(L, BX - 1, BY - 1, AVC, RFC, VXC, VYC)
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
      CALL H2GETN(1, -1, 0, AV, RF, X, Y)
      IF (RF .EQ. 0 .AND. X .EQ. 0 .AND. Y .EQ. 0) RETURN
      CALL H2GETN(1, 0, -1, AV, RF, X, Y)
      IF (RF .EQ. 0 .AND. X .EQ. 0 .AND. Y .EQ. 0) RETURN
      CALL H2MVPR(1, 0, 0, 4, 0, 0, VX, VY)
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
C     The result is the unweighted prediction of 8.4.2.2.  8.4.2.3 is a
C     separate pass in H2PMB, because a bi-predicted partition has to
C     weight two of these against each other and cannot do it while
C     either one is still being interpolated.
      SUBROUTINE H2MCY(K, XA, YA, W, H, MX, MY, PRD)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER K, XA, YA, W, H, MX, MY
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
            PRD(XA + I, YA + J) = V
   90    CONTINUE
  100 CONTINUE
      RETURN
      END

C     8.4.2.2.2: chroma, where the vector has three fractional bits
C     because the plane is half the size, and where the filter is a
C     bilinear one over four samples rather than a six-tap one.  Both
C     planes are done at once because they share every weight.
      SUBROUTINE H2MCC(K, XA, YA, W, H, MX, MY, PU8, PV8)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER K, XA, YA, W, H, MX, MY
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

C     The shape of one sub-macroblock.  Table 7-17 for a P slice, Table
C     7-18 for a B slice; NS partitions of PW by PH 4x4 blocks each.
C
C     A direct 8x8 has no shape of its own.  It is listed as one 8x8 when
C     direct_8x8_inference_flag is set and as four 4x4s when it is not,
C     because that is the granularity at which 8.4.1.2 derives motion for
C     it, and the prediction is the same either way: interpolating a
C     rectangle in pieces reads exactly the reference samples that
C     interpolating it whole would read.
      SUBROUTINE H2SSHP(S, NS, PW, PH)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER S, NS, PW, PH
      NS = 1
      PW = 2
      PH = 2
      IF (SLTYPE .NE. 1) THEN
         IF (S .EQ. 1) THEN
            NS = 2
            PH = 1
         ELSE IF (S .EQ. 2) THEN
            NS = 2
            PW = 1
         ELSE IF (S .EQ. 3) THEN
            NS = 4
            PW = 1
            PH = 1
         END IF
         RETURN
      END IF
      IF (S .EQ. 0) THEN
         IF (D8INF .EQ. 0) THEN
            NS = 4
            PW = 1
            PH = 1
         END IF
      ELSE IF (S .EQ. 4 .OR. S .EQ. 6 .OR. S .EQ. 8) THEN
         NS = 2
         PH = 1
      ELSE IF (S .EQ. 5 .OR. S .EQ. 7 .OR. S .EQ. 9) THEN
         NS = 2
         PW = 1
      ELSE IF (S .GE. 10) THEN
         NS = 4
         PW = 1
         PH = 1
      END IF
      RETURN
      END

C     Which lists a B sub-macroblock type predicts from, as 1 for list 0
C     only, 2 for list 1 only and 3 for both.  Table 7-18 again, read
C     down its SubMbPredMode column.
      INTEGER FUNCTION H2SPRD(S)
      IMPLICIT NONE
      INTEGER S
      IF (S .EQ. 1 .OR. S .EQ. 4 .OR. S .EQ. 5 .OR. S .EQ. 10) THEN
         H2SPRD = 1
      ELSE IF (S .EQ. 2 .OR. S .EQ. 6 .OR. S .EQ. 7 .OR.
     +         S .EQ. 11) THEN
         H2SPRD = 2
      ELSE
         H2SPRD = 3
      END IF
      RETURN
      END

C     The partitions of the macroblock being decoded, in decoding order,
C     measured in 4x4 blocks.  Table 7-13 gives the macroblock shapes,
C     Table 7-17 the P sub-macroblock shapes and Table 7-18 the B ones;
C     between them a macroblock is one to sixteen rectangles.
C
C     MODE is which of 8.4.1.3's special cases the partition's predictor
C     takes: the two halves of a 16x8 look up and left respectively, the
C     two halves of an 8x16 look left and up-right, and everything else
C     takes the median.  Carrying it here rather than working it out
C     again in the predictor keeps the shape of a macroblock described in
C     exactly one place.
C
C     DIR says the partition is direct-predicted and so has neither a
C     reference index nor a motion vector difference in the bitstream.
      SUBROUTINE H2PLST(NP, BX, BY, BW, BH, MODE, DIR)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER NP, BX(16), BY(16), BW(16), BH(16), MODE(16), DIR(16)
      INTEGER I, K, S, X8, Y8, NS, PW, PH, NX
      NP = 0
      IF (CPTYP .EQ. 40 .OR. CPTYP .EQ. 45) THEN
C     A wholly direct macroblock: B_Direct_16x16 or B_Skip.
         CALL H2SSHP(0, NS, PW, PH)
         DO 8 K = 0, 3
            X8 = MOD(K, 2) * 2
            Y8 = (K / 2) * 2
            NX = 2 / PW
            DO 6 I = 0, NS - 1
               NP = NP + 1
               BX(NP) = X8 + MOD(I, NX) * PW
               BY(NP) = Y8 + (I / NX) * PH
               BW(NP) = PW
               BH(NP) = PH
               MODE(NP) = 0
               DIR(NP) = 1
    6       CONTINUE
    8    CONTINUE
      ELSE IF (CPTYP .EQ. 31 .OR. CPTYP .EQ. 42) THEN
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
         DIR(1) = 0
         DIR(2) = 0
      ELSE IF (CPTYP .EQ. 32 .OR. CPTYP .EQ. 43) THEN
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
         DIR(1) = 0
         DIR(2) = 0
      ELSE IF (CPTYP .EQ. 33 .OR. CPTYP .EQ. 44) THEN
         DO 20 K = 0, 3
            X8 = MOD(K, 2) * 2
            Y8 = (K / 2) * 2
            S = CSUB(K + 1)
            CALL H2SSHP(S, NS, PW, PH)
            NX = 2 / PW
            DO 15 I = 0, NS - 1
               NP = NP + 1
               BX(NP) = X8 + MOD(I, NX) * PW
               BY(NP) = Y8 + (I / NX) * PH
               BW(NP) = PW
               BH(NP) = PH
               MODE(NP) = 0
               DIR(NP) = 0
               IF (CDIR(K + 1) .NE. 0) DIR(NP) = 1
   15       CONTINUE
   20    CONTINUE
      ELSE
         NP = 1
         BX(1) = 0
         BY(1) = 0
         BW(1) = 4
         BH(1) = 4
         MODE(1) = 0
         DIR(1) = 0
      END IF
      RETURN
      END

C     8.4.1.2.1: the motion of the block at this address in the
C     colocated picture, which is RefPicList1[0].
C
C     BI is a 4x4 block in raster order within the macroblock and Q the
C     8x8 quadrant it lies in; the reference index is per quadrant
C     because a reference index is, in every macroblock shape the
C     standard has.  A colocated block that used list 0 answers with it,
C     and one that did not answers with list 1 -- that is the whole of
C     the listCol derivation and it is not symmetric.
      SUBROUTINE H2COLB(BI, Q, RC, PIC, MVX, MVY)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER BI, Q, RC, PIC, MVX, MVY
      INTEGER K, L, MB
      RC = -1
      PIC = -1
      MVX = 0
      MVY = 0
      K = COLSL
      IF (K .LT. 1 .OR. K .GT. MXREF) RETURN
      MB = CMBA + 1
      IF (CLINT(MB, K) .NE. 0) RETURN
      L = 1
      IF (CLREF(Q, 1, MB, K) .LT. 0) L = 2
      RC = CLREF(Q, L, MB, K)
      IF (RC .LT. 0) THEN
         RC = -1
         RETURN
      END IF
      PIC = CLPIC(Q, L, MB, K)
      MVX = CLMVX(BI, L, MB, K)
      MVY = CLMVY(BI, L, MB, K)
      RETURN
      END

C     8.4.1.3.2's MinPositive: the smaller of two reference indices when
C     both are real, and the larger when one of them is the -1 that means
C     "this neighbour did not use this list".
      INTEGER FUNCTION H2MINP(A, B)
      IMPLICIT NONE
      INTEGER A, B
      IF (A .GE. 0 .AND. B .GE. 0) THEN
         H2MINP = MIN(A, B)
      ELSE
         H2MINP = MAX(A, B)
      END IF
      RETURN
      END

C     8.4.1.2, direct prediction: the motion of a macroblock that carries
C     none of its own.
C
C     Two entirely different derivations share the name.  The spatial one
C     takes the reference indices and the vector from this picture's
C     neighbours and consults the colocated picture only to ask whether
C     it was standing still; the temporal one takes the vector from the
C     colocated picture and rescales it by the ratio of two picture order
C     count differences.  A slice picks between them with one flag in its
C     header and encoders change their minds between the two, so both are
C     here and neither is the default.
C
C     Only the quadrants marked in CDIR are written.  A B_8x8 macroblock
C     may have any mixture of direct and coded 8x8s, and the coded ones
C     must keep the reference indices they are about to read.
      SUBROUTINE H2DRCT(ST)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER ST
      INTEGER Q, I, J, BI, QX, QY, NU, U
      INTEGER RC, PIC, CX, CY, K
      INTEGER R0, R1, ZP, CZ
      INTEGER M0X, M0Y, M1X, M1Y, V0X, V0Y, V1X, V1Y
      INTEGER AVA, AVB, AVC, RFA, RFB, RFC, TVX, TVY
      INTEGER TD, TB, TX, DSF, PC0, PC1
      INTEGER H2MINP
      EXTERNAL H2MINP

      ST = 0
      IF (COLSL .LT. 1 .OR. COLSL .GT. MXREF) THEN
         ST = -54
         RETURN
      END IF
      IF (RL0N .LT. 1 .OR. RL1N .LT. 1) THEN
         ST = -54
         RETURN
      END IF

      R0 = -1
      R1 = -1
      ZP = 0
      M0X = 0
      M0Y = 0
      M1X = 0
      M1Y = 0
      IF (DSMVP .NE. 0) THEN
C     8.4.1.2.2.  The three neighbours are read raw: the substitution of
C     8.4.1.3.1 that lets A stand in for a missing B and C belongs to the
C     median vector prediction and not to this minimum over indices, and
C     applying it here would pick a reference picture the encoder did
C     not pick.
         CALL H2GETN(1, -1, 0, AVA, RFA, TVX, TVY)
         CALL H2GETN(1, 0, -1, AVB, RFB, TVX, TVY)
         CALL H2GETN(1, 4, -1, AVC, RFC, TVX, TVY)
         IF (AVC .EQ. 0) CALL H2GETN(1, -1, -1, AVC, RFC, TVX, TVY)
         R0 = H2MINP(RFA, H2MINP(RFB, RFC))
         CALL H2GETN(2, -1, 0, AVA, RFA, TVX, TVY)
         CALL H2GETN(2, 0, -1, AVB, RFB, TVX, TVY)
         CALL H2GETN(2, 4, -1, AVC, RFC, TVX, TVY)
         IF (AVC .EQ. 0) CALL H2GETN(2, -1, -1, AVC, RFC, TVX, TVY)
         R1 = H2MINP(RFA, H2MINP(RFB, RFC))
         IF (R0 .LT. 0 .AND. R1 .LT. 0) THEN
C     directZeroPredictionFlag: no neighbour predicted from anything, so
C     the macroblock predicts from the nearest picture in each direction
C     without moving.
            R0 = 0
            R1 = 0
            ZP = 1
         ELSE
            IF (R0 .GE. 0) CALL H2MVPR(1, 0, 0, 4, R0, 0, M0X, M0Y)
            IF (R1 .GE. 0) CALL H2MVPR(2, 0, 0, 4, R1, 0, M1X, M1Y)
         END IF
         IF (R0 .GE. RL0N .OR. R1 .GE. RL1N) THEN
            ST = -54
            RETURN
         END IF
      END IF

C     The unit of derivation: an 8x8 when direct_8x8_inference_flag says
C     the corner 4x4 speaks for its quadrant, a 4x4 otherwise.
      NU = 16
      IF (D8INF .NE. 0) NU = 4
      DO 40 U = 1, NU
         IF (D8INF .NE. 0) THEN
            Q = U
            QX = MOD(U - 1, 2) * 2
            QY = ((U - 1) / 2) * 2
C     8.4.1.2.1's corner: luma4x4BlkIdx 5 * mbPartIdx in z-scan, which
C     lands on (0,0), (3,0), (0,3) and (3,3) in raster order.
            BI = 1 + (QX / 2) * 3 + 4 * ((QY / 2) * 3)
         ELSE
            Q = 1 + MOD(U - 1, 4) / 2 + 2 * (((U - 1) / 4) / 2)
            QX = MOD(U - 1, 4)
            QY = (U - 1) / 4
            BI = U
         END IF
         IF (CDIR(Q) .EQ. 0) GOTO 40
         CALL H2COLB(BI, Q, RC, PIC, CX, CY)

         IF (DSMVP .NE. 0) THEN
C     colZeroFlag: the colocated block sat on the nearest picture and
C     barely moved, so a block predicting from the nearest picture on
C     this side should not move either.
            CZ = 0
            IF (RC .EQ. 0 .AND. ABS(CX) .LE. 1 .AND. ABS(CY) .LE. 1)
     +         CZ = 1
            V0X = M0X
            V0Y = M0Y
            V1X = M1X
            V1Y = M1Y
            IF (ZP .NE. 0 .OR. R0 .LT. 0 .OR.
     +          (R0 .EQ. 0 .AND. CZ .NE. 0)) THEN
               V0X = 0
               V0Y = 0
            END IF
            IF (ZP .NE. 0 .OR. R1 .LT. 0 .OR.
     +          (R1 .EQ. 0 .AND. CZ .NE. 0)) THEN
               V1X = 0
               V1Y = 0
            END IF
            CREF(Q, 1) = R0
            CREF(Q, 2) = R1
         ELSE
C     8.4.1.2.3.  Both indices are forced: list 1 points at the
C     colocated picture itself and list 0 at whatever the colocated
C     block was pointing at, found again in this slice's list 0.
            R1 = 0
            IF (RC .LT. 0) THEN
               R0 = 0
               CX = 0
               CY = 0
            ELSE
               R0 = 0
               DO 10 K = 0, RL0N - 1
                  IF (DPID(RL0(K)) .EQ. PIC) THEN
                     R0 = K
                     GOTO 15
                  END IF
   10          CONTINUE
   15          CONTINUE
            END IF
            PC0 = DPPOC(RL0(R0))
            PC1 = DPPOC(RL1(0))
            TD = PC1 - PC0
            IF (TD .LT. -128) TD = -128
            IF (TD .GT. 127) TD = 127
            IF (TD .EQ. 0) THEN
               V0X = CX
               V0Y = CY
               V1X = 0
               V1Y = 0
            ELSE
               TB = CURPOC - PC0
               IF (TB .LT. -128) TB = -128
               IF (TB .GT. 127) TB = 127
               TX = (16384 + ABS(TD / 2)) / TD
               DSF = SHIFTA(TB * TX + 32, 6)
               IF (DSF .LT. -1024) DSF = -1024
               IF (DSF .GT. 1023) DSF = 1023
               V0X = SHIFTA(DSF * CX + 128, 8)
               V0Y = SHIFTA(DSF * CY + 128, 8)
               V1X = V0X - CX
               V1Y = V0Y - CY
            END IF
            CREF(Q, 1) = R0
            CREF(Q, 2) = R1
         END IF

         IF (D8INF .NE. 0) THEN
            DO 34 J = QY, QY + 1
               DO 32 I = QX, QX + 1
                  BI = 1 + I + 4 * J
                  CMVX(BI, 1) = V0X
                  CMVY(BI, 1) = V0Y
                  CMVX(BI, 2) = V1X
                  CMVY(BI, 2) = V1Y
   32          CONTINUE
   34       CONTINUE
         ELSE
            BI = 1 + QX + 4 * QY
            CMVX(BI, 1) = V0X
            CMVY(BI, 1) = V0Y
            CMVX(BI, 2) = V1X
            CMVY(BI, 2) = V1Y
         END IF
   40 CONTINUE
      RETURN
      END

C     Which slot of the decoded picture buffer reference index RI of list
C     L names, refusing rather than reading a slot that holds nothing.
      SUBROUTINE H2SLOT(L, RI, K, ST)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER L, RI, K, ST
      ST = 0
      K = 0
      IF (RI .LT. 0 .OR. RI .GT. 31) GOTO 90
      IF (L .EQ. 1) THEN
         IF (RI .GE. RL0N) GOTO 90
         K = RL0(RI)
      ELSE
         IF (RI .GE. RL1N) GOTO 90
         K = RL1(RI)
      END IF
      IF (K .LT. 1 .OR. K .GT. MXREF) GOTO 90
      IF (DPUSE(K) .EQ. 0) GOTO 90
      RETURN
   90 ST = -54
      K = 0
      RETURN
      END

C     8.4.2.3.2 for a block predicted from one list.  In place, because
C     the unweighted prediction is already where it needs to be.
      SUBROUTINE H2WUNI(P, ND, XA, YA, W, H, WT, OF, LG)
      IMPLICIT NONE
      INTEGER ND, XA, YA, W, H, WT, OF, LG
      INTEGER P(0:ND-1, 0:ND-1)
      INTEGER I, J, V
      DO 20 J = 0, H - 1
         DO 10 I = 0, W - 1
            V = P(XA + I, YA + J)
            IF (LG .GE. 1) THEN
               V = SHIFTA(V * WT + ISHFT(1, LG - 1), LG) + OF
            ELSE
               V = V * WT + OF
            END IF
            P(XA + I, YA + J) = MAX(0, MIN(255, V))
   10    CONTINUE
   20 CONTINUE
      RETURN
      END

C     8.4.2.3.1, the default combination of two predictions: their
C     average, rounded away from zero.
      SUBROUTINE H2AVG(S0, S1, D, ND, XA, YA, W, H)
      IMPLICIT NONE
      INTEGER ND, XA, YA, W, H
      INTEGER S0(0:ND-1, 0:ND-1), S1(0:ND-1, 0:ND-1)
      INTEGER D(0:ND-1, 0:ND-1)
      INTEGER I, J
      DO 20 J = 0, H - 1
         DO 10 I = 0, W - 1
            D(XA + I, YA + J) = SHIFTA(S0(XA + I, YA + J)
     +                                 + S1(XA + I, YA + J) + 1, 1)
   10    CONTINUE
   20 CONTINUE
      RETURN
      END

C     8.4.2.3.2 for a block predicted from both lists.  The rounding
C     constant is 1 << logWD against a shift of logWD + 1, which is not
C     the same as averaging two separately rounded halves, and the two
C     offsets are averaged rather than added.
      SUBROUTINE H2WBI(S0, S1, D, ND, XA, YA, W, H, W0, W1, O0, O1, LG)
      IMPLICIT NONE
      INTEGER ND, XA, YA, W, H, W0, W1, O0, O1, LG
      INTEGER S0(0:ND-1, 0:ND-1), S1(0:ND-1, 0:ND-1)
      INTEGER D(0:ND-1, 0:ND-1)
      INTEGER I, J, V, OF
      OF = SHIFTA(O0 + O1 + 1, 1)
      DO 20 J = 0, H - 1
         DO 10 I = 0, W - 1
            V = SHIFTA(S0(XA + I, YA + J) * W0
     +                 + S1(XA + I, YA + J) * W1
     +                 + ISHFT(1, LG), LG + 1) + OF
            D(XA + I, YA + J) = MAX(0, MIN(255, V))
   10    CONTINUE
   20 CONTINUE
      RETURN
      END

C     8.4.2.3.1's implicit weights: the two halves of a bi-prediction
C     weighted by where this picture sits between the two it predicts
C     from, so that a picture three quarters of the way from one to the
C     other takes three quarters of the far one.
C
C     The arithmetic is 8.4.1.2.3's motion vector scaling reused whole,
C     which is why the same clipping to eight bits and the same
C     reciprocal appear here.  A pair that straddles nothing -- two
C     references with the same order count -- falls back to an even
C     split, and so does a weight that came out beyond the range the
C     standard allows, which is the case a stream can reach by putting
C     both references on the same side of the picture.
      SUBROUTINE H2IMPW(R0, R1, W0, W1)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER R0, R1, W0, W1
      INTEGER PC0, PC1, TD, TB, TX, DSF, WW
      W0 = 32
      W1 = 32
      PC0 = DPPOC(RL0(R0))
      PC1 = DPPOC(RL1(R1))
      TD = PC1 - PC0
      IF (TD .LT. -128) TD = -128
      IF (TD .GT. 127) TD = 127
      IF (TD .EQ. 0) RETURN
      TB = CURPOC - PC0
      IF (TB .LT. -128) TB = -128
      IF (TB .GT. 127) TB = 127
      TX = (16384 + ABS(TD / 2)) / TD
      DSF = SHIFTA(TB * TX + 32, 6)
      IF (DSF .LT. -1024) DSF = -1024
      IF (DSF .GT. 1023) DSF = 1023
      WW = SHIFTA(DSF, 2)
      IF (WW .LT. -64 .OR. WW .GT. 128) RETURN
      W1 = WW
      W0 = 64 - WW
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
C
C     A bi-predicted partition is interpolated twice, unweighted, and the
C     two halves combined afterwards.  Weighting each half as it comes
C     out of the filter and averaging the results would round twice where
C     8.4.2.3 rounds once, which is a difference of one in the last bit
C     on about a third of the samples -- invisible, and wrong.
      SUBROUTINE H2PMB(ST)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER ST
      INTEGER PRD(0:15,0:15), PU8(0:7,0:7), PV8(0:7,0:7)
      INTEGER Q0(0:15,0:15), Q1(0:15,0:15)
      INTEGER U0(0:7,0:7), V0(0:7,0:7), U1(0:7,0:7), V1(0:7,0:7)
      INTEGER NP, BX(16), BY(16), BW(16), BH(16), MODE(16), DIR(16)
      INTEGER I, BI, Q, R0, R1, S0, S1, EXPL, L, RI, SK
      INTEGER XL, YL, WL, HL, XC, YC, WC, HC, IW0, IW1
      ST = 0
      EXPL = 0
      IF (SLTYPE .EQ. 0 .AND. WPRED .NE. 0) EXPL = 1
      IF (SLTYPE .EQ. 1 .AND. WBIDC .EQ. 1) EXPL = 1
      CALL H2PLST(NP, BX, BY, BW, BH, MODE, DIR)
      DO 10 I = 1, NP
         Q = 1 + BX(I) / 2 + 2 * (BY(I) / 2)
         BI = 1 + BX(I) + 4 * BY(I)
         R0 = CREF(Q, 1)
         R1 = CREF(Q, 2)
         IF (SLTYPE .NE. 1) R1 = -1
         IF (R0 .LT. 0 .AND. R1 .LT. 0) THEN
            ST = -54
            RETURN
         END IF
         S0 = 0
         S1 = 0
         IF (R0 .GE. 0) THEN
            CALL H2SLOT(1, R0, S0, ST)
            IF (ST .NE. 0) RETURN
         END IF
         IF (R1 .GE. 0) THEN
            CALL H2SLOT(2, R1, S1, ST)
            IF (ST .NE. 0) RETURN
         END IF
         XL = 4 * BX(I)
         YL = 4 * BY(I)
         WL = 4 * BW(I)
         HL = 4 * BH(I)
         XC = 2 * BX(I)
         YC = 2 * BY(I)
         WC = 2 * BW(I)
         HC = 2 * BH(I)
         IF (R0 .GE. 0 .AND. R1 .GE. 0) THEN
            CALL H2MCY(S0, XL, YL, WL, HL, CMVX(BI,1), CMVY(BI,1), Q0)
            CALL H2MCC(S0, XC, YC, WC, HC, CMVX(BI,1), CMVY(BI,1),
     +                 U0, V0)
            CALL H2MCY(S1, XL, YL, WL, HL, CMVX(BI,2), CMVY(BI,2), Q1)
            CALL H2MCC(S1, XC, YC, WC, HC, CMVX(BI,2), CMVY(BI,2),
     +                 U1, V1)
            IF (EXPL .NE. 0) THEN
               CALL H2WBI(Q0, Q1, PRD, 16, XL, YL, WL, HL,
     +                    WPL(R0,1), WPL(R1,2), WOL(R0,1), WOL(R1,2),
     +                    LOGWL)
               CALL H2WBI(U0, U1, PU8, 8, XC, YC, WC, HC,
     +                    WPCB(R0,1), WPCB(R1,2), WOCB(R0,1),
     +                    WOCB(R1,2), LOGWC)
               CALL H2WBI(V0, V1, PV8, 8, XC, YC, WC, HC,
     +                    WPCR(R0,1), WPCR(R1,2), WOCR(R0,1),
     +                    WOCR(R1,2), LOGWC)
            ELSE IF (SLTYPE .EQ. 1 .AND. WBIDC .EQ. 2) THEN
               CALL H2IMPW(R0, R1, IW0, IW1)
               CALL H2WBI(Q0, Q1, PRD, 16, XL, YL, WL, HL,
     +                    IW0, IW1, 0, 0, 5)
               CALL H2WBI(U0, U1, PU8, 8, XC, YC, WC, HC,
     +                    IW0, IW1, 0, 0, 5)
               CALL H2WBI(V0, V1, PV8, 8, XC, YC, WC, HC,
     +                    IW0, IW1, 0, 0, 5)
            ELSE
               CALL H2AVG(Q0, Q1, PRD, 16, XL, YL, WL, HL)
               CALL H2AVG(U0, U1, PU8, 8, XC, YC, WC, HC)
               CALL H2AVG(V0, V1, PV8, 8, XC, YC, WC, HC)
            END IF
         ELSE
            IF (R0 .GE. 0) THEN
               L = 1
               RI = R0
               SK = S0
            ELSE
               L = 2
               RI = R1
               SK = S1
            END IF
            CALL H2MCY(SK, XL, YL, WL, HL, CMVX(BI,L), CMVY(BI,L), PRD)
            CALL H2MCC(SK, XC, YC, WC, HC, CMVX(BI,L), CMVY(BI,L),
     +                 PU8, PV8)
C     8.4.2.3: a partition that used one list in a slice coded with the
C     implicit weights is predicted with no weight at all.  The implicit
C     derivation has nothing to say about it -- it is a statement about
C     where this picture sits between two others, and there is only one.
            IF (EXPL .NE. 0) THEN
               CALL H2WUNI(PRD, 16, XL, YL, WL, HL,
     +                     WPL(RI,L), WOL(RI,L), LOGWL)
               CALL H2WUNI(PU8, 8, XC, YC, WC, HC,
     +                     WPCB(RI,L), WOCB(RI,L), LOGWC)
               CALL H2WUNI(PV8, 8, XC, YC, WC, HC,
     +                     WPCR(RI,L), WOCR(RI,L), LOGWC)
            END IF
         END IF
   10 CONTINUE

      CALL H2ADDY(PRD)
      CALL H2ADDC(PU, 1, PU8)
      CALL H2ADDC(PV, 2, PV8)
      RETURN
      END
