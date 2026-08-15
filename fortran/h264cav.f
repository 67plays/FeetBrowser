C     CAVLC: clause 9.2, and the slice and macroblock syntax that differs
C     when entropy_coding_mode_flag is zero.
C
C     This file is the other half of h264mb.f.  Everything here is
C     reached only when ECMODE is 0, and everything in h264mb.f that
C     reads a bin through the arithmetic decoder is reached only when it
C     is 1.  The two halves share the prediction, the transforms, the
C     reconstruction and the neighbour bookkeeping, and they share the
C     per-macroblock records -- which is the whole point of MNZ holding a
C     count rather than a flag, because that same count is the CABAC
C     coded_block_flag context on one side and nC on the other.
C
C     The tables of 9.2 are variable-length codes and are written below
C     as the bit strings they are, one string per codeword, in the order
C     the standard prints them.  A length and a right-justified integer
C     would be smaller and would also be unreadable: '0000000001111' can
C     be checked against Table 9-5 by eye and 13/15 cannot.  H2VLC turns
C     the strings into (length, code) pairs once, on its first call, and
C     sorts each table by code length so that matching a codeword reads
C     one bit at a time and never rescans.
C
C     The one thing worth knowing before reading further: a CAVLC
C     residual block is decoded backwards.  coeff_token gives how many
C     coefficients there are and how many of them are +/-1; the levels
C     arrive from the highest frequency down; and only then do
C     total_zeros and run_before say where in the block they go.  So
C     nothing can be written into the block until the last run has been
C     read, and TotalCoeff -- which the next block needs as nC -- is
C     known long before the coefficients it counts.

C     One codeword from table GRP.  VAL comes back with the value the
C     codeword stands for: 4*TotalCoeff + TrailingOnes for a coeff_token
C     table, and the syntax element itself for the others.  A codeword
C     that is in no table sets BITERR, which every caller checks.
C
C     GRP 1..3 are coeff_token by the nC band (0..1, 2..3, 4..7; nC of 8
C     or more is a fixed-length code and needs no table), 4 is the
C     chroma DC coeff_token, 5..19 are total_zeros for a 4x4 block by
C     tzVlcIndex, 20..22 the chroma DC total_zeros, and 23..29
C     run_before by zerosLeft with the last covering seven or more.
      SUBROUTINE H2VLC(GRP, VAL)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER GRP, VAL
      INTEGER NPOOL, NGRP
      PARAMETER (NPOOL = 386, NGRP = 29)
      CHARACTER*16 CVTXT(NPOOL)
      INTEGER CVGS(NGRP), CVGN(NGRP)
      INTEGER CVLEN(NPOOL), CVCOD(NPOOL), CVVAL(NPOOL)
      INTEGER READY, G, S, N, I, K, L, V, W, TC, T1, LAST
      INTEGER H2U1
      EXTERNAL H2U1
      SAVE CVLEN, CVCOD, CVVAL, READY
      DATA READY /0/
C     Table 9-5, coeff_token, 0 <= nC < 2.
C     TotalCoeff 0..16, TrailingOnes 0..min(TotalCoeff,3).
      DATA (CVTXT(I), I = 1, 62) /
     +   '1', '000101', '01', '00000111', '000100', '001', '000000111',
     +   '00000110', '0000101', '00011', '0000000111', '000000110',
     +   '00000101', '000011', '00000000111', '0000000110',
     +   '000000101', '0000100', '0000000001111', '00000000110',
     +   '0000000101', '00000100', '0000000001011', '0000000001110',
     +   '00000000101', '000000100', '0000000001000', '0000000001010',
     +   '0000000001101', '0000000100', '00000000001111',
     +   '00000000001110', '0000000001001', '00000000100',
     +   '00000000001011', '00000000001010', '00000000001101',
     +   '0000000001100', '000000000001111', '000000000001110',
     +   '00000000001001', '00000000001100', '000000000001011',
     +   '000000000001010', '000000000001101', '00000000001000',
     +   '0000000000001111', '000000000000001', '000000000001001',
     +   '000000000001100', '0000000000001011', '0000000000001110',
     +   '0000000000001101', '000000000001000', '0000000000000111',
     +   '0000000000001010', '0000000000001001', '0000000000001100',
     +   '0000000000000100', '0000000000000110', '0000000000000101',
     +   '0000000000001000' /
C     Table 9-5, coeff_token, 2 <= nC < 4.
C     TotalCoeff 0..16, TrailingOnes 0..min(TotalCoeff,3).
      DATA (CVTXT(I), I = 63, 124) /
     +   '11', '001011', '10', '000111', '00111', '011', '0000111',
     +   '001010', '001001', '0101', '00000111', '000110', '000101',
     +   '0100', '00000100', '0000110', '0000101', '00110',
     +   '000000111', '00000110', '00000101', '001000', '00000001111',
     +   '000000110', '000000101', '000100', '00000001011',
     +   '00000001110', '00000001101', '0000100', '000000001111',
     +   '00000001010', '00000001001', '000000100', '000000001011',
     +   '000000001110', '000000001101', '00000001100', '000000001000',
     +   '000000001010', '000000001001', '00000001000',
     +   '0000000001111', '0000000001110', '0000000001101',
     +   '000000001100', '0000000001011', '0000000001010',
     +   '0000000001001', '0000000001100', '0000000000111',
     +   '00000000001011', '0000000000110', '0000000001000',
     +   '00000000001001', '00000000001000', '00000000001010',
     +   '0000000000001', '00000000000111', '00000000000110',
     +   '00000000000101', '00000000000100' /
