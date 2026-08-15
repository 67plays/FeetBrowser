C     The outside of the decoder: what a stream says it is, how a frame
C     is built out of elements, and the handful of entry points the
C     Python side calls through ctypes.
C
C     Two ways in.  An MP4 hands us an AudioSpecificConfig once and then
C     raw_data_blocks with no framing at all; an ADTS stream repeats a
C     seven byte header in front of every frame and never sends a config
C     as such.  They agree on everything that matters, so both end up
C     filling the same /IPCFG/ and calling the same frame decoder.
C
C     Status codes are negative and grouped by the routine that produces
C     them, which is what makes a decode failure worth reading:
C
C       -1 .. -19   configuration: what the stream claims to be
C       -20 .. -29  tools we refuse by name rather than mis-decode
C       -30 .. -39  the bitstream disagreeing with itself
C       -40 .. -49  the frame's element structure
C
C     Nothing here returns a positive number and nothing here throws.  A
C     stream that is wrong is refused with a code the Python side turns
C     into a sentence.

C     -- configuration ----------------------------------------------------

C     The five bit audio object type, with its escape to a six bit
C     extension.  Object types are the whole of AAC's version history in
C     one field: 2 is LC, 1 is Main, 3 SSR, 4 LTP, 5 SBR, 29 PS.
      INTEGER FUNCTION IPAOT()
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER V, IPUN
      EXTERNAL IPUN
      V = IPUN(5)
      IF (V .EQ. 31) V = 32 + IPUN(6)
      IPAOT = V
      RETURN
      END

C     One object type, judged.  Everything that is not AAC-LC is refused
C     with a code of its own, because "unsupported" is a useless thing to
C     tell somebody whose file will not play: an HE-AAC stream needs a
C     spectral band replicator, an LTP stream needs a predictor across
C     frames, and those are different amounts of missing.
      SUBROUTINE IPJUDG(AOT, ST)
      IMPLICIT NONE
      INTEGER AOT, ST
      ST = 0
      IF (AOT .EQ. 2) RETURN
      IF (AOT .EQ. 1) THEN
         ST = -20
      ELSE IF (AOT .EQ. 3) THEN
         ST = -21
      ELSE IF (AOT .EQ. 4) THEN
         ST = -24
      ELSE IF (AOT .EQ. 5) THEN
         ST = -25
      ELSE IF (AOT .EQ. 29) THEN
         ST = -26
      ELSE
         ST = -27
      END IF
      RETURN
      END

C     4.4.1.1, program_config_element.  We read it for one number -- how
C     many channels the programme has -- and for the sampling frequency
C     index when the configuration did not give one.  Everything else in
C     it describes loudspeaker positions and downmix coefficients for
C     channel counts we do not accept anyway.
      SUBROUTINE IPPCE(ADOPT, ST)
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER ADOPT, ST
      INTEGER NFRT, NSID, NBAK, NLFE, NAD, NCC, J, K, NCH, SRI, JUNK
      INTEGER IPU1, IPUN
      EXTERNAL IPU1, IPUN
      ST = 0
      JUNK = IPUN(4)
      JUNK = IPUN(2)
      SRI = IPUN(4)
      NFRT = IPUN(4)
      NSID = IPUN(4)
      NBAK = IPUN(4)
      NLFE = IPUN(2)
      NAD = IPUN(3)
      NCC = IPUN(4)
      IF (IPU1() .NE. 0) JUNK = IPUN(4)
      IF (IPU1() .NE. 0) JUNK = IPUN(4)
      IF (IPU1() .NE. 0) THEN
         JUNK = IPUN(2)
         JUNK = IPU1()
      END IF
      NCH = 0
      K = NFRT + NSID + NBAK
      DO 10 J = 1, K
