C     Sequence and picture parameter sets, and the slice header.
C
C     These are the only places in H.264 where a field we will never use
C     has to be read anyway.  A bitstream has no field offsets:
C     pic_order_cnt stands between the frame number and the picture
C     size, so an SPS parser that skips it does not save the work, it
C     loses the picture size.  Every ue(v) below that goes into a
C     variable nothing reads is there for that reason and is commented
C     as such.
C
C     What we refuse, and why refusing is better than guessing: * chroma
C     other than 4:2:0, and bit depths above 8 -- the sample arrays, the
C     chroma QP mapping and the intra predictors are all written for
C     4:2:0 8-bit, and half-supporting the rest would decode to
C     plausible wrong colours instead of to an error; * interlace of any
C     kind, which changes the scan tables, the CABAC context offsets and
C     the neighbour derivation all at once; * more than one slice group,
C     which reorders the macroblocks.

C     Refuse a parameter set we cannot honour.  Returns 0 for accepted.
      SUBROUTINE H2SPSP(ST)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER ST
      INTEGER PROF, I, N, CNT, DUMMY, UD, SUBW, SUBH
      INTEGER H2U1, H2UN, H2UE, H2SE
      EXTERNAL H2U1, H2UN, H2UE, H2SE

      ST = 0
      PROF = H2UN(8)
      DUMMY = H2UN(8)
      DUMMY = H2UN(8)
      DUMMY = H2UE()

      CHFMT = 1
      BITDL = 8
      BITDC = 8
      SEPCP = 0
      SLPRES = 0
C     The High profiles and everything above them carry a chroma format
C     and bit depth of their own; Baseline, Main and Extended are 4:2:0
C     8-bit by definition and say nothing.
      IF (PROF .EQ. 100 .OR. PROF .EQ. 110 .OR. PROF .EQ. 122 .OR.
     +    PROF .EQ. 244 .OR. PROF .EQ. 44  .OR. PROF .EQ. 83  .OR.
     +    PROF .EQ. 86  .OR. PROF .EQ. 118 .OR. PROF .EQ. 128 .OR.
     +    PROF .EQ. 138 .OR. PROF .EQ. 139 .OR. PROF .EQ. 134 .OR.
     +    PROF .EQ. 135) THEN
         CHFMT = H2UE()
         IF (CHFMT .EQ. 3) SEPCP = H2U1()
         BITDL = H2UE() + 8
         BITDC = H2UE() + 8
C     qpprime_y_zero_transform_bypass_flag.  With it set, a macroblock at
C     QP 0 skips the transform and the deblocking filter entirely and its
C     residual is added to the prediction as it stands -- x264's lossless
C     mode, which is what --qp 0 turns on.  Reading the flag and ignoring
C     it does not produce a slightly wrong picture, it produces a wholly
C     wrong one, because every coefficient goes through an inverse
C     transform that was never applied.  Refuse instead.
         IF (H2U1() .NE. 0) THEN
            ST = -9
            RETURN
         END IF
         SLPRES = H2U1()
         IF (SLPRES .NE. 0) THEN
            N = 8
            IF (CHFMT .EQ. 3) N = 12
            DO 10 I = 1, N
               IF (H2U1() .NE. 0) THEN
                  CALL H2SCLL(I, 1, UD)
               ELSE
                  CALL H2FBA(I)
               END IF
   10       CONTINUE
         END IF
      END IF
      IF (SLPRES .EQ. 0) CALL H2SFLT(1)

      L2FNUM = H2UE() + 4
      POCTYP = H2UE()
      L2POC = 4
      IF (POCTYP .EQ. 0) THEN
         L2POC = H2UE() + 4
      ELSE IF (POCTYP .EQ. 1) THEN
         DUMMY = H2U1()
         DUMMY = H2SE()
         DUMMY = H2SE()
         CNT = H2UE()
         IF (CNT .GT. 255) THEN
            ST = -1
            RETURN
         END IF
         DO 20 I = 1, CNT
            DUMMY = H2SE()
   20    CONTINUE
      END IF
      NUMREF = H2UE()
      DUMMY = H2U1()
      MBW = H2UE() + 1
      MBH = H2UE() + 1
      FRMBO = H2U1()
      MBAFF = 0
      IF (FRMBO .EQ. 0) MBAFF = H2U1()
      D8INF = H2U1()
      CRPL = 0
      CRPR = 0
      CRPT = 0
      CRPB = 0
      IF (H2U1() .NE. 0) THEN
         CRPL = H2UE()
         CRPR = H2UE()
         CRPT = H2UE()
         CRPB = H2UE()
      END IF
