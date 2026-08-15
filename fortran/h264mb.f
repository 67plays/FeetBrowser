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
C
C     IAVA..IAVD are the same four neighbours as an intra macroblock is
C     allowed to predict from.  With constrained_intra_pred_flag set, an
C     inter neighbour is off limits for prediction while remaining
C     perfectly available to every CABAC context and to the deblocking
C     filter -- so the two sets have to be kept apart rather than one of
C     them being cleared.
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
      IAVA = AVLA
      IAVB = AVLB
      IAVC = AVLC
      IAVD = AVLD
      IF (CIPF .NE. 0) THEN
         IF (AVLA .NE. 0) IAVA = MINT(ADRA + 1)
         IF (AVLB .NE. 0) IAVB = MINT(ADRB + 1)
         IF (AVLC .NE. 0) IAVC = MINT(ADRC + 1)
         IF (AVLD .NE. 0) IAVD = MINT(ADRD + 1)
      END IF
      RETURN
      END

C     7.3.4, slice_data(), CABAC.  An I slice is macroblock,
C     end_of_slice_flag, macroblock; a P or B slice puts an mb_skip_flag
C     in front of each macroblock, and a set flag replaces the whole
C     macroblock layer rather than merely emptying it -- P_Skip and
C     B_Skip have no mb_type, no residual and no motion vector of their
C     own, and derive all three.  They derive them differently: P_Skip
C     takes the median of its neighbours, B_Skip takes whichever of the
C     two direct derivations the slice header asked for.
C
C     The end_of_slice_flag is read after a skipped macroblock too.  That
C     is the difference between CABAC and CAVLC here: CAVLC sends a run
C     length and can end a slice inside it, CABAC sends one flag per
C     macroblock and terminates explicitly.
      SUBROUTINE H2SLIC(ST)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER ST, N, H2TRM, H2SKPF
      EXTERNAL H2TRM, H2SKPF
      ST = 0
      CMBA = SLFMB
      QPY = SLQPY
      DQLAST = 0
      N = 0
   10 CONTINUE
         CALL H2NBR
         CSKP = 0
         IF (SLTYPE .EQ. 0 .OR. SLTYPE .EQ. 1) CSKP = H2SKPF()
         IF (CSKP .NE. 0) THEN
            CALL H2MBSK(ST)
            IF (ST .NE. 0) RETURN
         ELSE
            CALL H2MBLY(ST)
            IF (ST .NE. 0) RETURN
            IF (CINTR .NE. 0) THEN
               CALL H2RECM
            ELSE
               CALL H2PMB(ST)
               IF (ST .NE. 0) RETURN
            END IF
         END IF
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
      INTEGER I, K, M, PRED, MODE, NTS, N8
      INTEGER H2MBTY, H2MBTP, H2MBTB, H2I4PM, H2PRDM, H2CHPM
      INTEGER H2CBPL, H2CBPC, H2DEC
      EXTERNAL H2MBTY, H2MBTP, H2MBTB, H2I4PM, H2PRDM, H2CHPM
      EXTERNAL H2CBPL, H2CBPC, H2DEC

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
      DO 10 I = 1, 24
         CNZ(I) = 0
   10 CONTINUE
      CALL H2ZCOF
      CALL H2ZMOT
      CDCF(1) = 0
      CDCF(2) = 0
      CDCF(3) = 0

      IF (SLTYPE .EQ. 0) THEN
         M = H2MBTP()
      ELSE IF (SLTYPE .EQ. 1) THEN
         M = H2MBTB()
      ELSE
         M = H2MBTY()
      END IF
      MTYP(CMBA + 1) = M
      IF (M .GE. 30) THEN
C     Table 7-13's inter types, numbered from 30 for P and from 40 for B
C     so that nothing downstream can confuse one with an I_16x16 type or
C     the two families with each other.  Which lists each B shape
C     predicts from is not in the number and does not need to be: it is
C     in CREF, where a list this partition did not use holds -1.
         CINTR = 0
         CPTYP = M
      END IF
      IF (M .EQ. 25) THEN
         CALL H2PCM(ST)
         RETURN
      END IF
      IF (M .GT. 0 .AND. M .LT. 30) THEN
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

      IF (CINTR .EQ. 0) THEN
         CALL H2PPRD(N8, ST)
         IF (ST .NE. 0) RETURN
         DO 15 I = 1, 16
            CI4(I) = 2
   15    CONTINUE
      ELSE IF (M .EQ. 0) THEN
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
      IF (CINTR .NE. 0) CCPM = H2CHPM()

      IF (CI16 .EQ. 0) THEN
         CBPL = H2CBPL()
         CBPC = H2CBPC()
      END IF
