C     Slice data and the macroblock layer, CABAC side.
C
C     Everything here is clause 7.3.4 and 7.3.5 read through 9.3.3:
C     which syntax element comes next, and which of the 1024 context
C     variables decodes it.  The context derivations are the fiddly half
C     -- almost every one of them asks a question about the macroblock
C     to the left and the macroblock above, and gets a different answer
C     when that macroblock is off the edge of the picture, in another
C     slice, or I_PCM.
C
C     Two conventions make the neighbour questions answerable in one
C     line each.  The 4x4 luma blocks are numbered in the z-order of
C     6.4.3, and ZORD/BLKX/BLKY convert between that number and a
C     position, so "the block to the left" is either ZORD(bx-1, by) in
C     this macroblock or ZORD(3, by) in the one to the left.  And an
C     I_PCM macroblock is recorded as having every coefficient present
C     (MNZ = 16, MCBP = 15 and chroma 2), which is exactly what every
C     context derivation wants of it, so I_PCM needs no special case
C     anywhere below.

C     Neighbour availability for the macroblock at CMBA.  A neighbour is
C     available when it carries this slice's number: that one test
C     covers the picture edges (nothing was ever written there), the
C     slice boundaries, and macroblocks later in decoding order at once.
      SUBROUTINE H2NBR
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      CMBX = MOD(CMBA, MBW)
      CMBY = CMBA / MBW
      ADRA = CMBA - 1
      ADRB = CMBA - MBW
      ADRC = CMBA - MBW + 1
      ADRD = CMBA - MBW - 1
      AVLA = 0
      AVLB = 0
      AVLC = 0
      AVLD = 0
      IF (CMBX .GT. 0) THEN
         IF (MSLC(ADRA + 1) .EQ. SLID) AVLA = 1
      END IF
      IF (CMBY .GT. 0) THEN
         IF (MSLC(ADRB + 1) .EQ. SLID) AVLB = 1
         IF (CMBX .LT. MBW - 1) THEN
            IF (MSLC(ADRC + 1) .EQ. SLID) AVLC = 1
         END IF
         IF (CMBX .GT. 0) THEN
            IF (MSLC(ADRD + 1) .EQ. SLID) AVLD = 1
         END IF
      END IF
      RETURN
      END

C     7.3.4, slice_data(), for a CABAC I slice.  No mb_skip_run and no
C     mb_skip_flag, because an I slice has neither; the loop is
C     macroblock, end_of_slice_flag, macroblock.
      SUBROUTINE H2SLIC(ST)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER ST, N, H2TRM
      EXTERNAL H2TRM
      ST = 0
      CMBA = SLFMB
      QPY = SLQPY
      DQLAST = 0
      N = 0
   10 CONTINUE
         CALL H2NBR
         CALL H2MBLY(ST)
         IF (ST .NE. 0) RETURN
         CALL H2RECM
         CMBA = CMBA + 1
         N = N + 1
C     The count is the only thing standing between a corrupt slice and a
C     loop that never ends: CABAC past the end of the buffer reads
C     zeroes for ever, and zeroes are a perfectly decodable macroblock.
         IF (N .GT. MBN) THEN
            ST = -20
            RETURN
         END IF
         IF (H2TRM() .NE. 0) RETURN
         IF (CMBA .GE. MBN) THEN
            ST = -21
            RETURN
         END IF
         GOTO 10
      END

C     7.3.5, macroblock_layer().
      SUBROUTINE H2MBLY(ST)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER ST
      INTEGER I, K, M, PRED, MODE, NTS
      INTEGER H2MBTY, H2I4PM, H2PRDM, H2CHPM, H2CBPL, H2CBPC, H2DEC
      EXTERNAL H2MBTY, H2I4PM, H2PRDM, H2CHPM, H2CBPL, H2CBPC, H2DEC

      ST = 0
      CI16 = 0
      CPCM = 0
      CPRED = 0
      CBPL = 0
      CBPC = 0
      T8FLG = 0
      CCPM = 0
      DO 10 I = 1, 24
         CNZ(I) = 0
   10 CONTINUE
      CALL H2ZCOF
      CDCF(1) = 0
      CDCF(2) = 0
      CDCF(3) = 0

      M = H2MBTY()
      MTYP(CMBA + 1) = M
      IF (M .EQ. 25) THEN
         CALL H2PCM(ST)
         RETURN
      END IF
      IF (M .GT. 0) THEN
