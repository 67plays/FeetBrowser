C     The outside of the decoder: one frame end to end, and the handful
C     of entry points the Python side calls through ctypes.
C
C     Status codes are negative and grouped by what went wrong, which is
C     what makes a decode failure worth reading:
C
C       -1 .. -9    the caller, and the bytes handed over
C       -10 .. -19  the bitstream disagreeing with itself
C       -20 .. -29  tools we refuse by name rather than mis-decode
C
C     Nothing here returns a positive number except a sample count, and
C     nothing here throws.  A stream that is wrong is refused with a code
C     the Python side turns into a sentence.

C     -- one frame --------------------------------------------------------

C     Copy a frame in.  OFF is a byte offset into BUF, so that a caller
C     with a buffer of several frames does not have to slice it.
      SUBROUTINE BLLOAD(BUF, OFF, N, ST)
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER*1 BUF(*)
      INTEGER OFF, N, ST, I, K
      ST = 0
      IF (N .LE. 0) THEN
         ST = -3
         RETURN
      END IF
      K = N
      IF (K .GT. MXFRM) K = MXFRM
      DO 10 I = 1, K
         FBUF(I) = BUF(OFF + I)
   10 CONTINUE
      FN = K
      HPOS = 0
      HERR = 0
      RETURN
      END

C     One granule and channel, from its scalefactors to its spectrum.
C     POS is the absolute bit position in the reservoir where the granule
C     starts; it comes back moved on by exactly part2_3_length, whatever
C     the Huffman decoder made of the bits in between.
      SUBROUTINE BLGRAN(GR, CH, POS)
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER GR, CH, POS
      INTEGER GEND, ISTER

      GEND = POS + P23LEN(GR, CH)
      CALL BLSEEK(POS)
      ISTER = 0
      IF (HLSF .EQ. 1 .AND. HIS .EQ. 1 .AND. CH .EQ. 2) ISTER = 1
      CALL BLSCF(GR, CH, ISTER)
      CALL BLSPEC(GR, CH, GEND)
C     What the granule cost against what it promised.  A decoder that
C     mis-parses a granule almost never stops on exactly the right bit,
C     and a stream is still decodable from the next granule onwards
C     however badly this one went, so the position is set from the side
C     information rather than from where the reader happened to stop.
      LASTBP(GR, CH) = BPOS - POS
      LASTPR(GR, CH) = P23LEN(GR, CH)
      POS = GEND
      CALL BLSEEK(POS)
      CALL BLDEQ(GR, CH)
      CALL BLXCPY(CH)

      UGRAN = UGRAN + 1
      UBLK(BLKTYP(GR, CH)) = UBLK(BLKTYP(GR, CH)) + 1
      IF (MIXBLK(GR, CH) .EQ. 1) UMIX = UMIX + 1
      IF (PREFLG(GR, CH) .EQ. 1) UPRE = UPRE + 1
      IF (SFSCAL(GR, CH) .EQ. 1) USCL = USCL + 1
      RETURN
      END

C     A whole frame: header, CRC, side information, both granules of both
C     channels, and 1152 or 576 samples out the other end.
      SUBROUTINE BLFRM(ST)
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER ST
      INTEGER GR, CH, I, POS, OK, WANT, GOT, BASE, BLCRC, BLFBYT
      DOUBLE PRECISION SB(0:17,0:31)
      EXTERNAL BLCRC, BLFBYT

      FSTARV = 0
      CALL BLHEAD(ST)
      IF (ST .NE. 0) RETURN
      IF (HFLEN .GT. FN) THEN
         ST = -6
         RETURN
      END IF
      FN = HFLEN

      IF (HPROT .EQ. 0) THEN
         WANT = ISHFT(BLFBYT(5), 8) + BLFBYT(6)
         GOT = BLCRC()
         IF (WANT .NE. GOT) THEN
            ST = -10
            RETURN
         END IF
      END IF

      CALL BLSINF(ST)
      IF (ST .NE. 0) RETURN

      NOUTCH = HNCH
      NOUT = HNGR * 576
      DO 10 I = 0, MXPCM - 1
         PCMO(I) = 0.0D0
   10 CONTINUE

      CALL BLRADD(OK)
      POS = BPOS

      DO 90 GR = 1, HNGR
         IF (OK .EQ. 0) THEN