C     7.3.5: for an inter macroblock transform_size_8x8_flag comes after
C     the coded block pattern rather than before the prediction, and only
C     when there is luma residual to transform and no partition smaller
C     than 8x8 -- an 8x4 partition and an 8x8 transform would be a
C     transform across a motion discontinuity, which the standard does
C     not allow.
      IF (CINTR .EQ. 0 .AND. TR8x8 .NE. 0 .AND. CBPL .NE. 0 .AND.
     +    N8 .NE. 0) THEN
         T8FLG = H2DEC(399 + NTS)
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
      INTEGER I, L, R, RI, N
      DO 10 I = 1, 24
         MNZ(I, CMBA + 1) = CNZ(I)
   10 CONTINUE
      DO 20 I = 1, 16
         MI4(I, CMBA + 1) = CI4(I)
         DO 15 L = 1, 2
            MMVX(I, L, CMBA + 1) = CMVX(I, L)
            MMVY(I, L, CMBA + 1) = CMVY(I, L)
            MMDX(I, L, CMBA + 1) = CMDX(I, L)
            MMDY(I, L, CMBA + 1) = CMDY(I, L)
   15    CONTINUE
   20 CONTINUE
      MINT(CMBA + 1) = CINTR
      MSKP(CMBA + 1) = CSKP
      DO 27 L = 1, 2
         N = RL0N
         IF (L .EQ. 2) N = RL1N
         DO 25 I = 1, 4
            RI = CREF(I, L)
            IF (SLTYPE .NE. 1 .AND. L .EQ. 2) RI = -1
            MREF(I, L, CMBA + 1) = RI
C     Which picture, not which index.  8.7.2.1 asks whether the two sides
C     of an edge used the same reference picture, and two slices of the
C     same picture can reach one picture through different list indices
C     -- comparing indices would filter an edge that needs no filtering
C     and, worse, skip one that does.  For a B macroblock it is worse
C     still: the same picture sits at different indices in the two lists
C     of one slice, so an edge between a list-0 partition and a list-1
C     partition of the same picture would be filtered on every
C     macroblock of every B frame.
            R = -1
            IF (CINTR .EQ. 0 .AND. RI .GE. 0 .AND. RI .LT. N) THEN
               IF (L .EQ. 1) THEN
                  R = DPID(RL0(RI))
               ELSE
                  R = DPID(RL1(RI))
               END IF
            END IF
            MRPI(I, L, CMBA + 1) = R
   25    CONTINUE
   27 CONTINUE
      DO 28 I = 1, 4
         MDIR(I, CMBA + 1) = CDIR(I)
   28 CONTINUE
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

C     Empty the motion of the macroblock about to be decoded.  CREF
C     starts at -1 and not at 0 because -1 is the answer a neighbour
C     question must get for a partition whose ref_idx has not been read
C     yet, and 0 is a perfectly good reference index.
      SUBROUTINE H2ZMOT
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER I, L
      DO 15 L = 1, 2
         DO 10 I = 1, 16
            CMVX(I, L) = 0
            CMVY(I, L) = 0
            CMDX(I, L) = 0
            CMDY(I, L) = 0
            CMVOK(I, L) = 0
   10    CONTINUE
   15 CONTINUE
      DO 20 I = 1, 4
         CREF(I, 1) = -1
         CREF(I, 2) = -1
         CSUB(I) = 0
         CDIR(I) = 0
   20 CONTINUE
      RETURN
      END

C     9.3.3.1.1.1: mb_skip_flag.  Its context counts the neighbours that
C     were *not* skipped, so a run of skipped macroblocks decodes each
C     next flag with the context that has learned to expect another one.
C
C     A B slice reads the same flag through its own block of three
C     contexts at 24.  It has to: skipping is much commoner in a B
C     picture than in a P one, and a set of contexts trained on P
C     pictures would spend the first few hundred macroblocks of every B
C     slice catching up.
      INTEGER FUNCTION H2SKPF()
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER C, BASE, H2DEC
      EXTERNAL H2DEC
      C = 0
      IF (AVLA .NE. 0) THEN
         IF (MSKP(ADRA + 1) .EQ. 0) C = C + 1
      END IF
      IF (AVLB .NE. 0) THEN
         IF (MSKP(ADRB + 1) .EQ. 0) C = C + 1
      END IF
      BASE = 11
      IF (SLTYPE .EQ. 1) BASE = 24
      H2SKPF = H2DEC(BASE + C)
      RETURN
      END