C     Table 9-5, coeff_token, 4 <= nC < 8.
C     TotalCoeff 0..16, TrailingOnes 0..min(TotalCoeff,3).
      DATA (CVTXT(I), I = 125, 186) /
     +   '1111', '001111', '1110', '001011', '01111', '1101', '001000',
     +   '01100', '01110', '1100', '0001111', '01010', '01011', '1011',
     +   '0001011', '01000', '01001', '1010', '0001001', '001110',
     +   '001101', '1001', '0001000', '001010', '001001', '1000',
     +   '00001111', '0001110', '0001101', '01101', '00001011',
     +   '00001110', '0001010', '001100', '000001111', '00001010',
     +   '00001101', '0001100', '000001011', '000001110', '00001001',
     +   '00001100', '000001000', '000001010', '000001101', '00001000',
     +   '0000001101', '000000111', '000001001', '000001100',
     +   '0000001001', '0000001100', '0000001011', '0000001010',
     +   '0000000101', '0000001000', '0000000111', '0000000110',
     +   '0000000001', '0000000100', '0000000011', '0000000010' /
C     Table 9-5, coeff_token, nC == -1 (the 2x2 chroma DC block).
C     TotalCoeff 0..4, TrailingOnes 0..min(TotalCoeff,3).
      DATA (CVTXT(I), I = 187, 200) /
     +   '01', '000111', '1', '000100', '000110', '001', '000011',
     +   '0000011', '0000010', '000101', '000010', '00000011',
     +   '00000010', '0000000' /
C     Table 9-7/9-8, total_zeros, tzVlcIndex = 1.
      DATA (CVTXT(I), I = 201, 216) /
     +   '1', '011', '010', '0011', '0010', '00011', '00010', '000011',
     +   '000010', '0000011', '0000010', '00000011', '00000010',
     +   '000000011', '000000010', '000000001' /
C     Table 9-7/9-8, total_zeros, tzVlcIndex = 2.
      DATA (CVTXT(I), I = 217, 231) /
     +   '111', '110', '101', '100', '011', '0101', '0100', '0011',
     +   '0010', '00011', '00010', '000011', '000010', '000001',
     +   '000000' /
C     Table 9-7/9-8, total_zeros, tzVlcIndex = 3.
      DATA (CVTXT(I), I = 232, 245) /
     +   '0101', '111', '110', '101', '0100', '0011', '100', '011',
     +   '0010', '00011', '00010', '000001', '00001', '000000' /
C     Table 9-7/9-8, total_zeros, tzVlcIndex = 4.
      DATA (CVTXT(I), I = 246, 258) /
     +   '00011', '111', '0101', '0100', '110', '101', '100', '0011',
     +   '011', '0010', '00010', '00001', '00000' /
C     Table 9-7/9-8, total_zeros, tzVlcIndex = 5.
      DATA (CVTXT(I), I = 259, 270) /
     +   '0101', '0100', '0011', '111', '110', '101', '100', '011',
     +   '0010', '00001', '0001', '00000' /
C     Table 9-7/9-8, total_zeros, tzVlcIndex = 6.
      DATA (CVTXT(I), I = 271, 281) /
     +   '000001', '00001', '111', '110', '101', '100', '011', '010',
     +   '0001', '001', '000000' /
C     Table 9-7/9-8, total_zeros, tzVlcIndex = 7.
      DATA (CVTXT(I), I = 282, 291) /
     +   '000001', '00001', '101', '100', '011', '11', '010', '0001',
     +   '001', '000000' /
C     Table 9-7/9-8, total_zeros, tzVlcIndex = 8.
      DATA (CVTXT(I), I = 292, 300) /
     +   '000001', '0001', '00001', '011', '11', '10', '010', '001',
     +   '000000' /
C     Table 9-7/9-8, total_zeros, tzVlcIndex = 9.
      DATA (CVTXT(I), I = 301, 308) /
     +   '000001', '000000', '0001', '11', '10', '001', '01', '00001' /
C     Table 9-7/9-8, total_zeros, tzVlcIndex = 10.
      DATA (CVTXT(I), I = 309, 315) /
     +   '00001', '00000', '001', '11', '10', '01', '0001' /
C     Table 9-7/9-8, total_zeros, tzVlcIndex = 11.
      DATA (CVTXT(I), I = 316, 321) /
     +   '0000', '0001', '001', '010', '1', '011' /
C     Table 9-7/9-8, total_zeros, tzVlcIndex = 12.
      DATA (CVTXT(I), I = 322, 326) /
     +   '0000', '0001', '01', '1', '001' /
C     Table 9-7/9-8, total_zeros, tzVlcIndex = 13.
      DATA (CVTXT(I), I = 327, 330) /
     +   '000', '001', '1', '01' /
C     Table 9-7/9-8, total_zeros, tzVlcIndex = 14.
      DATA (CVTXT(I), I = 331, 333) /
     +   '00', '01', '1' /
