C     The individual_channel_stream: everything one channel of one frame
C     says about itself.  Clause 4.4.2 of ISO/IEC 14496-3, read top to
C     bottom -- ics_info, section_data, scale_factor_data, pulse_data,
C     tns_data, then the spectrum.
C
C     The order matters more than it looks.  Every one of these is coded
C     relative to the one before it: the sections say which codebook each
C     band used, the scalefactors are a difference chain that only makes
C     sense once you know which bands are noise and which are intensity,
C     and the spectrum cannot be read at all without both.  There is no
C     resynchronisation point inside a frame.  Get one bit wrong and
C     everything after it is noise -- which is exactly why the decoder
C     checks, at the end, that it stopped on the bit the frame said it
C     would.

C     4.4.6, ics_info.  Window sequence, window shape, and the band
C     layout they imply.
      SUBROUTINE IPICSI(CH, ST)
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER CH, ST
      INTEGER G, I, SFG, PRED, IPU1, IPUN
      EXTERNAL IPU1, IPUN
      ST = 0
      I = IPU1()
      WSEQ(CH) = IPUN(2)
      WSHP(CH) = IPU1()
      IF (WSEQ(CH) .EQ. 2) THEN
         MAXSFB(CH) = IPUN(4)
C     scale_factor_grouping: seven bits, one per gap between the eight
C     short windows, a one meaning "same group as the window before".
C     The groups are what the scalefactors and the sections are counted
C     in; the windows themselves are only ever eight.
         SFG = IPUN(7)
         NWIN(CH) = 8
         NGRP(CH) = 1
         GLEN(1,CH) = 1
         DO 10 I = 0, 6
            IF (IAND(ISHFT(SFG, -(6 - I)), 1) .NE. 0) THEN
               GLEN(NGRP(CH),CH) = GLEN(NGRP(CH),CH) + 1
            ELSE
               NGRP(CH) = NGRP(CH) + 1
               GLEN(NGRP(CH),CH) = 1
            END IF
   10    CONTINUE
         NSWB(CH) = NSWBS(CSRI)
         DO 20 I = 0, NSWB(CH)
            SWBO(I,CH) = SWBS(I,CSRI)
   20    CONTINUE
      ELSE
         MAXSFB(CH) = IPUN(6)
C     predictor_data_present.  In AAC-LC this bit is always zero: the
C     field it introduces belongs to Main profile's backward prediction
C     or to LTP, and decoding either as if it were not there would put
C     the cursor in the wrong place and turn the rest of the frame into
C     noise.  Refused by name instead.
         PRED = IPU1()
         IF (PRED .NE. 0) THEN
            ST = -22
            RETURN
         END IF
         NWIN(CH) = 1
         NGRP(CH) = 1
         GLEN(1,CH) = 1
         NSWB(CH) = NSWBL(CSRI)
         DO 30 I = 0, NSWB(CH)
            SWBO(I,CH) = SWBL(I,CSRI)
   30    CONTINUE
      END IF
      IF (MAXSFB(CH) .GT. NSWB(CH)) THEN
         ST = -33
         RETURN
      END IF
      G = 0
      DO 40 I = 1, NGRP(CH)
         G = G + GLEN(I,CH)
   40 CONTINUE
      IF (G .NE. NWIN(CH)) ST = -33
      IF (BERR .NE. 0) ST = -30
      RETURN
      END