C     The intra suffix of Table 9-34 and Table 9-37: the same six
C     decisions as Table 9-36's I-slice mb_type, one context lower at
C     each step because the suffix has no neighbour-dependent first bin
C     to spend three contexts on.  BASE is 17 in a P slice and 32 in a B
C     slice; nothing else about it differs.
      INTEGER FUNCTION H2ISUF(BASE)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER BASE, M, H2DEC, H2TRM
      EXTERNAL H2DEC, H2TRM
      IF (H2DEC(BASE) .EQ. 0) THEN
         H2ISUF = 0
         RETURN
      END IF
      IF (H2TRM() .NE. 0) THEN
         H2ISUF = 25
         RETURN
      END IF
      M = 1 + 12 * H2DEC(BASE + 1)
      IF (H2DEC(BASE + 2) .NE. 0) M = M + 4 + 4 * H2DEC(BASE + 2)
      M = M + 2 * H2DEC(BASE + 3)
      M = M + H2DEC(BASE + 3)
      H2ISUF = M
      RETURN
      END

C     Table 9-37 and Table 7-14: mb_type for a B slice, decoded through
C     the contexts at 27.
C
C     Table 7-14 has twenty-three inter entries because it spells out
C     every combination of shape and prediction direction: a 16x8 whose
C     top half predicts from list 1 and whose bottom half predicts from
C     both is a type of its own.  The binarization walks a tree rather
C     than counting, so the arithmetic at the end is not a formula
C     anyone would have guessed -- it is the inverse of a table.
C
C     The first bin's context counts the neighbours that were neither
C     B_Skip nor B_Direct_16x16, which is 9.3.3.1.1.3 asking "did the
C     neighbourhood bother to code any motion", and MTYP answers it
C     because both of those are recorded as 40 and 45.
C
C     Every bin is its own statement.  Fortran does not fix the order in
C     which the operands of an expression are evaluated, and two calls
C     to the arithmetic decoder in one expression would be two bins read
C     in whichever order the compiler liked that day.
      INTEGER FUNCTION H2MBTB()
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER C, T, B, M, H2DEC, H2ISUF
      EXTERNAL H2DEC, H2ISUF
      C = 0
      IF (AVLA .NE. 0) THEN
         M = MTYP(ADRA + 1)
         IF (M .NE. 40 .AND. M .NE. 45) C = C + 1
      END IF
      IF (AVLB .NE. 0) THEN
         M = MTYP(ADRB + 1)
         IF (M .NE. 40 .AND. M .NE. 45) C = C + 1
      END IF
      IF (H2DEC(27 + C) .EQ. 0) THEN
         H2MBTB = 40
         CBTYP = 0
         RETURN
      END IF
      IF (H2DEC(30) .EQ. 0) THEN
         T = 1 + H2DEC(32)
      ELSE
         B = 8 * H2DEC(31)
         B = B + 4 * H2DEC(32)
         B = B + 2 * H2DEC(32)
         B = B + H2DEC(32)
         IF (B .LT. 8) THEN
            T = B + 3
         ELSE IF (B .EQ. 13) THEN
            H2MBTB = H2ISUF(32)
            RETURN
         ELSE IF (B .EQ. 14) THEN
            T = 11
         ELSE IF (B .EQ. 15) THEN
            T = 22
         ELSE
            T = 2 * B - 4 + H2DEC(32)
         END IF
      END IF
C     Table 7-14 into the shapes this decoder keeps: type 0 is direct,
C     1 to 3 are 16x16, 4 to 21 alternate 16x8 and 8x16, and 22 is the
C     one that has sub-macroblock types of its own.
      IF (T .EQ. 0) THEN
         H2MBTB = 40
      ELSE IF (T .LE. 3) THEN
         H2MBTB = 41
      ELSE IF (T .EQ. 22) THEN
         H2MBTB = 44
      ELSE IF (MOD(T, 2) .EQ. 0) THEN
         H2MBTB = 42
      ELSE
         H2MBTB = 43
      END IF
      CBTYP = T
      RETURN
      END

C     Table 9-38's B column: sub_mb_type in a B slice, through the
C     contexts at 36.  Returns Table 7-18's index unchanged, because
C     unlike mb_type there is nothing about it a shape number could
C     usefully hide.
      INTEGER FUNCTION H2SUBB()
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER T, H2DEC
      EXTERNAL H2DEC
      IF (H2DEC(36) .EQ. 0) THEN
         H2SUBB = 0
         RETURN
      END IF
      IF (H2DEC(37) .EQ. 0) THEN
         H2SUBB = 1 + H2DEC(39)
         RETURN
      END IF
      IF (H2DEC(38) .NE. 0) THEN
         IF (H2DEC(39) .NE. 0) THEN
            H2SUBB = 11 + H2DEC(39)
            RETURN
         END IF
         T = 7
      ELSE
         T = 3
      END IF
      T = T + 2 * H2DEC(39)
      T = T + H2DEC(39)
      H2SUBB = T
      RETURN
      END

