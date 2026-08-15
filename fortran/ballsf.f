C     Scalefactors.
C
C     The two standards code these completely differently and neither is
C     complicated on its own.  MPEG-1 sends two field widths chosen by a
C     four-bit index, applies them to four fixed groups of bands, and
C     lets the second granule of a frame say "as before" for any group.
C     The low sampling frequency extension has one granule, so it has no
C     "as before" to say; instead it sends nine bits that select one of
C     six partitionings of the bands into four runs, each run with its
C     own width, and it has a separate set of partitionings for the
C     channel that carries intensity positions rather than scalefactors.
C
C     Both fill the same flat array, in the order the transform reads it:
C     long bands first, then the short bands three windows at a time.

C     Which bands of the granule are long and which are short.  A normal
C     block is all long and a short block is all short; a mixed block is
C     long over the bottom of the spectrum and short above it, which is
C     the only case where LONGE and SHRTS are both interesting.
      SUBROUTINE BLSPLT(GR, CH)
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER GR, CH
      IF (BLKTYP(GR, CH) .EQ. 2) THEN
         IF (MIXBLK(GR, CH) .EQ. 1) THEN
C           The switch point is the eighth long band, except at 8 kHz
C           where the bands are wide enough that the sixth is already
C           past the two subbands a mixed block keeps long.
            IF (HSFI .EQ. 8) THEN
               LONGE(CH) = 6
            ELSE
               LONGE(CH) = 8
            END IF
            SHRTS(CH) = 3
         ELSE
            LONGE(CH) = 0
            SHRTS(CH) = 0
         END IF
      ELSE
         LONGE(CH) = 22
         SHRTS(CH) = 13
      END IF
      RETURN
      END

C     MPEG-1 scalefactors, ISO/IEC 11172-3 2.4.2.7.
      SUBROUTINE BLSF1(GR, CH)
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER GR, CH
      INTEGER SL1(0:15), SL2(0:15)
      INTEGER GS(0:3), GE(0:3)
      INTEGER S1, S2, I, W, G, K, BLUN
      EXTERNAL BLUN
      SAVE SL1, SL2, GS, GE
      DATA SL1 / 0, 0, 0, 0, 3, 1, 1, 1, 2, 2, 2, 3, 3, 3, 4, 4 /
      DATA SL2 / 0, 1, 2, 3, 0, 1, 2, 3, 1, 2, 3, 1, 2, 3, 2, 3 /
C     The four groups scfsi can inherit, as first and last band.
      DATA GS / 0, 6, 11, 16 /
      DATA GE / 5, 10, 15, 20 /

      S1 = SL1(SCFCMP(GR, CH))
      S2 = SL2(SCFCMP(GR, CH))
      DO 10 I = 0, 39
         SCF(I, CH) = 0
   10 CONTINUE

      IF (BLKTYP(GR, CH) .EQ. 2) THEN
         IF (MIXBLK(GR, CH) .EQ. 1) THEN
            DO 20 I = 0, 7
               SCF(I, CH) = BLUN(S1)
   20       CONTINUE
            K = 8
            DO 40 I = 3, 5
               DO 30 W = 0, 2
                  SCF(K, CH) = BLUN(S1)
                  K = K + 1
   30          CONTINUE
   40       CONTINUE
            DO 60 I = 6, 11
               DO 50 W = 0, 2
                  SCF(K, CH) = BLUN(S2)
                  K = K + 1
   50          CONTINUE
   60       CONTINUE
         ELSE
            K = 0
            DO 80 I = 0, 5
               DO 70 W = 0, 2
                  SCF(K, CH) = BLUN(S1)
                  K = K + 1
   70          CONTINUE
   80       CONTINUE
            DO 100 I = 6, 11
               DO 90 W = 0, 2
                  SCF(K, CH) = BLUN(S2)
                  K = K + 1
   90          CONTINUE
  100       CONTINUE
         END IF
      ELSE
         DO 120 G = 0, 3
            IF (G .LE. 1) THEN
               K = S1
            ELSE
               K = S2
            END IF
C           Granule two may inherit a group from granule one rather than
C           resend it.  Nothing is read in that case, which is why this
C           has to be decided before the bits are taken and not after.
            IF (GR .EQ. 2 .AND. SCFSI(G, CH) .EQ. 1) THEN
               USCF = USCF + 1
               DO 110 I = GS(G), GE(G)
                  SCF(I, CH) = SCFP(I, CH)
  110          CONTINUE
            ELSE
               DO 115 I = GS(G), GE(G)
                  SCF(I, CH) = BLUN(K)
  115          CONTINUE
            END IF
  120    CONTINUE
      END IF

C     Band 21 has no scalefactor of its own and is left at zero; the
C     requantiser reads band 20's for it, which is what the standard
C     says and not a convenience.
      IF (GR .EQ. 1) THEN
         DO 130 I = 0, 39
            SCFP(I, CH) = SCF(I, CH)
  130    CONTINUE
      END IF
      ISCALE(CH) = 0
      RETURN
      END