C     One bit says whether this slot is a pair or a single, and that bit
C     is the whole of the channel count.
         NCH = NCH + 1 + IPU1()
         JUNK = IPUN(4)
   10 CONTINUE
      DO 20 J = 1, NLFE
         JUNK = IPUN(4)
         NCH = NCH + 1
   20 CONTINUE
      DO 30 J = 1, NAD
         JUNK = IPUN(4)
   30 CONTINUE
      DO 40 J = 1, NCC
         JUNK = IPU1()
         JUNK = IPUN(4)
   40 CONTINUE
      CALL IPALGN
      JUNK = IPUN(8)
      CALL IPSKIP(8 * JUNK)
      IF (BERR .NE. 0) THEN
         ST = -2
         RETURN
      END IF
      IF (NLFE .GT. 0) THEN
         ST = -14
         RETURN
      END IF
      IF (NCH .LT. 1 .OR. NCH .GT. MXCH) THEN
         ST = -13
         RETURN
      END IF
      IF (ADOPT .NE. 0) THEN
         IF (SRI .GT. 12) THEN
            ST = -12
            RETURN
         END IF
         CSRI = SRI
         CNCH = NCH
      ELSE IF (NCH .NE. CNCH .OR. SRI .NE. CSRI) THEN
C     A programme config inside a frame that disagrees with the one the
C     stream was configured by.  It is not a thing to average out.
         ST = -43
         RETURN
      END IF
      RETURN
      END

C     1.6.2.1, AudioSpecificConfig, which is what an MP4's esds box
C     carries and what everything else quotes.  Two bytes, usually.
      SUBROUTINE IPCFGP(ST)
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER ST
      INTEGER AOT, FLF, DEP, EXT, SYNC, EAOT, NC(0:7)
      INTEGER IPAOT, IPU1, IPUN, IPLEFT
      EXTERNAL IPAOT, IPU1, IPUN, IPLEFT
      DATA NC /0, 1, 2, 3, 4, 5, 6, 8/
      ST = 0
      CFGOK = 0
      AOT = IPAOT()
      CSRI = IPUN(4)
      IF (CSRI .EQ. 15) THEN
C     An explicit 24 bit sampling frequency.  It is legal, it is never
C     used by anything that also encodes plain AAC-LC, and the whole of
C     the band table machinery is indexed by the four bit index -- there
C     is no band layout defined for an arbitrary rate.
         ST = -11
         RETURN
      END IF
      IF (CSRI .GT. 12) THEN
         ST = -12
         RETURN
      END IF
      CCHC = IPUN(4)
      CALL IPJUDG(AOT, ST)
      IF (ST .NE. 0) RETURN
      CAOT = AOT
C     GASpecificConfig, 4.4.1.
      FLF = IPU1()
      IF (FLF .NE. 0) THEN
C     960 sample frames.  Every window, every band table and the whole
C     transform would be a different length; this is not a flag to
C     ignore.
         ST = -15
         RETURN
      END IF
      DEP = IPU1()
      IF (DEP .NE. 0) THEN
         ST = -16
         RETURN
      END IF
      EXT = IPU1()
      IF (CCHC .EQ. 0) THEN
         CALL IPPCE(1, ST)
         IF (ST .NE. 0) RETURN
      ELSE
         IF (CCHC .GT. 7) THEN
            ST = -13
            RETURN
         END IF
         CNCH = NC(CCHC)
         IF (CNCH .GT. MXCH) THEN
            ST = -13
            RETURN
         END IF
      END IF
      IF (BERR .NE. 0) THEN
         ST = -2
         RETURN
      END IF
C     Backward compatible SBR signalling: an 11 bit sync word after the
C     configuration proper, then the extension's object type.  A decoder
C     that ignores it plays the stream at half its intended bandwidth and
C     often at half its sample rate, which sounds like a fault rather
C     than like a missing feature.  Refused by name.
      IF (IPLEFT() .GE. 16) THEN
         SYNC = IPUN(11)
         IF (SYNC .EQ. 695) THEN
            EAOT = IPAOT()
            IF (EAOT .EQ. 5) THEN
               ST = -25
               RETURN
            END IF
            IF (EAOT .EQ. 29) THEN
               ST = -26
               RETURN
            END IF
         END IF
      END IF
      CFLEN = 1024
      CFGOK = 1
      RETURN
      END

C     -- the frame --------------------------------------------------------

