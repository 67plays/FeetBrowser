C     The frame header, its CRC, and the side information.
C
C     These are the only parts of a Layer III frame that live in the
C     frame they were sent in.  Everything after them is main data and
C     belongs to the reservoir.

C     Parse the four header bytes at the front of FBUF and work out how
C     long the frame is and what shape it has.
C
C     Status is zero on success.  The refusals -20 to -25 are separate
C     codes on purpose: a caller that is told "unsupported" cannot tell
C     the user anything, and a user whose file will not play is owed the
C     reason.  Layer I and Layer II are real formats with real files in
C     the world and are named rather than lumped in with corruption.
      SUBROUTINE BLHEAD(ST)
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER ST
      INTEGER B0, B1, B2, B3, BLFBYT
      INTEGER BR1(0:15), BR2(0:15), SR(0:8), IDX
      EXTERNAL BLFBYT
      SAVE BR1, BR2, SR
C     Layer III bitrates in kbit/s.  Index 0 is free format and index 15
C     is the reserved value that is never a legal frame.
      DATA BR1 / 0, 32, 40, 48, 56, 64, 80, 96,
     +           112, 128, 160, 192, 224, 256, 320, -1 /
      DATA BR2 / 0, 8, 16, 24, 32, 40, 48, 56,
     +           64, 80, 96, 112, 128, 144, 160, -1 /
      DATA SR / 44100, 48000, 32000, 22050, 24000, 16000,
     +          11025, 12000, 8000 /

      ST = 0
      HERR = 0
      IF (FN .LT. 4) THEN
         ST = -6
         RETURN
      END IF
      B0 = BLFBYT(1)
      B1 = BLFBYT(2)
      B2 = BLFBYT(3)
      B3 = BLFBYT(4)
      IF (B0 .NE. 255 .OR. IAND(B1, 224) .NE. 224) THEN
         ST = -5
         RETURN
      END IF

      HVER = IAND(ISHFT(B1, -3), 3)
      HLAYER = IAND(ISHFT(B1, -1), 3)
      HPROT = IAND(B1, 1)
      HBRIDX = IAND(ISHFT(B2, -4), 15)
      HSRIDX = IAND(ISHFT(B2, -2), 3)
      HPAD = IAND(ISHFT(B2, -1), 1)
      HPRIV = IAND(B2, 1)
      HMODE = IAND(ISHFT(B3, -6), 3)
      HMODX = IAND(ISHFT(B3, -4), 3)
      HCOPY = IAND(ISHFT(B3, -3), 1)
      HORIG = IAND(ISHFT(B3, -2), 1)
      HEMPH = IAND(B3, 3)

C     Version 1 is the reserved value in the two-bit field; 0 is the
C     MPEG-2.5 extension, which is not in either standard document but is
C     in a great many files.
      IF (HVER .EQ. 1) THEN
         ST = -25
         RETURN
      END IF
      HLSF = 0
      IF (HVER .NE. 3) HLSF = 1

C     The layer field counts downwards: 3 is Layer I, 2 is Layer II, 1 is
C     Layer III, 0 is reserved.
      IF (HLAYER .EQ. 3) THEN
         ST = -20
         RETURN
      ELSE IF (HLAYER .EQ. 2) THEN
         ST = -21
         RETURN
      ELSE IF (HLAYER .EQ. 0) THEN
         ST = -24
         RETURN
      END IF

      IF (HSRIDX .EQ. 3) THEN
         ST = -11
         RETURN
      END IF
      IF (HBRIDX .EQ. 15) THEN
         ST = -12
         RETURN
      END IF
C     Free format carries no bitrate at all: the frame length has to be
C     found by looking for the next sync word, and every frame in the
C     stream has to be the same length for that to be sound.  It is
C     refused by name rather than guessed at.
      IF (HBRIDX .EQ. 0) THEN
         ST = -22
         RETURN
      END IF

      IF (HVER .EQ. 3) THEN
         HSFI = HSRIDX
         HKBPS = BR1(HBRIDX)
      ELSE IF (HVER .EQ. 2) THEN
         HSFI = HSRIDX + 3
         HKBPS = BR2(HBRIDX)
      ELSE
         HSFI = HSRIDX + 6
         HKBPS = BR2(HBRIDX)
      END IF
      HRATE = SR(HSFI)

      HNCH = 2
      IF (HMODE .EQ. 3) HNCH = 1
      IF (CFGCH .GT. 0 .AND. CFGCH .NE. HNCH) THEN
         ST = -23
         RETURN
      END IF

