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
      INTEGER MXW, MXH, MXMB, MXBUF, MXREF
      PARAMETER (MXW = 1920, MXH = 1088)
      PARAMETER (MXMB = 8160)
      PARAMETER (MXBUF = 4194304)

C     How many reference frames we keep.  The standard allows sixteen;
C     dimensioning for sixteen would cost 50 MB of planes to serve a
C     stream nobody sends.  Four covers x264's default (--ref 3), every
C     hardware encoder we have seen (one or two), and the "slow" preset's
C     four; a sequence parameter set asking for more is refused by name
C     rather than decoded against the wrong picture.  Raising it is this
C     one number -- the cost is 3.1 MB of planes per frame.
      PARAMETER (MXREF = 4)

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
      INTEGER NUMREF, NRDEF, WPRED, MXFNUM, MXPOCL
      COMMON /H2CFG/ SPSOK, PPSOK, CHFMT, BITDL, BITDC, L2FNUM,
     +               POCTYP, L2POC, MBW, MBH, MBN, FRMBO, MBAFF,
     +               PICW, PICH, OUTW, OUTH, CRPL, CRPR, CRPT, CRPB,
     +               ECMODE, NSG, PIQP, CQPO, CQPO2, DBLPRS, CIPF,
     +               RPCP, TR8x8, BFPOC, SEPCP,
     +               NUMREF, NRDEF, WPRED, MXFNUM, MXPOCL

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
C     The fields after IDRF belong to a P slice: which of the four CABAC
C     initialisation columns it asked for, how many entries its reference
C     list has, and the frame number and picture order count that place
C     the picture among the ones already decoded.
      INTEGER SLQPY, SLTYPE, SLFMB, SLID, DBIDC, ALPHOF, BETAOF, IDRF
      INTEGER CBIDC, CMODEL, NREF0, FRNUM, POCLSB, POCBOT, NALRI
      COMMON /H2SLC/ SLQPY, SLTYPE, SLFMB, SLID, DBIDC, ALPHOF,
     +               BETAOF, IDRF, CBIDC, CMODEL, NREF0, FRNUM,
     +               POCLSB, POCBOT, NALRI

C     -- the decoded picture buffer ------------------------------------
C     The reference frames, and everything 8.2.4 needs to order them.
C     One byte per sample and not one INTEGER: four frames of INTEGER
C     planes would be 50 MB of BSS that is resident the moment the second
C     picture is decoded, where four frames of bytes are 12.5 MB.  The
C     mask on every read costs an AND in the interpolator's inner loop
C     and buys back three quarters of the decoder's memory.
C
C     DPUSE says a slot holds a picture marked "used for short-term
C     reference"; DPFN is its frame_num, DPFNW the FrameNumWrap of 8.2.4.1
C     recomputed for the current picture, DPPOC its picture order count,
C     and DPID a number that is never reused.  The deblocking filter
C     compares DPID and not the reference index, because 8.7.2.1 asks
C     whether two blocks used the same picture and two slices of one
C     picture can reach the same picture through different indices.
      INTEGER*1 DPY, DPU, DPV
      INTEGER DPUSE, DPFN, DPFNW, DPPOC, DPID, NXTID
      INTEGER PRVMSB, PRVLSB, PRVFNO, PRVFN, CURPOC, CURID
      INTEGER RL0, RL0N
      COMMON /H2DPB/ DPY(MXW*MXH, MXREF), DPU(MXW*MXH/4, MXREF),
     +               DPV(MXW*MXH/4, MXREF),
     +               DPUSE(MXREF), DPFN(MXREF), DPFNW(MXREF),
     +               DPPOC(MXREF), DPID(MXREF), NXTID,
     +               PRVMSB, PRVLSB, PRVFNO, PRVFN, CURPOC, CURID,
     +               RL0(0:31), RL0N

C     The two lists of commands a slice header can carry: 7.3.3.1's
C     reference list modification and 7.3.3.3's decoded reference picture
C     marking.  Both are parsed where they appear in the bitstream and
C     acted on later -- the first after the whole header is read, the
C     second after the whole picture is decoded -- so they are kept
C     rather than executed as they arrive.
      INTEGER NRMOP, RMOP, RMVAL, NAMOP, AMOP, AMV, ADAPTF
      COMMON /H2RPM/ NRMOP, RMOP(64), RMVAL(64),
     +               NAMOP, AMOP(64), AMV(64), ADAPTF