C     Table 7-11: the twenty-four I_16x16 types spell out the prediction
C     mode and both halves of the coded block pattern, so there is
C     nothing left to read for them.
         CI16 = 1
         CPRED = MOD(M - 1, 4)
         CBPC = MOD((M - 1) / 4, 3)
         CBPL = ((M - 1) / 12) * 15
      END IF

C     Neighbour transform sizes, needed before either place
C     transform_size_8x8_flag can appear.
      NTS = 0
      IF (AVLA .NE. 0) NTS = NTS + MT8(ADRA + 1)
      IF (AVLB .NE. 0) NTS = NTS + MT8(ADRB + 1)

      IF (M .EQ. 0) THEN
         IF (TR8x8 .NE. 0) T8FLG = H2DEC(399 + NTS)
         IF (T8FLG .NE. 0) THEN
            DO 30 K = 0, 3
               PRED = H2PRDM(4 * K)
               MODE = H2I4PM(PRED)
               DO 20 I = 1, 4
                  CI4(4 * K + I) = MODE
   20          CONTINUE
   30       CONTINUE
         ELSE
            DO 40 I = 0, 15
               PRED = H2PRDM(I)
               CI4(I + 1) = H2I4PM(PRED)
   40       CONTINUE
         END IF
      ELSE
         DO 50 I = 1, 16
            CI4(I) = 2
   50    CONTINUE
      END IF
      CCPM = H2CHPM()

      IF (CI16 .EQ. 0) THEN
         CBPL = H2CBPL()
         CBPC = H2CBPC()
      END IF
      MCBP(CMBA + 1) = CBPL + 16 * CBPC
      MT8(CMBA + 1) = T8FLG

      IF (CBPL .NE. 0 .OR. CBPC .NE. 0 .OR. CI16 .NE. 0) THEN
         CALL H2DQPD
         CALL H2RES
      ELSE
C     No mb_qp_delta is sent for a macroblock with nothing to dequantise,
C     but QPY still carries forward, and the chroma pair has to be
C     recomputed anyway because 8.5 will be asked for it by the
C     deblocking filter on the next macroblock's edge.
         DQLAST = 0
         CALL H2CQP
      END IF

      IF (BITERR .NE. 0) THEN
         ST = -22
         RETURN
      END IF
      CALL H2SAVE
      RETURN
      END

C     Write the macroblock's record back, so the next macroblock and the
C     deblocking filter can ask about it.
      SUBROUTINE H2SAVE
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER I
      DO 10 I = 1, 24
         MNZ(I, CMBA + 1) = CNZ(I)
   10 CONTINUE
      DO 20 I = 1, 16
         MI4(I, CMBA + 1) = CI4(I)
   20 CONTINUE
      MDCF(1, CMBA + 1) = CDCF(1)
      MDCF(2, CMBA + 1) = CDCF(2)
      MDCF(3, CMBA + 1) = CDCF(3)
      MQPY(CMBA + 1) = QPY
      MI16(CMBA + 1) = CI16
      MPCM(CMBA + 1) = CPCM
      MCPM(CMBA + 1) = CCPM
      MDBI(CMBA + 1) = DBIDC
      MALP(CMBA + 1) = ALPHOF
      MBET(CMBA + 1) = BETAOF
      MSLC(CMBA + 1) = SLID
      RETURN
      END