C     Joint stereo's two tools are signalled in the same two bits, and
C     both may be on at once.  In any other mode neither is.
      HMS = 0
      HIS = 0
      IF (HMODE .EQ. 1) THEN
         HIS = IAND(HMODX, 1)
         HMS = IAND(ISHFT(HMODX, -1), 1)
      END IF

C     A granule is 576 samples; MPEG-1 sends two of them per frame and
C     the low sampling frequency extension sends one.  That single
C     difference is where the 144 and the 72 in the frame length come
C     from -- 1152 samples over 8 bits a byte is 144, and half of it is
C     72 -- so the two are written the same way round here as they are in
C     the standard.
      HNGR = 2
      IDX = 144
      IF (HLSF .EQ. 1) THEN
         HNGR = 1
         IDX = 72
      END IF
      HFLEN = (IDX * HKBPS * 1000) / HRATE + HPAD

      IF (HLSF .EQ. 0) THEN
         HSIDE = 17
         IF (HNCH .EQ. 2) HSIDE = 32
      ELSE
         HSIDE = 9
         IF (HNCH .EQ. 2) HSIDE = 17
      END IF

      IF (HFLEN .GT. MXFRM) THEN
         ST = -4
         RETURN
      END IF
      IF (HFLEN .LT. 4 + HSIDE + (1 - HPROT) * 2) THEN
         ST = -15
         RETURN
      END IF
      RETURN
      END

C     The CRC that follows the header when protection_bit is clear.
C
C     It covers the last two bytes of the header and the whole of the
C     side information, and nothing else: the main data is unprotected,
C     because by the time it is read it may have come from three frames
C     ago and there is nothing to check it against.  The generator is
C     x^16 + x^15 + x^2 + 1 with every bit of the register set to start
C     with, fed most significant bit first.
      INTEGER FUNCTION BLCRC()
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER C, I, K, B, BIT, CARRY, BLFBYT
      EXTERNAL BLFBYT
      C = 65535
      DO 20 I = 1, HSIDE + 2
         IF (I .LE. 2) THEN
            B = BLFBYT(2 + I)
         ELSE
            B = BLFBYT(4 + I)
         END IF
         DO 10 K = 7, 0, -1
            BIT = IAND(ISHFT(B, -K), 1)
            CARRY = IAND(ISHFT(C, -15), 1)
            C = IAND(ISHFT(C, 1), 65535)
            IF (CARRY .NE. BIT) C = IEOR(C, 32773)
   10    CONTINUE
   20 CONTINUE
      BLCRC = C
      RETURN
      END

C     -- side information -------------------------------------------------

C     Everything the frame says about its granules before a bit of main
C     data is touched.  The layouts of the two versions differ in more
C     than the granule count: MPEG-1 sends nine bits of main_data_begin,
C     a scalefactor selection field per channel and a preflag per
C     granule, and the low sampling frequency extension sends eight bits,
C     no selection field, no preflag, and nine bits of scalefac_compress
C     where MPEG-1 sends four.
      SUBROUTINE BLSINF(ST)
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER ST
      INTEGER GR, CH, I, BLHN
      EXTERNAL BLHN

      ST = 0
      HPOS = 32
      IF (HPROT .EQ. 0) HPOS = 48
      HERR = 0

      IF (HLSF .EQ. 0) THEN
         MDBEG = BLHN(9)
         IF (HNCH .EQ. 1) THEN
            I = BLHN(5)
         ELSE
            I = BLHN(3)
         END IF
         DO 20 CH = 1, HNCH
            DO 10 I = 0, 3
               SCFSI(I, CH) = BLHN(1)
   10       CONTINUE
   20    CONTINUE
      ELSE
         MDBEG = BLHN(8)
         IF (HNCH .EQ. 1) THEN
            I = BLHN(1)
         ELSE
            I = BLHN(2)
         END IF
         DO 40 CH = 1, HNCH
            DO 30 I = 0, 3
               SCFSI(I, CH) = 0
   30       CONTINUE
   40    CONTINUE
      END IF

      DO 90 GR = 1, HNGR
         DO 80 CH = 1, HNCH
            P23LEN(GR, CH) = BLHN(12)
            BIGVAL(GR, CH) = BLHN(9)
            GGAIN(GR, CH) = BLHN(8)
            IF (HLSF .EQ. 0) THEN
               SCFCMP(GR, CH) = BLHN(4)
            ELSE
               SCFCMP(GR, CH) = BLHN(9)
            END IF
            WSWF(GR, CH) = BLHN(1)
            IF (WSWF(GR, CH) .EQ. 1) THEN
               BLKTYP(GR, CH) = BLHN(2)
               MIXBLK(GR, CH) = BLHN(1)
               DO 50 I = 1, 2
                  TBLSEL(I, GR, CH) = BLHN(5)
   50          CONTINUE
               TBLSEL(3, GR, CH) = 0
               DO 60 I = 1, 3
                  SBGAIN(I, GR, CH) = BLHN(3)
   60          CONTINUE