C     4.4.2, channel_pair_element.  The two channels may share one window
C     sequence, and if they do the mid/side mask comes with it.
      SUBROUTINE IPCPE(C1, ST)
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER C1, ST
      INTEGER COMW, G, SFB, I, C2
      INTEGER IPU1, IPUN
      EXTERNAL IPU1, IPUN
      ST = 0
      C2 = C1 + 1
      I = IPUN(4)
      COMW = IPU1()
      MSPRES = 0
      DO 10 G = 1, MXGRP
         DO 5 SFB = 0, MXSFB - 1
            MSMASK(SFB,G) = 0
    5    CONTINUE
   10 CONTINUE
      IF (COMW .NE. 0) THEN
         CALL IPICSI(C1, ST)
         IF (ST .NE. 0) RETURN
         WSEQ(C2) = WSEQ(C1)
         WSHP(C2) = WSHP(C1)
         MAXSFB(C2) = MAXSFB(C1)
         NWIN(C2) = NWIN(C1)
         NGRP(C2) = NGRP(C1)
         NSWB(C2) = NSWB(C1)
         DO 20 I = 1, MXGRP
            GLEN(I,C2) = GLEN(I,C1)
   20    CONTINUE
         DO 30 I = 0, MXSFB - 1
            SWBO(I,C2) = SWBO(I,C1)
   30    CONTINUE
         MSPRES = IPUN(2)
         IF (MSPRES .EQ. 1) THEN
            DO 50 G = 1, NGRP(C1)
               DO 40 SFB = 0, MAXSFB(C1) - 1
                  MSMASK(SFB,G) = IPU1()
   40          CONTINUE
   50       CONTINUE
         ELSE IF (MSPRES .EQ. 2) THEN
            DO 70 G = 1, MXGRP
               DO 60 SFB = 0, MXSFB - 1
                  MSMASK(SFB,G) = 1
   60          CONTINUE
   70       CONTINUE
         ELSE IF (MSPRES .EQ. 3) THEN
            ST = -36
            RETURN
         END IF
      END IF
      CALL IPICS(C1, COMW, ST)
      IF (ST .NE. 0) RETURN
      CALL IPICS(C2, COMW, ST)
      IF (ST .NE. 0) RETURN
C     The order below is the standard's and is not negotiable.  Mid/side
C     and intensity are defined on dequantised coefficients; TNS filters
C     what they leave; the transform sees the result.
      CALL IPDEQ(C1)
      CALL IPDEQ(C2)
      IF (MSPRES .NE. 0) CALL IPMSA
      CALL IPISA
      IF (TNSPR(C1) .NE. 0) CALL IPTNSA(C1)
      IF (TNSPR(C2) .NE. 0) CALL IPTNSA(C2)
      RETURN
      END

C     4.4.2, single_channel_element.
      SUBROUTINE IPSCE(C1, ST)
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER C1, ST
      INTEGER I
      INTEGER IPUN
      EXTERNAL IPUN
      ST = 0
      I = IPUN(4)
      CALL IPICS(C1, 0, ST)
      IF (ST .NE. 0) RETURN
      CALL IPDEQ(C1)
      IF (TNSPR(C1) .NE. 0) CALL IPTNSA(C1)
      RETURN
      END

C     4.4.2.2, data_stream_element: a container for anything at all, and
C     nothing we want.  It still has to be parsed exactly, because its
C     length is the only thing standing between us and the next element.
      SUBROUTINE IPDSE(ST)
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER ST
      INTEGER I, ALGN, CNT
      INTEGER IPU1, IPUN
      EXTERNAL IPU1, IPUN
      ST = 0
      I = IPUN(4)
      ALGN = IPU1()
      CNT = IPUN(8)
      IF (CNT .EQ. 255) CNT = CNT + IPUN(8)
      IF (ALGN .NE. 0) CALL IPALGN
      CALL IPSKIP(8 * CNT)
      IF (BERR .NE. 0) ST = -30
      RETURN
      END