C     9.3.2.5 and Table 9-36: mb_type for an I slice.  The first bin
C     says I_NxN or not, the second is a terminate bin that says I_PCM,
C     and the remaining five spell out an I_16x16 type in the order pred
C     mode, chroma pattern, luma pattern.
      INTEGER FUNCTION H2MBTY()
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER C, M, H2DEC, H2TRM
      EXTERNAL H2DEC, H2TRM
      C = 0
      IF (AVLA .NE. 0) THEN
         IF (MTYP(ADRA + 1) .NE. 0) C = C + 1
      END IF
      IF (AVLB .NE. 0) THEN
         IF (MTYP(ADRB + 1) .NE. 0) C = C + 1
      END IF
      IF (H2DEC(3 + C) .EQ. 0) THEN
         H2MBTY = 0
         RETURN
      END IF
      IF (H2TRM() .NE. 0) THEN
         H2MBTY = 25
         RETURN
      END IF
      M = 1 + 12 * H2DEC(6)
      IF (H2DEC(7) .NE. 0) M = M + 4 + 4 * H2DEC(8)
      M = M + 2 * H2DEC(9)
      M = M + H2DEC(10)
      H2MBTY = M
      RETURN
      END

C     8.3.1.1: the predicted Intra_4x4 (or Intra_8x8) prediction mode is
C     the smaller of the two neighbouring modes, and DC whenever either
C     neighbour is missing or was not coded with a 4x4 or 8x8 mode.  MI4
C     already holds 2 for those macroblocks, so only the availability
C     tests are left here.
      INTEGER FUNCTION H2PRDM(I)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER I, BX, BY, A, B
      BX = BLKX(I) / 4
      BY = BLKY(I) / 4
      IF (BX .GT. 0) THEN
         A = CI4(ZORD(BX - 1, BY) + 1)
      ELSE IF (AVLA .NE. 0) THEN
         A = MI4(ZORD(3, BY) + 1, ADRA + 1)
      ELSE
         H2PRDM = 2
         RETURN
      END IF
      IF (BY .GT. 0) THEN
         B = CI4(ZORD(BX, BY - 1) + 1)
      ELSE IF (AVLB .NE. 0) THEN
         B = MI4(ZORD(BX, 3) + 1, ADRB + 1)
      ELSE
         H2PRDM = 2
         RETURN
      END IF
      H2PRDM = MIN(A, B)
      RETURN
      END

C     prev_intra4x4_pred_mode_flag and rem_intra4x4_pred_mode: one
C     context for the flag, one shared by all three bits of the
C     remainder.
      INTEGER FUNCTION H2I4PM(PRED)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER PRED, MODE, H2DEC
      EXTERNAL H2DEC
      IF (H2DEC(68) .NE. 0) THEN
         H2I4PM = PRED
         RETURN
      END IF
      MODE = H2DEC(69)
      MODE = MODE + 2 * H2DEC(69)
      MODE = MODE + 4 * H2DEC(69)
      IF (MODE .GE. PRED) MODE = MODE + 1
      H2I4PM = MODE
      RETURN
      END

C     intra_chroma_pred_mode: unary, three bins at most, and its first
C     bin's context counts the neighbours that chose something other
C     than DC.
      INTEGER FUNCTION H2CHPM()
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER C, H2DEC
      EXTERNAL H2DEC
      C = 0
      IF (AVLA .NE. 0) THEN
         IF (MCPM(ADRA + 1) .NE. 0) C = C + 1
      END IF
      IF (AVLB .NE. 0) THEN
         IF (MCPM(ADRB + 1) .NE. 0) C = C + 1
      END IF
      IF (H2DEC(64 + C) .EQ. 0) THEN
         H2CHPM = 0
      ELSE IF (H2DEC(67) .EQ. 0) THEN
         H2CHPM = 1
      ELSE IF (H2DEC(67) .EQ. 0) THEN
         H2CHPM = 2
      ELSE
         H2CHPM = 3
      END IF
      RETURN
      END

