import cchess
import cchess.svg
board = cchess.Board()
board.push(cchess.Move.from_uci("h2h4"))
board.push(cchess.Move.from_uci('d9e8'))
board.push(cchess.Move.from_uci('h4g4'))
board.push(cchess.Move.from_uci('g6g5'))
board.push(cchess.Move.from_uci('g4g9'))
svg = cchess.svg.board(board, size=600, orientation=cchess.RED, lastmove=board.peek(), checkers=board.checkers())
with open('board.svg', 'w') as f:
    f.write(svg)