C     Table 9-7/9-8, total_zeros, tzVlcIndex = 15.
      DATA (CVTXT(I), I = 334, 335) /
     +   '0', '1' /
C     Table 9-9(a), chroma DC total_zeros, tzVlcIndex = 1.
      DATA (CVTXT(I), I = 336, 339) /
     +   '1', '01', '001', '000' /
C     Table 9-9(a), chroma DC total_zeros, tzVlcIndex = 2.
      DATA (CVTXT(I), I = 340, 342) /
     +   '1', '01', '00' /
C     Table 9-9(a), chroma DC total_zeros, tzVlcIndex = 3.
      DATA (CVTXT(I), I = 343, 344) /
     +   '1', '0' /
C     Table 9-10, run_before, zerosLeft = 1.
      DATA (CVTXT(I), I = 345, 346) /
     +   '1', '0' /
C     Table 9-10, run_before, zerosLeft = 2.
      DATA (CVTXT(I), I = 347, 349) /
     +   '1', '01', '00' /
C     Table 9-10, run_before, zerosLeft = 3.
      DATA (CVTXT(I), I = 350, 353) /
     +   '11', '10', '01', '00' /
C     Table 9-10, run_before, zerosLeft = 4.
      DATA (CVTXT(I), I = 354, 358) /
     +   '11', '10', '01', '001', '000' /
C     Table 9-10, run_before, zerosLeft = 5.
      DATA (CVTXT(I), I = 359, 364) /
     +   '11', '10', '011', '010', '001', '000' /
C     Table 9-10, run_before, zerosLeft = 6.
      DATA (CVTXT(I), I = 365, 371) /
     +   '11', '000', '001', '011', '010', '101', '100' /
C     Table 9-10, run_before, zerosLeft = 7 or more.
      DATA (CVTXT(I), I = 372, 386) /
     +   '111', '110', '101', '100', '011', '010', '001', '0001',
     +   '00001', '000001', '0000001', '00000001', '000000001',
     +   '0000000001', '00000000001' /
C
C     Where each group starts in the pool, and how many entries
C     it has.  Groups 1..3 are coeff_token by nC band, 4 is the
C     chroma DC coeff_token, 5..19 are total_zeros by tzVlcIndex,
C     20..22 are chroma DC total_zeros, 23..29 are run_before.
      DATA CVGS /
     +   1, 63, 125, 187, 201, 217, 232, 246, 259, 271, 282, 292, 301,
     +   309, 316, 322, 327, 331, 334, 336, 340, 343, 345, 347, 350,
     +   354, 359, 365, 372 /
      DATA CVGN /
     +   62, 62, 62, 14, 16, 15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4,
     +   3, 2, 4, 3, 2, 2, 3, 4, 5, 6, 7, 15 /

      IF (READY .EQ. 0) THEN
         DO 40 G = 1, NGRP
            S = CVGS(G)
            N = CVGN(G)
            TC = 0
            T1 = 0
            DO 20 I = S, S + N - 1
               L = INDEX(CVTXT(I), ' ') - 1
               IF (L .LT. 0) L = 16
               CVLEN(I) = L
               V = 0
               DO 10 K = 1, L
                  V = 2 * V
                  IF (CVTXT(I)(K:K) .EQ. '1') V = V + 1
   10          CONTINUE
               CVCOD(I) = V
               IF (G .LE. 4) THEN
C     The coeff_token tables run TotalCoeff 0 upwards with
C     TrailingOnes 0 to min(TotalCoeff, 3) inside it, which is the order
C     Table 9-5 prints, so the position in the table is the value.
                  CVVAL(I) = 4 * TC + T1
                  T1 = T1 + 1
                  IF (T1 .GT. MIN(TC, 3)) THEN
                     TC = TC + 1
                     T1 = 0
                  END IF
               ELSE
                  CVVAL(I) = I - S
               END IF
   20       CONTINUE
C     Insertion sort by code length.  Short tables, done once, and it
C     buys the matcher below the right to stop scanning a length as soon
C     as it passes it.
            DO 30 I = S + 1, S + N - 1
               L = CVLEN(I)
               V = CVCOD(I)
               W = CVVAL(I)
               K = I - 1
   25          IF (K .GE. S) THEN
                  IF (CVLEN(K) .GT. L) THEN
                     CVLEN(K + 1) = CVLEN(K)
                     CVCOD(K + 1) = CVCOD(K)
                     CVVAL(K + 1) = CVVAL(K)
                     K = K - 1
                     GOTO 25
                  END IF
               END IF
               CVLEN(K + 1) = L
               CVCOD(K + 1) = V
               CVVAL(K + 1) = W
   30       CONTINUE
   40    CONTINUE
         READY = 1
      END IF

      VAL = 0
      S = CVGS(GRP)
      LAST = S + CVGN(GRP) - 1
      I = S
      L = 0
      V = 0
   50 CONTINUE
      IF (I .GT. LAST) THEN
         BITERR = 1
         RETURN
      END IF
      L = L + 1
      V = IOR(ISHFT(V, 1), H2U1())
      IF (BITERR .NE. 0) RETURN
   60 IF (I .LE. LAST) THEN
         IF (CVLEN(I) .EQ. L) THEN
            IF (CVCOD(I) .EQ. V) THEN
               VAL = CVVAL(I)
               RETURN
            END IF
            I = I + 1
            GOTO 60
         END IF
      END IF
      GOTO 50
      END