C     Is the 8x8 luma block B of macroblock ADDR coded?  Used only by
C     the coded_block_pattern contexts, where "not coded" is the
C     condition that raises ctxIdxInc.
C
C     A missing neighbour answers "coded", not "not coded".  9.3.3.1.1.4
C     lists "mbAddrN is not available" alongside "the block is coded" as
C     the conditions that give condTermFlagN = 0, so the edge of the
C     picture has to behave like a fully coded macroblock rather than
C     like an empty one.  I_PCM lands on the same answer without a test,
C     because its stored pattern is 15.
      INTEGER FUNCTION H2CBPB(ADDR, AVAIL, B)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER ADDR, AVAIL, B
      IF (AVAIL .EQ. 0) THEN
         H2CBPB = 1
      ELSE
         H2CBPB = IAND(ISHFT(IAND(MCBP(ADDR + 1), 15), -B), 1)
      END IF
      RETURN
      END

C     9.3.3.1.1.4 for the four luma bins of coded_block_pattern.  Bins
C     1, 2 and 3 look at bits of the pattern this very call is
C     assembling.
      INTEGER FUNCTION H2CBPL()
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER CBP, C, H2DEC, H2CBPB
      EXTERNAL H2DEC, H2CBPB
      CBP = 0
      C = (1 - H2CBPB(ADRA, AVLA, 1))
     +    + 2 * (1 - H2CBPB(ADRB, AVLB, 2))
      CBP = H2DEC(73 + C)
      C = (1 - IAND(CBP, 1)) + 2 * (1 - H2CBPB(ADRB, AVLB, 3))
      CBP = CBP + 2 * H2DEC(73 + C)
      C = (1 - H2CBPB(ADRA, AVLA, 3)) + 2 * (1 - IAND(CBP, 1))
      CBP = CBP + 4 * H2DEC(73 + C)
      C = (1 - IAND(ISHFT(CBP, -2), 1))
     +    + 2 * (1 - IAND(ISHFT(CBP, -1), 1))
      CBP = CBP + 8 * H2DEC(73 + C)
      H2CBPL = CBP
      RETURN
      END

C     The two chroma bins: the first says whether there is any chroma
C     residual, the second whether there is more than DC.
      INTEGER FUNCTION H2CBPC()
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER C, CA, CB, H2DEC
      EXTERNAL H2DEC
      CA = 0
      CB = 0
      IF (AVLA .NE. 0) CA = ISHFT(MCBP(ADRA + 1), -4)
      IF (AVLB .NE. 0) CB = ISHFT(MCBP(ADRB + 1), -4)
      C = 0
      IF (CA .GT. 0) C = C + 1
      IF (CB .GT. 0) C = C + 2
      IF (H2DEC(77 + C) .EQ. 0) THEN
         H2CBPC = 0
         RETURN
      END IF
      C = 4
      IF (CA .EQ. 2) C = C + 1
      IF (CB .EQ. 2) C = C + 2
      H2CBPC = 1 + H2DEC(77 + C)
      RETURN
      END

C     mb_qp_delta: unary, and the value alternates in sign as it grows,
C     so bin count k means +k/2 rounded up for odd k and the negative of
C     it for even k.  The first bin's context asks only whether the
C     previous macroblock changed the QP at all.
      SUBROUTINE H2DQPD
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER V, C, N, H2DEC
      EXTERNAL H2DEC
      C = 0
      IF (DQLAST .NE. 0) C = 1
      IF (H2DEC(60 + C) .EQ. 0) THEN
         DQLAST = 0
      ELSE
         V = 1
         C = 2
         N = 0
   10    IF (H2DEC(60 + C) .EQ. 0) GOTO 20
            C = 3
            V = V + 1
            N = N + 1
            IF (N .GT. 104 .OR. BITERR .NE. 0) THEN
               BITERR = 1
               GOTO 20
            END IF
            GOTO 10
   20    CONTINUE
         IF (IAND(V, 1) .EQ. 1) THEN
            DQLAST = (V + 1) / 2
         ELSE
            DQLAST = -((V + 1) / 2)
         END IF
         QPY = QPY + DQLAST
         IF (QPY .LT. 0) THEN
            QPY = QPY + 52
         ELSE IF (QPY .GT. 51) THEN
            QPY = QPY - 52
         END IF
      END IF
      CALL H2CQP
      RETURN
      END

