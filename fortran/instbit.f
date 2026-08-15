C     The bitstream reader, and the Huffman decoder built on it.
C
C     AAC has one reader and one cursor: every syntax element in a
C     raw_data_block is a fixed number of bits or a codeword, read most
C     significant bit first, with no start codes, no emulation prevention
C     and no escape from a wrong turn.  That last part is why BERR
C     exists.  A read past the end of the buffer returns zero and sets
C     it, every loop that could run away checks it, and the frame is
C     refused; the alternative is a decoder that reads a corrupt stream
C     for as long as the corruption keeps looking like data.

C     One byte of the frame, unsigned.  INTEGER*1 is signed in Fortran,
C     so the mask is not decoration.
      INTEGER FUNCTION IPBYT(I)
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER I
      IPBYT = IAND(INT(FBUF(I)), 255)
      RETURN
      END

C     One bit.
      INTEGER FUNCTION IPU1()
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER B, IPBYT
      EXTERNAL IPBYT
      IF (BPOS .GE. BNBIT) THEN
         BERR = 1
         IPU1 = 0
      ELSE
         B = IPBYT(BPOS / 8 + 1)
         IPU1 = IAND(ISHFT(B, -(7 - MOD(BPOS, 8))), 1)
         BPOS = BPOS + 1
      END IF
      RETURN
      END

C     N bits, most significant first, for N up to 31.  Reading them one
C     at a time is a little slower than assembling them from bytes and a
C     great deal harder to get wrong at the two ends; the profile says
C     the decoder's time goes on the transform, not here.
      INTEGER FUNCTION IPUN(N)
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER N, I, V, IPU1
      EXTERNAL IPU1
      V = 0
      DO 10 I = 1, N
         V = IOR(ISHFT(V, 1), IPU1())
   10 CONTINUE
      IPUN = V
      RETURN
      END

C     Step over N bits without looking at them -- data_stream_element
C     payloads, fill elements, the parts of a program config element we
C     do not use.  Skipping past the end is still an error, because the
C     count came from the stream.
      SUBROUTINE IPSKIP(N)
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER N
      IF (N .LT. 0) THEN
         BERR = 1
         RETURN
      END IF
      IF (BPOS + N .GT. BNBIT) THEN
         BERR = 1
         BPOS = BNBIT
         RETURN
      END IF
      BPOS = BPOS + N
      RETURN
      END

C     Forward to the next byte boundary.
      SUBROUTINE IPALGN
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      IF (MOD(BPOS, 8) .NE. 0) CALL IPSKIP(8 - MOD(BPOS, 8))
      RETURN
      END

C     Bits not yet read.
      INTEGER FUNCTION IPLEFT()
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      IPLEFT = BNBIT - BPOS
      IF (IPLEFT .LT. 0) IPLEFT = 0
      RETURN
      END

C     -- Huffman ----------------------------------------------------------

C     Walk codebook B's tree until a leaf, and return the entry number
C     there.  The tables are prefix free and their Kraft sums are one, so
C     the tree is complete and every path reaches a leaf; the depth cap
C     is there for the stream that runs out of bits half way down, where
C     IPU1 returns zeroes forever and the walk would otherwise stay in
C     the tree.
      INTEGER FUNCTION IPHCW(B)
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER B, P, D, IPU1
      EXTERNAL IPU1
      P = HROOT(B)
      DO 10 D = 1, 20
         IF (IPU1() .EQ. 0) THEN
            P = HTL(P)
         ELSE
            P = HTR(P)
         END IF
         IF (P .EQ. 0) GOTO 20
         IF (HTV(P) .GE. 0) THEN
            IPHCW = HTV(P)
            RETURN
         END IF
         IF (BERR .NE. 0) GOTO 20
   10 CONTINUE
   20 BERR = 1
      IPHCW = 0
      RETURN
      END

C     The values an entry number stands for.  The standard lists each
C     codebook's entries in the order of the tuples they code, so the
C     tuple is the entry number written in base HMOD with HDIM digits,
C     most significant first, shifted down by the largest absolute value
C     where the codebook codes its signs in the tuple rather than in bits
C     after the codeword.
      SUBROUTINE IPHVAL(B, IDX, V)
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER B, IDX, V(4)
      INTEGER I, X, M
      X = IDX
      M = HMOD(B)
      DO 10 I = HDIM(B), 1, -1
         V(I) = MOD(X, M)
         X = X / M
         IF (HUNS(B) .EQ. 0) V(I) = V(I) - HLAV(B)
   10 CONTINUE
      RETURN
      END

C     The escape sequence of codebook 11, which is the only way a
C     magnitude above 15 is coded: a run of ones giving the width, a
C     zero, and then that many bits.  The largest magnitude the standard
C     allows is 8191, so a width past 13 is a corrupt stream and not a
C     very loud one.
      INTEGER FUNCTION IPHESC()
      IMPLICIT NONE
      INCLUDE 'instcom.inc'
      INTEGER N, W, IPU1, IPUN
      EXTERNAL IPU1, IPUN
      N = 0
   10 IF (IPU1() .EQ. 0) GOTO 20
         N = N + 1
         IF (N .GT. 16 .OR. BERR .NE. 0) THEN
            BERR = 1
            IPHESC = 0
            RETURN
         END IF
         GOTO 10
   20 CONTINUE
      W = N + 4
      IF (W .GT. 13) THEN
         BERR = 1
         IPHESC = 0
         RETURN
      END IF
      IPHESC = ISHFT(1, W) + IPUN(W)
      IF (IPHESC .GT. MXQNT) THEN
         BERR = 1
         IPHESC = 0
      END IF
      RETURN
      END