C           No main data to decode this granule from.  The spectrum is
C           zero, but the granule still goes through the transform and
C           the filterbank: both carry state, and the frames after this
C           one need that state to have had the silence in it.
            FSTARV = 1
            DO 30 CH = 1, HNCH
               DO 20 I = 0, MXSMP - 1
                  IS(I, CH) = 0
                  XRQ(I, CH) = 0.0D0
                  XR(I, CH) = 0.0D0
   20          CONTINUE
               LASTBP(GR, CH) = 0
               LASTPR(GR, CH) = P23LEN(GR, CH)
   30       CONTINUE
         ELSE
            DO 40 CH = 1, HNCH
               CALL BLGRAN(GR, CH, POS)
   40       CONTINUE
            IF (BERR .NE. 0) THEN
               ST = -13
               NOUT = 0
               NOUTCH = 0
               RETURN
            END IF
            CALL BLSTER(GR)
         END IF

         DO 80 CH = 1, HNCH
            IF (OK .NE. 0) THEN
               CALL BLREOR(CH)
               CALL BLALIA(GR, CH)
            END IF
            BASE = (GR - 1) * 576 * NOUTCH
            CALL BLIMDC(GR, CH, SB)
            CALL BLSYNT(CH, SB, BASE)
   80    CONTINUE
   90 CONTINUE

      CALL BLRTRM
      NFRAME = NFRAME + 1
      UFRAME = UFRAME + 1
      ST = 0
      RETURN
      END

C     -- the C interface --------------------------------------------------

C     Bump this whenever the meaning of any entry point changes, so that
C     a Python side built against an older library refuses rather than
C     misreads it.
      SUBROUTINE BLVERS(V) BIND(C, NAME='ball_version')
      IMPLICIT NONE
      INTEGER V
      V = 1
      RETURN
      END

C     Throw away everything: the tables are built if they have not been,
C     the reservoir is emptied and -- the part that matters for sound --
C     the transform overlap and the filterbank's own history are zeroed.
C     A decoder resumed after a seek without this adds the tail of the
C     frame before the seek to the head of the frame after it, which is a
C     click.
      SUBROUTINE BLRSET() BIND(C, NAME='ball_reset')
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER I, C
      IF (TABOK .NE. 12345) CALL BLTINI
      CALL BLRCLR
      FN = 0
      HPOS = 0
      HERR = 0
      NOUT = 0
      NOUTCH = 0
      NFRAME = 0
      CFGCH = 0
      FSTARV = 0
      HSFI = 0
      HNCH = 1
      HNGR = 2
      HLSF = 0
      HMODE = 3
      HMS = 0
      HIS = 0
      DO 20 C = 1, MXCH
         VPOS(C) = 0
         LONGE(C) = 22
         SHRTS(C) = 13
         ISCALE(C) = 0
         RZERO(C) = 0
         DO 10 I = 0, MXSMP - 1
            OVER(I, C) = 0.0D0
            XR(I, C) = 0.0D0
            XRQ(I, C) = 0.0D0
            IS(I, C) = 0
   10    CONTINUE
         DO 15 I = 0, 1023
            VBUF(I, C) = 0.0D0
   15    CONTINUE
   20 CONTINUE
      DO 30 I = 0, MXPCM - 1
         PCMO(I) = 0.0D0
   30 CONTINUE
      CALL BLUZRO
      RETURN
      END

C     The counters that say which tools a stream actually used.  A reset
C     zeroes them, and so does this on its own, so that a test can
C     measure one file's coverage without the file before it counting
C     towards it.
C
C     The work is in BLUZRO and the entry point is a shim around it,
C     because a BIND(C) subroutine has no Fortran-visible name and so
C     cannot be called from BLRSET next door.
      SUBROUTINE BLZERO() BIND(C, NAME='ball_zero_tools')
      IMPLICIT NONE
      CALL BLUZRO
      RETURN
      END

      SUBROUTINE BLUZRO
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER I
      DO 10 I = 0, 3
         UBLK(I) = 0
   10 CONTINUE
      DO 20 I = 0, MXTBL - 1
         UTBL(I) = 0
   20 CONTINUE
      UCT1(0) = 0
      UCT1(1) = 0
      UMIX = 0
      UMSB = 0
      UISB = 0
      URES = 0
      UBACK = 0
      UPRE = 0
      USCL = 0
      USCF = 0
      UGRAN = 0
      UFRAME = 0
      USTARV = 0
      RETURN
      END