C     Everything after this point in the SPS is VUI, which describes how
C     to display the picture rather than how to decode it.

      IF (BITERR .NE. 0) THEN
         ST = -2
         RETURN
      END IF
      IF (CHFMT .NE. 1 .OR. BITDL .NE. 8 .OR. BITDC .NE. 8) THEN
         ST = -3
         RETURN
      END IF
      IF (FRMBO .EQ. 0) THEN
         ST = -4
         RETURN
      END IF
C     MaxFrameNum and MaxPicOrderCntLsb, 7-1 and 7-2.  Both are wrapping
C     moduli rather than sizes, and both are needed by 8.2.1 and 8.2.4.1
C     on every picture, so they are computed once here.
      MXFNUM = ISHFT(1, L2FNUM)
      MXPOCL = ISHFT(1, L2POC)
      IF (NUMREF .GT. MXREF) THEN
C     More reference frames than we keep slots for.  Decoding it anyway
C     would mean predicting from whichever picture had not been evicted
C     yet, which produces a picture that is wrong in a way that looks
C     right; MXREF in h264com.inc says what raising this costs.
         ST = -8
         RETURN
      END IF
      MBH = MBH * (2 - FRMBO)
      IF (MBW .LT. 1 .OR. MBH .LT. 1) THEN
         ST = -5
         RETURN
      END IF
      PICW = MBW * 16
      PICH = MBH * 16
      IF (PICW .GT. MXW .OR. PICH .GT. MXH) THEN
         ST = -6
         RETURN
      END IF
      MBN = MBW * MBH
      IF (MBN .GT. MXMB) THEN
         ST = -6
         RETURN
      END IF
C     7-19..7-22.  For 4:2:0 the crop offsets count chroma samples
C     horizontally and chroma samples times (2 - frame_mbs_only_flag)
C     vertically, so they are doubled to reach luma.
      SUBW = 2
      SUBH = 2 * (2 - FRMBO)
      OUTW = PICW - SUBW * (CRPL + CRPR)
      OUTH = PICH - SUBH * (CRPT + CRPB)
      IF (OUTW .LT. 1 .OR. OUTH .LT. 1 .OR.
     +    OUTW .GT. PICW .OR. OUTH .GT. PICH) THEN
         ST = -7
         RETURN
      END IF
      CRPL = SUBW * CRPL
      CRPT = SUBH * CRPT
      SPSOK = 1
      RETURN
      END

C     The picture parameter set.
      SUBROUTINE H2PPSP(ST)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER ST
      INTEGER I, N, DUMMY, UD
      INTEGER H2U1, H2UN, H2UE, H2SE, H2MORE
      EXTERNAL H2U1, H2UN, H2UE, H2SE, H2MORE

      ST = 0
      IF (SPSOK .EQ. 0) THEN
         ST = -11
         RETURN
      END IF
      DUMMY = H2UE()
      DUMMY = H2UE()
      ECMODE = H2U1()
      BFPOC = H2U1()
      NSG = H2UE()
      NRDEF = H2UE() + 1
      NRDEF1 = H2UE() + 1
      WPRED = H2U1()
      WBIDC = H2UN(2)
      PIQP = H2SE() + 26
      DUMMY = H2SE()
      CQPO = H2SE()
      CQPO2 = CQPO
      DBLPRS = H2U1()
      CIPF = H2U1()
      RPCP = H2U1()
      TR8x8 = 0
      PLPRES = 0
      DO 10 I = 1, 6
         PLP4(I) = 0
   10 CONTINUE
      PLP8(1) = 0
      PLP8(2) = 0
      IF (H2MORE() .NE. 0) THEN
         TR8x8 = H2U1()
         PLPRES = H2U1()
         IF (PLPRES .NE. 0) THEN
            N = 6 + 2 * TR8x8
            IF (CHFMT .EQ. 3) N = 6 + 6 * TR8x8