C     9.2.1: nC, the number that picks which coeff_token table decodes
C     this block.  It is the rounded average of the two neighbouring
C     blocks' TotalCoeff when both exist, one of them when only one
C     does, and zero when neither does -- so a block surrounded by empty
C     blocks is decoded by the table that spends its shortest codeword
C     on "no coefficients", and a block in a busy neighbourhood by one
C     that does not.
C
C     The neighbours are the same ones 9.3.3.1.1.9 asks about for the
C     CABAC coded_block_flag, and they are read out of the same CNZ and
C     MNZ.  Two differences: a missing neighbour is missing here rather
C     than being answered for by the intra/inter default, and the value
C     wanted is the count itself and not whether it is non-zero.
C
C     9.2.1 also removes a neighbour when constrained_intra_pred_flag is
C     set, the current macroblock is intra, and slice data partitioning
C     is in use.  Slice data partitioning is NAL unit types 2, 3 and 4,
C     which H2DECD does not accept at all, so that clause can never
C     fire here and AVLA/AVLB rather than IAVA/IAVB are the right
C     availability to ask about.
      INTEGER FUNCTION H2CNC(CAT, N, COMP)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER CAT, N, COMP
      INTEGER BX, BY, NA, NB, AVA, AVB, K, NN
      IF (CAT .EQ. 3) THEN
C     The 2x2 chroma DC block has a table of its own and no neighbours.
         H2CNC = -1
         RETURN
      END IF
      NA = 0
      NB = 0
      AVA = 0
      AVB = 0
      IF (CAT .EQ. 4) THEN
C     The four chroma 4x4 blocks are a 2x2 grid in raster order: to the
C     left of N is N-1 here and N+1 in the macroblock to the left, above
C     is N-2 here and N+2 in the one above.  K is the index just before
C     this component's first block.
         K = 16 + 4 * (COMP - 1)
         IF (IAND(N, 1) .GT. 0) THEN
            AVA = 1
            NA = CNZ(K + N)
         ELSE IF (AVLA .NE. 0) THEN
            AVA = 1
            NA = MNZ(K + N + 2, ADRA + 1)
         END IF
         IF (ISHFT(N, -1) .GT. 0) THEN
            AVB = 1
            NB = CNZ(K + N - 1)
         ELSE IF (AVLB .NE. 0) THEN
            AVB = 1
            NB = MNZ(K + N + 3, ADRB + 1)
         END IF
      ELSE
C     Luma.  The Intra16x16 DC block is not a block of the 4x4 grid, and
C     9.2.1 sends it to luma4x4BlkIdx 0's neighbours.
         NN = N
         IF (CAT .EQ. 0) NN = 0
         BX = BLKX(NN) / 4
         BY = BLKY(NN) / 4
         IF (BX .GT. 0) THEN
            AVA = 1
            NA = CNZ(ZORD(BX - 1, BY) + 1)
         ELSE IF (AVLA .NE. 0) THEN
            AVA = 1
            NA = MNZ(ZORD(3, BY) + 1, ADRA + 1)
         END IF
         IF (BY .GT. 0) THEN
            AVB = 1
            NB = CNZ(ZORD(BX, BY - 1) + 1)
         ELSE IF (AVLB .NE. 0) THEN
            AVB = 1
            NB = MNZ(ZORD(BX, 3) + 1, ADRB + 1)
         END IF
      END IF
      IF (AVA .NE. 0 .AND. AVB .NE. 0) THEN
C     Both counts are non-negative, so the logical shift is the
C     arithmetic one.
         H2CNC = ISHFT(NA + NB + 1, -1)
      ELSE IF (AVA .NE. 0) THEN
         H2CNC = NA
      ELSE IF (AVB .NE. 0) THEN
         H2CNC = NB
      ELSE
         H2CNC = 0
      END IF
      RETURN
      END

C     9.2, one residual block.  LEV comes back with the coefficients and
C     NC with TotalCoeff, which the caller stores because the next
C     block's nC is made of it.
C
C     RAW says where the coefficients go: 0 puts scan position p at
C     H2SPOS(CAT, p), the same place the CABAC path puts it, and 1
C     leaves them in scan order.  Only the 8x8 transform under CAVLC
C     wants the second, because there is no 8x8 CAVLC block -- an 8x8 is
C     four 4x4 blocks whose scans interleave into the 8x8 scan, and the
C     interleaving is the caller's business.
      SUBROUTINE H2CBLK(CAT, N, COMP, NMAX, RAW, LEV, NC)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER CAT, N, COMP, NMAX, RAW, LEV(0:63), NC
      INTEGER LV(0:15), RN(0:15)
      INTEGER NCC, GRP, TOK, TC, T1, I, J, SL, LP, LS, SZ, LC
      INTEGER TZ, ZL, RB, CN
      INTEGER H2CNC, H2U1, H2UN, H2SPOS
      EXTERNAL H2CNC, H2U1, H2UN, H2SPOS

      DO 10 I = 0, 63
         LEV(I) = 0
   10 CONTINUE
      NC = 0

      NCC = H2CNC(CAT, N, COMP)
      IF (NCC .LT. 0) THEN
         GRP = 4
      ELSE IF (NCC .LT. 2) THEN
         GRP = 1
      ELSE IF (NCC .LT. 4) THEN
         GRP = 2
      ELSE IF (NCC .LT. 8) THEN
         GRP = 3
      ELSE
         GRP = 0
      END IF
      IF (GRP .EQ. 0) THEN
