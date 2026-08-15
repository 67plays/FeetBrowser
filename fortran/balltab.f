C     The tables that are derivable, worked out once per process.
C
C     Most of what an MP3 decoder needs is not a table at all: the
C     transform's cosines, its four window shapes, the alias
C     coefficients, the intensity ratios and the two power tables all
C     follow from formulas in the standard, and a list of them copied out
C     of a document is a list that can be copied wrong.  Computing them
C     costs about a millisecond at startup and cannot disagree with the
C     definition it came from.
C
C     What is left -- the Huffman code tables, the scalefactor band
C     boundaries, the pre-emphasis table and the 512-tap synthesis window
C     -- has no formula and is tabulated next door in balldat.f.

      SUBROUTINE BLTINI
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      IF (TABOK .EQ. 12345) RETURN
      CALL BLTDAT
      CALL BLTPOW
      CALL BLTMDC
      CALL BLTSYN
      CALL BLTMSC
      TABOK = 12345
      RETURN
      END

C     x to the four thirds for every magnitude a coefficient can carry,
C     and two to a quarter power for every exponent the gains can build.
C     Both are lookups because requantisation runs once per coefficient,
C     576 times a granule per channel, and a pow() there is the whole
C     cost of the stage.
      SUBROUTINE BLTPOW
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER I
      X43(0) = 0.0D0
      DO 10 I = 1, MXQNT
         X43(I) = DBLE(I) ** (4.0D0 / 3.0D0)
   10 CONTINUE
      DO 20 I = 0, 1023
         QPOW(I) = 2.0D0 ** (DBLE(I - 512) / 4.0D0)
   20 CONTINUE
      RETURN
      END

C     The inverse MDCT's cosines and its four window shapes, ISO/IEC
C     11172-3 2.4.3.4.10.
C
C     The start and stop windows are the ones that make window switching
C     work: a start window is a long window whose falling half has been
C     replaced by a short one padded with silence, so that it overlaps
C     correctly with the short blocks that follow it, and a stop window
C     is the same thing reflected.  Their flat and zero runs are not
C     approximations of anything -- they are exactly one and exactly
C     zero, and writing them as such is what makes the switch lossless.
      SUBROUTINE BLTMDC
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER I, K
      DOUBLE PRECISION PI
      PI = 4.0D0 * DATAN(1.0D0)

      DO 20 I = 0, 35
         DO 10 K = 0, 17
            CIM(I, K) = DCOS(PI / 72.0D0 * DBLE(2 * I + 1 + 18)
     +                       * DBLE(2 * K + 1))
   10    CONTINUE
   20 CONTINUE
      DO 40 I = 0, 11
         DO 30 K = 0, 5
            CIS(I, K) = DCOS(PI / 24.0D0 * DBLE(2 * I + 1 + 6)
     +                       * DBLE(2 * K + 1))
   30    CONTINUE
   40 CONTINUE

      DO 50 I = 0, 35
         IMDW(I, 0) = DSIN(PI / 36.0D0 * (DBLE(I) + 0.5D0))
         IMDW(I, 1) = 0.0D0
         IMDW(I, 2) = 0.0D0
         IMDW(I, 3) = 0.0D0
   50 CONTINUE

      DO 60 I = 0, 17
         IMDW(I, 1) = DSIN(PI / 36.0D0 * (DBLE(I) + 0.5D0))
   60 CONTINUE
      DO 70 I = 18, 23
         IMDW(I, 1) = 1.0D0
   70 CONTINUE
      DO 80 I = 24, 29
         IMDW(I, 1) = DSIN(PI / 12.0D0 * (DBLE(I - 18) + 0.5D0))
   80 CONTINUE

      DO 90 I = 0, 11
         IMDW(I, 2) = DSIN(PI / 12.0D0 * (DBLE(I) + 0.5D0))
   90 CONTINUE

      DO 100 I = 6, 11
         IMDW(I, 3) = DSIN(PI / 12.0D0 * (DBLE(I - 6) + 0.5D0))
  100 CONTINUE
      DO 110 I = 12, 17
         IMDW(I, 3) = 1.0D0
  110 CONTINUE
      DO 120 I = 18, 35
         IMDW(I, 3) = DSIN(PI / 36.0D0 * (DBLE(I) + 0.5D0))
  120 CONTINUE
      RETURN
      END

C     The synthesis filterbank's matrixing coefficients, and the map from
C     the standard's V vector to its U vector.
      SUBROUTINE BLTSYN
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER I, K
      DOUBLE PRECISION PI
      PI = 4.0D0 * DATAN(1.0D0)
      DO 20 I = 0, 63
         DO 10 K = 0, 31
            NMAT(I, K) = DCOS(DBLE(16 + I) * DBLE(2 * K + 1)
     +                        * PI / 64.0D0)
   10    CONTINUE
   20 CONTINUE
      DO 40 I = 0, 7
         DO 30 K = 0, 31
            UMAP(64 * I + K) = 128 * I + K
            UMAP(64 * I + 32 + K) = 128 * I + 96 + K
   30    CONTINUE
   40 CONTINUE
      RETURN
      END

C     Alias reduction and intensity stereo.
C
C     The eight alias coefficients are the standard's c[i]; cs and ca are
C     the butterfly's cosine and sine, normalised so that the pair is a
C     rotation and the operation is invertible, which is the whole point
C     of it.
C
C     The MPEG-1 intensity ratios are tangents of multiples of fifteen
C     degrees, split between the two channels so that they sum to one.
C     Position six is a right angle and the tangent is infinite; the
C     limit is one and zero, and that is written directly rather than
C     computed, because computing it would be a division by something
C     that is only nearly zero.
      SUBROUTINE BLTMSC
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER I
      DOUBLE PRECISION C(0:7), R, PI
      SAVE C
      DATA C / -0.6D0, -0.535D0, -0.33D0, -0.185D0,
     +         -0.095D0, -0.041D0, -0.0142D0, -0.0037D0 /
      PI = 4.0D0 * DATAN(1.0D0)
      DO 10 I = 0, 7
         R = DSQRT(1.0D0 + C(I) * C(I))
         CS(I) = 1.0D0 / R
         CA(I) = C(I) / R
   10 CONTINUE
      DO 20 I = 0, 5
         R = DTAN(DBLE(I) * PI / 12.0D0)
         ISL(I) = R / (1.0D0 + R)
         ISR(I) = 1.0D0 / (1.0D0 + R)
   20 CONTINUE
      ISL(6) = 1.0D0
      ISR(6) = 0.0D0
      RETURN
      END