C     Table 8-15 by way of 8-313: the chroma QPs this macroblock's
C     residual and the deblocking filter both use.
      SUBROUTINE H2CQP
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER Q
      Q = QPY + CQPO
      IF (Q .LT. 0) Q = 0
      IF (Q .GT. 51) Q = 51
      QPCB = CHQP(Q)
      Q = QPY + CQPO2
      IF (Q .LT. 0) Q = 0
      IF (Q .GT. 51) Q = 51
      QPCR = CHQP(Q)
      RETURN
      END

C     Empty every coefficient store this macroblock might read from.
C
C     This has to happen for every macroblock, not just the ones that
C     call H2RES, and that is not obvious.  A macroblock with an empty
C     coded_block_pattern never enters residual(), so if the arrays were
C     cleared there it would inherit whatever the last macroblock left
C     behind -- and the chroma reconstruction asks "is the DC non-zero?"
C     rather than "did this macroblock code anything?", because a chroma
C     DC arrives through a separate path from the AC coefficient count.
C     That question gets the wrong answer from a stale array, and the
C     macroblock is reconstructed with someone else's residual in it.
      SUBROUTINE H2ZCOF
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER I, K
      DO 20 I = 1, 24
         DO 10 K = 1, 16
            COEF(K, I) = 0
   10    CONTINUE
   20 CONTINUE
      DO 40 I = 1, 4
         DO 30 K = 1, 64
            CO8(K, I) = 0
   30    CONTINUE
   40 CONTINUE
      DO 50 I = 1, 16
         DCY(I) = 0
   50 CONTINUE
      DO 60 I = 1, 4
         DCC(I, 1) = 0
         DCC(I, 2) = 0
   60 CONTINUE
      RETURN
      END

C     7.3.5.3 residual() for an intra macroblock in 4:2:0.  The order is
C     fixed and total: luma DC if this is Intra_16x16, then luma AC or
C     the 4x4/8x8 blocks, then both chroma DCs, then both chroma AC
C     sets.
      SUBROUTINE H2RES
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER I, K, C, B, NC, LEV(0:63), Q

      IF (CI16 .NE. 0) THEN
         CALL H2RBLK(0, 0, 0, 16, LEV, NC)
         IF (NC .GT. 0) CDCF(1) = 1
         CALL H2DCY(LEV)
         IF (CBPL .NE. 0) THEN
            DO 70 I = 0, 15
               CALL H2RBLK(1, I, 0, 15, LEV, NC)
               CNZ(I + 1) = NC
               CALL H2DQ4(LEV, 1, QPY, I + 1)
   70       CONTINUE
         END IF
      ELSE
         DO 100 K = 0, 3
            IF (IAND(ISHFT(CBPL, -K), 1) .EQ. 0) GOTO 100
            IF (T8FLG .NE. 0) THEN
               CALL H2RBLK(5, 4 * K, 0, 64, LEV, NC)
               DO 80 I = 1, 4
                  CNZ(4 * K + I) = NC
   80          CONTINUE
               CALL H2DQ8(LEV, QPY, K + 1)
            ELSE
               DO 90 I = 0, 3
                  B = 4 * K + I
                  CALL H2RBLK(2, B, 0, 16, LEV, NC)
                  CNZ(B + 1) = NC
                  CALL H2DQ4(LEV, 1, QPY, B + 1)
   90          CONTINUE
            END IF
  100    CONTINUE
      END IF

      IF (CBPC .GT. 0) THEN
         DO 110 C = 1, 2
            CALL H2RBLK(3, 0, C, 4, LEV, NC)
            IF (NC .GT. 0) CDCF(1 + C) = 1
            CALL H2DCC(LEV, C)
  110    CONTINUE
      END IF
      IF (CBPC .EQ. 2) THEN
         DO 130 C = 1, 2
            Q = QPCB
            IF (C .EQ. 2) Q = QPCR
            DO 120 I = 0, 3
               B = 16 + 4 * (C - 1) + I
               CALL H2RBLK(4, I, C, 15, LEV, NC)
               CNZ(B + 1) = NC
               CALL H2DQ4(LEV, C + 1, Q, B + 1)
  120       CONTINUE
  130    CONTINUE
      END IF
      RETURN
      END