C     Table 9-5's last column is not a variable-length code at all: six
C     bits, with 000011 standing for an empty block and everything else
C     spelling out TotalCoeff-1 and TrailingOnes directly.  Two of the
C     sixty-four patterns spell out a TrailingOnes larger than the
C     TotalCoeff beside it and mean nothing.
         TOK = H2UN(6)
         IF (BITERR .NE. 0) RETURN
         IF (TOK .EQ. 3) THEN
            TC = 0
            T1 = 0
         ELSE IF (TOK .EQ. 2 .OR. TOK .EQ. 7) THEN
            BITERR = 1
            RETURN
         ELSE
            TC = ISHFT(TOK, -2) + 1
            T1 = IAND(TOK, 3)
         END IF
      ELSE
         CALL H2VLC(GRP, TOK)
         IF (BITERR .NE. 0) RETURN
         TC = ISHFT(TOK, -2)
         T1 = IAND(TOK, 3)
      END IF
      IF (TC .GT. NMAX) THEN
         BITERR = 1
         RETURN
      END IF
      IF (TC .EQ. 0) RETURN

C     9.2.2.  The trailing ones are +/-1 and carry a sign bit each; a
C     set bit means minus, which is the opposite way round from most of
C     the standard's sign flags.
      DO 20 I = 0, T1 - 1
         IF (H2U1() .EQ. 0) THEN
            LV(I) = 1
         ELSE
            LV(I) = -1
         END IF
   20 CONTINUE

C     suffixLength starts at 1 for a block with many coefficients and
C     fewer than three trailing ones, because such a block is already
C     known to hold levels bigger than one and a one-bit suffix is the
C     cheaper guess.
      SL = 0
      IF (TC .GT. 10 .AND. T1 .LT. 3) SL = 1

      DO 60 I = T1, TC - 1
         LP = 0
   30    CONTINUE
         IF (H2U1() .NE. 0) GOTO 40
            LP = LP + 1
            IF (LP .GT. 31 .OR. BITERR .NE. 0) THEN
               BITERR = 1
               RETURN
            END IF
            GOTO 30
   40    CONTINUE
         IF (BITERR .NE. 0) RETURN
C     9.2.2.1.  The two escapes: prefix 14 with no suffix length yet
C     borrows a four-bit suffix, and prefix 15 and above becomes a plain
C     escape whose suffix carries the whole magnitude.
C
C     The prefix-15 escape is reached by the low-QP vector.  The
C     prefix-16-and-above one is written here and is not: 7.4.5.3.2
C     forbids a prefix above 15 outside the high bit depth profiles, and
C     no 8-bit 4:2:0 residual is large enough to want one.  It is here
C     because the arithmetic is three lines and a decoder that meets the
C     code it does not implement should not produce a picture.
         IF (LP .EQ. 14 .AND. SL .EQ. 0) THEN
            SZ = 4
         ELSE IF (LP .GE. 15) THEN
            SZ = LP - 3
         ELSE
            SZ = SL
         END IF
         LS = 0
         IF (SZ .GT. 0) LS = H2UN(SZ)
         IF (BITERR .NE. 0) RETURN
         LC = ISHFT(MIN(15, LP), SL) + LS
         IF (LP .GE. 15 .AND. SL .EQ. 0) LC = LC + 15
         IF (LP .GE. 16) LC = LC + ISHFT(1, LP - 3) - 4096
C     The first level after the trailing ones cannot be +/-1 unless
C     there were three of them, so the encoder subtracted one from its
C     magnitude and this puts it back.
         IF (I .EQ. T1 .AND. T1 .LT. 3) LC = LC + 2
C     Even levelCode is positive, odd is negative, and both divisions
C     are exact -- levelCode+2 and -levelCode-1 are both even -- so
C     truncating division is the floor the standard's >> asks for.
         IF (MOD(LC, 2) .EQ. 0) THEN
            LV(I) = (LC + 2) / 2
         ELSE
            LV(I) = (-LC - 1) / 2
         END IF
C     The escalation.  A block whose levels keep growing gets a longer
C     suffix and a shorter prefix for the next one; this is the rule
C     that drifts silently if it is applied in the wrong order, because
C     it is only ever reached on blocks with large coefficients.
         IF (SL .EQ. 0) SL = 1
         IF (ABS(LV(I)) .GT. ISHFT(3, SL - 1) .AND. SL .LT. 6) THEN
            SL = SL + 1
         END IF
   60 CONTINUE

C     9.2.3 and 9.2.4: how many zeroes are in front of the coefficients,
C     and how they are shared out between them.  A block that is full
C     has no total_zeros to send.
      TZ = 0
      IF (TC .LT. NMAX) THEN
         IF (CAT .EQ. 3) THEN
            CALL H2VLC(19 + TC, TZ)
         ELSE
            CALL H2VLC(4 + TC, TZ)
         END IF
         IF (BITERR .NE. 0) RETURN
      END IF
      IF (TZ .GT. NMAX - TC) THEN
         BITERR = 1
         RETURN
      END IF

      ZL = TZ
      DO 70 I = 0, TC - 2
         RN(I) = 0
         IF (ZL .GT. 0) THEN
            CALL H2VLC(22 + MIN(ZL, 7), RB)
            IF (BITERR .NE. 0) RETURN
            IF (RB .GT. ZL) THEN
               BITERR = 1
               RETURN
            END IF
            RN(I) = RB
            ZL = ZL - RB
         END IF
   70 CONTINUE
      RN(TC - 1) = ZL

