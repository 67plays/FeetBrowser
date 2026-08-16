C     Stereo, reordering and alias reduction: everything that happens to
C     the spectrum between requantisation and the transform.
C
C     The order of these three matters and is not obvious.  The stereo
C     tools run first and run over the coefficients in the order the
C     bitstream sent them, because that is the order the scalefactor
C     bands are in.  A short block is then reordered from band order into
C     the transform's window order.  Alias reduction runs last, on
C     subband boundaries, which only exist once the reordering has put
C     the coefficients where the subbands are.

C     Copy the requantised spectrum into the working array.  They are
C     kept apart so that a test can recompute the requantisation from the
C     decoder's own quantised values and scalefactors and compare exactly
C     -- which it could not do if the stereo tools had already been over
C     the top of it.
      SUBROUTINE BLXCPY(CH)
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER CH, I
      DO 10 I = 0, MXSMP - 1
         XR(I, CH) = XRQ(I, CH)
   10 CONTINUE
      RETURN
      END

C     Mid/side over a run of coefficients.  The standard's normalisation
C     is a factor of one over root two on both outputs; some decoders
C     fold it into the requantisation gain instead, which comes to the
C     same thing and makes intensity stereo have to undo it.  Doing it
C     here costs one multiply and keeps the two tools independent.
      SUBROUTINE BLMS(J0, N)
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER J0, N, I
      DOUBLE PRECISION M, S, R
      R = 1.0D0 / DSQRT(2.0D0)
      DO 10 I = J0, J0 + N - 1
         IF (I .LT. 0 .OR. I .GE. MXSMP) GOTO 10
         M = XR(I, 1)
         S = XR(I, 2)
         XR(I, 1) = (M + S) * R
         XR(I, 2) = (M - S) * R
         UMSB = UMSB + 1
   10 CONTINUE
      RETURN
      END

C     The pan ratios for one intensity position.
C
C     MPEG-1 pans by an angle: position p stands for a ratio of tan(p pi
C     / 12) between the channels, and the two gains are that ratio and
C     one, each over their sum.  Position 6 is a right angle, where the
C     ratio is infinite and the gains are one and zero; it is a separate
C     case here for the same reason it is in the standard.
C
C     The low sampling frequency extension pans by a power instead: one
C     channel keeps the energy and the other is attenuated by a quarter
C     or a half power of two per step, intensity_scale saying which.  Odd
C     and even positions swap which channel is attenuated, so a position
C     of one and a position of two are the same distance from centre in
C     opposite directions.
      SUBROUTINE BLISK(P, KL, KR, OK)
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER P, OK
      DOUBLE PRECISION KL, KR
      INTEGER M
      OK = 1
      KL = 1.0D0
      KR = 1.0D0
      IF (HLSF .EQ. 0) THEN
         IF (P .LT. 0 .OR. P .GT. 6) THEN
            OK = 0
            RETURN
         END IF
         KL = ISL(P)
         KR = ISR(P)
      ELSE
         IF (P .LT. 0 .OR. P .GT. 15) THEN
            OK = 0
            RETURN
         END IF
         M = ISHFT(P + 1, -1)
         M = ISHFT(M, ISCALE(2))
         IF (M .GT. 511) M = 511
         IF (IAND(P, 1) .EQ. 1) THEN
            KL = QPOW(512 - M)
            KR = 1.0D0
         ELSE
            KL = 1.0D0
            KR = QPOW(512 - M)
         END IF
      END IF
      RETURN
      END

C     Intensity stereo over a run of coefficients: the second channel
C     carries no spectrum at all up here, only a position, and both
C     outputs are the first channel's spectrum panned to it.
      SUBROUTINE BLIS(J0, N, P, DONE)
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER J0, N, P, DONE
      INTEGER I, OK
      DOUBLE PRECISION KL, KR, M
      CALL BLISK(P, KL, KR, OK)
      IF (OK .EQ. 0) THEN
         DONE = 0
         RETURN
      END IF
      DONE = 1
      DO 10 I = J0, J0 + N - 1
         IF (I .LT. 0 .OR. I .GE. MXSMP) GOTO 10
         M = XR(I, 1)
         XR(I, 1) = M * KL
         XR(I, 2) = M * KR
         UISB = UISB + 1
   10 CONTINUE
      RETURN
      END

C     The stereo tools of one granule.
C
C     Intensity stereo does not apply to a fixed part of the spectrum: it
C     applies to every band above the highest one the second channel
C     actually coded anything in.  So the bands are walked from the top
C     down, and the first band with a non-zero second channel ends it --
C     separately for each of a short block's three windows, because they
C     are independent.  Below that point mid/side applies if it is on,
C     which is how a joint stereo frame comes to use both tools at once
C     on different parts of the same granule.
      SUBROUTINE BLSTER(GR)
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER GR
      INTEGER I, J, K, L, W, LEN, NZ(0:2), NZL, P, SFMAX, DONE
      INTEGER JB

      IF (HNCH .NE. 2) RETURN
      IF (HMODE .NE. 1) RETURN

      IF (HIS .EQ. 0) THEN
         IF (HMS .NE. 0) CALL BLMS(0, MXSMP)
         RETURN
      END IF

      SFMAX = 7
      IF (HLSF .EQ. 1) SFMAX = 16

      NZ(0) = 0
      NZ(1) = 0
      NZ(2) = 0
      J = MXSMP