C     9.3.3.1.1.9: the coded_block_flag context.  A neighbouring block
C     that is missing counts as coded, because this macroblock is intra
C     and 9.3 says so; a neighbouring block that exists but carried no
C     coefficients counts as not coded.  Both come out of the stored
C     counts directly.
      INTEGER FUNCTION H2CBFC(CAT, N, COMP)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER CAT, N, COMP, BX, BY, A, B, CX, CY, K
      A = 0
      B = 0
      IF (CAT .EQ. 0) THEN
         IF (AVLA .EQ. 0) THEN
            A = 1
         ELSE
            A = MDCF(1, ADRA + 1)
         END IF
         IF (AVLB .EQ. 0) THEN
            B = 1
         ELSE
            B = MDCF(1, ADRB + 1)
         END IF
      ELSE IF (CAT .EQ. 3) THEN
         IF (AVLA .EQ. 0) THEN
            A = 1
         ELSE
            A = MDCF(1 + COMP, ADRA + 1)
         END IF
         IF (AVLB .EQ. 0) THEN
            B = 1
         ELSE
            B = MDCF(1 + COMP, ADRB + 1)
         END IF
      ELSE IF (CAT .EQ. 4) THEN
C     The four chroma 4x4 blocks are a 2x2 grid numbered in raster
C     order, so the block to the left of N is N-1 within this macroblock
C     and N+1 in the one to the left; above is N-2 here and N+2 there.
C     K is the index just before this component's first block in CNZ and
C     MNZ.
         K = 16 + 4 * (COMP - 1)
         CX = IAND(N, 1)
         CY = ISHFT(N, -1)
         IF (CX .GT. 0) THEN
            IF (CNZ(K + N) .GT. 0) A = 1
         ELSE IF (AVLA .EQ. 0) THEN
            A = 1
         ELSE IF (MNZ(K + N + 2, ADRA + 1) .GT. 0) THEN
            A = 1
         END IF
         IF (CY .GT. 0) THEN
            IF (CNZ(K + N - 1) .GT. 0) B = 1
         ELSE IF (AVLB .EQ. 0) THEN
            B = 1
         ELSE IF (MNZ(K + N + 3, ADRB + 1) .GT. 0) THEN
            B = 1
         END IF
      ELSE
         BX = BLKX(N) / 4
         BY = BLKY(N) / 4
         IF (BX .GT. 0) THEN
            IF (CNZ(ZORD(BX - 1, BY) + 1) .GT. 0) A = 1
         ELSE IF (AVLA .EQ. 0) THEN
            A = 1
         ELSE IF (MNZ(ZORD(3, BY) + 1, ADRA + 1) .GT. 0) THEN
            A = 1
         END IF
         IF (BY .GT. 0) THEN
            IF (CNZ(ZORD(BX, BY - 1) + 1) .GT. 0) B = 1
         ELSE IF (AVLB .EQ. 0) THEN
            B = 1
         ELSE IF (MNZ(ZORD(BX, 3) + 1, ADRB + 1) .GT. 0) THEN
            B = 1
         END IF
      END IF
      H2CBFC = A + 2 * B
      RETURN
      END