C     4.4.7, section_data: a run length coded map from scalefactor band
C     to codebook.  Codebook 0 is a band of silence, 13 is noise, 14 and
C     15 are intensity stereo, 12 is reserved and 1 to 11 are the real
C     spectral books.
      SUBROUTINE IPSECT(CH, ST)
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER CH, ST
      INTEGER G, K, I, CB, LEN, INC, BITS, ESC, IPUN
      EXTERNAL IPUN
      ST = 0
      IF (WSEQ(CH) .EQ. 2) THEN
         BITS = 3
      ELSE
         BITS = 5
      END IF
      ESC = ISHFT(1, BITS) - 1
      DO 50 G = 1, NGRP(CH)
         DO 10 I = 0, MXSFB - 1
            BTYPE(I,G,CH) = 0
   10    CONTINUE
         K = 0
   20    IF (K .GE. MAXSFB(CH)) GOTO 40
            CB = IPUN(4)
            IF (CB .EQ. 12) THEN
               ST = -31
               RETURN
            END IF
            LEN = 0
   30       INC = IPUN(BITS)
            LEN = LEN + INC
            IF (BERR .NE. 0 .OR. LEN .GT. MAXSFB(CH)) THEN
               ST = -33
               RETURN
            END IF
            IF (INC .EQ. ESC) GOTO 30
            IF (K + LEN .GT. MAXSFB(CH)) THEN
               ST = -33
               RETURN
            END IF
            DO 35 I = K, K + LEN - 1
               BTYPE(I,G,CH) = CB
   35       CONTINUE
            K = K + LEN
            GOTO 20
   40    CONTINUE
   50 CONTINUE
      IF (BERR .NE. 0) ST = -30
      RETURN
      END

C     4.4.8, scale_factor_data.  Three difference chains share one
C     codebook and one array, because the bitstream shares them: an
C     ordinary band continues the scalefactor chain, an intensity band
C     the position chain, a noise band the energy chain, and which chain
C     a codeword belongs to is decided by the section data alone.
C
C     The first noise band is the exception: nine bits of plain binary,
C     because there is nothing yet to take a difference from.
      SUBROUTINE IPSFAC(CH, ST)
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER CH, ST
      INTEGER G, SFB, BT, OFF0, OFF1, OFF2, NFLAG, V
      INTEGER IPHCW, IPUN
      EXTERNAL IPHCW, IPUN
      ST = 0
      OFF0 = GGAIN(CH)
      OFF1 = GGAIN(CH) - 90
      OFF2 = 0
      NFLAG = 1
      DO 20 G = 1, NGRP(CH)
         DO 10 SFB = 0, MAXSFB(CH) - 1
            BT = BTYPE(SFB,G,CH)
            IF (BT .EQ. 0) THEN
               SFAC(SFB,G,CH) = 0
            ELSE IF (BT .EQ. 14 .OR. BT .EQ. 15) THEN
               OFF2 = OFF2 + IPHCW(12) - 60
               V = OFF2
               IF (V .LT. -155) V = -155
               IF (V .GT. 100) V = 100
               SFAC(SFB,G,CH) = V
            ELSE IF (BT .EQ. 13) THEN
               IF (NFLAG .NE. 0) THEN
                  OFF1 = OFF1 + IPUN(9) - 256
                  NFLAG = 0
               ELSE
                  OFF1 = OFF1 + IPHCW(12) - 60
               END IF
               V = OFF1
               IF (V .LT. -100) V = -100
               IF (V .GT. 155) V = 155
               SFAC(SFB,G,CH) = V
            ELSE
               OFF0 = OFF0 + IPHCW(12) - 60
C     A scalefactor outside eight bits is not a quiet band or a loud
C     one, it is a decoder that has lost the bitstream.  Everything
C     after it would be noise, so the frame goes rather than the sound.
               IF (OFF0 .LT. 0 .OR. OFF0 .GT. 255) THEN
                  ST = -32
                  RETURN
               END IF
               SFAC(SFB,G,CH) = OFF0
            END IF
            IF (BERR .NE. 0) THEN
               ST = -30
               RETURN
            END IF
   10    CONTINUE
   20 CONTINUE
      RETURN
      END

