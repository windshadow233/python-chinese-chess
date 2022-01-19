# Python-Chinese-Chess
这是一个使用纯Python编写的中国象棋库，改写自[Python-Chess](https://github.com/niklasf/python-chess)项目的核心部分。

![MIT](https://img.shields.io/github/license/windshadow233/python-chinese-chess?style=plastic)
![Python 3.7](https://img.shields.io/badge/Python-3.7-blue?style=plastic)

## 特点与说明
- 代码风格与主要功能和[Python-Chess](https://github.com/niklasf/python-chess)项目的核心部分高度相似
- 支持Python 3.7及以上的版本，且不依赖任何第三方库
- 采用坐标表示法表示棋子位置与着法
- 采用经典的棋盘与棋子的 svg 代码渲染UI
- 和棋判断默认采用60步自然限着、3次重复局面或子力不足(双方均无能过河的棋子)
- 由于中国象棋部分规则，例如长将、长捉、闲着等着法的判定尚无统一定论，且十分复杂，因此代码中没有对其进行实现。

## 基本操作
```python
>>> import cchess
>>> import cchess.svg

>>> board = cchess.Board()

>>> board.push(cchess.Move.from_uci("h2h4"))
>>> board.push(cchess.Move.from_uci('d9e8'))
>>> board.push(cchess.Move.from_uci('h4g4'))
>>> board.push(cchess.Move.from_uci('g6g5'))
>>> board.push(cchess.Move.from_uci('g4g9'))

>>> board.is_checkmate()
True

>>> board
Board('rnb1kaCnr/4a4/1c5c1/p1p1p3p/6p2/9/P1P1P1P1P/1C7/9/RNBAKABNR b - - 0 3')

>>> svg = cchess.svg.board(board, size=600, orientation=cchess.RED, lastmove=board.peek(), checkers=board.checkers())
>>> with open('images/board.svg', 'w') as f:
>>>    f.write(svg)
```

## 安装方法

```shell script
git clone https://github.com/windshadow233/python-chinese-chess.git
```

## 功能

- 简单的 svg 棋盘渲染，可以显示上一步（以一对红绿直角框标记始末位置）以及将军棋子（以棋子外的红圈示意）的位置。

<div align=center><img width="450" height="450" src="images/board.svg"/></div>

- 行棋、悔棋

```python
>>> board = cchess.Board()
>>> board.push(cchess.Move.from_uci("h2h4"))  # 行一步棋
>>> board.pop()  # 撤销上一步棋
Move.from_uci("h2h4")
```

- ASCII 棋盘
```python
>>> print(board)
r n b a k a b n r
. . . . . . . . .
. c . . . . . c .
p . p . p . p . p
. . . . . . . . .
. . . . . . . . .
P . P . P . P . P
. C . . . . . C .
. . . . . . . . .
R N B A K A B N R
```

- Unicode 棋盘
```python
>>> print(board.unicode(axes=True))
  ａｂｃｄｅｆｇｈｉ
9 車馬象士將士象馬車
8 ．．．．．．．．．
7 ．砲．．．．．砲．
6 卒．卒．卒．卒．卒
5 ．．．．．．．．．
4 ．．．．．．．．．
3 兵．兵．兵．兵．兵
2 ．炮．．．．．炮．
1 ．．．．．．．．．
0 俥傌相仕帥仕相傌俥
  ａｂｃｄｅｆｇｈｉ
```
- （伪）合法着法生成、合法性判断
```python
>>> board = cchess.Board()
>>> legal_moves = board.legal_moves
>>> board.legal_moves
<LegalMoveGenerator at ... (i3i4, g3g4, e3e4, c3c4, ...)>
>>> cchess.Move.from_uci("h2h8") in board.legal_moves
False
>>> cchess.Move.from_uci("h2h9") in board.legal_moves
True
>>> board.is_legal(cchess.Move.from_uci("h2h9"))
True

>>> board = cchess.Board('4k3R/2N2n3/5N3/9/9/9/9/9/9/3K5 b')
>>> board.pseudo_legal_moves
<PseudoLegalMoveGenerator at 0x7fd14c5f9510 (e9f9, e9d9, e9e8, f8h9, f8d9, f8h7, f8d7)>
>>> board.is_pseudo_legal(cchess.Move.from_uci("e9d9"))
True
>>> board.is_legal(cchess.Move.from_uci("e9d9"))
False
```

- 攻击（特殊情况：将军）检测
```python
>>> board= cchess.Board('4k4/2N6/9/9/9/9/9/9/9/3K5 b')
>>> board.is_attacked_by(cchess.RED, cchess.E9)
True
>>> board.is_check()
True
```

- 攻击者检测
```python
>>> board = cchess.Board('4k3R/2N2n3/5N3/9/9/9/9/9/9/3K5 b')
>>> attackers = board.attackers(cchess.RED, cchess.E9)
>>> attackers
SquareSet(0x20004000000000000000000)
>>> print(attackers)
. . . . . . . . 1
. . 1 . . . . . .
. . . . . . . . .
. . . . . . . . .
. . . . . . . . .
. . . . . . . . .
. . . . . . . . .
. . . . . . . . .
. . . . . . . . .
. . . . . . . . .
```

- 将杀、困毙、子力不足检测
```python
>>> board = cchess.Board('rnb1kaCnr/4a4/1c5c1/p1p1p3p/6p2/9/P1P1P1P1P/1C7/9/RNBAKABNR b - - 0 3')
>>> board.is_checkmate()
True

>>> board = cchess.Board('3k5/R8/9/9/9/9/9/9/9/4K4 b')
>>> board.is_checkmate()
False
>>> board.is_stalemate()
True

>>> board = cchess.Board('2b1k4/9/4b4/9/9/9/9/9/4A4/4KA3')
>>> board.is_insufficient_material()
True
```

- 局面合法性检验，包含棋子数量、棋子位置、将帅照面等情况
```python
>>> board = cchess.Board('3k5/R8/9/9/9/9/9/9/9/4K4')
>>> board.status()
<Status.VALID: 0>

>>> board = cchess.Board('4k4/9/9/9/9/9/9/9/9/4K4')
>>> board.is_white_face()
True
>>> board.status()
<Status.WHITE_FACE: 268435456>
```

- 重复局面检测
```python
>>> board.is_threefold_repetition()  # 一般比赛规定出现三次重复即不变作和
False
>>> n = 7
>>> board.is_repetition(n)  # 也可以根据情况任意指定不变作和需要达到的重复次数
False
```

- 自然限着检测
```python
>>> board.is_sixty_moves()  # 一般比赛规定60回合为自然限着数
False
>>> n = 75
>>> board.is_halfmoves(2 * n)  # 也可以根据情况任意指定限着数
False
```

- 终局判断
```python
>>> board = cchess.Board('rnb1kaCnr/4a4/1c5c1/p1p1p3p/6p2/9/P1P1P1P1P/1C7/9/RNBAKABNR b - - 0 3')
>>> board.is_game_over()  # 简单判断是否结束
True
>>> board.outcome()  # 棋局的结束状态(若非终局则返回None)
Outcome(termination=<Termination.CHECKMATE: 1>, winner=True)
```

## 待补充...