C     4.4.2.5, fill_element.  Mostly dynamic range control that nobody
C     sets and bit reservoir padding -- except when it is the spectral
C     band replication payload of an HE-AAC stream, which is the one
C     thing in here we must not skip silently.
      SUBROUTINE IPFIL(ST)
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER ST
      INTEGER CNT, XT
      INTEGER IPUN
      EXTERNAL IPUN
      ST = 0
      CNT = IPUN(4)
      IF (CNT .EQ. 15) CNT = CNT + IPUN(8) - 1
      IF (CNT .LE. 0) THEN
         IF (BERR .NE. 0) ST = -30
         RETURN
      END IF
      XT = IPUN(4)
      IF (XT .EQ. 13 .OR. XT .EQ. 14) THEN
         ST = -25
         RETURN
      END IF
      CALL IPSKIP(8 * CNT - 4)
      IF (BERR .NE. 0) ST = -30
      RETURN
      END

C     4.4.2, raw_data_block: elements until the terminator, then a byte
C     alignment.  Where each element's channels go is fixed by the order
C     the elements arrive in, and that order is the same in every frame
C     of a stream, which is what lets the overlap buffers be indexed by
C     channel number and stay indexed by the same channel next frame.
      SUBROUTINE IPFRM(ST)
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER ST
      INTEGER ID, NCH, NEL, I, C
      DOUBLE PRECISION OUT(0:1023)
      INTEGER IPUN
      EXTERNAL IPUN
      ST = 0
      NCH = 0
      NEL = 0
      DO 10 I = 0, 2047
         PCMO(I) = 0.0D0
   10 CONTINUE
      NOUT = 0
      NOUTCH = 0

   20 CONTINUE
         NEL = NEL + 1
         IF (NEL .GT. 64) THEN
            ST = -40
            RETURN
         END IF
         ID = IPUN(3)
         IF (BERR .NE. 0) THEN
            ST = -30
            RETURN
         END IF
         IF (ID .EQ. 7) GOTO 80
         IF (ID .EQ. 0) THEN
            IF (NCH + 1 .GT. CNCH) THEN
               ST = -41
               RETURN
            END IF
            CALL IPSCE(NCH + 1, ST)
            IF (ST .NE. 0) RETURN
            CALL IPWOVL(NCH + 1, OUT)
            CALL IPSTOR(NCH, OUT)
            NCH = NCH + 1
         ELSE IF (ID .EQ. 1) THEN
            IF (NCH + 2 .GT. CNCH) THEN
               ST = -41
               RETURN
            END IF
            CALL IPCPE(NCH + 1, ST)
            IF (ST .NE. 0) RETURN
            DO 30 C = 1, 2
               CALL IPWOVL(NCH + C, OUT)
               CALL IPSTOR(NCH + C - 1, OUT)
   30       CONTINUE
            NCH = NCH + 2
         ELSE IF (ID .EQ. 2) THEN
C     A coupling channel element mixes one coded channel into several
C     others at gains it carries itself.  Skipping it would leave the
C     channels it feeds quietly wrong.
            ST = -22
            RETURN
         ELSE IF (ID .EQ. 3) THEN
            ST = -14
            RETURN
         ELSE IF (ID .EQ. 4) THEN
            CALL IPDSE(ST)
            IF (ST .NE. 0) RETURN
         ELSE IF (ID .EQ. 5) THEN
            CALL IPPCE(0, ST)
            IF (ST .NE. 0) RETURN
         ELSE IF (ID .EQ. 6) THEN
            CALL IPFIL(ST)
            IF (ST .NE. 0) RETURN
         END IF
         GOTO 20

   80 CONTINUE
      CALL IPALGN
      LASTBP = BPOS
      IF (NCH .NE. CNCH) THEN
         ST = -41
         RETURN
      END IF
      NOUT = CFLEN
      NOUTCH = CNCH
      NFRAME = NFRAME + 1
      IF (BERR .NE. 0) ST = -30
      RETURN
      END

C     One channel's samples into the interleaved output.
      SUBROUTINE IPSTOR(CIDX, OUT)
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER CIDX
      DOUBLE PRECISION OUT(0:*)
      INTEGER I
      DO 10 I = 0, CFLEN - 1
         PCMO(I * CNCH + CIDX) = OUT(I)
   10 CONTINUE
      RETURN
      END