C     An absent picture-level list needs no work here: H2WSCL resolves
C     it from these present flags, because fall-back rule B for some of
C     the lists reaches back into the sequence-level ones.
            DO 20 I = 1, N
               IF (H2U1() .NE. 0) THEN
                  CALL H2SCLL(I, 0, UD)
                  IF (I .LE. 6) THEN
                     PLP4(I) = 1
                  ELSE
                     PLP8(I - 6) = 1
                  END IF
               END IF
   20       CONTINUE
         END IF
         CQPO2 = H2SE()
      END IF

      IF (BITERR .NE. 0) THEN
         ST = -12
         RETURN
      END IF
      IF (NSG .NE. 0) THEN
         ST = -13
         RETURN
      END IF
      IF (PIQP .LT. 0 .OR. PIQP .GT. 51) THEN
         ST = -14
         RETURN
      END IF
      IF (NRDEF .LT. 1 .OR. NRDEF .GT. 32) THEN
         ST = -15
         RETURN
      END IF
      PPSOK = 1
      RETURN
      END

C     7.3.2.1.1.1, scaling_list().  WHICH is 1..6 for the six 4x4 lists
C     and 7..8 for the two 8x8 ones; INSPS says whether the result lands
C     in the sequence or the picture copy.  The list arrives in zig-zag
C     order and is stored in raster, because everything downstream
C     indexes it the way it indexes a coefficient block.
      SUBROUTINE H2SCLL(WHICH, INSPS, UD)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER WHICH, INSPS, UD
      INTEGER J, SIZE, LAST, NEXT, D, V, H2SE
      EXTERNAL H2SE
      SIZE = 16
      IF (WHICH .GT. 6) SIZE = 64
      UD = 0
      LAST = 8
      NEXT = 8
      DO 10 J = 0, SIZE - 1
         IF (NEXT .NE. 0) THEN
            D = H2SE()
            NEXT = MOD(LAST + D + 256, 256)
            IF (J .EQ. 0 .AND. NEXT .EQ. 0) UD = 1
         END IF
         IF (NEXT .EQ. 0) THEN
            V = LAST
         ELSE
            V = NEXT
         END IF
         LAST = V
         IF (WHICH .LE. 6) THEN
            IF (INSPS .NE. 0) THEN
               SL4(ZZ4(J) + 1, WHICH) = V
            ELSE
               PL4(ZZ4(J) + 1, WHICH) = V
            END IF
         ELSE
            IF (INSPS .NE. 0) THEN
               SL8(ZZ8(J) + 1, WHICH - 6) = V
            ELSE
               PL8(ZZ8(J) + 1, WHICH - 6) = V
            END IF
         END IF
   10 CONTINUE
      IF (UD .NE. 0) CALL H2SCLD(WHICH, INSPS)
      RETURN
      END

C     Tables 7-3 and 7-4, the default scaling lists, written in raster
C     order rather than the zig-zag the standard prints them in -- the
C     numbers are the same numbers, and this way they can be read as the
C     matrices they are.
      SUBROUTINE H2DFLT(WHICH, DST)
      IMPLICIT NONE
      INTEGER WHICH, DST(*)
      INTEGER J, D4I(16), D4P(16), D8I(64), D8P(64)
      DATA D4I / 6,13,20,28, 13,20,28,32, 20,28,32,37, 28,32,37,42/
      DATA D4P /10,14,20,24, 14,20,24,27, 20,24,27,30, 24,27,30,34/
      DATA D8I /
     +   6,10,13,16,18,23,25,27, 10,11,16,18,23,25,27,29,
     +  13,16,18,23,25,27,29,31, 16,18,23,25,27,29,31,33,
     +  18,23,25,27,29,31,33,36, 23,25,27,29,31,33,36,38,
     +  25,27,29,31,33,36,38,40, 27,29,31,33,36,38,40,42/
      DATA D8P /
     +   9,13,15,17,19,21,22,24, 13,13,17,19,21,22,24,25,
     +  15,17,19,21,22,24,25,27, 17,19,21,22,24,25,27,28,
     +  19,21,22,24,25,27,28,30, 21,22,24,25,27,28,30,32,
     +  22,24,25,27,28,30,32,33, 24,25,27,28,30,32,33,35/

      IF (WHICH .LE. 6) THEN
         DO 10 J = 1, 16
            IF (WHICH .LE. 3) THEN
               DST(J) = D4I(J)
            ELSE
               DST(J) = D4P(J)
            END IF
   10    CONTINUE
      ELSE
         DO 20 J = 1, 64
            IF (WHICH .EQ. 7) THEN
               DST(J) = D8I(J)
            ELSE
               DST(J) = D8P(J)
            END IF
   20    CONTINUE
      END IF
      RETURN
      END