C     Table 9-34: mb_type for a P slice.  Three bins pick between the
C     four inter shapes, or the first bin says "intra" and the rest of
C     the macroblock type is the I-slice binarization read against a
C     different set of contexts starting at 17.
C
C     The inter types come back as 30 to 33 rather than 0 to 3, so that
C     the intra numbering can be returned unchanged from the same
C     function and no caller has to be told which kind it asked for.
      INTEGER FUNCTION H2MBTP()
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER H2DEC, H2ISUF
      EXTERNAL H2DEC, H2ISUF
      IF (H2DEC(14) .EQ. 0) THEN
         IF (H2DEC(15) .EQ. 0) THEN
            IF (H2DEC(16) .EQ. 0) THEN
               H2MBTP = 30
            ELSE
               H2MBTP = 33
            END IF
         ELSE
            IF (H2DEC(17) .EQ. 0) THEN
               H2MBTP = 32
            ELSE
               H2MBTP = 31
            END IF
         END IF
         RETURN
      END IF
      H2MBTP = H2ISUF(17)
      RETURN
      END

C     Table 9-38: sub_mb_type in a P slice.  One bin for 8x8, two for
C     8x4, three for the two 4x8 and 4x4 cases.
      INTEGER FUNCTION H2SUBT()
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER H2DEC, H2CSUB
      EXTERNAL H2DEC, H2CSUB
      IF (ECMODE .EQ. 0) THEN
         H2SUBT = H2CSUB()
         RETURN
      END IF
      IF (H2DEC(21) .NE. 0) THEN
         H2SUBT = 0
      ELSE IF (H2DEC(22) .EQ. 0) THEN
         H2SUBT = 1
      ELSE IF (H2DEC(23) .NE. 0) THEN
         H2SUBT = 2
      ELSE
         H2SUBT = 3
      END IF
      RETURN
      END

C     The reference index of a neighbouring 4x4 block, for
C     9.3.3.1.1.6's context only.
C
C     This is not H2GETN with the motion thrown away.  A partition of
C     this macroblock whose ref_idx has been read but whose motion vector
C     has not answers here with its index and answers there with "not
C     available", and both answers are right: 7.3.5.2 reads every
C     ref_idx of a macroblock before the first mvd, so by the time the
C     second partition's index is decoded the first one's index is known
C     and its vector is not.
C
C     A direct-predicted neighbour answers -1 whatever index it derived.
C     9.3.3.1.1.6 names B_Skip, B_Direct_16x16 and B_Direct_8x8 among the
C     conditions that clear condTermFlagN, and the reason is that a
C     derived index says nothing about what the encoder chose -- it is
C     not evidence that this partition will look past the nearest
C     reference picture, which is the only question the context asks.
      SUBROUTINE H2GETR(L, BX, BY, RF)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER L, BX, BY, RF
      INTEGER MB, NX, NY, Q
      RF = -1
      IF (BY .LT. 0) THEN
         IF (BX .LT. 0 .OR. BX .GT. 3) RETURN
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
         Q = 1 + BX / 2 + 2 * (BY / 2)
         IF (CDIR(Q) .EQ. 0) RF = CREF(Q, L)
         RETURN
      END IF
      IF (MINT(MB + 1) .NE. 0) RETURN
      Q = 1 + NX / 2 + 2 * (NY / 2)
      IF (MDIR(Q, MB + 1) .NE. 0) RETURN
      RF = MREF(Q, L, MB + 1)
      RETURN
      END

C     ref_idx_lX for the partition whose top left 4x4 block is (BX, BY):
C     unary, with the first bin's context asking whether either
C     neighbour looked past the nearest reference picture.  Both lists
C     share one block of contexts at 54 -- the standard gives ref_idx_l0
C     and ref_idx_l1 the same ctxIdxOffset, which is the one place the
C     two lists are not kept apart.
      INTEGER FUNCTION H2REFI(L, BX, BY)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER L, BX, BY, C, R, RA, RB, H2DEC, H2CREF
      EXTERNAL H2DEC, H2CREF
