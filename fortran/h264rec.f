C     Dequantisation, the inverse transforms, and macroblock
C     reconstruction: clauses 8.5.10 through 8.5.13.
C
C     H.264's transforms are integer transforms, not approximations of a
C     DCT that happen to use integers.  Every step below is exact, which
C     is the whole reason a decoder can be checked for pixel-equality
C     against another decoder rather than for closeness.  That also means
C     there is no latitude anywhere here: a shift in the wrong direction
C     is not a slightly worse picture, it is a different picture.
C
C     Right shifts are SHIFTA and not ISHFT throughout.  Coefficients are
C     signed, the spec's >> is arithmetic, and Fortran's ISHFT is
C     logical; the two agree on every positive value and on nothing else.
C     Left shifts stay ISHFT, where the two are the same operation.

C     8.5.12.1 for a 4x4 block: multiply by the weighting matrix and the
C     normalisation for this QP, then shift by the QP's sixth.
      SUBROUTINE H2DQ4(LEV, WH, QP, BLK)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER LEV(0:63), WH, QP, BLK
      INTEGER I, M, S, LS
      M = MOD(QP, 6)
      S = QP / 6
      DO 10 I = 0, 15
         LS = W4(I + 1, WH) * NADJ4(M, I)
         IF (S .GE. 4) THEN
            COEF(I + 1, BLK) = ISHFT(LEV(I) * LS, S - 4)
         ELSE
            COEF(I + 1, BLK) = SHIFTA(LEV(I) * LS
     +                                + ISHFT(1, 3 - S), 4 - S)
         END IF
   10 CONTINUE
      RETURN
      END

C     8.5.13.1 for an 8x8 block.  The same shape with six where four was,
C     because the 8x8 transform has two more bits of gain in it.
      SUBROUTINE H2DQ8(LEV, QP, BLK)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER LEV(0:63), QP, BLK
      INTEGER I, M, S, LS
      M = MOD(QP, 6)
      S = QP / 6
      DO 10 I = 0, 63
         LS = W8(I + 1, 1) * NADJ8(M, I)
         IF (S .GE. 6) THEN
            CO8(I + 1, BLK) = ISHFT(LEV(I) * LS, S - 6)
         ELSE
            CO8(I + 1, BLK) = SHIFTA(LEV(I) * LS
     +                               + ISHFT(1, 5 - S), 6 - S)
         END IF
   10 CONTINUE
      RETURN
      END

C     8.5.10: the sixteen DC coefficients of an Intra_16x16 macroblock
C     get a 4x4 Hadamard transform of their own before they are scattered
C     back into the sixteen 4x4 blocks.  All the multiplications are by
C     one, so the transform is sixteen adds and no multiplies.
      SUBROUTINE H2DCY(LEV)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER LEV(0:63)
      INTEGER G(0:15), I, J, K, M, S, LS, V
      DO 10 I = 0, 3
         K = 4 * I
         G(K) = LEV(K) + LEV(K+1) + LEV(K+2) + LEV(K+3)
         G(K+1) = LEV(K) + LEV(K+1) - LEV(K+2) - LEV(K+3)
         G(K+2) = LEV(K) - LEV(K+1) - LEV(K+2) + LEV(K+3)
         G(K+3) = LEV(K) - LEV(K+1) + LEV(K+2) - LEV(K+3)
   10 CONTINUE
      M = MOD(QPY, 6)
      S = QPY / 6
      LS = W4(1, 1) * NADJ4(M, 0)
      DO 20 J = 0, 3
         DCY(J+1) = G(J) + G(J+4) + G(J+8) + G(J+12)
         DCY(J+5) = G(J) + G(J+4) - G(J+8) - G(J+12)
         DCY(J+9) = G(J) - G(J+4) - G(J+8) + G(J+12)
         DCY(J+13) = G(J) - G(J+4) + G(J+8) - G(J+12)
   20 CONTINUE
      DO 30 I = 1, 16
         V = DCY(I) * LS
         IF (S .GE. 6) THEN
            DCY(I) = ISHFT(V, S - 6)
         ELSE
            DCY(I) = SHIFTA(V + ISHFT(1, 5 - S), 6 - S)
         END IF
   30 CONTINUE
      RETURN
      END