C     Only the carried state, for a seek: the reservoir, the transform
C     overlap and the filterbank history.  Everything a Layer III frame
C     depends on that is not in the frame.
      SUBROUTINE BLFLSH() BIND(C, NAME='ball_flush')
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER I, C
      CALL BLRCLR
      DO 20 C = 1, MXCH
         VPOS(C) = 0
         DO 10 I = 0, MXSMP - 1
            OVER(I, C) = 0.0D0
   10    CONTINUE
         DO 15 I = 0, 1023
            VBUF(I, C) = 0.0D0
   15    CONTINUE
   20 CONTINUE
      RETURN
      END

C     A channel count a container claimed, which the frames then have to
C     agree with.  MP3 carried inside MP4 can say up to five; a Layer III
C     frame header cannot describe more than two, and the mismatch is
C     refused by name rather than decoded as the first two channels of
C     something wider.  Zero means nobody claimed one.
      SUBROUTINE BLCFGE(NCH, INFO) BIND(C, NAME='ball_config')
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER NCH, INFO(8), I
      DO 10 I = 1, 8
         INFO(I) = 0
   10 CONTINUE
      IF (NCH .LT. 0) THEN
         INFO(1) = -3
         RETURN
      END IF
      IF (NCH .GT. MXCH) THEN
         INFO(1) = -23
         INFO(2) = NCH
         RETURN
      END IF
      CFGCH = NCH
      INFO(2) = NCH
      RETURN
      END

C     Look at a frame header without decoding anything: how long the
C     frame is, what rate it is at, how many channels.  A demuxer needs
C     this to walk a stream, and walking a stream should not cost a
C     transform.
      SUBROUTINE BLHDRE(BUF, N, INFO) BIND(C, NAME='ball_header')
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER*1 BUF(*)
      INTEGER N, INFO(16)
      INTEGER ST, I
      IF (TABOK .NE. 12345) CALL BLTINI
      DO 10 I = 1, 16
         INFO(I) = 0
   10 CONTINUE
      CALL BLLOAD(BUF, 0, N, ST)
      IF (ST .NE. 0) THEN
         INFO(1) = ST
         RETURN
      END IF
      CALL BLHEAD(ST)
      INFO(1) = ST
      IF (ST .NE. 0) RETURN
      INFO(2) = HFLEN
      INFO(3) = HRATE
      INFO(4) = HNCH
      INFO(5) = HKBPS
      INFO(6) = HNGR * 576
      INFO(7) = HVER
      INFO(8) = HMODE
      INFO(9) = HMODX
      INFO(10) = 1 - HPROT
      INFO(11) = HSIDE
      RETURN
      END

C     One frame in, samples ready to be read out.  INFO comes back as
C     (status, samples per channel, channels, frame bytes, main_data_begin,
C     reservoir bits, starved).
      SUBROUTINE BLDECD(BUF, N, INFO) BIND(C, NAME='ball_decode')
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER*1 BUF(*)
      INTEGER N, INFO(16)
      INTEGER ST, I
      IF (TABOK .NE. 12345) CALL BLTINI
      DO 10 I = 1, 16
         INFO(I) = 0
   10 CONTINUE
      IF (HTBOK .EQ. 0) THEN
         INFO(1) = -18
         RETURN
      END IF
      CALL BLLOAD(BUF, 0, N, ST)
      IF (ST .NE. 0) THEN
         INFO(1) = ST
         RETURN
      END IF
      CALL BLFRM(ST)
      INFO(1) = ST
      INFO(2) = NOUT
      INFO(3) = NOUTCH
      INFO(4) = HFLEN
      INFO(5) = MDBEG
      INFO(6) = BNBIT
      INFO(7) = FSTARV
      INFO(8) = HRATE
      IF (ST .NE. 0) THEN
         NOUT = 0
         NOUTCH = 0
         INFO(2) = 0
         INFO(3) = 0
      END IF
      RETURN
      END

C     The last frame's samples, interleaved, as C floats in [-1, 1].  The
C     filterbank's output is already at that scale: nothing here divides
C     by 32768, because nothing here ever multiplied by it.
      SUBROUTINE BLPCM(DST, CAP, ST) BIND(C, NAME='ball_pcm')
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
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

C     -- what the decoder did ---------------------------------------------