C     CAVLC sends ref_idx as te(v), which needs no neighbours and no
C     context modelling.  H2CREF bounds the value by NREF0 and takes no
C     list, which is right because L is always 0 here: a B slice under
C     CAVLC is refused in H2SLIC before any macroblock is read.
      IF (ECMODE .EQ. 0) THEN
         H2REFI = H2CREF()
         RETURN
      END IF
      CALL H2GETR(L, BX - 1, BY, RA)
      CALL H2GETR(L, BX, BY - 1, RB)
      C = 0
      IF (RA .GT. 0) C = C + 1
      IF (RB .GT. 0) C = C + 2
      IF (H2DEC(54 + C) .EQ. 0) THEN
         H2REFI = 0
         RETURN
      END IF
      R = 1
      C = 4
   10 IF (H2DEC(54 + C) .EQ. 0) GOTO 20
         R = R + 1
         C = 5
         IF (R .GE. 32 .OR. BITERR .NE. 0) THEN
            BITERR = 1
            GOTO 20
         END IF
         GOTO 10
   20 CONTINUE
      H2REFI = R
      RETURN
      END

C     One component of one mvd, 9.3.2.3's UEG3 binarization: a truncated
C     unary prefix of up to nine bins through six contexts, then an
C     exp-Golomb suffix of bypass bins, then the sign.
C
C     COMP is 0 for the horizontal component and 1 for the vertical, and
C     the two have separate context blocks -- 40 and 47 -- because
C     horizontal motion is commoner and larger than vertical motion in
C     almost every real picture, and one shared set of contexts would
C     learn neither well.
      INTEGER FUNCTION H2MVDC(L, BX, BY, COMP)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER L, BX, BY, COMP
      INTEGER AX, AY, BBX, BBY, S, BASE, C, V, NB, K, SUF
      INTEGER H2DEC, H2BYP, H2SE
      EXTERNAL H2DEC, H2BYP, H2SE
      IF (ECMODE .EQ. 0) THEN
C     CAVLC sends mvd as a plain se(v) and needs none of the context
C     modelling below -- not the neighbours, not the two component
C     context blocks, not the UEG3 suffix.
         H2MVDC = H2SE()
         RETURN
      END IF
      CALL H2GETD(L, BX - 1, BY, AX, AY)
      CALL H2GETD(L, BX, BY - 1, BBX, BBY)
      IF (COMP .EQ. 0) THEN
         S = ABS(AX) + ABS(BBX)
         BASE = 40
      ELSE
         S = ABS(AY) + ABS(BBY)
         BASE = 47
      END IF
C     9.3.3.1.1.7: three bands, and the boundaries are not round numbers
C     by accident -- three quarter samples is under a pixel of motion and
C     thirty-two is eight pixels of it.
      C = 0
      IF (S .GE. 3) C = 1
      IF (S .GT. 32) C = 2
      IF (H2DEC(BASE + C) .EQ. 0) THEN
         H2MVDC = 0
         RETURN
      END IF
      V = 1
      NB = 1
   10 IF (V .GE. 9) GOTO 20
         C = 3 + MIN(NB - 1, 3)
         IF (H2DEC(BASE + C) .EQ. 0) GOTO 40
         V = V + 1
         NB = NB + 1
         GOTO 10
   20 CONTINUE
      K = 3
      SUF = 0
   25 IF (H2BYP() .EQ. 0) GOTO 30
         SUF = SUF + ISHFT(1, K)
         K = K + 1
         IF (K .GT. 24 .OR. BITERR .NE. 0) THEN
            BITERR = 1
            GOTO 30
         END IF
         GOTO 25
   30 CONTINUE
   35 IF (K .LE. 0) GOTO 38
         K = K - 1
         SUF = SUF + ISHFT(H2BYP(), K)
         GOTO 35
   38 CONTINUE
      V = V + SUF
   40 CONTINUE
      IF (H2BYP() .NE. 0) V = -V
      H2MVDC = V
      RETURN
      END