C     Walk back up from the highest frequency, spending one position per
C     coefficient plus its run of zeroes.
      CN = -1
      DO 80 I = TC - 1, 0, -1
         CN = CN + RN(I) + 1
         IF (CN .GT. NMAX - 1) THEN
            BITERR = 1
            RETURN
         END IF
         IF (RAW .NE. 0) THEN
            J = CN
         ELSE
            J = H2SPOS(CAT, CN)
         END IF
         LEV(J) = LV(I)
   80 CONTINUE
      NC = TC
      RETURN
      END

C     7.3.5.3 residual(), CAVLC.  Same order as H2RES and the same
C     dequantisation; the difference is the 8x8 transform, which CAVLC
C     does not have a block category for.  Its sixty-four coefficients
C     arrive as four ordinary 4x4 blocks, each with its own nC and its
C     own TotalCoeff, and the four scans interleave: scan position i of
C     sub-block j is position 4i+j of the 8x8 scan.
      SUBROUTINE H2CRES
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER I, J, K, C, B, NC, LEV(0:63), L8(0:63), Q, WY, WC, W8I

      WY = 1
      W8I = 1
      IF (CINTR .EQ. 0) THEN
         WY = 4
         W8I = 2
      END IF

      IF (CI16 .NE. 0) THEN
         CALL H2CBLK(0, 0, 0, 16, 0, LEV, NC)
         IF (NC .GT. 0) CDCF(1) = 1
         CALL H2DCY(LEV)
         IF (CBPL .NE. 0) THEN
            DO 20 I = 0, 15
               CALL H2CBLK(1, I, 0, 15, 0, LEV, NC)
               CNZ(I + 1) = NC
               CALL H2DQ4(LEV, 1, QPY, I + 1)
   20       CONTINUE
         END IF
      ELSE
         DO 70 K = 0, 3
            IF (IAND(ISHFT(CBPL, -K), 1) .EQ. 0) GOTO 70
            IF (T8FLG .NE. 0) THEN
               DO 30 I = 0, 63
                  L8(I) = 0
   30          CONTINUE
               DO 50 J = 0, 3
                  B = 4 * K + J
                  CALL H2CBLK(2, B, 0, 16, 1, LEV, NC)
                  CNZ(B + 1) = NC
                  DO 40 I = 0, 15
                     L8(ZZ8(4 * I + J)) = LEV(I)
   40             CONTINUE
   50          CONTINUE
               CALL H2DQ8(L8, W8I, QPY, K + 1)
            ELSE
               DO 60 I = 0, 3
                  B = 4 * K + I
                  CALL H2CBLK(2, B, 0, 16, 0, LEV, NC)
                  CNZ(B + 1) = NC
                  CALL H2DQ4(LEV, WY, QPY, B + 1)
   60          CONTINUE
            END IF
   70    CONTINUE
      END IF

      IF (CBPC .GT. 0) THEN
         DO 80 C = 1, 2
            CALL H2CBLK(3, 0, C, 4, 0, LEV, NC)
            IF (NC .GT. 0) CDCF(1 + C) = 1
            CALL H2DCC(LEV, C)
   80    CONTINUE
      END IF
      IF (CBPC .EQ. 2) THEN
         DO 100 C = 1, 2
            Q = QPCB
            IF (C .EQ. 2) Q = QPCR
            WC = C + 1
            IF (CINTR .EQ. 0) WC = C + 4
            DO 90 I = 0, 3
               B = 16 + 4 * (C - 1) + I
               CALL H2CBLK(4, I, C, 15, 0, LEV, NC)
               CNZ(B + 1) = NC
               CALL H2DQ4(LEV, WC, Q, B + 1)
   90       CONTINUE
  100    CONTINUE
      END IF
      RETURN
      END

C     Table 9-4: coded_block_pattern is me(v), a mapped exp-Golomb code.
C     The mapping is a permutation of 0 to 47 and there are two of them,
C     because the patterns an Intra_4x4 macroblock is likely to send are
C     not the ones an inter macroblock is likely to send and the code
C     number is what costs bits.
      INTEGER FUNCTION H2CCBP(INTRA)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER INTRA, K, H2UE
      EXTERNAL H2UE
      INTEGER CBPI(0:47), CBPP(0:47)
      DATA CBPI /
     +   47, 31, 15,  0, 23, 27, 29, 30,  7, 11, 13, 14, 39, 43, 45,
     +   46, 16,  3,  5, 10, 12, 19, 21, 26, 28, 35, 37, 42, 44,  1,
     +    2,  4,  8, 17, 18, 20, 24,  6,  9, 22, 25, 32, 33, 34, 36,
     +   40, 38, 41 /
      DATA CBPP /
     +    0, 16,  1,  2,  4,  8, 32,  3,  5, 10, 12, 15, 47,  7, 11,
     +   13, 14,  6,  9, 31, 35, 37, 42, 44, 33, 34, 36, 40, 39, 43,
     +   45, 46, 17, 18, 20, 24, 19, 21, 26, 28, 23, 27, 29, 30, 22,
     +   25, 38, 41 /
      H2CCBP = 0
      K = H2UE()
      IF (BITERR .NE. 0 .OR. K .LT. 0 .OR. K .GT. 47) THEN
         BITERR = 1
         RETURN
      END IF
      IF (INTRA .NE. 0) THEN
         H2CCBP = CBPI(K)
      ELSE
         H2CCBP = CBPP(K)
      END IF
      RETURN
      END