C     The frame's header and shape, for a caller that wants to say what
C     it played rather than only play it.
      SUBROUTINE BLFRME(A, CAP, ST) BIND(C, NAME='ball_frame')
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER CAP, ST, A(*), I
      IF (CAP .LT. 20) THEN
         ST = -9
         RETURN
      END IF
      DO 10 I = 1, 20
         A(I) = 0
   10 CONTINUE
      A(1) = HVER
      A(2) = HLSF
      A(3) = HRATE
      A(4) = HKBPS
      A(5) = HNCH
      A(6) = HNGR
      A(7) = HMODE
      A(8) = HMODX
      A(9) = HMS
      A(10) = HIS
      A(11) = MDBEG
      A(12) = HFLEN
      A(13) = HSIDE
      A(14) = 1 - HPROT
      A(15) = HPAD
      A(16) = HSFI
      A(17) = FSTARV
      A(18) = RN
      A(19) = HEMPH
      A(20) = NFRAME
      ST = 20
      RETURN
      END

C     One granule and channel of side information, and what it cost.
      SUBROUTINE BLGRNE(GR, CH, A, CAP, ST) BIND(C, NAME='ball_granule')
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER GR, CH, CAP, ST, A(*), I, R0, R1
      IF (GR .LT. 1 .OR. GR .GT. MXGR .OR. CH .LT. 1 .OR.
     +    CH .GT. MXCH .OR. CAP .LT. 24) THEN
         ST = -9
         RETURN
      END IF
      DO 10 I = 1, 24
         A(I) = 0
   10 CONTINUE
      CALL BLRGN(GR, CH, R0, R1)
      A(1) = P23LEN(GR, CH)
      A(2) = BIGVAL(GR, CH)
      A(3) = GGAIN(GR, CH)
      A(4) = SCFCMP(GR, CH)
      A(5) = WSWF(GR, CH)
      A(6) = BLKTYP(GR, CH)
      A(7) = MIXBLK(GR, CH)
      A(8) = TBLSEL(1, GR, CH)
      A(9) = TBLSEL(2, GR, CH)
      A(10) = TBLSEL(3, GR, CH)
      A(11) = SBGAIN(1, GR, CH)
      A(12) = SBGAIN(2, GR, CH)
      A(13) = SBGAIN(3, GR, CH)
      A(14) = REG0(GR, CH)
      A(15) = REG1(GR, CH)
      A(16) = PREFLG(GR, CH)
      A(17) = SFSCAL(GR, CH)
      A(18) = CNT1TS(GR, CH)
      A(19) = LASTBP(GR, CH)
      A(20) = LASTPR(GR, CH)
      A(21) = R0
      A(22) = R1
      A(23) = RZERO(CH)
      A(24) = SCFSI(0, CH) + 2 * SCFSI(1, CH) + 4 * SCFSI(2, CH)
     +        + 8 * SCFSI(3, CH)
      ST = 24
      RETURN
      END

C     Which tools have been used since the counters were last zeroed.
C     This exists so that the test suite can assert its vectors reach the
C     code rather than assert a tolerance over code nothing runs: a
C     threshold proves nothing about a stage no frame exercises.
      SUBROUTINE BLTOOL(A, CAP, ST) BIND(C, NAME='ball_tools')
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER CAP, ST, A(*), I, N
      N = 16 + MXTBL
      IF (CAP .LT. N) THEN
         ST = -9
         RETURN
      END IF
      DO 10 I = 1, N
         A(I) = 0
   10 CONTINUE
      A(1) = UBLK(0)
      A(2) = UBLK(1)
      A(3) = UBLK(2)
      A(4) = UBLK(3)
      A(5) = UMIX
      A(6) = UCT1(0)
      A(7) = UCT1(1)
      A(8) = UMSB
      A(9) = UISB
      A(10) = URES
      A(11) = UBACK
      A(12) = UPRE
      A(13) = USCL
      A(14) = USCF
      A(15) = UGRAN
      A(16) = USTARV
      DO 20 I = 0, MXTBL - 1
         A(17 + I) = UTBL(I)
   20 CONTINUE
      ST = N
      RETURN
      END

C     -- hooks for the test suite -----------------------------------------

C     The quantised spectrum, straight out of the Huffman decoder.
      SUBROUTINE BLISE(CH, DST, CAP, ST) BIND(C, NAME='ball_is')
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER CH, CAP, ST, DST(*), I
      IF (CH .LT. 1 .OR. CH .GT. MXCH .OR. CAP .LT. MXSMP) THEN
         ST = -9
         RETURN
      END IF
      DO 10 I = 1, MXSMP
         DST(I) = IS(I-1, CH)
   10 CONTINUE
      ST = MXSMP
      RETURN
      END