C     8.5.11: the same idea for chroma, where 4:2:0 leaves only four DC
C     coefficients and the transform is a 2x2 Hadamard.
      SUBROUTINE H2DCC(LEV, C)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER LEV(0:63), C
      INTEGER F(4), I, M, S, LS, QP
      F(1) = LEV(0) + LEV(1) + LEV(2) + LEV(3)
      F(2) = LEV(0) - LEV(1) + LEV(2) - LEV(3)
      F(3) = LEV(0) + LEV(1) - LEV(2) - LEV(3)
      F(4) = LEV(0) - LEV(1) - LEV(2) + LEV(3)
      QP = QPCB
      IF (C .EQ. 2) QP = QPCR
      M = MOD(QP, 6)
      S = QP / 6
      LS = W4(1, C + 1) * NADJ4(M, 0)
      DO 10 I = 1, 4
         DCC(I, C) = SHIFTA(ISHFT(F(I) * LS, S), 5)
   10 CONTINUE
      RETURN
      END

C     8.5.12.2, the 4x4 inverse transform.  Rows then columns, four adds
C     and two halvings per pass, and a final rounding shift by six that
C     takes out the gain the two passes put in.
      SUBROUTINE H2IT4(D, R)
      IMPLICIT NONE
      INTEGER D(0:15), R(0:15)
      INTEGER F(0:15), I, J, K, E0, E1, E2, E3
      DO 10 I = 0, 3
         K = 4 * I
         E0 = D(K) + D(K+2)
         E1 = D(K) - D(K+2)
         E2 = SHIFTA(D(K+1), 1) - D(K+3)
         E3 = D(K+1) + SHIFTA(D(K+3), 1)
         F(K) = E0 + E3
         F(K+1) = E1 + E2
         F(K+2) = E1 - E2
         F(K+3) = E0 - E3
   10 CONTINUE
      DO 20 J = 0, 3
         E0 = F(J) + F(J+8)
         E1 = F(J) - F(J+8)
         E2 = SHIFTA(F(J+4), 1) - F(J+12)
         E3 = F(J+4) + SHIFTA(F(J+12), 1)
         R(J) = SHIFTA(E0 + E3 + 32, 6)
         R(J+4) = SHIFTA(E1 + E2 + 32, 6)
         R(J+8) = SHIFTA(E1 - E2 + 32, 6)
         R(J+12) = SHIFTA(E0 - E3 + 32, 6)
   20 CONTINUE
      RETURN
      END

C     8.5.13.2, the 8x8 inverse transform.  Twice the size and eight
C     times the butterfly, but the same two-pass shape and the same final
C     shift by six.
      SUBROUTINE H2IT8(D, R)
      IMPLICIT NONE
      INTEGER D(0:63), R(0:63)
      INTEGER F(0:63), I, J, K
      INTEGER E0, E1, E2, E3, E4, E5, E6, E7
      INTEGER G0, G1, G2, G3, G4, G5, G6, G7
      DO 10 I = 0, 7
         K = 8 * I
         E0 = D(K) + D(K+4)
         E1 = -D(K+3) + D(K+5) - D(K+7) - SHIFTA(D(K+7), 1)
         E2 = D(K) - D(K+4)
         E3 = D(K+1) + D(K+7) - D(K+3) - SHIFTA(D(K+3), 1)
         E4 = SHIFTA(D(K+2), 1) - D(K+6)
         E5 = -D(K+1) + D(K+7) + D(K+5) + SHIFTA(D(K+5), 1)
         E6 = D(K+2) + SHIFTA(D(K+6), 1)
         E7 = D(K+3) + D(K+5) + D(K+1) + SHIFTA(D(K+1), 1)
         G0 = E0 + E6
         G1 = E1 + SHIFTA(E7, 2)
         G2 = E2 + E4
         G3 = E3 + SHIFTA(E5, 2)
         G4 = E2 - E4
         G5 = SHIFTA(E3, 2) - E5
         G6 = E0 - E6
         G7 = E7 - SHIFTA(E1, 2)
         F(K) = G0 + G7
         F(K+1) = G2 + G5
         F(K+2) = G4 + G3
         F(K+3) = G6 + G1
         F(K+4) = G6 - G1
         F(K+5) = G4 - G3
         F(K+6) = G2 - G5
         F(K+7) = G0 - G7
   10 CONTINUE
      DO 20 J = 0, 7
         E0 = F(J) + F(J+32)
         E1 = -F(J+24) + F(J+40) - F(J+56) - SHIFTA(F(J+56), 1)
         E2 = F(J) - F(J+32)
         E3 = F(J+8) + F(J+56) - F(J+24) - SHIFTA(F(J+24), 1)
         E4 = SHIFTA(F(J+16), 1) - F(J+48)
         E5 = -F(J+8) + F(J+56) + F(J+40) + SHIFTA(F(J+40), 1)
         E6 = F(J+16) + SHIFTA(F(J+48), 1)
         E7 = F(J+24) + F(J+40) + F(J+8) + SHIFTA(F(J+8), 1)
         G0 = E0 + E6
         G1 = E1 + SHIFTA(E7, 2)
         G2 = E2 + E4
         G3 = E3 + SHIFTA(E5, 2)
         G4 = E2 - E4
         G5 = SHIFTA(E3, 2) - E5
         G6 = E0 - E6
         G7 = E7 - SHIFTA(E1, 2)
         R(J) = SHIFTA(G0 + G7 + 32, 6)
         R(J+8) = SHIFTA(G2 + G5 + 32, 6)
         R(J+16) = SHIFTA(G4 + G3 + 32, 6)
         R(J+24) = SHIFTA(G6 + G1 + 32, 6)
         R(J+32) = SHIFTA(G6 - G1 + 32, 6)
         R(J+40) = SHIFTA(G4 - G3 + 32, 6)
         R(J+48) = SHIFTA(G2 - G5 + 32, 6)
         R(J+56) = SHIFTA(G0 - G7 + 32, 6)
   20 CONTINUE
      RETURN
      END