C     mb_qp_delta, se(v).  Its range is -26 to +25, which is narrow
C     enough that the wrap below can only ever apply once.
      SUBROUTINE H2CQPD
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER D, H2SE
      EXTERNAL H2SE
      D = H2SE()
      IF (D .LT. -26 .OR. D .GT. 25) THEN
         BITERR = 1
         D = 0
      END IF
      DQLAST = D
      QPY = QPY + D
      IF (QPY .LT. 0) THEN
         QPY = QPY + 52
      ELSE IF (QPY .GT. 51) THEN
         QPY = QPY - 52
      END IF
      CALL H2CQP
      RETURN
      END

C     prev_intra4x4_pred_mode_flag and rem_intra4x4_pred_mode.  The
C     remainder is u(3) and so arrives most significant bit first, which
C     is the other way round from the CABAC binarization of the same
C     three bits in H2I4PM.
      INTEGER FUNCTION H2CI4P(PRED)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER PRED, MODE, H2U1, H2UN
      EXTERNAL H2U1, H2UN
      IF (H2U1() .NE. 0) THEN
         H2CI4P = PRED
         RETURN
      END IF
      MODE = H2UN(3)
      IF (MODE .GE. PRED) MODE = MODE + 1
      H2CI4P = MODE
      RETURN
      END

C     sub_mb_type, ue(v).  Called from H2PPRD, which is shared with the
C     CABAC side; see H2SUBT.
      INTEGER FUNCTION H2CSUB()
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER V, H2UE
      EXTERNAL H2UE
      V = H2UE()
      IF (BITERR .NE. 0 .OR. V .LT. 0 .OR. V .GT. 3) THEN
         BITERR = 1
         V = 0
      END IF
      H2CSUB = V
      RETURN
      END

C     ref_idx_l0, te(v).  9.1.1: a truncated exp-Golomb code is one
C     inverted bit when the value it carries can only be 0 or 1, and an
C     ordinary ue(v) otherwise.
C
C     P8R0 is the P_8x8ref0 macroblock type, which is P_8x8 with a
C     promise that every reference index is zero and none of them is
C     sent.  H2PPRD reads the indices for both entropy coders, so the
C     promise has to be kept here.
      INTEGER FUNCTION H2CREF()
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER V, H2U1, H2UE
      EXTERNAL H2U1, H2UE
      INTEGER P8R0
      COMMON /H2CVP/ P8R0
      IF (P8R0 .NE. 0) THEN
         H2CREF = 0
         RETURN
      END IF
      IF (NREF0 .EQ. 2) THEN
         V = 1 - H2U1()
      ELSE
         V = H2UE()
      END IF
      IF (BITERR .NE. 0 .OR. V .LT. 0 .OR. V .GE. NREF0) THEN
         BITERR = 1
         V = 0
      END IF
      H2CREF = V
      RETURN
      END

C     7.3.5, macroblock_layer(), CAVLC.  Same shape as H2MBLY and the
C     same internal mb_type numbering -- 0 for I_NxN, 1 to 24 for the
C     I_16x16 types, 25 for I_PCM, 30 to 33 for the four inter shapes --
C     which the ue(v) codes fall straight onto: an I slice's mb_type is
C     already that numbering, and a P slice's is the four inter types
C     followed by the same intra list offset by five.
      SUBROUTINE H2CMBL(ST)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER ST
      INTEGER I, K, M, MT, PRED, MODE, N8, CBP
      INTEGER H2UE, H2U1, H2PRDM, H2CI4P, H2CCBP
      EXTERNAL H2UE, H2U1, H2PRDM, H2CI4P, H2CCBP
      INTEGER P8R0
      COMMON /H2CVP/ P8R0

      ST = 0
      CI16 = 0
      CPCM = 0
      CPRED = 0
      CBPL = 0
      CBPC = 0
      T8FLG = 0
      CCPM = 0
      CINTR = 1
      CPTYP = 0
      N8 = 0
      P8R0 = 0
      DO 10 I = 1, 24
         CNZ(I) = 0
   10 CONTINUE
      CALL H2ZCOF
      CALL H2ZMOT
      CDCF(1) = 0
      CDCF(2) = 0
      CDCF(3) = 0

      MT = H2UE()
      IF (BITERR .NE. 0) THEN
         ST = -24
         RETURN
      END IF
      IF (SLTYPE .EQ. 0) THEN
         IF (MT .LE. 4) THEN
