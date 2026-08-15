C     Shared state for the AAC decoder, INCLUDEd by every source file.
C
C     FORTRAN 77 has no structures, no allocation and no modules, so the
C     decoder's state is COMMON and its buffers are dimensioned for the
C     largest frame we accept.  As in the H.264 decoder next door that is
C     the point rather than a workaround: a stranger's AudioSpecificConfig
C     cannot make this decoder allocate anything, because it never
C     allocates.  Every limit below is checked before a sample is written.
C
C     One include rather than a copy per file, because a COMMON block
C     whose declaration drifts between two files does not fail to
C     compile.  It fails to decode, quietly, in whichever routine got the
C     shorter array.
C
C     The names all start with IP, and the blocks with /IP, so that
C     nothing here can collide with the H.264 decoder's H2 -- both end up
C     in one process, and Fortran COMMON is matched by name.

C     -- what we accept ---------------------------------------------------
C     One coded frame, in bytes.  ADTS caps a frame at 8191 and no MP4
C     encoder we have met comes near it; 16384 is twice the largest legal
C     frame and is refused rather than truncated above that.
      INTEGER MXFRM, MXCH, MXSFB, MXGRP, MXWIN, MXQNT, MXNODE
      INTEGER MXORD, MXFLT
      PARAMETER (MXFRM = 16384)
C     Mono and stereo.  Multichannel would need a channel map, a coupling
C     element and four more of everything below; it is refused by name.
      PARAMETER (MXCH = 2)
C     Scalefactor bands: 51 is the most any sampling frequency has (32
C     kHz, long windows), and the array holds the closing offset too.
      PARAMETER (MXSFB = 52)
      PARAMETER (MXGRP = 8, MXWIN = 8)
C     The largest quantised magnitude a stream may carry.  The escape
C     coding could express more; the standard does not allow it, and a
C     value past this is a corrupt stream rather than a loud one.
      PARAMETER (MXQNT = 8191)
C     Huffman tree nodes for all twelve codebooks.  Counted from the
C     tables themselves -- 2712 -- and rounded up.
      PARAMETER (MXNODE = 3000)
C     TNS: at most three filters per window, of order at most 20.
      PARAMETER (MXORD = 20, MXFLT = 3)

C     -- the coded bitstream ----------------------------------------------
C     FBUF holds one raw_data_block.  BPOS is a bit cursor, zero based,
C     BNBIT the number of bits in the buffer, and BERR is set by any read
C     that would pass the end.  Nothing here walks off the buffer: the
C     bytes came off the network.
      INTEGER*1 FBUF
      INTEGER BPOS, BNBIT, BERR
      COMMON /IPBIT/ FBUF(MXFRM), BPOS, BNBIT, BERR

C     -- the tables -------------------------------------------------------
C     The twelve Huffman codebooks as one pool of tree nodes: HTL and HTR
C     are the two children, zero where there is none, and HTV is the
C     entry number at a leaf and -1 above it.  HROOT(b) is codebook b's
C     root; books 1 to 11 are the spectral ones and book 12 is the
C     scalefactor book.  HDIM, HLAV, HUNS and HMOD say how to turn an
C     entry number back into the values it stands for.
      INTEGER HTL, HTR, HTV, HROOT, HDIM, HLAV, HUNS, HMOD, HCNT, HNODE
      COMMON /IPHUF/ HTL(MXNODE), HTR(MXNODE), HTV(MXNODE), HROOT(12),
     +               HDIM(12), HLAV(12), HUNS(12), HMOD(12), HCNT(12),
     +               HNODE

C     Scalefactor band offsets, expanded per sampling frequency index:
C     SWBL for long windows and SWBS for short ones, both ending in the
C     closing offset (1024 and 128).  NSWBL and NSWBS are the band counts,
C     TNSML and TNSMS the highest band TNS may touch, SRATE the frequency
C     in Hz.
      INTEGER SWBL, SWBS, NSWBL, NSWBS, TNSML, TNSMS, SRATE
      COMMON /IPSWB/ SWBL(0:MXSFB-1,0:12), SWBS(0:15,0:12),
     +               NSWBL(0:12), NSWBS(0:12), TNSML(0:12), TNSMS(0:12),
     +               SRATE(0:12)

C     x^(4/3) for every magnitude, and 2^((sf-100)/4) for every
C     scalefactor.  Both are lookups because inverse quantisation runs
C     once per coefficient, a thousand times a frame per channel, and a
C     pow() there is the whole cost of the stage.
      DOUBLE PRECISION X43, GAINT
      COMMON /IPDQT/ X43(0:MXQNT), GAINT(0:255)

C     The rising half of each window shape at each length.  The falling
C     half is the same numbers read backwards, so it is not stored.
      DOUBLE PRECISION WSINL, WKBDL, WSINS, WKBDS
      COMMON /IPWIN/ WSINL(0:1023), WKBDL(0:1023), WSINS(0:127),
     +               WKBDS(0:127)