C     7.3.5.2 and 8.4.1.3: the whole prediction half of an inter
C     macroblock.  Sub-macroblock types first, then every reference index,
C     then every motion vector difference -- that order is the syntax's
C     and not a choice, and it is the reason a reference index has to be
C     readable by a neighbour before the vector that goes with it exists.
C
C     In a B slice the order is all of list 0's indices, then all of list
C     1's, then all of list 0's differences, then all of list 1's.  So a
C     partition can be half decoded: known in list 0 and unknown in list
C     1, at the same instant.  That is why CMVOK is per list.
C
C     8.4.1's own order is by partition rather than by list, and the two
C     give the same answer: within a macroblock the neighbours A, B and C
C     of a partition are never later partitions in the same list, so a
C     list-major sweep and a partition-major sweep read the same state.
C     The one exception is neighbour C of a sub-partition in the top left
C     8x8, which lands in the top right 8x8 and must read as unavailable
C     either way -- it does, because that 8x8's turn has not come.
C
C     A direct partition takes no part in either pass.  Its motion was
C     derived before the first index was read, which is where 7.3.5.2
C     puts it and also where it has to be: a coded 8x8 next to a direct
C     one predicts from the direct one's vector.
C
C     N8 comes back 0 when any 8x8 was split further, which is what
C     7.3.5 calls noSubMbPartSizeLessThan8x8Flag and uses to decide
C     whether an 8x8 transform may be offered at all.  A direct 8x8
C     counts as split unless direct_8x8_inference_flag says its motion
C     is uniform across the 8x8.
      SUBROUTINE H2PPRD(N8, ST)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER N8, ST
      INTEGER NP, BX(16), BY(16), BW(16), BH(16), MODE(16), DIR(16)
      INTEGER NMBP, PBX(4), PBY(4), PBW, PBH, PPRD(4)
      INTEGER I, K, L, P, Q, RI, QX, QY, VX, VY, DX, DY, PVX, PVY, B
      INTEGER NL, ANY, IDX
      INTEGER PDA(0:8), PDB(0:8)
      INTEGER H2SUBT, H2SUBB, H2SPRD, H2REFI, H2MVDC
      EXTERNAL H2SUBT, H2SUBB, H2SPRD, H2REFI, H2MVDC
C     Table 7-14's prediction pairs for the eighteen 16x8 and 8x16 types,
C     as 1 for list 0, 2 for list 1 and 3 for both.  The number doubles
C     as a bit mask: partition P uses list L exactly when L is set in it.
      DATA PDA /1, 2, 1, 2, 1, 2, 3, 3, 3/
      DATA PDB /1, 2, 2, 1, 3, 3, 1, 2, 3/

      ST = 0
      N8 = 1
      ANY = 0
      IF (CPTYP .EQ. 33) THEN
         DO 10 K = 1, 4
            CSUB(K) = H2SUBT()
            IF (CSUB(K) .NE. 0) N8 = 0
   10    CONTINUE
      ELSE IF (CPTYP .EQ. 44) THEN
         DO 12 K = 1, 4
            CSUB(K) = H2SUBB()
            IF (CSUB(K) .EQ. 0) THEN
               CDIR(K) = 1
               ANY = 1
               IF (D8INF .EQ. 0) N8 = 0
            ELSE IF (CSUB(K) .GT. 3) THEN
               N8 = 0
            END IF
   12    CONTINUE
      ELSE IF (CPTYP .EQ. 40) THEN
         DO 14 K = 1, 4
            CDIR(K) = 1
   14    CONTINUE
         ANY = 1
         N8 = D8INF
      END IF

      IF (ANY .NE. 0) THEN
         CALL H2DRCT(ST)
         IF (ST .NE. 0) RETURN
      END IF

      IF (CPTYP .EQ. 33 .OR. CPTYP .EQ. 44) THEN
         NMBP = 4
         PBW = 2
         PBH = 2
      ELSE IF (CPTYP .EQ. 31 .OR. CPTYP .EQ. 42) THEN
         NMBP = 2
         PBW = 4
         PBH = 2
      ELSE IF (CPTYP .EQ. 32 .OR. CPTYP .EQ. 43) THEN
         NMBP = 2
         PBW = 2
         PBH = 4
      ELSE
         NMBP = 1
         PBW = 4
         PBH = 4
      END IF
      DO 20 P = 1, NMBP
         PPRD(P) = 1
         IF (NMBP .EQ. 4) THEN
            PBX(P) = MOD(P - 1, 2) * 2
            PBY(P) = ((P - 1) / 2) * 2
         ELSE IF (PBH .EQ. 2) THEN
            PBX(P) = 0
            PBY(P) = (P - 1) * 2
         ELSE IF (PBW .EQ. 2) THEN
            PBX(P) = (P - 1) * 2
            PBY(P) = 0
         ELSE
            PBX(P) = 0
            PBY(P) = 0
         END IF
   20 CONTINUE
      IF (SLTYPE .EQ. 1) THEN
         IF (CPTYP .EQ. 41) THEN
            PPRD(1) = CBTYP
         ELSE IF (CPTYP .EQ. 42 .OR. CPTYP .EQ. 43) THEN
            IDX = (CBTYP - 4) / 2
            IF (IDX .LT. 0 .OR. IDX .GT. 8) THEN
               ST = -54
               RETURN
            END IF
            PPRD(1) = PDA(IDX)
            PPRD(2) = PDB(IDX)
         ELSE IF (CPTYP .EQ. 44) THEN
            DO 22 P = 1, 4
               PPRD(P) = H2SPRD(CSUB(P))
   22       CONTINUE
         END IF
      END IF

      DO 50 L = 1, 2
         IF (L .EQ. 2 .AND. SLTYPE .NE. 1) GOTO 50
         NL = NREF0
         IF (L .EQ. 2) NL = NREF1
         DO 48 P = 1, NMBP
            Q = 1 + PBX(P) / 2 + 2 * (PBY(P) / 2)
            IF (CDIR(Q) .NE. 0) GOTO 48
            RI = -1
            IF (IAND(PPRD(P), L) .NE. 0) THEN
               IF (NL .GT. 1) THEN
                  RI = H2REFI(L, PBX(P), PBY(P))
               ELSE
                  RI = 0
               END IF
               IF (RI .GE. NL) THEN
                  ST = -54
                  RETURN
               END IF
            END IF
            DO 40 QY = PBY(P) / 2, (PBY(P) + PBH - 1) / 2
               DO 30 QX = PBX(P) / 2, (PBX(P) + PBW - 1) / 2
                  CREF(1 + QX + 2 * QY, L) = RI
   30          CONTINUE
   40       CONTINUE
   48    CONTINUE
   50 CONTINUE

      CALL H2PLST(NP, BX, BY, BW, BH, MODE, DIR)
      DO 90 L = 1, 2
         IF (L .EQ. 2 .AND. SLTYPE .NE. 1) GOTO 90
         DO 88 I = 1, NP
            B = 1 + BX(I) + 4 * BY(I)
            Q = 1 + BX(I) / 2 + 2 * (BY(I) / 2)
            RI = CREF(Q, L)
            DX = 0
            DY = 0
            IF (DIR(I) .NE. 0) THEN