C     One past the last short scalefactor, so that the first step back
C     lands on band 11's -- band 12 has no scalefactor of its own and
C     borrows band 11's, exactly as long band 21 borrows band 20's.
      K = LONGE(2) + 3 * (13 - SHRTS(2)) - 3
      DO 40 I = 12, SHRTS(2), -1
         IF (I .NE. 11) K = K - 3
         LEN = SBS(I + 1, HSFI) - SBS(I, HSFI)
         DO 30 L = 2, 0, -1
            J = J - LEN
            IF (NZ(L) .EQ. 0) THEN
               DO 10 W = 0, LEN - 1
                  JB = J + W
                  IF (JB .GE. 0 .AND. JB .LT. MXSMP) THEN
                     IF (IS(JB, 2) .NE. 0) NZ(L) = 1
                  END IF
   10          CONTINUE
            END IF
            IF (NZ(L) .EQ. 0) THEN
               P = -1
               IF (K + L .GE. 0 .AND. K + L .LE. 39) P = SCF(K + L, 2)
               DONE = 0
               IF (P .GE. 0 .AND. P .LT. SFMAX) CALL BLIS(J, LEN, P,
     +                                                    DONE)
               IF (DONE .EQ. 0 .AND. HMS .NE. 0) CALL BLMS(J, LEN)
            ELSE
               IF (HMS .NE. 0) CALL BLMS(J, LEN)
            END IF
   30    CONTINUE
   40 CONTINUE

      NZL = IOR(IOR(NZ(0), NZ(1)), NZ(2))
      DO 70 I = LONGE(2) - 1, 0, -1
         LEN = SBL(I + 1, HSFI) - SBL(I, HSFI)
         J = J - LEN
         IF (NZL .EQ. 0) THEN
            DO 50 W = 0, LEN - 1
               JB = J + W
               IF (JB .GE. 0 .AND. JB .LT. MXSMP) THEN
                  IF (IS(JB, 2) .NE. 0) NZL = 1
               END IF
   50       CONTINUE
         END IF
         IF (NZL .EQ. 0) THEN
            K = I
            IF (K .GT. 20) K = 20
            P = SCF(K, 2)
            DONE = 0
            IF (P .LT. SFMAX) CALL BLIS(J, LEN, P, DONE)
            IF (DONE .EQ. 0 .AND. HMS .NE. 0) CALL BLMS(J, LEN)
         ELSE
            IF (HMS .NE. 0) CALL BLMS(J, LEN)
         END IF
   70 CONTINUE
      RETURN
      END

C     A short block's coefficients arrive band by band, all three windows
C     of a band together, and the transform wants them subband by
C     subband.  The short scalefactor bands are laid out so that three
C     times their widths tile the transform's eighteen-sample blocks
C     exactly, which is what makes this a permutation and not a resample.
      SUBROUTINE BLREOR(CH)
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER CH
      INTEGER I, J, W, LEN, P
C     One band of three windows at a time; the widest short band in any
C     of the nine layouts is 56 coefficients.
      DOUBLE PRECISION T(0:255)
      IF (SHRTS(CH) .GE. 13) RETURN
      P = SBL(LONGE(CH), HSFI)
      DO 30 I = SHRTS(CH), 12
         LEN = SBS(I + 1, HSFI) - SBS(I, HSFI)
         IF (LEN .LT. 0 .OR. 3 * LEN .GT. 256) RETURN
         IF (P + 3 * LEN .GT. MXSMP) RETURN
         DO 20 J = 0, LEN - 1
            DO 10 W = 0, 2
               T(3 * J + W) = XR(P + W * LEN + J, CH)
   10       CONTINUE
   20    CONTINUE
         DO 25 J = 0, 3 * LEN - 1
            XR(P + J, CH) = T(J)
   25    CONTINUE
         P = P + 3 * LEN
   30 CONTINUE
      RETURN
      END

C     Alias reduction: eight butterflies across every subband boundary,
C     undoing the overlap the analysis filterbank left behind.
C
C     A short block has none, because its transform is too short for the
C     aliasing to be there in the first place; a mixed block has one, at
C     the single boundary between its long part and its short part.  That
C     one butterfly is the whole of the difference, and it is the reason
C     mixed blocks cannot be treated as short ones with a long bottom.
      SUBROUTINE BLALIA(GR, CH)
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER GR, CH
      INTEGER SB, I, N, A, B
      DOUBLE PRECISION X1, X2
      IF (BLKTYP(GR, CH) .EQ. 2) THEN
         IF (MIXBLK(GR, CH) .EQ. 0) RETURN
         N = 1
      ELSE
         N = MXSB - 1
      END IF
      DO 20 SB = 1, N
         DO 10 I = 0, 7
            A = 18 * SB - 1 - I
            B = 18 * SB + I
            X1 = XR(A, CH)
            X2 = XR(B, CH)
            XR(A, CH) = X1 * CS(I) - X2 * CA(I)
            XR(B, CH) = X2 * CS(I) + X1 * CA(I)
   10    CONTINUE
   20 CONTINUE
      RETURN
      END