C     4.4.10, tns_data, and the inverse quantisation of its coefficients.
C     They arrive as reflection coefficients of two, three or four bits,
C     quantised on a sine scale so that a coefficient near the ends of
C     the range costs the same as one near the middle.
      SUBROUTINE IPTNSD(CH, ST)
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER CH, ST
      INTEGER W, F, NF, RES, LN, OR, CMP, RB, CL, I, RAW, HALF
      INTEGER NFB, LNB, ORB
      DOUBLE PRECISION PI, IQF, IQFM
      INTEGER IPU1, IPUN
      EXTERNAL IPU1, IPUN
      ST = 0
      PI = 4.0D0 * ATAN(1.0D0)
      IF (WSEQ(CH) .EQ. 2) THEN
         NFB = 1
         LNB = 4
         ORB = 3
      ELSE
         NFB = 2
         LNB = 6
         ORB = 5
      END IF
      DO 40 W = 0, NWIN(CH) - 1
         NF = IPUN(NFB)
         TNSNF(W,CH) = NF
         RES = 0
         IF (NF .GT. 0) RES = IPU1()
         IF (NF .GT. MXFLT) THEN
            ST = -34
            RETURN
         END IF
         DO 30 F = 1, NF
            LN = IPUN(LNB)
            OR = IPUN(ORB)
            TNSLN(F,W,CH) = LN
            TNSOR(F,W,CH) = OR
            TNSDR(F,W,CH) = 0
            IF (OR .GT. MXORD) THEN
               ST = -34
               RETURN
            END IF
            IF (OR .GT. 0) THEN
               TNSDR(F,W,CH) = IPU1()
               CMP = IPU1()
               RB = 3 + RES
               CL = RB - CMP
               HALF = ISHFT(1, CL - 1)
               IQF = (DBLE(ISHFT(1, RB - 1)) - 0.5D0) / (PI / 2.0D0)
               IQFM = (DBLE(ISHFT(1, RB - 1)) + 0.5D0) / (PI / 2.0D0)
               DO 20 I = 1, OR
                  RAW = IPUN(CL)
C     The field is two's complement in CL bits, and CL is as narrow as
C     two, so the sign has to be put back by hand.
                  IF (RAW .GE. HALF) RAW = RAW - 2 * HALF
                  IF (RAW .GE. 0) THEN
                     TNSCF(I,F,W,CH) = SIN(DBLE(RAW) / IQF)
                  ELSE
                     TNSCF(I,F,W,CH) = SIN(DBLE(RAW) / IQFM)
                  END IF
   20          CONTINUE
            END IF
   30    CONTINUE
         IF (BERR .NE. 0) THEN
            ST = -30
            RETURN
         END IF
   40 CONTINUE
      RETURN
      END

C     4.4.9, spectral_data: the quantised coefficients themselves, band
C     by band, in whichever codebook the section data named.
C
C     Where they land is the whole trick of this routine.  The bitstream
C     runs group by group and window by window inside a group, but the
C     transform wants each window's 128 coefficients contiguous, so the
C     coefficients are scattered as they are decoded rather than
C     shuffled afterwards.  For a long block there is one window and the
C     scatter is the identity.
      SUBROUTINE IPSPEC(CH, ST)
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER CH, ST
      INTEGER G, SFB, W, CB, S, E, K, I, D, GB, BASE, IDX, M
      INTEGER V(4)
      INTEGER IPHCW, IPU1, IPHESC
      EXTERNAL IPHCW, IPU1, IPHESC
      ST = 0
      GB = 0
      DO 80 G = 1, NGRP(CH)
         DO 70 SFB = 0, MAXSFB(CH) - 1
            CB = BTYPE(SFB,G,CH)
            IF (CB .GT. 0 .AND. CB .LT. 13) THEN
               S = SWBO(SFB,CH)
               E = SWBO(SFB+1,CH)
               D = HDIM(CB)
               DO 60 W = 0, GLEN(G,CH) - 1
                  BASE = 128 * (GB + W)
                  IF (NWIN(CH) .EQ. 1) BASE = 0
                  K = S
   50             IF (K .GE. E) GOTO 60
                     IDX = IPHCW(CB)
                     CALL IPHVAL(CB, IDX, V)
                     IF (HUNS(CB) .NE. 0) THEN
                        DO 20 I = 1, D
                           IF (V(I) .NE. 0) THEN
                              IF (IPU1() .NE. 0) V(I) = -V(I)
                           END IF
   20                   CONTINUE
                     END IF
                     IF (CB .EQ. 11) THEN
                        DO 30 I = 1, D
                           IF (IABS(V(I)) .EQ. 16) THEN
                              M = IPHESC()
                              IF (V(I) .GT. 0) THEN
                                 V(I) = M
                              ELSE
                                 V(I) = -M
                              END IF
                           END IF
   30                   CONTINUE
                     END IF
                     IF (BERR .NE. 0) THEN
                        ST = -30
                        RETURN
                     END IF
                     DO 40 I = 1, D
                        QSPEC(BASE+K,CH) = V(I)
                        K = K + 1
   40                CONTINUE
                     GOTO 50
   60          CONTINUE
            END IF
   70    CONTINUE
         GB = GB + GLEN(G,CH)
   80 CONTINUE
      RETURN
      END