C     Already derived; only the availability marking is owed.
               VX = CMVX(B, L)
               VY = CMVY(B, L)
            ELSE IF (RI .LT. 0) THEN
C     A partition that does not predict from this list still takes its
C     turn in this list's sweep, and is marked available with a zero
C     vector and the -1 index it already has.  A later partition asking
C     it for a prediction must get "available, but not from this list",
C     which is not the same answer as "not decoded yet".
               VX = 0
               VY = 0
            ELSE
               CALL H2MVPR(L, BX(I), BY(I), BW(I), RI, MODE(I),
     +                     PVX, PVY)
               DX = H2MVDC(L, BX(I), BY(I), 0)
               DY = H2MVDC(L, BX(I), BY(I), 1)
               VX = PVX + DX
               VY = PVY + DY
            END IF
            DO 80 QY = BY(I), BY(I) + BH(I) - 1
               DO 70 QX = BX(I), BX(I) + BW(I) - 1
                  B = 1 + QX + 4 * QY
                  CMVX(B, L) = VX
                  CMVY(B, L) = VY
                  CMDX(B, L) = DX
                  CMDY(B, L) = DY
                  CMVOK(B, L) = 1
   70          CONTINUE
   80       CONTINUE
   88    CONTINUE
   90 CONTINUE
      RETURN
      END

C     8.4.1.1: a skipped macroblock.  Nothing is read for it at all --
C     not a type, not a coefficient, not a vector -- so all of this is
C     derivation, and the only thing that makes it work is that the
C     encoder derived exactly the same numbers.
C
C     P_Skip and B_Skip share nothing but the flag that announces them.
C     P_Skip takes the median of its neighbours against reference 0, with
C     two special cases that force it to zero; B_Skip is B_Direct_16x16
C     without the coded block pattern, and derives whatever the slice
C     header's direct mode derives.
      SUBROUTINE H2MBSK(ST)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER ST, I, L, VX, VY
      ST = 0
      CINTR = 0
      CPTYP = 34
      IF (SLTYPE .EQ. 1) CPTYP = 45
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
      CALL H2ZMOT
      CDCF(1) = 0
      CDCF(2) = 0
      CDCF(3) = 0
      DO 20 I = 1, 16
         CI4(I) = 2
   20 CONTINUE
      MTYP(CMBA + 1) = CPTYP
      MCBP(CMBA + 1) = 0
      MT8(CMBA + 1) = 0
      IF (SLTYPE .EQ. 1) THEN
         DO 25 I = 1, 4
            CDIR(I) = 1
   25    CONTINUE
         CALL H2DRCT(ST)
         IF (ST .NE. 0) RETURN
         DO 35 L = 1, 2
            DO 30 I = 1, 16
               CMVOK(I, L) = 1
   30       CONTINUE
   35    CONTINUE
      ELSE
         CALL H2SKMV(VX, VY)
         DO 38 I = 1, 4
            CREF(I, 1) = 0
   38    CONTINUE
         DO 40 I = 1, 16
            CMVX(I, 1) = VX
            CMVY(I, 1) = VY
            CMVOK(I, 1) = 1
   40    CONTINUE
      END IF