C     9.3.2.3 and 9.3.3.1.3: one residual block.  A significance map
C     first, read forwards, then the levels read backwards from the last
C     non-zero coefficient -- which is why the map has to be kept rather
C     than acted on as it arrives.
      SUBROUTINE H2RBLK(CAT, N, COMP, NMAX, LEV, NC)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER CAT, N, COMP, NMAX, LEV(0:63), NC
      INTEGER IDX(0:63), CNT, I, LAST, NODE, CTX, V, M, J, K
      INTEGER A1(0:7), G1(0:7), T0(0:7), T1(0:7)
      INTEGER SIGO(0:5), LSTO(0:5), ABSO(0:5), BASC(0:5)
      INTEGER H2DEC, H2BYP, H2CBFC, H2SPOS
      EXTERNAL H2DEC, H2BYP, H2CBFC, H2SPOS
C     Table 9-40's node contexts, as the four small maps that 9.3.3.1.3
C     describes in prose: which context decodes "is this level 1", which
C     decodes "is it bigger still", and where the node moves next.
      DATA A1 / 1, 2, 3, 4, 0, 0, 0, 0/
      DATA G1 / 5, 5, 5, 5, 6, 7, 8, 9/
      DATA T0 / 1, 2, 3, 3, 4, 5, 6, 7/
      DATA T1 / 4, 4, 4, 4, 5, 6, 7, 7/
C     ctxIdxOffset per ctxBlockCat, Tables 9-34 and 9-11.
      DATA SIGO /105, 120, 134, 149, 152, 402/
      DATA LSTO /166, 181, 195, 210, 213, 417/
      DATA ABSO /227, 237, 247, 257, 266, 426/
      DATA BASC / 85,  89,  93,  97, 101,1012/

C     All sixty-four, not NMAX of them.  The AC categories code fifteen
C     coefficients but scatter them over scan positions one to fifteen,
C     so the highest position a block writes is one past the number of
C     coefficients it has; clearing only NMAX entries leaves position
C     fifteen holding whatever the previous block put there, and the
C     dequantiser reads it.  That shows up as a single wrong
C     high-frequency coefficient in chroma and nowhere else, which is a
C     long way to walk back from the picture.
      DO 10 I = 0, 63
         LEV(I) = 0
   10 CONTINUE
      NC = 0
C     An 8x8 luma block in 4:2:0 has no coded_block_flag of its own: the
C     coded block pattern already said whether it is there.
      IF (CAT .NE. 5) THEN
         IF (H2DEC(BASC(CAT) + H2CBFC(CAT, N, COMP)) .EQ. 0) RETURN
      END IF

      CNT = 0
      LAST = 0
   20 IF (LAST .GT. NMAX - 2) GOTO 40
         IF (CAT .EQ. 5) THEN
            I = H2DEC(SIGO(5) + SIG8(LAST))
         ELSE
            I = H2DEC(SIGO(CAT) + LAST)
         END IF
         IF (I .NE. 0) THEN
            IDX(CNT) = LAST
            CNT = CNT + 1
            IF (CAT .EQ. 5) THEN
               I = H2DEC(LSTO(5) + LST8(LAST))
            ELSE
               I = H2DEC(LSTO(CAT) + LAST)
            END IF
            IF (I .NE. 0) GOTO 50
         END IF
         LAST = LAST + 1
         GOTO 20
   40 CONTINUE
C     Falling out of the loop means the map never said "last", so the
C     final position is significant by elimination and is never coded.
      IDX(CNT) = NMAX - 1
      CNT = CNT + 1
   50 CONTINUE

      NODE = 0
      DO 90 K = CNT - 1, 0, -1
         J = H2SPOS(CAT, IDX(K))
         CTX = ABSO(CAT) + A1(NODE)
         IF (H2DEC(CTX) .EQ. 0) THEN
            NODE = T0(NODE)
            V = 1
         ELSE
            V = 2
            CTX = ABSO(CAT) + G1(NODE)
            NODE = T1(NODE)
   60       IF (V .GE. 15) GOTO 70
            IF (H2DEC(CTX) .EQ. 0) GOTO 70
               V = V + 1
               GOTO 60
   70       CONTINUE
            IF (V .GE. 15) THEN