C     8.4.2.3's explicit weights, one set per reference index.  Weighted
C     prediction is not an exotic corner: x264 turns it on by default for
C     P slices, so a decoder that refuses it refuses most of the web.
      INTEGER WPL, WOL, WPCB, WOCB, WPCR, WOCR, LOGWL, LOGWC
      COMMON /H2WGT/ WPL(0:31), WOL(0:31), WPCB(0:31), WOCB(0:31),
     +               WPCR(0:31), WOCR(0:31), LOGWL, LOGWC

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

C     -- per-macroblock motion -----------------------------------------
C     The motion vectors of the picture being decoded, kept because three
C     separate things ask for them: 8.4.1.3's predictor reads the
C     neighbouring partitions, 9.3.3.1.1.7's context reads the
C     neighbouring mvd magnitudes, and 8.7.2.1's boundary strength reads
C     both sides of every edge.  All four are indexed by 4x4 block in
C     raster order within the macroblock (bx + 4*by), which is not the
C     z-order the residual uses -- motion is asked about by position and
C     coefficients by block number, and converting in one place beats
C     converting at every question.
C
C     MRPI holds the identity of the picture a partition pointed at, not
C     its index in this slice's list; MINT and MSKP say whether the
C     macroblock was intra and whether it was skipped, which is what the
C     skip flag's own context asks about its neighbours.
      INTEGER MMVX, MMVY, MMDX, MMDY, MREF, MRPI, MINT, MSKP
      COMMON /H2MOT/ MMVX(16,MXMB), MMVY(16,MXMB),
     +               MMDX(16,MXMB), MMDY(16,MXMB),
     +               MREF(4,MXMB), MRPI(4,MXMB),
     +               MINT(MXMB), MSKP(MXMB)

C     -- the macroblock being decoded ----------------------------------
C     COEF holds dequantised 4x4 coefficients for the 24 blocks (16
C     luma, 4 Cb, 4 Cr) in raster order within each block; CO8 the four
C     8x8 luma blocks; DCY and DCC the DC arrays that 8.5.10 and 8.5.11
C     produce.
C     IAVA..IAVD are the same four neighbours seen through
C     constrained_intra_pred_flag: an intra macroblock in a P slice may
C     not predict from an inter neighbour when the flag is set, while
C     every CABAC context still asks about plain availability.  In an I
C     picture the two sets are equal, which is why nothing intra-only
C     changed when they were split.
C     CMVOK marks which of the sixteen 4x4 blocks already have their
C     motion, because a partition's neighbour C often lies in a partition
C     of this same macroblock that has not been decoded yet, and
C     6.4.11.7 calls that not available rather than calling it zero.
      INTEGER CMBA, CMBX, CMBY
      INTEGER AVLA, AVLB, AVLC, AVLD, ADRA, ADRB, ADRC, ADRD
      INTEGER IAVA, IAVB, IAVC, IAVD
      INTEGER QPY, QPCB, QPCR, QPLAST, DQLAST
      INTEGER CBPL, CBPC, T8FLG, CI16, CPCM, CPRED, CCPM
      INTEGER CI4, CNZ, CDCF
      INTEGER COEF, CO8, DCY, DCC
      INTEGER CINTR, CSKP, CPTYP, CSUB, CMVX, CMVY, CMDX, CMDY
      INTEGER CREF, CRPI, CMVOK
      COMMON /H2CUR/ CMBA, CMBX, CMBY,
     +               AVLA, AVLB, AVLC, AVLD, ADRA, ADRB, ADRC, ADRD,
     +               IAVA, IAVB, IAVC, IAVD,
     +               QPY, QPCB, QPCR, QPLAST, DQLAST,
     +               CBPL, CBPC, T8FLG, CI16, CPCM, CPRED, CCPM,
     +               CI4(16), CNZ(24), CDCF(3),
     +               COEF(16,24), CO8(64,4), DCY(16), DCC(4,2),
     +               CINTR, CSKP, CPTYP, CSUB(4),
     +               CMVX(16), CMVY(16), CMDX(16), CMDY(16),
     +               CREF(4), CRPI(4), CMVOK(16)

C     -- constant tables -----------------------------------------------
C     Filled once by H2INIT.  They are COMMON rather than
C     DATA-in-each-routine so the numbers exist in exactly one place; a
C     table of 2048 numbers typed twice is a table typed wrong once.
C     CTXM/CTXN carry four columns, not one: column 0 is the I slice's
C     initialisation values and columns 1 to 3 the three a P or B slice
C     chooses between with cabac_init_idc.  The column is picked once per
C     slice by H2CINI.
      INTEGER RLPS, TLPS, TMPS, CTXM, CTXN
      COMMON /H2TAB/ RLPS(0:63,0:3), TLPS(0:63), TMPS(0:63),
     +               CTXM(0:1023,0:3), CTXN(0:1023,0:3)

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