C     Which neighbours a 4x4 luma block has.  Above and to the left are
C     easy; above-right is the awkward one, because in z-order the block
C     above and to the right has sometimes been decoded already and
C     sometimes has not.  Comparing the two z-order numbers answers that
C     without a table: if it comes earlier, it exists.
      SUBROUTINE H2AVL4(I)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER I, BX, BY
      BX = BLKX(I) / 4
      BY = BLKY(I) / 4
      PTOK = AVLB
      IF (BY .GT. 0) PTOK = 1
      PLOK = AVLA
      IF (BX .GT. 0) PLOK = 1
      IF (BX .GT. 0 .AND. BY .GT. 0) THEN
         PDOK = 1
      ELSE IF (BX .GT. 0) THEN
         PDOK = AVLB
      ELSE IF (BY .GT. 0) THEN
         PDOK = AVLA
      ELSE
         PDOK = AVLD
      END IF
      IF (BY .EQ. 0) THEN
         PTROK = AVLB
         IF (BX .EQ. 3) PTROK = AVLC
      ELSE IF (BX .EQ. 3) THEN
         PTROK = 0
      ELSE IF (ZORD(BX + 1, BY - 1) .LT. I) THEN
         PTROK = 1
      ELSE
         PTROK = 0
      END IF
      RETURN
      END

C     The same for an 8x8 luma block, where there are only four cases and
C     they are worth naming: the top left block borrows from above, the
C     top right from above-right, the bottom left from the block that was
C     just decoded, and the bottom right has nothing above-right at all.
      SUBROUTINE H2AVL8(K)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER K, BX, BY
      BX = MOD(K, 2)
      BY = K / 2
      PTOK = AVLB
      IF (BY .GT. 0) PTOK = 1
      PLOK = AVLA
      IF (BX .GT. 0) PLOK = 1
      IF (BX .GT. 0 .AND. BY .GT. 0) THEN
         PDOK = 1
      ELSE IF (BX .GT. 0) THEN
         PDOK = AVLB
      ELSE IF (BY .GT. 0) THEN
         PDOK = AVLA
      ELSE
         PDOK = AVLD
      END IF
      IF (K .EQ. 0) THEN
         PTROK = AVLB
      ELSE IF (K .EQ. 1) THEN
         PTROK = AVLC
      ELSE IF (K .EQ. 2) THEN
         PTROK = 1
      ELSE
         PTROK = 0
      END IF
      RETURN
      END

C     Predict and reconstruct one macroblock into the picture planes.
C     Intra_4x4 and Intra_8x8 have to interleave prediction with
C     reconstruction, block by block, because each block predicts from
C     the reconstructed samples of the one before it; Intra_16x16
C     predicts the whole macroblock first and only then adds residual.
      SUBROUTINE H2RECM
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER PRD(0:7,0:7), P16(0:15,0:15)
      INTEGER RES(0:63), DC(0:15)
      INTEGER BX, BY, I, K, X, Y, X0, Y0, R, V
      IF (CPCM .NE. 0) RETURN
      BX = CMBX * 16
      BY = CMBY * 16

      IF (CI16 .NE. 0) THEN
         PTOK = AVLB
         PLOK = AVLA
         PDOK = AVLD
         PTROK = 0
         CALL H2GATH(PY, MXW, BX, BY, 16, 0)
         CALL H2P16(CPRED, P16)
         DO 40 I = 0, 15
            X0 = BLKX(I)
            Y0 = BLKY(I)