C     The spectrum after requantisation and nothing else, which is the
C     one stage of this decoder a test can recompute exactly.
      SUBROUTINE BLXRQE(CH, DST, CAP, ST) BIND(C, NAME='ball_xrq')
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER CH, CAP, ST, I
      DOUBLE PRECISION DST(*)
      IF (CH .LT. 1 .OR. CH .GT. MXCH .OR. CAP .LT. MXSMP) THEN
         ST = -9
         RETURN
      END IF
      DO 10 I = 1, MXSMP
         DST(I) = XRQ(I-1, CH)
   10 CONTINUE
      ST = MXSMP
      RETURN
      END

C     The spectrum as the transform saw it: stereo, reordering and alias
C     reduction all applied.
      SUBROUTINE BLXRE(CH, DST, CAP, ST) BIND(C, NAME='ball_xr')
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER CH, CAP, ST, I
      DOUBLE PRECISION DST(*)
      IF (CH .LT. 1 .OR. CH .GT. MXCH .OR. CAP .LT. MXSMP) THEN
         ST = -9
         RETURN
      END IF
      DO 10 I = 1, MXSMP
         DST(I) = XR(I-1, CH)
   10 CONTINUE
      ST = MXSMP
      RETURN
      END

C     The granule's scalefactors, flat, with the long and short split
C     that says how to read them.
      SUBROUTINE BLSCFE(CH, DST, CAP, ST) BIND(C, NAME='ball_scf')
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER CH, CAP, ST, DST(*), I
      IF (CH .LT. 1 .OR. CH .GT. MXCH .OR. CAP .LT. 43) THEN
         ST = -9
         RETURN
      END IF
      DO 10 I = 1, 40
         DST(I) = SCF(I-1, CH)
   10 CONTINUE
      DST(41) = LONGE(CH)
      DST(42) = SHRTS(CH)
      DST(43) = ISCALE(CH)
      ST = 43
      RETURN
      END

C     The scalefactor band boundaries in force, so that a test can check
C     them against the standard's tables rather than against themselves.
      SUBROUTINE BLBAND(SFI, WHICH, DST, CAP, ST)
     +   BIND(C, NAME='ball_bands')
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER SFI, WHICH, CAP, ST, DST(*), I, N
      IF (TABOK .NE. 12345) CALL BLTINI
      IF (SFI .LT. 0 .OR. SFI .GT. 8) THEN
         ST = -9
         RETURN
      END IF
      IF (WHICH .EQ. 0) THEN
         N = MXSFL
      ELSE
         N = MXSFS
      END IF
      IF (CAP .LT. N) THEN
         ST = -9
         RETURN
      END IF
      DO 10 I = 1, N
         IF (WHICH .EQ. 0) THEN
            DST(I) = SBL(I-1, SFI)
         ELSE
            DST(I) = SBS(I-1, SFI)
         END IF
   10 CONTINUE
      ST = N
      RETURN
      END

C     The four inverse-MDCT window shapes and the synthesis window, so
C     that the test suite can hold them to their definitions.
      SUBROUTINE BLWNDW(WHICH, DST, CAP, ST) BIND(C, NAME='ball_window')
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER WHICH, CAP, ST, I, N
      DOUBLE PRECISION DST(*)
      IF (TABOK .NE. 12345) CALL BLTINI
      IF (WHICH .EQ. 4) THEN
         N = 512
      ELSE IF (WHICH .GE. 0 .AND. WHICH .LE. 3) THEN
         N = 36
      ELSE
         ST = -9
         RETURN
      END IF
      IF (CAP .LT. N) THEN
         ST = -9
         RETURN
      END IF
      DO 10 I = 1, N
         IF (WHICH .EQ. 4) THEN
            DST(I) = SYNW(I-1)
         ELSE
            DST(I) = IMDW(I-1, WHICH)
         END IF
   10 CONTINUE
      ST = N
      RETURN
      END

C     One inverse MDCT, unwindowed, so that the transform can be held to
C     the standard's summation written out independently.  MODE 0 is the
C     36-point transform of 18 coefficients and MODE 1 the 12-point
C     transform of 6.
      SUBROUTINE BLIMDE(X, MODE, Y) BIND(C, NAME='ball_imdct')
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER MODE, I, K
      DOUBLE PRECISION X(*), Y(*), S
      IF (TABOK .NE. 12345) CALL BLTINI
      IF (MODE .EQ. 0) THEN
         DO 20 I = 0, 35
            S = 0.0D0
            DO 10 K = 0, 17
               S = S + X(K+1) * CIM(I, K)
   10       CONTINUE
            Y(I+1) = S
   20    CONTINUE
      ELSE
         DO 40 I = 0, 11
            S = 0.0D0
            DO 30 K = 0, 5
               S = S + X(K+1) * CIS(I, K)
   30       CONTINUE
            Y(I+1) = S
   40    CONTINUE
      END IF
      RETURN
      END

