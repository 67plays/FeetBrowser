C     Shared state for the MP3 decoder, INCLUDEd by every source file.
C
C     FORTRAN 77 has no structures, no allocation and no modules, so the
C     decoder's state is COMMON and its buffers are dimensioned for the
C     largest frame the standards allow.  As in the two decoders next
C     door that is the point rather than a workaround: a stranger's frame
C     header cannot make this decoder allocate anything, because it never
C     allocates.  Every limit below is checked before a sample is
C     written.
C
C     One include rather than a copy per file, because a COMMON block
C     whose declaration drifts between two files does not fail to
C     compile.  It fails to decode, quietly, in whichever routine got the
C     shorter array.
C
C     The names all start with BL, and the blocks with /BL, so that
C     nothing here can collide with the H.264 decoder's H2 or the AAC
C     decoder's IP.  All three end up in one process, Fortran has one
C     global namespace, and COMMON is matched by name and not by type: a
C     collision would link cleanly and corrupt memory at run time.

C     -- what we accept ---------------------------------------------------
C     One coded frame, in bytes.  The largest Layer III frame any of the
C     three standards can describe is 144000 * 320 / 32000 + 1 = 1441
C     bytes (MPEG-1 at 320 kbit/s and 32 kHz); MPEG-2 and MPEG-2.5 reach
C     the same 1441 from the other direction, 72000 * 160 / 8000 + 1.
C     2048 is that rounded up, and anything longer is refused rather than
C     truncated.
      INTEGER MXFRM, MXRES, MXCH, MXGR, MXSB, MXSMP, MXPCM
      INTEGER MXSFL, MXSFS, MXNODE, MXQNT, MXTBL
      PARAMETER (MXFRM = 2048)
C     The bit reservoir.  main_data_begin is nine bits for MPEG-1 and
C     eight for MPEG-2, so a granule can reach at most 511 bytes back
C     past the start of its own frame's main data; 511 carried over plus
C     one whole frame is the most that is ever addressable at once.
      PARAMETER (MXRES = 4096)
C     Mono and stereo.  A Layer III frame header cannot describe more --
C     the mode field is two bits and every one of its values is one or
C     two channels -- but MP3 carried inside MP4 can claim up to five,
C     and that is refused by name rather than decoded as the first two.
      PARAMETER (MXCH = 2)
C     Granules per frame: two for MPEG-1, one for MPEG-2 and MPEG-2.5.
      PARAMETER (MXGR = 2)
      PARAMETER (MXSB = 32, MXSMP = 576)
C     Interleaved output: two granules of 576 samples, two channels.
      PARAMETER (MXPCM = 2 * MXSMP * MXCH)
C     Scalefactor bands: 21 long bands and a closing offset, 12 short
C     bands and a closing offset.  The tables in the standard carry 22
C     and 13 boundaries because band 21 (and short band 12) exists in the
C     table and is never coded; keeping it is what makes the closing
C     offset of band 20 readable without a special case.
      PARAMETER (MXSFL = 23, MXSFS = 14)
C     Huffman tree nodes for the sixteen distinct big-value code tables
C     and the two count1 tables.  1378 + 32 leaves, so at most 2819
C     nodes; the room above that is for a table whose Kraft sum is short
C     of one, where the tree carries internal nodes no codeword reaches.
      PARAMETER (MXNODE = 8192)
C     The largest quantised magnitude a big-value pair can carry: 15 from
C     the code table plus the largest linbits escape, 2^13 - 1.
      PARAMETER (MXQNT = 8206)
      PARAMETER (MXTBL = 32)

C     -- the coded frame --------------------------------------------------
C     FBUF holds one frame, header included.  HPOS is a bit cursor over
C     it, zero based, used only for the header, the CRC and the side
C     information: those are fixed width fields at the front of the frame
C     and are read in place, before the main data is moved into the
C     reservoir.  HERR is set by any read that would pass the end.
      INTEGER*1 FBUF
      INTEGER FN, HPOS, HERR
      COMMON /BLFBF/ FBUF(MXFRM), FN, HPOS, HERR