C     8.5.10 scatters the transformed DC array back one value per 4x4
C     block, in the raster order of the blocks rather than their z-order.
            COEF(1, I + 1) = DCY((Y0 / 4) * 4 + X0 / 4 + 1)
            CALL H2IT4(COEF(1, I + 1), RES)
            DO 30 Y = 0, 3
               R = (BY + Y0 + Y) * MXW + BX + X0
               DO 20 X = 0, 3
                  V = P16(X0 + X, Y0 + Y) + RES(Y * 4 + X)
                  PY(R + X + 1) = MAX(0, MIN(255, V))
   20          CONTINUE
   30       CONTINUE
   40    CONTINUE
      ELSE IF (T8FLG .NE. 0) THEN
         DO 80 K = 0, 3
            X0 = MOD(K, 2) * 8
            Y0 = (K / 2) * 8
            CALL H2AVL8(K)
            CALL H2GATH(PY, MXW, BX + X0, BY + Y0, 8, 1)
            CALL H2F8
            CALL H2PNN(8, CI4(4 * K + 1), PRD)
            IF (CNZ(4 * K + 1) .GT. 0) THEN
               CALL H2IT8(CO8(1, K + 1), RES)
            ELSE
               DO 50 I = 0, 63
                  RES(I) = 0
   50          CONTINUE
            END IF
            DO 70 Y = 0, 7
               R = (BY + Y0 + Y) * MXW + BX + X0
               DO 60 X = 0, 7
                  V = PRD(X, Y) + RES(Y * 8 + X)
                  PY(R + X + 1) = MAX(0, MIN(255, V))
   60          CONTINUE
   70       CONTINUE
   80    CONTINUE
      ELSE
         DO 120 I = 0, 15
            X0 = BLKX(I)
            Y0 = BLKY(I)
            CALL H2AVL4(I)
            CALL H2GATH(PY, MXW, BX + X0, BY + Y0, 4, 1)
            CALL H2PNN(4, CI4(I + 1), PRD)
            IF (CNZ(I + 1) .GT. 0) THEN
               CALL H2IT4(COEF(1, I + 1), RES)
            ELSE
               DO 90 K = 0, 15
                  RES(K) = 0
   90          CONTINUE
            END IF
            DO 110 Y = 0, 3
               R = (BY + Y0 + Y) * MXW + BX + X0
               DO 100 X = 0, 3
                  V = PRD(X, Y) + RES(Y * 4 + X)
                  PY(R + X + 1) = MAX(0, MIN(255, V))
  100          CONTINUE
  110       CONTINUE
  120    CONTINUE
      END IF

      CALL H2RCH(PU, 1)
      CALL H2RCH(PV, 2)
      RETURN
      END

C     One chroma plane of one macroblock.  Chroma is always predicted
C     8x8 as a whole, whatever the luma did, so there is no interleaving
C     to do here.
      SUBROUTINE H2RCH(P, C)
      IMPLICIT NONE
      INCLUDE 'h264com.inc'
      INTEGER P(*), C
      INTEGER PRD(0:7,0:7), RES(0:63)
      INTEGER STR, CX, CY, I, K, X, Y, X0, Y0, B, R, V
      STR = MXW / 2
      CX = CMBX * 8
      CY = CMBY * 8
      PTOK = AVLB
      PLOK = AVLA
      PDOK = AVLD
      PTROK = 0
      CALL H2GATH(P, STR, CX, CY, 8, 0)
      CALL H2PCH(CCPM, PRD)
      DO 50 I = 0, 3
         B = 16 + 4 * (C - 1) + I + 1
         X0 = MOD(I, 2) * 4
         Y0 = (I / 2) * 4
         COEF(1, B) = DCC(I + 1, C)
         IF (CNZ(B) .GT. 0 .OR. COEF(1, B) .NE. 0) THEN
            CALL H2IT4(COEF(1, B), RES)
         ELSE
            DO 10 K = 0, 15
               RES(K) = 0
   10       CONTINUE
         END IF
         DO 30 Y = 0, 3
            R = (CY + Y0 + Y) * STR + CX + X0
            DO 20 X = 0, 3
               V = PRD(X0 + X, Y0 + Y) + RES(Y * 4 + X)
               P(R + X + 1) = MAX(0, MIN(255, V))
   20       CONTINUE
   30    CONTINUE
   50 CONTINUE
      RETURN
      END