C     Load a frame into the bit reader.
      SUBROUTINE IPLOAD(BUF, OFF, N, ST)
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER*1 BUF(*)
      INTEGER OFF, N, ST, I
      ST = 0
      IF (N .LE. 0) THEN
         ST = -3
         RETURN
      END IF
      IF (N .GT. MXFRM) THEN
         ST = -4
         RETURN
      END IF
      DO 10 I = 1, N
         FBUF(I) = BUF(OFF + I)
   10 CONTINUE
      BPOS = 0
      BNBIT = 8 * N
      BERR = 0
      RETURN
      END

C     -- the C interface --------------------------------------------------

C     Bump this whenever the meaning of any entry point changes, so that
C     a Python side built against an older library refuses rather than
C     misreads it.
      SUBROUTINE IPVERS(V) BIND(C, NAME='instep_version')
      IMPLICIT NONE
      INTEGER V
      V = 1
      RETURN
      END

C     Throw away everything: the tables are rebuilt, the configuration is
C     forgotten, and -- the part that matters for sound -- the overlap
C     buffers are zeroed.  A decoder resumed after a seek without this
C     adds the tail of the frame before the seek to the head of the frame
C     after it, which is a click.
      SUBROUTINE IPRSET() BIND(C, NAME='instep_reset')
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER I, C
      IF (TABOK .NE. 12345) THEN
         CALL IPTINI
         TABOK = 12345
      END IF
      CFGOK = 0
      CAOT = 0
      CSRI = 0
      CCHC = 0
      CNCH = 0
      CFLEN = 1024
      NOUT = 0
      NOUTCH = 0
      LASTBP = 0
      NFRAME = 0
      BPOS = 0
      BNBIT = 0
      BERR = 0
      MSPRES = 0
      RNDST = 523124044
      DO 20 C = 1, MXCH
         PWSHP(C) = 0
         PWSEQ(C) = 0
         WSEQ(C) = 0
         WSHP(C) = 0
         MAXSFB(C) = 0
         NWIN(C) = 1
         NGRP(C) = 1
         TNSPR(C) = 0
         DO 10 I = 0, 1023
            OVLP(I,C) = 0.0D0
            SPEC(I,C) = 0.0D0
            QSPEC(I,C) = 0
   10    CONTINUE
   20 CONTINUE
      RETURN
      END

C     Only the overlap, for a seek: the configuration is still true.
      SUBROUTINE IPFLSH() BIND(C, NAME='instep_flush')
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER I, C
      DO 20 C = 1, MXCH
         PWSHP(C) = 0
         PWSEQ(C) = 0
         DO 10 I = 0, 1023
            OVLP(I,C) = 0.0D0
   10    CONTINUE
   20 CONTINUE
      RNDST = 523124044
      RETURN
      END

C     Configure from an AudioSpecificConfig.  INFO comes back as
C     (status, sample rate, channels, samples per frame, object type).
      SUBROUTINE IPCFGE(BUF, N, INFO) BIND(C, NAME='instep_config')
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER*1 BUF(*)
      INTEGER N, INFO(8)
      INTEGER ST, I
      DO 10 I = 1, 8
         INFO(I) = 0
   10 CONTINUE
      CALL IPLOAD(BUF, 0, N, ST)
      IF (ST .NE. 0) THEN
         INFO(1) = ST
         RETURN
      END IF
      CALL IPCFGP(ST)
      INFO(1) = ST
      IF (ST .NE. 0) THEN
         CFGOK = 0
         RETURN
      END IF
      INFO(2) = SRATE(CSRI)
      INFO(3) = CNCH
      INFO(4) = CFLEN
      INFO(5) = CAOT
      INFO(6) = CSRI
      RETURN
      END

