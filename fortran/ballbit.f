C     The two bitstream readers, the reservoir they sit on, and the
C     Huffman codeword walk.
C
C     Layer III needs two cursors rather than one.  The header, the CRC
C     and the side information are fixed width fields at the front of the
C     frame that is in hand, and are read from it in place.  The main
C     data is not in that frame: it is in the reservoir, a running buffer
C     of every frame's payload with the headers taken out, and a
C     granule's bits may have arrived up to three frames ago.  Sharing
C     one cursor between the two would mean the side information reader
C     could walk into main data belonging to some other frame, which is
C     exactly the failure the reservoir invites.
C
C     Both readers set an error flag rather than trap.  A read past the
C     end returns zero and sets it, every loop that could run away checks
C     it, and the granule is abandoned; the alternative is a decoder that
C     reads a corrupt stream for as long as the corruption keeps looking
C     like data.

C     -- the frame in hand ------------------------------------------------

C     One byte of the frame, unsigned.  INTEGER*1 is signed in Fortran,
C     so the mask is not decoration.
      INTEGER FUNCTION BLFBYT(I)
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER I
      IF (I .LT. 1 .OR. I .GT. FN) THEN
         HERR = 1
         BLFBYT = 0
      ELSE
         BLFBYT = IAND(INT(FBUF(I)), 255)
      END IF
      RETURN
      END

C     One bit of the frame.
      INTEGER FUNCTION BLH1()
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER B, BLFBYT
      EXTERNAL BLFBYT
      IF (HPOS .GE. FN * 8) THEN
         HERR = 1
         BLH1 = 0
      ELSE
         B = BLFBYT(HPOS / 8 + 1)
         BLH1 = IAND(ISHFT(B, -(7 - MOD(HPOS, 8))), 1)
         HPOS = HPOS + 1
      END IF
      RETURN
      END

C     N bits of the frame, most significant first.  Every field the frame
C     header and the side information hold is 12 bits or fewer, so there
C     is no width to worry about; reading them a bit at a time is a
C     little slower than assembling them from bytes and a great deal
C     harder to get wrong at the two ends.
      INTEGER FUNCTION BLHN(N)
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER N, I, V, BLH1
      EXTERNAL BLH1
      V = 0
      DO 10 I = 1, N
         V = IOR(ISHFT(V, 1), BLH1())
   10 CONTINUE
      BLHN = V
      RETURN
      END

C     -- the reservoir ----------------------------------------------------

C     Throw the reservoir away.  Done on reset and on a seek, and the
C     reason a decoder that is handed a frame from the middle of a file
C     produces something quieter than it should for a frame or two: the
C     bits the first granules refer to are genuinely not there.
      SUBROUTINE BLRCLR
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      RN = 0
      BPOS = 0
      BNBIT = 0
      BERR = 0
      RETURN
      END

C     Append this frame's main data -- everything after the header, the
C     optional CRC and the side information -- to the reservoir, and
C     point the cursor at where the granule the side information
C     describes actually begins.
C
C     OK comes back zero when main_data_begin reaches further back than
C     the reservoir holds.  That is not corruption and not an error: it
C     is what every stream looks like at its first frame, and what a
C     stream looks like for a frame or two after a seek.  The caller
C     emits silence for the frame and carries on, because the frame's own
C     payload still has to go into the reservoir for the frames that come
C     after it to read.  Dropping the frame instead would lose a granule
C     of alignment and every later granule with it.
      SUBROUTINE BLRADD(OK)
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER OK
      INTEGER SKIPB, MDLEN, I, START
      OK = 1
      SKIPB = 4 + HSIDE
      IF (HPROT .EQ. 0) SKIPB = SKIPB + 2
      MDLEN = FN - SKIPB
      IF (MDLEN .LT. 0) MDLEN = 0
C     Nothing a header can say makes the reservoir grow without bound:
C     the trim below leaves at most 511 bytes, and one frame is at most
C     1441, so RN + MDLEN cannot reach MXRES.  The check is here because
C     an arithmetic slip upstream would otherwise be a memory fault
C     rather than a decode error.
      IF (RN + MDLEN .GT. MXRES) THEN
         CALL BLRCLR
         OK = 0
         RETURN
      END IF
      START = RN - MDBEG
      DO 10 I = 1, MDLEN
         RBUF(RN + I) = FBUF(SKIPB + I)
   10 CONTINUE
      RN = RN + MDLEN
      BNBIT = RN * 8
      IF (START .LT. 0) THEN