C     The same defaults, into whichever of the two stored copies the
C     caller is filling in.
      SUBROUTINE H2SCLD(WHICH, INSPS)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER WHICH, INSPS
      IF (WHICH .LE. 6) THEN
         IF (INSPS .NE. 0) THEN
            CALL H2DFLT(WHICH, SL4(1, WHICH))
         ELSE
            CALL H2DFLT(WHICH, PL4(1, WHICH))
         END IF
      ELSE
         IF (INSPS .NE. 0) THEN
            CALL H2DFLT(WHICH, SL8(1, WHICH - 6))
         ELSE
            CALL H2DFLT(WHICH, PL8(1, WHICH - 6))
         END IF
      END IF
      RETURN
      END

C     Table 7-2, fall-back rule A: a sequence-level list that is absent
C     is either the standard default or a copy of the one before it.
C     The copy-the-previous cases are why this cannot be folded into
C     H2SCLD.
      SUBROUTINE H2FBA(WHICH)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER WHICH, J
      IF (WHICH .EQ. 1 .OR. WHICH .EQ. 4 .OR. WHICH .GE. 7) THEN
         CALL H2SCLD(WHICH, 1)
      ELSE
         DO 10 J = 1, 16
            SL4(J,WHICH) = SL4(J,WHICH-1)
   10    CONTINUE
      END IF
      RETURN
      END

C     Flat_4x4_16 and Flat_8x8_16: no weighting at all, which is what a
C     stream without scaling matrices means and what almost every
C     encoder on the web emits.
      SUBROUTINE H2SFLT(INSPS)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER INSPS, I, J
      DO 20 I = 1, 6
         DO 10 J = 1, 16
            IF (INSPS .NE. 0) THEN
               SL4(J,I) = 16
            ELSE
               PL4(J,I) = 16
            END IF
   10    CONTINUE
   20 CONTINUE
      DO 40 I = 1, 2
         DO 30 J = 1, 64
            IF (INSPS .NE. 0) THEN
               SL8(J,I) = 16
            ELSE
               PL8(J,I) = 16
            END IF
   30    CONTINUE
   40 CONTINUE
      RETURN
      END

C     Table 7-2, fall-back rule B: which list a picture-level list that
C     is absent inherits from.  Rule A has already been applied inside
C     the SPS.
      SUBROUTINE H2WSCL
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER I, J
      DO 20 I = 1, 6
         DO 10 J = 1, 16
            W4(J,I) = SL4(J,I)
   10    CONTINUE
   20 CONTINUE
      DO 40 I = 1, 2
         DO 30 J = 1, 64
            W8(J,I) = SL8(J,I)
   30    CONTINUE
   40 CONTINUE
      IF (PLPRES .EQ. 0) RETURN
C     The first list of each of the four groups falls back to the
C     sequence's copy when the sequence had one and to the standard
C     default when it did not -- and "did not" is not the same as "was
C     flat".  Leaving the flat 16s in place there is the difference
C     between a picture and a smear, because every encoder that sends
C     picture-level matrices at all sends most of them by omission.
      DO 60 I = 1, 6
         IF (PLP4(I) .NE. 0) THEN
            DO 50 J = 1, 16
               W4(J,I) = PL4(J,I)
   50       CONTINUE
         ELSE IF (I .EQ. 1 .OR. I .EQ. 4) THEN
            IF (SLPRES .EQ. 0) CALL H2DFLT(I, W4(1, I))
         ELSE
            DO 55 J = 1, 16
               W4(J,I) = W4(J,I-1)
   55       CONTINUE
         END IF
   60 CONTINUE
      DO 80 I = 1, 2
         IF (PLP8(I) .NE. 0) THEN
            DO 70 J = 1, 64
               W8(J,I) = PL8(J,I)
   70       CONTINUE
         ELSE IF (SLPRES .EQ. 0) THEN
            CALL H2DFLT(I + 6, W8(1, I))
         END IF
   80 CONTINUE
      RETURN
      END