C     9.3.2.3, the UEGk suffix with k = 0: a unary prefix of bypass bins
C     giving the exponent, then that many more bypass bins of mantissa.
C     The cap of 23 is the largest exponent a 16-bit coefficient can
C     need.
               M = 0
   75          IF (M .GE. 23) GOTO 78
               IF (H2BYP() .EQ. 0) GOTO 78
                  M = M + 1
                  GOTO 75
   78          CONTINUE
               V = 1
   80          IF (M .LE. 0) GOTO 85
                  V = 2 * V + H2BYP()
                  M = M - 1
                  GOTO 80
   85          CONTINUE
               V = V + 14
            END IF
         END IF
         IF (H2BYP() .NE. 0) V = -V
         LEV(J) = V
   90 CONTINUE
      NC = CNT
      RETURN
      END

C     Where scan position P of a block of category CAT lands in the
C     block, counting across rows.  The AC categories start at zig-zag
C     position 1 because their DC was carried by a block of its own.
      INTEGER FUNCTION H2SPOS(CAT, P)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER CAT, P
      IF (CAT .EQ. 5) THEN
         H2SPOS = ZZ8(P)
      ELSE IF (CAT .EQ. 3) THEN
         H2SPOS = P
      ELSE IF (CAT .EQ. 1 .OR. CAT .EQ. 4) THEN
         H2SPOS = ZZ4(P + 1)
      ELSE
         H2SPOS = ZZ4(P)
      END IF
      RETURN
      END

C     7.3.5, the I_PCM branch.  The samples are copied straight out of
C     the bitstream and the arithmetic decoder is restarted after them,
C     which is the only place in a slice where CABAC stops and starts
C     again.
      SUBROUTINE H2PCM(ST)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER ST, I, X, Y, BX, BY, H2UN
      EXTERNAL H2UN
      ST = 0
      CPCM = 1
      CI16 = 0
      CBPL = 15
      CBPC = 2
      T8FLG = 0
      CCPM = 0
C     9.3.1.2 leaves the bit cursor nine bits ahead of the last bin, so
C     the PCM samples begin at the next byte boundary from there minus
C     that lookahead.  Backing up by the two bytes of lookahead the
C     engine holds and then aligning forwards lands on the right byte.
      BITP = BITP - 16
      IF (BITP .LT. 0) BITP = 0
      CALL H2ALGN
      IF (BITP + 3072 .GT. BITN) THEN
         ST = -23
         RETURN
      END IF
      BX = CMBX * 16
      BY = CMBY * 16
      DO 20 Y = 0, 15
         DO 10 X = 0, 15
            PY((BY + Y) * MXW + BX + X + 1) = H2UN(8)
   10    CONTINUE
   20 CONTINUE
      DO 40 Y = 0, 7
         DO 30 X = 0, 7
            PU((CMBY * 8 + Y) * (MXW / 2) + CMBX * 8 + X + 1) = H2UN(8)
   30    CONTINUE
   40 CONTINUE
      DO 60 Y = 0, 7
         DO 50 X = 0, 7
            PV((CMBY * 8 + Y) * (MXW / 2) + CMBX * 8 + X + 1) = H2UN(8)
   50    CONTINUE
   60 CONTINUE
      DO 70 I = 1, 24
         CNZ(I) = 16
   70 CONTINUE
      DO 80 I = 1, 16
         CI4(I) = 2
   80 CONTINUE
      CDCF(1) = 1
      CDCF(2) = 1
      CDCF(3) = 1
      MCBP(CMBA + 1) = 15 + 16 * 2
      MT8(CMBA + 1) = 0
      QPY = 0
      DQLAST = 0
      CALL H2CQP
      CALL H2SAVE
      CALL H2CINI(SLQPY)
      RETURN
      END