C     Twiddle factors and bit reversals for the two FFT sizes the IMDCT
C     uses: 512 points for a long window, 64 for a short one.
      DOUBLE PRECISION TWLR, TWLI, TWSR, TWSI, PTLR, PTLI, PTSR, PTSI
      INTEGER BRL, BRS
      COMMON /IPFFT/ TWLR(0:255), TWLI(0:255), TWSR(0:31), TWSI(0:31),
     +               PTLR(0:511), PTLI(0:511), PTSR(0:63), PTSI(0:63),
     +               BRL(0:511), BRS(0:63)

C     -- the stream's configuration ---------------------------------------
C     Filled from the AudioSpecificConfig, or from an ADTS header, and
C     then left alone: every frame of a stream is coded against it.
C     CAOT is the audio object type (2 is AAC-LC and is the only one we
C     decode), CSRI the sampling frequency index, CCHC the channel
C     configuration, CNCH the channels that implies, CFLEN the samples a
C     frame produces.
      INTEGER CFGOK, CAOT, CSRI, CCHC, CNCH, CFLEN
      COMMON /IPCFG/ CFGOK, CAOT, CSRI, CCHC, CNCH, CFLEN

C     -- the current frame, per channel -----------------------------------
C     WSEQ is the window sequence and WSHP the window shape; PWSHP is the
C     previous frame's shape, which is what the left half of this frame's
C     window is built from, and PWSEQ its sequence.  NWIN is 1 or 8,
C     NGRP the window groups and GLEN their lengths, NSWB the bands in
C     force and SWBO their offsets.  BTYPE is the codebook a band was
C     coded with and SFAC its scalefactor -- or its intensity position,
C     or its noise energy, depending on BTYPE, which is why they share an
C     array exactly as they share a differential coder in the bitstream.
      INTEGER WSEQ, WSHP, PWSHP, PWSEQ, MAXSFB, NWIN, NGRP, GLEN
      INTEGER NSWB, SWBO, BTYPE, SFAC, GGAIN
      COMMON /IPCHN/ WSEQ(MXCH), WSHP(MXCH), PWSHP(MXCH), PWSEQ(MXCH),
     +               MAXSFB(MXCH), NWIN(MXCH), NGRP(MXCH),
     +               GLEN(MXGRP,MXCH), NSWB(MXCH),
     +               SWBO(0:MXSFB-1,MXCH),
     +               BTYPE(0:MXSFB-1,MXGRP,MXCH),
     +               SFAC(0:MXSFB-1,MXGRP,MXCH), GGAIN(MXCH)

C     The spectrum, quantised and dequantised.  Both are held the way the
C     transform wants to read them: window w of a short block occupies
C     coefficients 128w to 128w+127, and band b of that window starts at
C     128w + SWBO(b).  The bitstream delivers them grouped instead, so
C     the scatter happens as they are decoded rather than in a pass of
C     its own.  QSPEC is kept after dequantisation because it is the one
C     thing in the decoder a test can compare exactly.
      INTEGER QSPEC
      DOUBLE PRECISION SPEC
      COMMON /IPSPC/ QSPEC(0:1023,MXCH), SPEC(0:1023,MXCH)

C     Temporal noise shaping, per channel and per window.
      INTEGER TNSPR, TNSNF, TNSLN, TNSOR, TNSDR
      DOUBLE PRECISION TNSCF
      COMMON /IPTNS/ TNSPR(MXCH), TNSNF(0:MXWIN-1,MXCH),
     +               TNSLN(MXFLT,0:MXWIN-1,MXCH),
     +               TNSOR(MXFLT,0:MXWIN-1,MXCH),
     +               TNSDR(MXFLT,0:MXWIN-1,MXCH),
     +               TNSCF(MXORD,MXFLT,0:MXWIN-1,MXCH)

C     Mid/side: MSPRES is ms_mask_present (0 none, 1 a mask follows, 2
C     every band) and MSMASK the mask itself.  It is read once per
C     channel pair and used by both mid/side and intensity stereo, which
C     is why it does not live in the per-channel block.
      INTEGER MSPRES, MSMASK
      COMMON /IPMS/ MSPRES, MSMASK(0:MXSFB-1,MXGRP)

C     -- output -----------------------------------------------------------
C     OVLP is the second half of the last frame's windowed transform,
C     waiting to be added to the first half of the next one.  It is the
C     reason an AAC decoder cannot be handed a frame from the middle of a
C     stream and produce the right sound: there is no such thing as a key
C     frame here, only the overlap.  PCMO holds the frame's samples,
C     interleaved, NOUT how many there are per channel and NOUTCH how
C     many channels.
      DOUBLE PRECISION OVLP, PCMO
      INTEGER NOUT, NOUTCH
      COMMON /IPOUT/ OVLP(0:1023,MXCH), PCMO(0:2047), NOUT, NOUTCH

C     -- odds and ends ----------------------------------------------------
C     RNDST is the noise generator's state, carried across frames so that
C     two bands of noise never come out identical.  LASTBP is where the
C     bit cursor stopped, which the test suite compares against the frame
C     length: a decoder that mis-parses a frame almost never stops on
C     exactly the right bit.  NFRAME counts frames since the last reset.
C     TABOK is a sentinel saying the constant tables have been built --
C     they are the same for every stream, so the work is done once per
C     process rather than once per reset.
      INTEGER RNDST, LASTBP, NFRAME, TABOK
      COMMON /IPMSC/ RNDST, LASTBP, NFRAME, TABOK