C     Configure from an ADTS header, which carries the same facts in a
C     different order and two bits fewer of channel configuration.
C     INFO(7) comes back as the whole frame's length in bytes and INFO(8)
C     as the header's, so the caller can find the next frame without
C     parsing the header a second time.
      SUBROUTINE IPADTS(BUF, N, INFO) BIND(C, NAME='instep_adts')
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER*1 BUF(*)
      INTEGER N, INFO(8)
      INTEGER ST, I, SYNC, LAY, PROT, PROF, CHC, FLEN, NBLK, NC(0:7)
      INTEGER IPU1, IPUN
      EXTERNAL IPU1, IPUN
      DATA NC /0, 1, 2, 3, 4, 5, 6, 8/
      DO 10 I = 1, 8
         INFO(I) = 0
   10 CONTINUE
      CALL IPLOAD(BUF, 0, N, ST)
      IF (ST .NE. 0) THEN
         INFO(1) = ST
         RETURN
      END IF
      IF (N .LT. 7) THEN
         INFO(1) = -5
         RETURN
      END IF
      SYNC = IPUN(12)
      IF (SYNC .NE. 4095) THEN
         INFO(1) = -5
         RETURN
      END IF
      I = IPU1()
      LAY = IPUN(2)
      IF (LAY .NE. 0) THEN
         INFO(1) = -6
         RETURN
      END IF
      PROT = IPU1()
      PROF = IPUN(2)
      CALL IPJUDG(PROF + 1, ST)
      IF (ST .NE. 0) THEN
         INFO(1) = ST
         RETURN
      END IF
      CAOT = PROF + 1
      CSRI = IPUN(4)
      IF (CSRI .GT. 12) THEN
         INFO(1) = -12
         RETURN
      END IF
      I = IPU1()
      CHC = IPUN(3)
      IF (CHC .EQ. 0 .OR. NC(CHC) .GT. MXCH) THEN
C     An ADTS frame with channel_configuration zero puts its programme
C     config inside the frame.  Legal, vanishingly rare, and it would
C     make the channel count of a stream a per frame property.
         INFO(1) = -13
         RETURN
      END IF
      CCHC = CHC
      CNCH = NC(CHC)
      I = IPU1()
      I = IPU1()
      I = IPU1()
      I = IPU1()
      FLEN = IPUN(13)
      I = IPUN(11)
      NBLK = IPUN(2)
      IF (NBLK .NE. 0) THEN
C     Several raw_data_blocks behind one header, each needing its own
C     CRC accounting.  No encoder in circulation emits it.
         INFO(1) = -42
         RETURN
      END IF
      IF (PROT .EQ. 0) THEN
         I = IPUN(16)
      END IF
      IF (BERR .NE. 0) THEN
         INFO(1) = -5
         RETURN
      END IF
      IF (FLEN .LT. BPOS / 8 .OR. FLEN .GT. N) THEN
         INFO(1) = -7
         RETURN
      END IF
      CFLEN = 1024
      CFGOK = 1
      INFO(1) = 0
      INFO(2) = SRATE(CSRI)
      INFO(3) = CNCH
      INFO(4) = CFLEN
      INFO(5) = CAOT
      INFO(6) = CSRI
      INFO(7) = FLEN
      INFO(8) = BPOS / 8
      RETURN
      END

C     Decode one raw_data_block.  INFO comes back as (status, samples per
C     channel, channels, bits consumed, bits offered).
      SUBROUTINE IPDECD(BUF, N, INFO) BIND(C, NAME='instep_decode')
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER*1 BUF(*)
      INTEGER N, INFO(8)
      INTEGER ST, I
      DO 10 I = 1, 8
         INFO(I) = 0
   10 CONTINUE
      IF (CFGOK .EQ. 0) THEN
         INFO(1) = -1
         RETURN
      END IF
      CALL IPLOAD(BUF, 0, N, ST)
      IF (ST .NE. 0) THEN
         INFO(1) = ST
         RETURN
      END IF
      LASTBP = 0
      CALL IPFRM(ST)
      INFO(1) = ST
      INFO(2) = NOUT
      INFO(3) = NOUTCH
      INFO(4) = LASTBP
      INFO(5) = BNBIT
      IF (ST .NE. 0) THEN
         NOUT = 0
         NOUTCH = 0
         INFO(2) = 0
         INFO(3) = 0
      END IF
      RETURN
      END