C     -- the bit reservoir ------------------------------------------------
C     This is the part of Layer III that has no counterpart in the other
C     two decoders in this tree, and the part that is got wrong most
C     often.  A frame's main data does not begin at that frame's own
C     header: main_data_begin says how many bytes *before* the end of
C     everything received so far the granule's data starts, so a frame
C     can be describing bits that arrived one, two or three frames ago.
C     RBUF is therefore a running buffer of main data with the headers
C     and side information taken out, RN the bytes in it, and BPOS an
C     absolute bit cursor into it rather than into any one frame.
C
C     BNBIT is the bits available and BERR is set by a read past them.
C     Nothing here walks off the buffer: the bytes came off the network.
      INTEGER*1 RBUF
      INTEGER RN, BPOS, BNBIT, BERR
      COMMON /BLRES/ RBUF(MXRES), RN, BPOS, BNBIT, BERR

C     -- the frame header -------------------------------------------------
C     HVER is 3 for MPEG-1, 2 for MPEG-2 and 0 for MPEG-2.5, which is the
C     two-bit field's own encoding; HLSF is 1 for the last two, which
C     between them differ from MPEG-1 in the granule count, the side
C     information layout, the scalefactor coding and the intensity
C     stereo derivation.  HSFI is 0..8, the index into the nine
C     scalefactor band layouts: 0..2 are MPEG-1 at 44100, 48000, 32000,
C     3..5 MPEG-2 at 22050, 24000, 16000, 6..8 MPEG-2.5 at 11025, 12000,
C     8000.
      INTEGER HVER, HLAYER, HPROT, HBRIDX, HSRIDX, HPAD, HPRIV
      INTEGER HMODE, HMODX, HCOPY, HORIG, HEMPH
      INTEGER HLSF, HSFI, HRATE, HKBPS, HFLEN, HNCH, HNGR, HSIDE
      INTEGER HMS, HIS
      COMMON /BLHDR/ HVER, HLAYER, HPROT, HBRIDX, HSRIDX, HPAD, HPRIV,
     +               HMODE, HMODX, HCOPY, HORIG, HEMPH,
     +               HLSF, HSFI, HRATE, HKBPS, HFLEN, HNCH, HNGR,
     +               HSIDE, HMS, HIS

C     -- the side information ---------------------------------------------
C     Everything the frame says about its granules before any main data
C     is read.  MDBEG is main_data_begin and SCFSI the scalefactor
C     selection information, which MPEG-2 does not have.  The rest is per
C     granule and per channel; the arrays are dimensioned for two
C     granules whatever the version, and MPEG-2 uses granule one.
      INTEGER MDBEG, SCFSI, P23LEN, BIGVAL, GGAIN, SCFCMP, WSWF
      INTEGER BLKTYP, MIXBLK, TBLSEL, SBGAIN, REG0, REG1
      INTEGER PREFLG, SFSCAL, CNT1TS
      COMMON /BLSID/ MDBEG, SCFSI(0:3,MXCH),
     +               P23LEN(MXGR,MXCH), BIGVAL(MXGR,MXCH),
     +               GGAIN(MXGR,MXCH), SCFCMP(MXGR,MXCH),
     +               WSWF(MXGR,MXCH), BLKTYP(MXGR,MXCH),
     +               MIXBLK(MXGR,MXCH), TBLSEL(3,MXGR,MXCH),
     +               SBGAIN(3,MXGR,MXCH), REG0(MXGR,MXCH),
     +               REG1(MXGR,MXCH), PREFLG(MXGR,MXCH),
     +               SFSCAL(MXGR,MXCH), CNT1TS(MXGR,MXCH)