C     P_8x8ref0 is P_8x8 with the reference indices left out.
            IF (MT .EQ. 4) THEN
               M = 33
               P8R0 = 1
            ELSE
               M = 30 + MT
            END IF
         ELSE
            M = MT - 5
         END IF
      ELSE
         M = MT
      END IF
      IF (M .LT. 0 .OR. (M .GT. 25 .AND. M .LT. 30) .OR. M .GT. 33) THEN
         ST = -24
         RETURN
      END IF

      MTYP(CMBA + 1) = M
      IF (M .GE. 30) THEN
         CINTR = 0
         CPTYP = M
      END IF
      IF (M .EQ. 25) THEN
         CALL H2PCM(ST)
         RETURN
      END IF
      IF (M .GT. 0 .AND. M .LT. 30) THEN
         CI16 = 1
         CPRED = MOD(M - 1, 4)
         CBPC = MOD((M - 1) / 4, 3)
         CBPL = ((M - 1) / 12) * 15
      END IF

      IF (CINTR .EQ. 0) THEN
         CALL H2PPRD(N8)
         DO 20 I = 1, 16
            CI4(I) = 2
   20    CONTINUE
      ELSE IF (M .EQ. 0) THEN
         IF (TR8x8 .NE. 0) T8FLG = H2U1()
         IF (T8FLG .NE. 0) THEN
            DO 40 K = 0, 3
               PRED = H2PRDM(4 * K)
               MODE = H2CI4P(PRED)
               DO 30 I = 1, 4
                  CI4(4 * K + I) = MODE
   30          CONTINUE
   40       CONTINUE
         ELSE
            DO 50 I = 0, 15
               PRED = H2PRDM(I)
               CI4(I + 1) = H2CI4P(PRED)
   50       CONTINUE
         END IF
      ELSE
         DO 60 I = 1, 16
            CI4(I) = 2
   60    CONTINUE
      END IF
      IF (CINTR .NE. 0) THEN
         CCPM = H2UE()
         IF (BITERR .NE. 0 .OR. CCPM .LT. 0 .OR. CCPM .GT. 3) THEN
            ST = -24
            RETURN
         END IF
      END IF

      IF (CI16 .EQ. 0) THEN
         CBP = H2CCBP(CINTR)
         IF (BITERR .NE. 0) THEN
            ST = -24
            RETURN
         END IF
         CBPL = IAND(CBP, 15)
         CBPC = ISHFT(CBP, -4)
      END IF
      IF (CINTR .EQ. 0 .AND. TR8x8 .NE. 0 .AND. CBPL .NE. 0 .AND.
     +    N8 .NE. 0) THEN
         T8FLG = H2U1()
      END IF
      MCBP(CMBA + 1) = CBPL + 16 * CBPC
      MT8(CMBA + 1) = T8FLG

      IF (CBPL .NE. 0 .OR. CBPC .NE. 0 .OR. CI16 .NE. 0) THEN
         CALL H2CQPD
         CALL H2CRES
      ELSE
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

C     7.3.4, slice_data(), CAVLC.
C
C     Two differences from H2SLIC, and both are about where a slice
C     ends.  Skipped macroblocks arrive as a run length in front of the
C     next coded one rather than as a flag each, so a slice that ends in
C     skipped macroblocks sends its last run and then simply stops.  And
C     there is no end_of_slice_flag: the loop runs until
C     more_rbsp_data() says the only thing left is the stop bit.  That
C     predicate is a comparison against BITN, which means the CAVLC
C     slice NAL must have had its trailing bits trimmed -- see H2DECD,
C     where the three cases are laid out.
      SUBROUTINE H2CSLC(ST)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER ST, N, RUN, I, MORE
      INTEGER H2UE, H2MORE
      EXTERNAL H2UE, H2MORE
      ST = 0
      CMBA = SLFMB
      QPY = SLQPY
      DQLAST = 0
      N = 0
      MORE = 1
   10 CONTINUE
         IF (SLTYPE .EQ. 0) THEN
            RUN = H2UE()
            IF (BITERR .NE. 0 .OR. RUN .LT. 0 .OR. RUN .GT. MBN) THEN
               ST = -24
               RETURN
            END IF
            DO 20 I = 1, RUN
               IF (CMBA .GE. MBN) THEN
                  ST = -20
                  RETURN
               END IF
               CALL H2NBR
               CSKP = 1
               CALL H2MBSK(ST)
               IF (ST .NE. 0) RETURN
               CMBA = CMBA + 1
               N = N + 1
   20       CONTINUE
            IF (RUN .GT. 0) MORE = H2MORE()
         END IF
         IF (MORE .NE. 0) THEN
            IF (CMBA .GE. MBN) THEN
               ST = -20
               RETURN
            END IF
            CALL H2NBR
            CSKP = 0
            CALL H2CMBL(ST)
            IF (ST .NE. 0) RETURN
            IF (CINTR .NE. 0) THEN
               CALL H2RECM
            ELSE
               CALL H2PMB(ST)
               IF (ST .NE. 0) RETURN
            END IF
            CMBA = CMBA + 1
            N = N + 1
         END IF
C     The same guard H2SLIC needs, for the same reason: a corrupt slice
C     must stop the decoder rather than the decoder stopping the
C     machine.
         IF (N .GT. MBN) THEN
            ST = -20
            RETURN
         END IF
         MORE = H2MORE()
         IF (MORE .EQ. 0) RETURN
         GOTO 10
      END