C     MPEG-2 and MPEG-2.5 scalefactors, ISO/IEC 13818-3 2.4.3.2.
C
C     ISTER says this is the second channel of a frame that has intensity
C     stereo switched on, in which case the field being read is not a set
C     of scalefactors but a set of pan positions, with its own six
C     partitionings and its own two-way split of the nine-bit
C     scalefac_compress into an eight-bit selector and one bit that says
C     how coarse the panning steps are.
      SUBROUTINE BLSF2(GR, CH, ISTER)
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER GR, CH, ISTER
      INTEGER NSFB(0:3,0:2,0:5)
      INTEGER SLEN(0:3)
      INTEGER C, I, J, K, N, TI, BI, BLUN
      EXTERNAL BLUN
      SAVE NSFB
C     How many scalefactors each of the four runs holds, by partitioning
C     (0 to 5) and by block shape (0 long, 1 short, 2 mixed).
      DATA NSFB /
     +     6,  5,  5,  5,    9,  9,  9,  9,    6,  9,  9,  9,
     +     6,  5,  7,  3,    9,  9, 12,  6,    6,  9, 12,  6,
     +    11, 10,  0,  0,   18, 18,  0,  0,   15, 18,  0,  0,
     +     7,  7,  7,  0,   12, 12, 12,  0,    6, 15, 12,  0,
     +     6,  6,  6,  3,   12,  9,  9,  6,    6, 12,  9,  6,
     +     8,  8,  5,  0,   15, 12,  9,  0,    6, 18,  9,  0 /

      DO 10 I = 0, 39
         SCF(I, CH) = 0
   10 CONTINUE
      ISCALE(CH) = 0
      C = SCFCMP(GR, CH)

      IF (ISTER .EQ. 0) THEN
         IF (C .LT. 400) THEN
            SLEN(0) = ISHFT(C, -4) / 5
            SLEN(1) = MOD(ISHFT(C, -4), 5)
            SLEN(2) = ISHFT(MOD(C, 16), -2)
            SLEN(3) = MOD(C, 4)
            TI = 0
         ELSE IF (C .LT. 500) THEN
            SLEN(0) = ISHFT(C - 400, -2) / 5
            SLEN(1) = MOD(ISHFT(C - 400, -2), 5)
            SLEN(2) = MOD(C - 400, 4)
            SLEN(3) = 0
            TI = 1
         ELSE
            SLEN(0) = (C - 500) / 3
            SLEN(1) = MOD(C - 500, 3)
            SLEN(2) = 0
            SLEN(3) = 0
            TI = 2
C           This is the one place the low sampling frequency extension
C           has pre-emphasis: it is not a bit in the side information as
C           it is in MPEG-1, it is implied by the partitioning.
            PREFLG(GR, CH) = 1
         END IF
      ELSE
         ISCALE(CH) = IAND(C, 1)
         C = ISHFT(C, -1)
         IF (C .LT. 180) THEN
            SLEN(0) = C / 36
            SLEN(1) = MOD(C, 36) / 6
            SLEN(2) = MOD(MOD(C, 36), 6)
            SLEN(3) = 0
            TI = 3
         ELSE IF (C .LT. 244) THEN
            SLEN(0) = ISHFT(MOD(C - 180, 64), -4)
            SLEN(1) = ISHFT(MOD(C - 180, 16), -2)
            SLEN(2) = MOD(C - 180, 4)
            SLEN(3) = 0
            TI = 4
         ELSE
            SLEN(0) = (C - 244) / 3
            SLEN(1) = MOD(C - 244, 3)
            SLEN(2) = 0
            SLEN(3) = 0
            TI = 5
         END IF
      END IF

      BI = 0
      IF (BLKTYP(GR, CH) .EQ. 2) THEN
         IF (MIXBLK(GR, CH) .EQ. 1) THEN
            BI = 2
         ELSE
            BI = 1
         END IF
      END IF

      J = 0
      DO 30 K = 0, 3
         N = NSFB(K, BI, TI)
         DO 20 I = 1, N
            IF (J .GT. 39) THEN
               BERR = 1
               RETURN
            END IF
            IF (SLEN(K) .GT. 0) THEN
               SCF(J, CH) = BLUN(SLEN(K))
            ELSE
               SCF(J, CH) = 0
            END IF
            J = J + 1
   20    CONTINUE
   30 CONTINUE
      RETURN
      END

C     The scalefactors of one granule and channel, whichever version.
      SUBROUTINE BLSCF(GR, CH, ISTER)
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER GR, CH, ISTER
      CALL BLSPLT(GR, CH)
      IF (HLSF .EQ. 0) THEN
         CALL BLSF1(GR, CH)
      ELSE
         CALL BLSF2(GR, CH, ISTER)
      END IF
      RETURN
      END