C     The last frame's samples, interleaved, as C floats in [-1, 1].  The
C     transform's output is already at that scale: nothing here divides
C     by 32768, because nothing here ever multiplied by it.
      SUBROUTINE IPPCM(DST, CAP, ST) BIND(C, NAME='instep_pcm')
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      REAL DST(*)
      INTEGER CAP, ST, I, NS
      NS = NOUT * NOUTCH
      IF (NS .LE. 0) THEN
         ST = -8
         RETURN
      END IF
      IF (CAP .LT. NS) THEN
         ST = -9
         RETURN
      END IF
      DO 10 I = 1, NS
         DST(I) = REAL(PCMO(I-1))
   10 CONTINUE
      ST = NS
      RETURN
      END

C     All of one decoder's continuity, out and back in.
C
C     The library has one set of COMMON blocks, so two streams playing at
C     once share them.  For a video decoder that is merely slow; here it
C     would be wrong, because the overlap buffers are the only thing
C     joining one frame to the next and there is no keyframe to recover
C     at.  So a decoder that finds another has been at the library puts
C     its own overlap back rather than starting cold and clicking.
      SUBROUTINE IPSAVS(D, DCAP, IA, ICAP, ST)
     +   BIND(C, NAME='instep_save')
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER DCAP, ICAP, ST, IA(*)
      DOUBLE PRECISION D(*)
      INTEGER I, C, K
      IF (DCAP .LT. 2 * 1024 .OR. ICAP .LT. 16) THEN
         ST = -9
         RETURN
      END IF
      K = 0
      DO 20 C = 1, MXCH
         DO 10 I = 0, 1023
            K = K + 1
            D(K) = OVLP(I,C)
   10    CONTINUE
   20 CONTINUE
      DO 30 I = 1, 16
         IA(I) = 0
   30 CONTINUE
      IA(1) = RNDST
      IA(2) = PWSHP(1)
      IA(3) = PWSHP(2)
      IA(4) = PWSEQ(1)
      IA(5) = PWSEQ(2)
      IA(6) = NFRAME
      ST = K
      RETURN
      END

      SUBROUTINE IPRSTR(D, DCAP, IA, ICAP, ST)
     +   BIND(C, NAME='instep_restore')
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER DCAP, ICAP, ST, IA(*)
      DOUBLE PRECISION D(*)
      INTEGER I, C, K
      IF (DCAP .LT. 2 * 1024 .OR. ICAP .LT. 16) THEN
         ST = -9
         RETURN
      END IF
      K = 0
      DO 20 C = 1, MXCH
         DO 10 I = 0, 1023
            K = K + 1
            OVLP(I,C) = D(K)
   10    CONTINUE
   20 CONTINUE
      RNDST = IA(1)
      PWSHP(1) = IA(2)
      PWSHP(2) = IA(3)
      PWSEQ(1) = IA(4)
      PWSEQ(2) = IA(5)
      NFRAME = IA(6)
      ST = K
      RETURN
      END

C     -- hooks for the test suite -----------------------------------------
C
C     A decoder compared only against its own final output is compared
C     against nothing, and a tolerance at the end of a chain this long
C     will hide almost any bug in the middle of it.  These expose the
C     stages that can be compared exactly -- the quantised spectrum, the
C     scalefactors, the codebook each band used -- so that a difference
C     can be located rather than merely detected.

      SUBROUTINE IPQSPC(CH, DST, CAP, ST) BIND(C, NAME='instep_qspec')
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER CH, CAP, ST, DST(*), I
      IF (CH .LT. 1 .OR. CH .GT. MXCH .OR. CAP .LT. 1024) THEN
         ST = -9
         RETURN
      END IF
      DO 10 I = 1, 1024
         DST(I) = QSPEC(I-1,CH)
   10 CONTINUE
      ST = 1024
      RETURN
      END

      SUBROUTINE IPSPCD(CH, DST, CAP, ST) BIND(C, NAME='instep_spec')
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER CH, CAP, ST, I
      DOUBLE PRECISION DST(*)
      IF (CH .LT. 1 .OR. CH .GT. MXCH .OR. CAP .LT. 1024) THEN
         ST = -9
         RETURN
      END IF
      DO 10 I = 1, 1024
         DST(I) = SPEC(I-1,CH)
   10 CONTINUE
      ST = 1024
      RETURN
      END