C     7.3.3, the slice header, for I, P and B slices.
C
C     An SP or SI slice is rejected as soon as slice_type is known,
C     because everything after that point in the header is read
C     conditionally on the slice type and a header parsed under the wrong
C     assumption does not fail, it succeeds with the wrong numbers.
      SUBROUTINE H2SHDR(NALT, NALR, ST)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER NALT, NALR, ST
      INTEGER DUMMY, H2U1, H2UN, H2UE, H2SE
      EXTERNAL H2U1, H2UN, H2UE, H2SE

      ST = 0
      IF (SPSOK .EQ. 0 .OR. PPSOK .EQ. 0) THEN
         ST = -41
         RETURN
      END IF
      IDRF = 0
      IF (NALT .EQ. 5) IDRF = 1
      NALRI = NALR
      SLFMB = H2UE()
      SLTYPE = H2UE()
C     A slice_type of 5 or more says every slice of this picture has the
C     same type; the type itself is the low five.
      IF (SLTYPE .GE. 5) SLTYPE = SLTYPE - 5
      IF (SLTYPE .NE. 0 .AND. SLTYPE .NE. 1 .AND. SLTYPE .NE. 2) THEN
         ST = -43
         RETURN
      END IF
C     A B slice under CAVLC is refused rather than attempted.  The two
C     were written for different halves of the syntax and their overlap
C     is untested: CAVLC's mb_type table stops at the four P shapes,
C     sub_mb_type at the four P sub-shapes rather than B's thirteen, and
C     ref_idx is bounded by NREF0 with no list to select NREF1.  Every
C     one of those reads the wrong number of bits rather than failing,
C     so the whole slice would decode to plausible rubbish.  Baseline
C     has no B slices and Main and High streams that use them use CABAC,
C     so this refuses a combination that a browser does not meet.
      IF (SLTYPE .EQ. 1 .AND. ECMODE .EQ. 0) THEN
         ST = -56
         RETURN
      END IF
      DUMMY = H2UE()
      IF (SEPCP .NE. 0) DUMMY = H2UN(2)
      FRNUM = H2UN(L2FNUM)
C     frame_mbs_only_flag is 1 or the SPS was already rejected, so there
C     is no field_pic_flag to read here.
      IF (IDRF .NE. 0) DUMMY = H2UE()
      POCLSB = 0
      POCBOT = 0
      IF (POCTYP .EQ. 0) THEN
         POCLSB = H2UN(L2POC)
         IF (BFPOC .NE. 0) POCBOT = H2SE()
      ELSE IF (POCTYP .EQ. 1) THEN
C     delta_pic_order_always_zero_flag lives in the SPS and we did not
C     keep it, because pic_order_cnt_type 1 does not reach a browser:
C     x264, every hardware encoder and every muxer on the web emit type
C     0 or 2. Refusing is honest; guessing the field's presence is not.
         ST = -42
         RETURN
      END IF
      IF (RPCP .NE. 0) DUMMY = H2UE()

      NREF0 = 0
      NREF1 = 0
      NRMOP(1) = 0
      NRMOP(2) = 0
      DSMVP = 0
      IF (SLTYPE .EQ. 1) DSMVP = H2U1()
      IF (SLTYPE .EQ. 0 .OR. SLTYPE .EQ. 1) THEN
         NREF0 = NRDEF
         IF (SLTYPE .EQ. 1) NREF1 = NRDEF1
         IF (H2U1() .NE. 0) THEN
            NREF0 = H2UE() + 1
            IF (SLTYPE .EQ. 1) NREF1 = H2UE() + 1
         END IF
         CALL H2RPLM(1, ST)
         IF (ST .NE. 0) RETURN
         IF (SLTYPE .EQ. 1) THEN
            CALL H2RPLM(2, ST)
            IF (ST .NE. 0) RETURN
         END IF