C     4.4.2, individual_channel_stream.  COMWIN says the channel's window
C     came from the pair it belongs to rather than from its own bits.
      SUBROUTINE IPICS(CH, COMWIN, ST)
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER CH, COMWIN, ST
      INTEGER I, K, NP, PSFB, POFF(4), PAMP(4), PRES, GAINP
      INTEGER IPU1, IPUN
      EXTERNAL IPU1, IPUN
      ST = 0
      DO 10 I = 0, 1023
         QSPEC(I,CH) = 0
   10 CONTINUE
      DO 15 I = 0, MXWIN - 1
         TNSNF(I,CH) = 0
   15 CONTINUE
      TNSPR(CH) = 0
      GGAIN(CH) = IPUN(8)
      IF (COMWIN .EQ. 0) THEN
         CALL IPICSI(CH, ST)
         IF (ST .NE. 0) RETURN
      END IF
      CALL IPSECT(CH, ST)
      IF (ST .NE. 0) RETURN
      CALL IPSFAC(CH, ST)
      IF (ST .NE. 0) RETURN

      PRES = IPU1()
      NP = 0
      IF (PRES .NE. 0) THEN
C     4.4.11, pulse_data.  Long windows only: the standard forbids it
C     with eight short windows, and a stream that does it anyway has
C     told us something that cannot be true.
         IF (WSEQ(CH) .EQ. 2) THEN
            ST = -35
            RETURN
         END IF
         NP = IPUN(2) + 1
         PSFB = IPUN(6)
         IF (PSFB .GE. NSWB(CH)) THEN
            ST = -35
            RETURN
         END IF
         DO 20 I = 1, NP
            POFF(I) = IPUN(5)
            PAMP(I) = IPUN(4)
   20    CONTINUE
      END IF

      TNSPR(CH) = IPU1()
      IF (TNSPR(CH) .NE. 0) THEN
         CALL IPTNSD(CH, ST)
         IF (ST .NE. 0) RETURN
      END IF

C     gain_control_data belongs to SSR profile, which nothing encodes and
C     which needs a four band polyphase filterbank this decoder does not
C     have.  Refused by name rather than skipped.
      GAINP = IPU1()
      IF (GAINP .NE. 0) THEN
         ST = -23
         RETURN
      END IF

      CALL IPSPEC(CH, ST)
      IF (ST .NE. 0) RETURN

C     The pulses are added to the quantised values, magnitude first, so a
C     pulse never changes a coefficient's sign.  They exist so that one
C     very loud line does not force a whole band into a bigger codebook.
      IF (NP .GT. 0) THEN
         K = SWBO(PSFB,CH)
         DO 30 I = 1, NP
            K = K + POFF(I)
            IF (K .GT. 1023) THEN
               ST = -35
               RETURN
            END IF
            IF (QSPEC(K,CH) .GT. 0) THEN
               QSPEC(K,CH) = QSPEC(K,CH) + PAMP(I)
            ELSE
               QSPEC(K,CH) = QSPEC(K,CH) - PAMP(I)
            END IF
            IF (IABS(QSPEC(K,CH)) .GT. MXQNT) THEN
               ST = -39
               RETURN
            END IF
   30    CONTINUE
      END IF
      IF (BERR .NE. 0) ST = -30
      RETURN
      END
