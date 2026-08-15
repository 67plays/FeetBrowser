C     Shared state for the H.264 decoder, INCLUDEd by every source file.
C
C     FORTRAN 77 has no structures, no allocation and no modules, so the
C     decoder's state is COMMON and its buffers are dimensioned for the
C     largest picture we accept.  That is not a limitation we are
C     working around; it is the reason a decoder written this way cannot
C     be made to allocate a gigabyte because a stranger's SPS said 65535
C     macroblocks. The size cap below is checked in H2SPS before a
C     single sample is written, and every array here is bounded by it.
C
C     One include rather than a per-file copy, because a COMMON block
C     whose declaration drifts between two files does not fail to
C     compile: it fails to decode, silently, in whichever routine got
C     the shorter array.

C     Largest picture we decode: 1920x1088 luma, which is 1080p rounded
C     up to whole macroblocks.  8160 macroblocks.
      INTEGER MXW, MXH, MXMB, MXBUF
      PARAMETER (MXW = 1920, MXH = 1088)
      PARAMETER (MXMB = 8160)
      PARAMETER (MXBUF = 4194304)

C     -- the coded bitstream -------------------------------------------
C     RBSP holds one NAL unit with its emulation-prevention bytes
C     already removed.  BITP is a bit cursor, zero-based, and BITN the
C     number of bits in the buffer; every read checks one against the
C     other and sets BITERR rather than walking off the end, because the
C     buffer came from the network.
      INTEGER*1 RBSP
      INTEGER BITP, BITN, BITERR
      COMMON /H2BIT/ RBSP(MXBUF), BITP, BITN, BITERR

C     -- sequence and picture parameter sets ---------------------------
C     Only the fields an I-frame decoder consults.  A field we parse but
C     do not use is still parsed, because the bits after it are only
C     findable by reading through it.
      INTEGER SPSOK, PPSOK, CHFMT, BITDL, BITDC, L2FNUM, POCTYP, L2POC
      INTEGER MBW, MBH, MBN, FRMBO, MBAFF
      INTEGER PICW, PICH, OUTW, OUTH
      INTEGER CRPL, CRPR, CRPT, CRPB
      INTEGER ECMODE, NSG, PIQP, CQPO, CQPO2
      INTEGER DBLPRS, CIPF, RPCP, TR8x8, BFPOC, SEPCP
      COMMON /H2CFG/ SPSOK, PPSOK, CHFMT, BITDL, BITDC, L2FNUM,
     +               POCTYP, L2POC, MBW, MBH, MBN, FRMBO, MBAFF,
     +               PICW, PICH, OUTW, OUTH, CRPL, CRPR, CRPT, CRPB,
     +               ECMODE, NSG, PIQP, CQPO, CQPO2, DBLPRS, CIPF,
     +               RPCP, TR8x8, BFPOC, SEPCP

C     Weighting matrices.  W4/W8 are the lists actually in force for the
C     current picture; the SPS and PPS copies are kept apart because the
C     fallback rules in 7.4.2.1.1.1 read one when the other is absent.
C     Both are held in raster order, not the zig-zag they arrive in.
      INTEGER W4, W8, SL4, SL8, PL4, PL8, SLPRES, PLPRES, PLP4, PLP8
      COMMON /H2SCL/ W4(16,6), W8(64,2),
     +               SL4(16,6), SL8(64,2), SLPRES,
     +               PL4(16,6), PL8(64,2), PLPRES, PLP4(6), PLP8(2)

C     -- the current slice ---------------------------------------------
C     SLID counts slices within the picture and is written into MSLC for
C     every macroblock, which is how availability is answered: a
C     neighbour is available exactly when it carries the slice number we
C     are in.
      INTEGER SLQPY, SLTYPE, SLFMB, SLID, DBIDC, ALPHOF, BETAOF, IDRF
      COMMON /H2SLC/ SLQPY, SLTYPE, SLFMB, SLID, DBIDC, ALPHOF,
     +               BETAOF, IDRF

C     -- the arithmetic decoder ----------------------------------------
C     codIRange and codIOffset of 9.3.3.2, and the 1024 context
C     variables of 9.3.1.1 as two parallel arrays rather than one packed
C     byte, because the packing buys nothing when the array is already
C     in cache.
      INTEGER CST, CMP, CRNG, COFF
      COMMON /H2CAB/ CST(0:1023), CMP(0:1023), CRNG, COFF