C     Per band state, flattened as band + MXSFB * group: the codebook,
C     the scalefactor, and the band's first coefficient.
      SUBROUTINE IPBAND(CH, BT, SF, OFF, CAP, ST)
     +   BIND(C, NAME='instep_bands')
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER CH, CAP, ST, BT(*), SF(*), OFF(*)
      INTEGER G, B, K
      IF (CH .LT. 1 .OR. CH .GT. MXCH .OR. CAP .LT. MXSFB * MXGRP) THEN
         ST = -9
         RETURN
      END IF
      DO 20 G = 1, MXGRP
         DO 10 B = 0, MXSFB - 1
            K = B + MXSFB * (G - 1) + 1
            BT(K) = BTYPE(B,G,CH)
            SF(K) = SFAC(B,G,CH)
            OFF(K) = 0
   10    CONTINUE
   20 CONTINUE
      DO 30 B = 0, MXSFB - 1
         OFF(B+1) = SWBO(B,CH)
   30 CONTINUE
      ST = MXSFB * MXGRP
      RETURN
      END

C     The frame's shape: window sequence, shape, band count, groups and
C     their lengths, and the mid/side mode.
      SUBROUTINE IPSHAP(CH, A, CAP, ST) BIND(C, NAME='instep_shape')
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER CH, CAP, ST, A(*), I
      IF (CH .LT. 1 .OR. CH .GT. MXCH .OR. CAP .LT. 16) THEN
         ST = -9
         RETURN
      END IF
      DO 10 I = 1, 16
         A(I) = 0
   10 CONTINUE
      A(1) = WSEQ(CH)
      A(2) = WSHP(CH)
      A(3) = MAXSFB(CH)
      A(4) = NWIN(CH)
      A(5) = NGRP(CH)
      A(6) = NSWB(CH)
      A(7) = MSPRES
      A(8) = TNSPR(CH)
      DO 20 I = 1, MXGRP
         A(8+I) = GLEN(I,CH)
   20 CONTINUE
      ST = 16
      RETURN
      END

C     Both inverse transforms, side by side, so the fast one can be held
C     to the formula the slow one is a transcription of.
      SUBROUTINE IPIMDE(X, M, MODE, Y) BIND(C, NAME='instep_imdct')
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER M, MODE
      DOUBLE PRECISION X(*), Y(*)
      IF (TABOK .NE. 12345) THEN
         CALL IPTINI
         TABOK = 12345
      END IF
      IF (M .NE. 128 .AND. M .NE. 1024) RETURN
      IF (MODE .EQ. 0) THEN
         CALL IPIMDC(X, M, Y)
      ELSE
         CALL IPIMDS(X, M, Y)
      END IF
      RETURN
      END

C     The window shapes, so the test suite can check the one property
C     that makes overlap-add reconstruct anything at all: w(n)^2 +
C     w(N-1-n)^2 = 1.
      SUBROUTINE IPWNDW(WHICH, DST, CAP, ST)
     +   BIND(C, NAME='instep_window')
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER WHICH, CAP, ST, I, N
      DOUBLE PRECISION DST(*)
      IF (TABOK .NE. 12345) THEN
         CALL IPTINI
         TABOK = 12345
      END IF
      IF (WHICH .LE. 1) THEN
         N = 1024
      ELSE
         N = 128
      END IF
      IF (CAP .LT. N) THEN
         ST = -9
         RETURN
      END IF
      DO 10 I = 1, N
         IF (WHICH .EQ. 0) THEN
            DST(I) = WSINL(I-1)
         ELSE IF (WHICH .EQ. 1) THEN
            DST(I) = WKBDL(I-1)
         ELSE IF (WHICH .EQ. 2) THEN
            DST(I) = WSINS(I-1)
         ELSE
            DST(I) = WKBDS(I-1)
         END IF
   10 CONTINUE
      ST = N
      RETURN
      END