C     One Huffman code table, flattened, so that the test suite can check
C     the trees were built from the codes rather than check the trees
C     against themselves.  Entry index is x * ylen + y.
      SUBROUTINE BLHTBE(T, DST, CAP, ST) BIND(C, NAME='ball_htable')
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER T, CAP, ST, DST(*), I, N
      IF (TABOK .NE. 12345) CALL BLTINI
      IF (T .LT. 0 .OR. T .GT. 17) THEN
         ST = -9
         RETURN
      END IF
      IF (T .LE. 15) THEN
         N = HXLEN(T) * HYLEN(T)
      ELSE
         N = 16
      END IF
      IF (CAP .LT. N + 2) THEN
         ST = -9
         RETURN
      END IF
      IF (T .LE. 15) THEN
         DST(1) = HXLEN(T)
         DST(2) = HYLEN(T)
      ELSE
         DST(1) = 16
         DST(2) = 1
      END IF
      DO 10 I = 1, N
         DST(2 + I) = 0
   10 CONTINUE
      CALL BLTWLK(HROOT(T), DST(3), N)
      ST = N + 2
      RETURN
      END

C     -- carrying state between decoders ----------------------------------

C     The library has one set of COMMON blocks, so two streams playing at
C     once share them.  Layer III has no key frame: the reservoir, the
C     transform overlap and the filterbank history are the only things
C     joining one frame to the next, so a decoder that finds another has
C     been at the library puts its own back rather than starting cold and
C     clicking.
      SUBROUTINE BLSAVS(D, DCAP, IA, ICAP, ST) BIND(C, NAME='ball_save')
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER DCAP, ICAP, ST, IA(*)
      DOUBLE PRECISION D(*)
      INTEGER I, C, K
      IF (DCAP .LT. MXCH * (MXSMP + 1024) .OR.
     +    ICAP .LT. MXRES + 16) THEN
         ST = -9
         RETURN
      END IF
      K = 0
      DO 20 C = 1, MXCH
         DO 10 I = 0, MXSMP - 1
            K = K + 1
            D(K) = OVER(I, C)
   10    CONTINUE
   20 CONTINUE
      DO 40 C = 1, MXCH
         DO 30 I = 0, 1023
            K = K + 1
            D(K) = VBUF(I, C)
   30    CONTINUE
   40 CONTINUE
      IA(1) = RN
      IA(2) = VPOS(1)
      IA(3) = VPOS(2)
      IA(4) = CFGCH
      DO 50 I = 1, MXRES
         IA(16 + I) = IAND(INT(RBUF(I)), 255)
   50 CONTINUE
      ST = 0
      RETURN
      END

      SUBROUTINE BLREST(D, DCAP, IA, ICAP, ST)
     +   BIND(C, NAME='ball_restore')
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER DCAP, ICAP, ST, IA(*)
      DOUBLE PRECISION D(*)
      INTEGER I, C, K, V
      IF (DCAP .LT. MXCH * (MXSMP + 1024) .OR.
     +    ICAP .LT. MXRES + 16) THEN
         ST = -9
         RETURN
      END IF
      K = 0
      DO 20 C = 1, MXCH
         DO 10 I = 0, MXSMP - 1
            K = K + 1
            OVER(I, C) = D(K)
   10    CONTINUE
   20 CONTINUE
      DO 40 C = 1, MXCH
         DO 30 I = 0, 1023
            K = K + 1
            VBUF(I, C) = D(K)
   30    CONTINUE
   40 CONTINUE
      RN = IA(1)
      IF (RN .LT. 0 .OR. RN .GT. MXRES) RN = 0
      VPOS(1) = IA(2)
      VPOS(2) = IA(3)
      CFGCH = IA(4)
      DO 50 I = 1, MXRES
         V = IAND(IA(16 + I), 255)
         IF (V .GT. 127) V = V - 256
         RBUF(I) = V
   50 CONTINUE
      BNBIT = RN * 8
      BPOS = BNBIT
      BERR = 0
      ST = 0
      RETURN
      END