C              With window switching the region counts are not sent.
C              A short block's bands are three windows wide, so eight
C              of them reach the same place three short bands do;
C              anything else gets the long block's seven.
               IF (BLKTYP(GR, CH) .EQ. 2 .AND.
     +             MIXBLK(GR, CH) .EQ. 0) THEN
                  REG0(GR, CH) = 8
               ELSE
                  REG0(GR, CH) = 7
               END IF
               REG1(GR, CH) = 20 - REG0(GR, CH)
C              block_type 0 with window_switching_flag set is not a
C              legal combination -- the flag exists to say the block is
C              not a normal one -- and a stream that does it would send
C              a granule with two table selects where the transform
C              expects three.  Treat it as the long block it claims to
C              be rather than reading past the field.
               IF (BLKTYP(GR, CH) .EQ. 0) THEN
                  ST = -16
                  RETURN
               END IF
            ELSE
               DO 70 I = 1, 3
                  TBLSEL(I, GR, CH) = BLHN(5)
   70          CONTINUE
               REG0(GR, CH) = BLHN(4)
               REG1(GR, CH) = BLHN(3)
               BLKTYP(GR, CH) = 0
               MIXBLK(GR, CH) = 0
            END IF
            IF (HLSF .EQ. 0) THEN
               PREFLG(GR, CH) = BLHN(1)
            ELSE
               PREFLG(GR, CH) = 0
            END IF
            SFSCAL(GR, CH) = BLHN(1)
            CNT1TS(GR, CH) = BLHN(1)
   80    CONTINUE
   90 CONTINUE

      IF (HERR .NE. 0) THEN
         ST = -15
         RETURN
      END IF
C     big_values counts pairs, so twice it is coefficients, and the
C     standard caps it at 288 pairs.  A larger value would have the
C     Huffman decoder writing past the granule.
      DO 110 GR = 1, HNGR
         DO 100 CH = 1, HNCH
            IF (BIGVAL(GR, CH) * 2 .GT. MXSMP) THEN
               ST = -17
               RETURN
            END IF
  100    CONTINUE
  110 CONTINUE
      RETURN
      END

C     Where the three Huffman regions of a granule end, in coefficients.
C
C     The boundaries are scalefactor band edges, which is why they cannot
C     be worked out until the sampling frequency is known.  A short block
C     counts its bands three windows at a time, so region0_count of eight
C     lands on three times the third short band's edge -- 36
C     coefficients, or 72 at 8 kHz where the short bands are twice as
C     wide.
      SUBROUTINE BLRGN(GR, CH, R0, R1)
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER GR, CH, R0, R1
      INTEGER I, NB
      NB = BIGVAL(GR, CH) * 2
      IF (WSWF(GR, CH) .EQ. 1 .AND. BLKTYP(GR, CH) .EQ. 2 .AND.
     +    MIXBLK(GR, CH) .EQ. 0) THEN
         R0 = 3 * SBS(3, HSFI)
         R1 = MXSMP
      ELSE
         I = REG0(GR, CH) + 1
         IF (I .GT. MXSFL - 1) I = MXSFL - 1
         R0 = SBL(I, HSFI)
         I = REG0(GR, CH) + REG1(GR, CH) + 2
         IF (I .GT. MXSFL - 1) I = MXSFL - 1
         R1 = SBL(I, HSFI)
      END IF
      IF (R0 .GT. NB) R0 = NB
      IF (R1 .GT. NB) R1 = NB
      IF (R1 .LT. R0) R1 = R0
      RETURN
      END