C     A skipped macroblock sends no mb_qp_delta, and the next one that
C     does must not see this as a macroblock that changed the quantiser.
      DQLAST = 0
      CALL H2CQP
      CALL H2SAVE
      CALL H2PMB(ST)
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
      INTEGER I, K, C, B, NC, LEV(0:63), Q, WY, WC, W8I

C     Which of the six 4x4 and two 8x8 scaling lists this macroblock's
C     residual is weighted by: the intra set or the inter set.
      WY = 1
      W8I = 1
      IF (CINTR .EQ. 0) THEN
         WY = 4
         W8I = 2
      END IF

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
               CALL H2DQ8(LEV, W8I, QPY, K + 1)
            ELSE
               DO 90 I = 0, 3
                  B = 4 * K + I
                  CALL H2RBLK(2, B, 0, 16, LEV, NC)
                  CNZ(B + 1) = NC
                  CALL H2DQ4(LEV, WY, QPY, B + 1)
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
            WC = C + 1
            IF (CINTR .EQ. 0) WC = C + 4
            DO 120 I = 0, 3
               B = 16 + 4 * (C - 1) + I
               CALL H2RBLK(4, I, C, 15, LEV, NC)
               CNZ(B + 1) = NC
               CALL H2DQ4(LEV, WC, Q, B + 1)
  120       CONTINUE
  130    CONTINUE
      END IF
      RETURN
      END

C     9.3.3.1.1.9: the coded_block_flag context.  A neighbouring block
C     that exists but carried no coefficients counts as not coded, which
C     comes out of the stored counts directly.
C
C     A neighbouring block that is missing counts as coded for an intra
C     macroblock and as not coded for an inter one.  That asymmetry is in
C     the standard and it is not decoration: at the edge of a picture an
C     intra macroblock is surrounded by nothing and its neighbours are
C     assumed busy, while an inter macroblock at the same edge is usually
C     predicting well and its neighbours are assumed empty.  UN carries
C     which of the two this macroblock is.
      INTEGER FUNCTION H2CBFC(CAT, N, COMP)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER CAT, N, COMP, BX, BY, A, B, CX, CY, K, UN
      A = 0
      B = 0
      UN = 0
      IF (CINTR .NE. 0) UN = 1
      IF (CAT .EQ. 0) THEN
         IF (AVLA .EQ. 0) THEN
            A = UN
         ELSE
            A = MDCF(1, ADRA + 1)
         END IF
         IF (AVLB .EQ. 0) THEN
            B = UN
         ELSE
            B = MDCF(1, ADRB + 1)
         END IF
      ELSE IF (CAT .EQ. 3) THEN
         IF (AVLA .EQ. 0) THEN
            A = UN
         ELSE
            A = MDCF(1 + COMP, ADRA + 1)
         END IF
         IF (AVLB .EQ. 0) THEN
            B = UN
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
            A = UN
         ELSE IF (MNZ(K + N + 2, ADRA + 1) .GT. 0) THEN
            A = 1
         END IF
         IF (CY .GT. 0) THEN
            IF (CNZ(K + N - 1) .GT. 0) B = 1
         ELSE IF (AVLB .EQ. 0) THEN
            B = UN
         ELSE IF (MNZ(K + N + 3, ADRB + 1) .GT. 0) THEN
            B = 1
         END IF
      ELSE
         BX = BLKX(N) / 4
         BY = BLKY(N) / 4
         IF (BX .GT. 0) THEN
            IF (CNZ(ZORD(BX - 1, BY) + 1) .GT. 0) A = 1
         ELSE IF (AVLA .EQ. 0) THEN
            A = UN
         ELSE IF (MNZ(ZORD(3, BY) + 1, ADRA + 1) .GT. 0) THEN
            A = 1
         END IF
         IF (BY .GT. 0) THEN
            IF (CNZ(ZORD(BX, BY - 1) + 1) .GT. 0) B = 1
         ELSE IF (AVLB .EQ. 0) THEN
            B = UN
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
C     Under CAVLC the cursor is exactly where the syntax says it is and
C     pcm_alignment_zero_bit is a plain alignment.
      IF (ECMODE .NE. 0) THEN
         BITP = BITP - 16
         IF (BITP .LT. 0) BITP = 0
      END IF
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
C     Only CABAC has to be restarted; CAVLC never stopped.
      IF (ECMODE .NE. 0) CALL H2CINI(SLQPY)
      RETURN
      END