C     -- the granule being decoded ----------------------------------------
C     SCF holds the granule's scalefactors, flat: the long bands 0 to
C     LONGE-1 first, then short band SHRTS upwards three windows at a
C     time.  One array rather than two because a mixed block has both
C     kinds at once and the bitstream sends them in exactly this order,
C     and because the two versions' very different scalefactor coding
C     then differ only in how the array is filled and not at all in how
C     it is read.
C
C     It is also where intensity stereo reads its positions from, because
C     in a joint stereo frame the right channel's scalefactors are not
C     scalefactors at all -- they are the angle the left channel's energy
C     is to be panned to, coded in the same field.  That sharing is the
C     standard's, not ours.
C
C     SCFP is the same array as it was in granule one, kept because
C     MPEG-1's scfsi lets granule two say "the same as last time" for a
C     group of bands rather than resend them.  ISCALE is MPEG-2's
C     intensity_scale, the low bit of a right channel's
C     scalefac_compress, which picks between the two ratios the low
C     sampling frequency extension pans with.
      INTEGER SCF, SCFP, LONGE, SHRTS, ISCALE, RZERO
      COMMON /BLSF/ SCF(0:39,MXCH), SCFP(0:39,MXCH), LONGE(MXCH),
     +              SHRTS(MXCH), ISCALE(MXCH), RZERO(MXCH)

C     -- the spectrum -----------------------------------------------------
C     IS is what the Huffman decoder produced, XRQ the same values
C     requantised and nothing else, and XR the working spectrum that the
C     stereo tools, the alias butterflies and the transform run over.
C     XRQ is kept separately at the cost of nine kilobytes because it is
C     the one stage of this decoder a test can recompute exactly, from
C     the decoder's own quantised values and scalefactors, and compare to
C     the last bit.
      INTEGER IS
      DOUBLE PRECISION XRQ, XR
      COMMON /BLSPC/ IS(0:MXSMP-1,MXCH), XRQ(0:MXSMP-1,MXCH),
     +               XR(0:MXSMP-1,MXCH)

C     -- output -----------------------------------------------------------
C     OVER is the second half of the last granule's inverse transform,
C     eighteen values per subband, waiting to be added to the first half
C     of the next granule's.  VBUF is the polyphase synthesis filter's
C     own FIFO -- sixteen shifts of 64 values, the standard's V vector --
C     and VPOS where the next shift goes.  Between them they are the
C     reason an MP3 decoder cannot be handed a frame from the middle of a
C     stream and produce the right sound: there is no key frame in Layer
C     III, only carried state.
C
C     PCMO holds the frame's samples, interleaved, NOUT how many there
C     are per channel and NOUTCH how many channels.
      DOUBLE PRECISION OVER, VBUF, PCMO
      INTEGER VPOS, NOUT, NOUTCH
      COMMON /BLOUT/ OVER(0:MXSMP-1,MXCH), VBUF(0:1023,MXCH),
     +               PCMO(0:MXPCM-1), VPOS(MXCH), NOUT, NOUTCH

C     -- the tables -------------------------------------------------------
C     The Huffman code tables as one pool of tree nodes: HTL and HTR are
C     the two children, zero where there is none, and HTV is the entry
C     number at a leaf and -1 above it.  HROOT(t) is code table t's root;
C     0..15 are the sixteen distinct big-value trees, 16 and 17 the two
C     count1 quadruple tables.  HXLEN and HYLEN are a big-value table's
C     dimensions, which is how an entry number turns back into a pair,
C     HLINB its linbits and HWHICH which of the sixteen trees table
C     number 0..31 uses.  HTBOK is set once the trees are built and is
C     zero if they did not fit, which is a build-time fault and not a
C     stream's doing.
      INTEGER HTL, HTR, HTV, HROOT, HXLEN, HYLEN, HLINB, HWHICH
      INTEGER HNODE, HTBOK
      COMMON /BLHUF/ HTL(MXNODE), HTR(MXNODE), HTV(MXNODE), HROOT(0:17),
     +               HXLEN(0:15), HYLEN(0:15), HLINB(0:MXTBL-1),
     +               HWHICH(0:MXTBL-1), HNODE, HTBOK

C     Scalefactor band boundaries per sampling frequency index, in
C     coefficients: SBL for long blocks and SBS for one window of a short
C     block.  SRATE is the frequency in Hz, so that the header and the
C     tables cannot disagree about which row is which.
      INTEGER SBL, SBS, SRATE
      COMMON /BLSWB/ SBL(0:MXSFL-1,0:8), SBS(0:MXSFS-1,0:8), SRATE(0:8)