C     7.3.3: the explicit table is present for a P slice when
C     weighted_pred_flag is set and for a B slice when
C     weighted_bipred_idc is exactly 1.  Value 2 is the implicit mode,
C     which carries no table at all and derives its weights per
C     partition from picture order counts in 8.4.2.3.1.
         IF ((SLTYPE .EQ. 0 .AND. WPRED .NE. 0) .OR.
     +       (SLTYPE .EQ. 1 .AND. WBIDC .EQ. 1)) THEN
            CALL H2PWT(ST)
            IF (ST .NE. 0) RETURN
         ELSE
            CALL H2DWT
         END IF
      ELSE
         CALL H2DWT
      END IF

      CALL H2DRPM(NALR, ST)
      IF (ST .NE. 0) RETURN

      CBIDC = 0
      CMODEL = 0
      IF (ECMODE .NE. 0 .AND. SLTYPE .NE. 2) THEN
         CBIDC = H2UE()
         IF (CBIDC .GT. 2) THEN
            ST = -48
            RETURN
         END IF
C     9.3.1.1: an I slice has one initialisation table and a P slice
C     picks between three.  Column 0 holds the first and columns 1 to 3
C     the others, so the column is the idc plus one.
         CMODEL = 1 + CBIDC
      END IF

      SLQPY = PIQP + H2SE()
      DBIDC = 0
      ALPHOF = 0
      BETAOF = 0
      IF (DBLPRS .NE. 0) THEN
         DBIDC = H2UE()
         IF (DBIDC .NE. 1) THEN
            ALPHOF = H2SE() * 2
            BETAOF = H2SE() * 2
         END IF
      END IF

      IF (BITERR .NE. 0) THEN
         ST = -44
         RETURN
      END IF
      IF (SLQPY .LT. 0 .OR. SLQPY .GT. 51) THEN
         ST = -45
         RETURN
      END IF
      IF (SLFMB .GE. MBN) THEN
         ST = -46
         RETURN
      END IF
      IF (DBIDC .GT. 2) THEN
         ST = -47
         RETURN
      END IF
      IF (SLTYPE .NE. 2 .AND. (NREF0 .LT. 1 .OR. NREF0 .GT. 32)) THEN
         ST = -49
         RETURN
      END IF
      IF (SLTYPE .EQ. 1 .AND. (NREF1 .LT. 1 .OR. NREF1 .GT. 32)) THEN
         ST = -49
         RETURN
      END IF
C     8.4.1.2.3 reads the colocated macroblock's motion at the corner of
C     each 8x8, which is only the same thing as its per-4x4 motion when
C     direct_8x8_inference_flag is set.  Every encoder that reaches a
C     browser sets it -- x264 emits 1 unconditionally, and levels 3 and
C     above require it -- so the zero case has no test vector we could
C     hold ourselves to, and shipping an untested derivation in the
C     fiddliest corner of the standard is worse than saying no.  Spatial
C     direct does not care: its reference indices are macroblock-level.
      IF (SLTYPE .EQ. 1 .AND. DSMVP .EQ. 0 .AND. D8INF .EQ. 0) THEN
         ST = -55
         RETURN
      END IF
      RETURN
      END

C     7.3.3.1, ref_pic_list_modification.  The commands are recorded and
C     not obeyed: 8.2.4.3 runs them against a list that does not exist
C     until the whole header has been read.
      SUBROUTINE H2RPLM(L, ST)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER L, ST, IDC, N, H2U1, H2UE
      EXTERNAL H2U1, H2UE
      ST = 0
      NRMOP(L) = 0
      IF (H2U1() .EQ. 0) RETURN
      N = 0
   10 IDC = H2UE()
      IF (IDC .EQ. 3 .OR. BITERR .NE. 0) RETURN
      IF (IDC .EQ. 2) THEN
C     A long-term picture, which we never mark and therefore never have.
         ST = -50
         RETURN
      END IF
      IF (IDC .GT. 3) THEN
         ST = -50
         RETURN
      END IF
      N = N + 1
      IF (N .GT. 64) THEN
         ST = -50
         RETURN
      END IF
      RMOP(N,L) = IDC
      RMVAL(N,L) = H2UE() + 1
      NRMOP(L) = N
      GOTO 10
      END