C        The granule points behind everything we have.  Keep the payload,
C        report the starvation, and let the caller produce silence.
         USTARV = USTARV + 1
         BPOS = BNBIT
         OK = 0
      ELSE
         BPOS = START * 8
      END IF
      BERR = 0
      IF (MDBEG .GT. UBACK) UBACK = MDBEG
      IF (MDBEG .GT. 0) URES = URES + 1
      RETURN
      END

C     Drop everything the next frame cannot reach.  main_data_begin is
C     nine bits at most, so no future granule can look further back than
C     511 bytes; keeping more would be a slow leak and keeping fewer
C     would break the frames that look furthest back.
      SUBROUTINE BLRTRM
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER I, KEEP, DROP
      KEEP = 511
      IF (RN .LE. KEEP) RETURN
      DROP = RN - KEEP
      DO 10 I = 1, KEEP
         RBUF(I) = RBUF(DROP + I)
   10 CONTINUE
      RN = KEEP
      BNBIT = RN * 8
      BPOS = BNBIT
      RETURN
      END

C     One bit of main data.
      INTEGER FUNCTION BLU1()
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER B
      IF (BPOS .GE. BNBIT) THEN
         BERR = 1
         BLU1 = 0
      ELSE
         B = IAND(INT(RBUF(BPOS / 8 + 1)), 255)
         BLU1 = IAND(ISHFT(B, -(7 - MOD(BPOS, 8))), 1)
         BPOS = BPOS + 1
      END IF
      RETURN
      END

C     N bits of main data, most significant first, for N up to 31.
      INTEGER FUNCTION BLUN(N)
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER N, I, V, BLU1
      EXTERNAL BLU1
      V = 0
      DO 10 I = 1, N
         V = IOR(ISHFT(V, 1), BLU1())
   10 CONTINUE
      BLUN = V
      RETURN
      END

C     Move the main data cursor to an absolute bit position.  This is how
C     a granule is left after decoding: the side information said how
C     many bits it occupies, so the next granule starts there whatever
C     the Huffman decoder thought.  A stream where the two disagree is
C     still decodable from the next granule on, which is worth more than
C     being right about where this one ended.
      SUBROUTINE BLSEEK(P)
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER P
      IF (P .LT. 0) THEN
         BPOS = 0
      ELSE IF (P .GT. BNBIT) THEN
         BPOS = BNBIT
      ELSE
         BPOS = P
      END IF
      RETURN
      END

C     Bits of main data not yet read.
      INTEGER FUNCTION BLLEFT()
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      BLLEFT = BNBIT - BPOS
      IF (BLLEFT .LT. 0) BLLEFT = 0
      RETURN
      END

C     -- Huffman ----------------------------------------------------------

C     Walk tree T until a leaf, and return the entry number there.  T is
C     an index into HROOT: 0 to 15 are the sixteen distinct big-value
C     trees and 16 and 17 the two count1 quadruple tables.
C
C     Several of the big-value tables are not complete -- their Kraft
C     sums fall short of one -- so a path can reach a node with no child
C     on the bit that was read.  That is a corrupt granule rather than a
C     value, and it takes the same exit as running out of bits.  The
C     depth cap is 24 against a longest codeword of 19, and exists for
C     the stream that runs out of bits half way down, where BLU1 returns
C     zeroes forever and the walk would otherwise stay in the tree.
      INTEGER FUNCTION BLHCW(T)
      IMPLICIT NONE
      INCLUDE 'ballcom.inc'
      INTEGER T, P, D, BLU1
      EXTERNAL BLU1
      P = HROOT(T)
      DO 10 D = 1, 24
         IF (BLU1() .EQ. 0) THEN
            P = HTL(P)
         ELSE
            P = HTR(P)
         END IF
         IF (P .EQ. 0) GOTO 20
         IF (HTV(P) .GE. 0) THEN
            BLHCW = HTV(P)
            RETURN
         END IF
         IF (BERR .NE. 0) GOTO 20
   10 CONTINUE
   20 BERR = 1
      BLHCW = 0
      RETURN
      END