C     x^(4/3) for every magnitude a big value can reach, and 2^(k/4) for
C     the gain exponents.  Both are lookups because requantisation runs
C     once per coefficient, 576 times a granule per channel, and a pow()
C     there is the whole cost of the stage.  QPOW is indexed by an
C     exponent shifted by 512, which covers the whole of what a global
C     gain of 0..255, a subblock gain of 0..7 and 21 scalefactors can
C     produce between them.
      DOUBLE PRECISION X43, QPOW
      COMMON /BLDQT/ X43(0:MXQNT), QPOW(0:1023)

C     The polyphase synthesis window, Table B.3, and the matrixing
C     coefficients cos((16 + i)(2k + 1)pi/64) that go in front of it.
C     UMAP is the standard's build of the 512-sample vector U out of the
C     1024-sample vector V, written out as an index per output rather
C     than as the two nested loops it is described by: it never changes,
C     and having it as a table means the windowing and the sixteen-way
C     sum below it can be one loop instead of three passes over memory.
      DOUBLE PRECISION SYNW, NMAT
      INTEGER UMAP
      COMMON /BLSYN/ SYNW(0:511), NMAT(0:63,0:31), UMAP(0:511)

C     The four inverse-MDCT window shapes -- normal, start, short and
C     stop, in the standard's own numbering 0, 1, 2, 3 -- and the cosine
C     tables of the two transform sizes.  CIM is the 36-point transform
C     of 18 coefficients and CIS the 12-point transform of 6.
      DOUBLE PRECISION IMDW, CIM, CIS
      COMMON /BLMDC/ IMDW(0:35,0:3), CIM(0:35,0:17), CIS(0:11,0:5)

C     Alias reduction: eight butterflies either side of each subband
C     boundary, from the eight c[i] the standard lists.  Intensity
C     stereo's MPEG-1 ratios, which are tangents; the MPEG-2 ones are a
C     power series and are computed where they are used.  PRETB is the
C     pre-emphasis added to the long scalefactors when preflag is set.
      DOUBLE PRECISION CS, CA, ISL, ISR
      INTEGER PRETB
      COMMON /BLMSC/ CS(0:7), CA(0:7), ISL(0:6), ISR(0:6), PRETB(0:20)

C     -- what the decoder did ---------------------------------------------
C     Counters over everything decoded since the last reset, so that a
C     test can assert the vectors reach the tools rather than assert a
C     threshold over code nothing runs.  A tool no vector exercises is a
C     tool that is not tested, and a tolerance at the end of the pipeline
C     cannot tell the difference.
C
C     UBLK counts granule-channels by block type, UMIX those with the
C     mixed flag, UTBL how often each of the 32 big-value tables was
C     selected, UCT1 the two count1 tables, UMSB and UISB the bands the
C     two stereo tools touched, URES the frames whose main data began
C     before their own header, UBACK the largest main_data_begin seen,
C     UPRE and USCL preflag and scalefac_scale, USCF the granules whose
C     scalefactors were inherited through scfsi, and USHORT the granules
C     that were not one long block.
      INTEGER UBLK, UMIX, UTBL, UCT1, UMSB, UISB, URES, UBACK
      INTEGER UPRE, USCL, USCF, UGRAN, UFRAME, USTARV
      COMMON /BLUSE/ UBLK(0:3), UMIX, UTBL(0:MXTBL-1), UCT1(0:1),
     +               UMSB, UISB, URES, UBACK, UPRE, USCL, USCF,
     +               UGRAN, UFRAME, USTARV

C     -- odds and ends ----------------------------------------------------
C     LASTBP records, per granule and channel, the bits the main data
C     reader actually consumed, against the part2_3_length the side
C     information promised.  A decoder that mis-parses a granule almost
C     never stops on exactly the right bit, so the pair is the cheapest
C     exact check there is on a stage the PCM comparison can only see
C     through a tolerance.
C
C     TABOK is a sentinel saying the constant tables have been built:
C     they are the same for every stream, so the work is done once per
C     process rather than once per reset.  NFRAME counts frames since the
C     last reset and CFGCH is a channel count a container claimed, zero
C     when nobody claimed one.
      INTEGER LASTBP, LASTPR, NFRAME, TABOK, CFGCH, FSTARV
      COMMON /BLSTA/ LASTBP(MXGR,MXCH), LASTPR(MXGR,MXCH), NFRAME,
     +               TABOK, CFGCH, FSTARV