C     -- the decoded picture -------------------------------------------
C     Full planes, uncropped, one INTEGER per sample.  A byte array
C     would halve the memory and cost a mask on every read; at 12 MB of
C     BSS that is not a trade worth making, and BSS is not resident
C     until written.
      INTEGER PY, PU, PV
      COMMON /H2PIC/ PY(MXW*MXH), PU(MXW*MXH/4), PV(MXW*MXH/4)

C     -- per-macroblock records ----------------------------------------
C     Everything a later macroblock, or the deblocking filter, has to
C     know about an earlier one.  MI4 holds Intra4x4PredMode per 4x4
C     block (and the 8x8 mode replicated over its four blocks, which is
C     what 8.3.1.1 asks for); MNZ the non-zero coefficient count per 4x4
C     block, which is both the CABAC coded_block_flag context and, in
C     CAVLC, nC.
C
C     The three deblocking fields are per-macroblock and not per-slice
C     even though the syntax puts them in the slice header, because the
C     filter is a whole-picture pass that runs after the last slice is
C     decoded and has to use each macroblock's own slice's settings.
      INTEGER MTYP, MQPY, MCBP, MT8, MSLC, MI16, MPCM, MCPM
      INTEGER MI4, MNZ, MDCF, MDBI, MALP, MBET
      COMMON /H2MB/ MTYP(MXMB), MQPY(MXMB), MCBP(MXMB), MT8(MXMB),
     +              MSLC(MXMB), MI16(MXMB), MPCM(MXMB), MCPM(MXMB),
     +              MDBI(MXMB), MALP(MXMB), MBET(MXMB),
     +              MI4(16,MXMB), MNZ(24,MXMB), MDCF(3,MXMB)

C     -- the macroblock being decoded ----------------------------------
C     COEF holds dequantised 4x4 coefficients for the 24 blocks (16
C     luma, 4 Cb, 4 Cr) in raster order within each block; CO8 the four
C     8x8 luma blocks; DCY and DCC the DC arrays that 8.5.10 and 8.5.11
C     produce.
      INTEGER CMBA, CMBX, CMBY
      INTEGER AVLA, AVLB, AVLC, AVLD, ADRA, ADRB, ADRC, ADRD
      INTEGER QPY, QPCB, QPCR, QPLAST, DQLAST
      INTEGER CBPL, CBPC, T8FLG, CI16, CPCM, CPRED, CCPM
      INTEGER CI4, CNZ, CDCF
      INTEGER COEF, CO8, DCY, DCC
      COMMON /H2CUR/ CMBA, CMBX, CMBY,
     +               AVLA, AVLB, AVLC, AVLD, ADRA, ADRB, ADRC, ADRD,
     +               QPY, QPCB, QPCR, QPLAST, DQLAST,
     +               CBPL, CBPC, T8FLG, CI16, CPCM, CPRED, CCPM,
     +               CI4(16), CNZ(24), CDCF(3),
     +               COEF(16,24), CO8(64,4), DCY(16), DCC(4,2)

C     -- constant tables -----------------------------------------------
C     Filled once by H2INIT.  They are COMMON rather than
C     DATA-in-each-routine so the numbers exist in exactly one place; a
C     table of 2048 numbers typed twice is a table typed wrong once.
      INTEGER RLPS, TLPS, TMPS, CTXM, CTXN
      COMMON /H2TAB/ RLPS(0:63,0:3), TLPS(0:63), TMPS(0:63),
     +               CTXM(0:1023), CTXN(0:1023)

      INTEGER ZZ4, ZZ8, BLKX, BLKY, ZORD, SIG8, LST8, NADJ4, NADJ8
      INTEGER CHQP, ALPHT, BETAT, TC0T
      COMMON /H2TB2/ ZZ4(0:15), ZZ8(0:63), BLKX(0:15), BLKY(0:15),
     +               ZORD(0:3,0:3), SIG8(0:62), LST8(0:62),
     +               NADJ4(0:5,0:15), NADJ8(0:5,0:63),
     +               CHQP(0:51), ALPHT(0:51), BETAT(0:51), TC0T(0:51,3)

C     Neighbour sample scratch shared by the intra predictors: the row
C     above a block, the column to its left, and the corner between
C     them.  Both arrays start at -1 and both hold the corner sample
C     there, because half the directional modes of 8.3.1.2 index off the
C     near end of one array and expect to find the corner; giving it an
C     index in both is cheaper than testing for it nine times.
      INTEGER PT, PL, PTOK, PLOK, PDOK, PTROK
      COMMON /H2NBS/ PT(-1:15), PL(-1:15), PTOK, PLOK, PDOK, PTROK