C     Table 8-2's default weights: a scale of one and no offset, which is
C     what a slice without a prediction weight table means and what every
C     reference index the table skips means.
      SUBROUTINE H2DWT
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER I, L
      LOGWL = 0
      LOGWC = 0
      DO 20 L = 1, 2
         DO 10 I = 0, 31
            WPL(I,L) = 1
            WOL(I,L) = 0
            WPCB(I,L) = 1
            WOCB(I,L) = 0
            WPCR(I,L) = 1
            WOCR(I,L) = 0
   10    CONTINUE
   20 CONTINUE
      RETURN
      END

C     7.3.3.2, pred_weight_table.  x264 turns weighted P prediction on at
C     every preset above ultrafast, so this is the common case and not an
C     exotic one; a decoder that ignored the table would fade every frame
C     of a stream that used it.
      SUBROUTINE H2PWT(ST)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER ST, I, L, H2UE
      EXTERNAL H2UE
      ST = 0
      LOGWL = H2UE()
      LOGWC = H2UE()
      IF (LOGWL .GT. 7 .OR. LOGWC .GT. 7) THEN
         ST = -50
         RETURN
      END IF
C     Every index the table does not mention keeps the neutral weight of
C     Table 8-2, which at this denominator is 1 << log2_denom and not 1.
      DO 20 L = 1, 2
         DO 10 I = 0, 31
            WPL(I,L) = ISHFT(1, LOGWL)
            WOL(I,L) = 0
            WPCB(I,L) = ISHFT(1, LOGWC)
            WOCB(I,L) = 0
            WPCR(I,L) = ISHFT(1, LOGWC)
            WOCR(I,L) = 0
   10    CONTINUE
   20 CONTINUE
      CALL H2PWTL(1, NREF0, ST)
      IF (ST .NE. 0) RETURN
      IF (SLTYPE .EQ. 1) CALL H2PWTL(2, NREF1, ST)
      RETURN
      END

C     The body of 7.3.3.2, split out because a B slice runs it twice.
      SUBROUTINE H2PWTL(L, NL, ST)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER L, NL, ST, I, H2U1, H2SE
      EXTERNAL H2U1, H2SE
      ST = 0
      DO 20 I = 0, NL - 1
         IF (I .GT. 31 .OR. BITERR .NE. 0) THEN
            ST = -50
            RETURN
         END IF
         IF (H2U1() .NE. 0) THEN
            WPL(I,L) = H2SE()
            WOL(I,L) = H2SE()
         END IF
         IF (H2U1() .NE. 0) THEN
            WPCB(I,L) = H2SE()
            WOCB(I,L) = H2SE()
            WPCR(I,L) = H2SE()
            WOCR(I,L) = H2SE()
         END IF
   20 CONTINUE
      RETURN
      END

C     7.3.3.3, dec_ref_pic_marking.  Long-term references are refused
C     here by name.  They are not rare because they are hard; they are
C     rare because no encoder aimed at streaming emits them, and
C     supporting them would mean a second ordering rule in 8.2.4.1, a
C     second numbering in 8.2.4.2 and four more marking operations, for
C     content that does not exist on the web.
      SUBROUTINE H2DRPM(NALR, ST)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER NALR, ST, OP, N, DUMMY, H2U1, H2UE
      EXTERNAL H2U1, H2UE
      ST = 0
      ADAPTF = 0
      NAMOP = 0
      IF (NALR .EQ. 0) RETURN
      IF (IDRF .NE. 0) THEN
         DUMMY = H2U1()
         IF (H2U1() .NE. 0) THEN
            ST = -50
            RETURN
         END IF
         RETURN
      END IF
      ADAPTF = H2U1()
      IF (ADAPTF .EQ. 0) RETURN
      N = 0
   10 OP = H2UE()
      IF (OP .EQ. 0 .OR. BITERR .NE. 0) RETURN
      IF (OP .EQ. 2 .OR. OP .EQ. 3 .OR. OP .EQ. 4 .OR. OP .EQ. 6) THEN
         ST = -50
         RETURN
      END IF
      IF (OP .GT. 6) THEN
         ST = -50
         RETURN
      END IF
      N = N + 1
      IF (N .GT. 64) THEN
         ST = -50
         RETURN
      END IF
      AMOP(N) = OP
      AMV(N) = 0
      IF (OP .EQ. 1) AMV(N) = H2UE() + 1
      NAMOP = N
      GOTO 10
      END
