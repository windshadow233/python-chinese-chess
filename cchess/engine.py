from __future__ import annotations

import abc
import asyncio
import collections
import concurrent.futures
import contextlib
import copy
import dataclasses
import enum
import logging
import math
import shlex
import subprocess
import sys
import threading
import time
import typing
import re

import cchess

from cchess import Color
from types import TracebackType
from typing import Any, Callable, Coroutine, Deque, Dict, Generator, Generic, Iterable, Iterator, List, Literal, Mapping, MutableMapping, Optional, Tuple, Type, TypedDict, TypeVar, Union

if typing.TYPE_CHECKING:
    from typing_extensions import override
else:
    F = typing.TypeVar("F", bound=Callable[..., Any])
    def override(fn: F, /) -> F:
        return fn

if typing.TYPE_CHECKING:
    from typing_extensions import Self

WdlModel = Literal["sf", "sf16.1", "sf16", "sf15.1", "sf15", "sf14", "sf12", "licchess"]


T = TypeVar("T")
ProtocolT = TypeVar("ProtocolT", bound="Protocol")

ConfigValue = Union[str, int, bool, None]
ConfigMapping = Mapping[str, ConfigValue]


LOGGER = logging.getLogger(__name__)


MANAGED_OPTIONS = ["uci_cchess960", "uci_variant", "multipv", "ponder"]


# No longer needed, but alias kept around for compatibility.
EventLoopPolicy = asyncio.DefaultEventLoopPolicy


def run_in_background(coroutine: Callable[[concurrent.futures.Future[T]], Coroutine[Any, Any, None]], *, name: Optional[str] = None, debug: Optional[bool] = None) -> T:
    """
    Runs ``coroutine(future)`` in a new event loop on a background thread.

    Blocks on *future* and returns the result as soon as it is resolved.
    The coroutine and all remaining tasks continue running in the background
    until complete.
    """
    assert asyncio.iscoroutinefunction(coroutine)

    future: concurrent.futures.Future[T] = concurrent.futures.Future()

    def background() -> None:
        try:
            asyncio.run(coroutine(future), debug=debug)
            future.cancel()
        except Exception as exc:
            future.set_exception(exc)

    threading.Thread(target=background, name=name).start()
    return future.result()


class EngineError(RuntimeError):
    """Runtime error caused by a misbehaving engine or incorrect usage."""


class EngineTerminatedError(EngineError):
    """The engine process exited unexpectedly."""


class AnalysisComplete(Exception):
    """
    Raised when analysis is complete, all information has been consumed, but
    further information was requested.
    """


@dataclasses.dataclass(frozen=True)
class Option:
    """Information about an available engine option."""

    name: str
    """The name of the option."""

    type: str
    """
    The type of the option.

    +--------+-----+------+------------------------------------------------+
    | type   | UCI | CECP | value                                          |
    +========+=====+======+================================================+
    | check  | X   | X    | ``True`` or ``False``                          |
    +--------+-----+------+------------------------------------------------+
    | spin   | X   | X    | integer, between *min* and *max*               |
    +--------+-----+------+------------------------------------------------+
    | combo  | X   | X    | string, one of *var*                           |
    +--------+-----+------+------------------------------------------------+
    | button | X   | X    | ``None``                                       |
    +--------+-----+------+------------------------------------------------+
    | reset  |     | X    | ``None``                                       |
    +--------+-----+------+------------------------------------------------+
    | save   |     | X    | ``None``                                       |
    +--------+-----+------+------------------------------------------------+
    | string | X   | X    | string without line breaks                     |
    +--------+-----+------+------------------------------------------------+
    | file   |     | X    | string, interpreted as the path to a file      |
    +--------+-----+------+------------------------------------------------+
    | path   |     | X    | string, interpreted as the path to a directory |
    +--------+-----+------+------------------------------------------------+
    """

    default: ConfigValue
    """The default value of the option."""

    min: Optional[int]
    """The minimum integer value of a *spin* option."""

    max: Optional[int]
    """The maximum integer value of a *spin* option."""

    var: Optional[List[str]]
    """A list of allowed string values for a *combo* option."""

    def parse(self, value: ConfigValue) -> ConfigValue:
        if self.type == "check":
            return value and value != "false"
        elif self.type == "spin":
            try:
                value = int(value)  # type: ignore
            except ValueError:
                raise EngineError(f"expected integer for spin option {self.name!r}, got: {value!r}")
            if self.min is not None and value < self.min:
                raise EngineError(f"expected value for option {self.name!r} to be at least {self.min}, got: {value}")
            if self.max is not None and self.max < value:
                raise EngineError(f"expected value for option {self.name!r} to be at most {self.max}, got: {value}")
            return value
        elif self.type == "combo":
            value = str(value)
            if value not in (self.var or []):
                raise EngineError("invalid value for combo option {!r}, got: {} (available: {})".format(self.name, value, ", ".join(self.var) if self.var else "-"))
            return value
        elif self.type in ["button", "reset", "save"]:
            return None
        elif self.type in ["string", "file", "path"]:
            value = str(value)
            if "\n" in value or "\r" in value:
                raise EngineError(f"invalid line-break in string option {self.name!r}: {value!r}")
            return value
        else:
            raise EngineError(f"unknown option type: {self.type!r}")

    def is_managed(self) -> bool:
        """
        Some options are managed automatically: ``UCI_cchess960``,
        ``UCI_Variant``, ``MultiPV``, ``Ponder``.
        """
        return self.name.lower() in MANAGED_OPTIONS


@dataclasses.dataclass
class Limit:
    """Search-termination condition."""

    time: Optional[float] = None
    """Search exactly *time* seconds."""

    depth: Optional[int] = None
    """Search *depth* ply only."""

    nodes: Optional[int] = None
    """Search only a limited number of *nodes*."""

    mate: Optional[int] = None
    """Search for a mate in *mate* moves."""

    red_clock: Optional[float] = None
    """Time in seconds remaining for Red."""

    black_clock: Optional[float] = None
    """Time in seconds remaining for Black."""

    red_inc: Optional[float] = None
    """Fisher increment for Red, in seconds."""

    black_inc: Optional[float] = None
    """Fisher increment for Black, in seconds."""

    remaining_moves: Optional[int] = None
    """
    Number of moves to the next time control. If this is not set, but
    *red_clock* and *black_clock* are, then it is sudden death.
    """

    clock_id: object = None
    """
    An identifier to use with XBoard engines to signal that the time
    control has changed. When this field changes, Xboard engines are
    sent level or st commands as appropriate. Otherwise, only time
    and otim commands are sent to update the engine's clock.
    """

    def __repr__(self) -> str:
        # Like default __repr__, but without None values.
        return "{}({})".format(
            type(self).__name__,
            ", ".join("{}={!r}".format(attr, getattr(self, attr))
                      for attr in ["time", "depth", "nodes", "mate", "red_clock", "black_clock", "red_inc", "black_inc", "remaining_moves"]
                      if getattr(self, attr) is not None))


class InfoDict(TypedDict, total=False):
    """
    Dictionary of aggregated information sent by the engine.

    Commonly used keys are: ``score`` (a :class:`~cchess.engine.PovScore`),
    ``pv`` (a list of :class:`~cchess.Move` objects), ``depth``,
    ``seldepth``, ``time`` (in seconds), ``nodes``, ``nps``, ``multipv``
    (``1`` for the mainline).

    Others: ``tbhits``, ``currmove``, ``currmovenumber``, ``hashfull``,
    ``cpuload``, ``refutation``, ``currline``, ``ebf`` (effective branching factor),
    ``wdl`` (a :class:`~cchess.engine.PovWdl`), and ``string``.
    """
    score: PovScore
    pv: List[cchess.Move]
    depth: int
    seldepth: int
    time: float
    nodes: int
    nps: int
    tbhits: int
    multipv: int
    currmove: cchess.Move
    currmovenumber: int
    hashfull: int
    cpuload: int
    refutation: Dict[cchess.Move, List[cchess.Move]]
    currline: Dict[int, List[cchess.Move]]
    ebf: float
    wdl: PovWdl
    string: str


class PlayResult:
    """Returned by :func:`cchess.engine.Protocol.play()`."""

    move: Optional[cchess.Move]
    """The best move according to the engine, or ``None``."""

    ponder: Optional[cchess.Move]
    """The response that the engine expects after *move*, or ``None``."""

    info: InfoDict
    """
    A dictionary of extra :class:`information <cchess.engine.InfoDict>`
    sent by the engine, if selected with the *info* argument of
    :func:`~cchess.engine.Protocol.play()`.
    """

    draw_offered: bool
    """Whether the engine offered a draw before moving."""

    resigned: bool
    """Whether the engine resigned."""

    def __init__(self,
                 move: Optional[cchess.Move],
                 ponder: Optional[cchess.Move],
                 info: Optional[InfoDict] = None,
                 *,
                 draw_offered: bool = False,
                 resigned: bool = False) -> None:
        self.move = move
        self.ponder = ponder
        self.info = info or {}
        self.draw_offered = draw_offered
        self.resigned = resigned

    def __repr__(self) -> str:
        return "<{} at {:#x} (move={}, ponder={}, info={}, draw_offered={}, resigned={})>".format(
            type(self).__name__, id(self), self.move, self.ponder, self.info,
            self.draw_offered, self.resigned)


class Info(enum.IntFlag):
    """Used to filter information sent by the cchess engine."""
    NONE = 0
    BASIC = 1
    SCORE = 2
    PV = 4
    REFUTATION = 8
    CURRLINE = 16
    ALL = BASIC | SCORE | PV | REFUTATION | CURRLINE

INFO_NONE = Info.NONE
INFO_BASIC = Info.BASIC
INFO_SCORE = Info.SCORE
INFO_PV = Info.PV
INFO_REFUTATION = Info.REFUTATION
INFO_CURRLINE = Info.CURRLINE
INFO_ALL = Info.ALL


@dataclasses.dataclass
class Opponent:
    """Used to store information about an engine's opponent."""

    name: Optional[str]
    """The name of the opponent."""

    title: Optional[str]
    """The opponent's title--for example, GM, IM, or BOT."""

    rating: Optional[int]
    """The opponent's ELO rating."""

    is_engine: Optional[bool]
    """Whether the opponent is a cchess engine/computer program."""


class PovScore:
    """A relative :class:`~cchess.engine.Score` and the point of view."""

    relative: Score
    """The relative :class:`~cchess.engine.Score`."""

    turn: Color
    """The point of view (``cchess.RED`` or ``cchess.BLACK``)."""

    def __init__(self, relative: Score, turn: Color) -> None:
        self.relative = relative
        self.turn = turn

    def red(self) -> Score:
        """Gets the score from Red's point of view."""
        return self.pov(cchess.RED)

    def black(self) -> Score:
        """Gets the score from Black's point of view."""
        return self.pov(cchess.BLACK)

    def pov(self, color: Color) -> Score:
        """Gets the score from the point of view of the given *color*."""
        return self.relative if self.turn == color else -self.relative

    def is_mate(self) -> bool:
        """Tests if this is a mate score."""
        return self.relative.is_mate()

    def wdl(self, *, model: WdlModel = "sf", ply: int = 30) -> PovWdl:
        """See :func:`~cchess.engine.Score.wdl()`."""
        return PovWdl(self.relative.wdl(model=model, ply=ply), self.turn)

    def __repr__(self) -> str:
        return "PovScore({!r}, {})".format(self.relative, "RED" if self.turn else "BLACK")

    def __eq__(self, other: object) -> bool:
        if isinstance(other, PovScore):
            return self.red() == other.red()
        else:
            return NotImplemented


class Score(abc.ABC):
    """
    Evaluation of a position.

    The score can be :class:`~cchess.engine.Cp` (centi-pawns),
    :class:`~cchess.engine.Mate` or :py:data:`~cchess.engine.MateGiven`.
    A positive value indicates an advantage.

    There is a total order defined on centi-pawn and mate scores.

    >>> from cchess.engine import Cp, Mate, MateGiven
    >>>
    >>> Mate(-0) < Mate(-1) < Cp(-50) < Cp(200) < Mate(4) < Mate(1) < MateGiven
    True

    Scores can be negated to change the point of view:

    >>> -Cp(20)
    Cp(-20)

    >>> -Mate(-4)
    Mate(+4)

    >>> -Mate(0)
    MateGiven
    """

    @typing.overload
    @abc.abstractmethod
    def score(self, *, mate_score: int) -> int: ...
    @typing.overload
    @abc.abstractmethod
    def score(self, *, mate_score: Optional[int] = None) -> Optional[int]: ...
    @abc.abstractmethod
    def score(self, *, mate_score: Optional[int] = None) -> Optional[int]:
        """
        Returns the centi-pawn score as an integer or ``None``.

        You can optionally pass a large value to convert mate scores to
        centi-pawn scores.

        >>> Cp(-300).score()
        -300
        >>> Mate(5).score() is None
        True
        >>> Mate(5).score(mate_score=100000)
        99995
        """

    @abc.abstractmethod
    def mate(self) -> Optional[int]:
        """
        Returns the number of plies to mate, negative if we are getting
        mated, or ``None``.

        .. warning::
            This conflates ``Mate(0)`` (we lost) and ``MateGiven``
            (we won) to ``0``.
        """

    def is_mate(self) -> bool:
        """Tests if this is a mate score."""
        return self.mate() is not None

    @abc.abstractmethod
    def wdl(self, *, model: WdlModel = "sf", ply: int = 30) -> Wdl:
        """
        Returns statistics for the expected outcome of this game, based on
        a *model*, given that this score is reached at *ply*.

        Scores have a total order, but it makes little sense to compute
        the difference between two scores. For example, going from
        ``Cp(-100)`` to ``Cp(+100)`` is much more significant than going
        from ``Cp(+300)`` to ``Cp(+500)``. It is better to compute differences
        of the expectation values for the outcome of the game (based on winning
        chances and drawing chances).

        >>> Cp(100).wdl().expectation() - Cp(-100).wdl().expectation()  # doctest: +ELLIPSIS
        0.379...

        >>> Cp(500).wdl().expectation() - Cp(300).wdl().expectation()  # doctest: +ELLIPSIS
        0.015...

        :param model:
            * ``sf``, the WDL model used by the latest Stockfish
              (currently ``sf16``).
            * ``sf16``, the WDL model used by Stockfish 16.
            * ``sf15.1``, the WDL model used by Stockfish 15.1.
            * ``sf15``, the WDL model used by Stockfish 15.
            * ``sf14``, the WDL model used by Stockfish 14.
            * ``sf12``, the WDL model used by Stockfish 12.
            * ``licchess``, the win rate model used by Licchess.
              Does not use *ply*, and does not consider drawing chances.
        :param ply: The number of half-moves played since the starting
            position. Models may scale scores slightly differently based on
            this. Defaults to middle game.
        """

    @abc.abstractmethod
    def __neg__(self) -> Score:
        ...

    @abc.abstractmethod
    def __pos__(self) -> Score:
        ...

    @abc.abstractmethod
    def __abs__(self) -> Score:
        ...

    def _score_tuple(self) -> Tuple[bool, bool, bool, int, Optional[int]]:
        mate = self.mate()
        return (
            isinstance(self, MateGivenType),
            mate is not None and mate > 0,
            mate is None,
            -(mate or 0),
            self.score(),
        )

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Score):
            return self._score_tuple() == other._score_tuple()
        else:
            return NotImplemented

    def __lt__(self, other: object) -> bool:
        if isinstance(other, Score):
            return self._score_tuple() < other._score_tuple()
        else:
            return NotImplemented

    def __le__(self, other: object) -> bool:
        if isinstance(other, Score):
            return self._score_tuple() <= other._score_tuple()
        else:
            return NotImplemented

    def __gt__(self, other: object) -> bool:
        if isinstance(other, Score):
            return self._score_tuple() > other._score_tuple()
        else:
            return NotImplemented

    def __ge__(self, other: object) -> bool:
        if isinstance(other, Score):
            return self._score_tuple() >= other._score_tuple()
        else:
            return NotImplemented

def _sf16_1_wins(cp: int, *, ply: int) -> int:
    # https://github.com/official-stockfish/Stockfish/blob/sf_16.1/src/uci.cpp#L48
    NormalizeToPawnValue = 356
    # https://github.com/official-stockfish/Stockfish/blob/sf_16.1/src/uci.cpp#L383-L384
    m = min(120, max(8, ply / 2 + 1)) / 32
    a = (((-1.06249702 * m + 7.42016937) * m + 0.89425629) * m) + 348.60356174
    b = (((-5.33122190 * m + 39.57831533) * m + -90.84473771) * m) + 123.40620748
    x = min(4000, max(cp * NormalizeToPawnValue / 100, -4000))
    return int(0.5 + 1000 / (1 + math.exp((a - x) / b)))

def _sf16_wins(cp: int, *, ply: int) -> int:
    # https://github.com/official-stockfish/Stockfish/blob/sf_16/src/uci.h#L38
    NormalizeToPawnValue = 328
    # https://github.com/official-stockfish/Stockfish/blob/sf_16/src/uci.cpp#L200-L224
    m = min(240, max(ply, 0)) / 64
    a = (((0.38036525 * m + -2.82015070) * m + 23.17882135) * m) + 307.36768407
    b = (((-2.29434733 * m + 13.27689788) * m + -14.26828904) * m) + 63.45318330
    x = min(4000, max(cp * NormalizeToPawnValue / 100, -4000))
    return int(0.5 + 1000 / (1 + math.exp((a - x) / b)))

def _sf15_1_wins(cp: int, *, ply: int) -> int:
    # https://github.com/official-stockfish/Stockfish/blob/sf_15.1/src/uci.h#L38
    NormalizeToPawnValue = 361
    # https://github.com/official-stockfish/Stockfish/blob/sf_15.1/src/uci.cpp#L200-L224
    m = min(240, max(ply, 0)) / 64
    a = (((-0.58270499 * m + 2.68512549) * m + 15.24638015) * m) + 344.49745382
    b = (((-2.65734562 * m + 15.96509799) * m + -20.69040836) * m) + 73.61029937
    x = min(4000, max(cp * NormalizeToPawnValue / 100, -4000))
    return int(0.5 + 1000 / (1 + math.exp((a - x) / b)))

def _sf15_wins(cp: int, *, ply: int) -> int:
    # https://github.com/official-stockfish/Stockfish/blob/sf_15/src/uci.cpp#L200-L220
    m = min(240, max(ply, 0)) / 64
    a = (((-1.17202460e-1 * m + 5.94729104e-1) * m + 1.12065546e+1) * m) + 1.22606222e+2
    b = (((-1.79066759 * m + 11.30759193) * m + -17.43677612) * m) + 36.47147479
    x = min(2000, max(cp, -2000))
    return int(0.5 + 1000 / (1 + math.exp((a - x) / b)))

def _sf14_wins(cp: int, *, ply: int) -> int:
    # https://github.com/official-stockfish/Stockfish/blob/sf_14/src/uci.cpp#L200-L220
    m = min(240, max(ply, 0)) / 64
    a = (((-3.68389304 * m + 30.07065921) * m + -60.52878723) * m) + 149.53378557
    b = (((-2.01818570 * m + 15.85685038) * m + -29.83452023) * m) + 47.59078827
    x = min(2000, max(cp, -2000))
    return int(0.5 + 1000 / (1 + math.exp((a - x) / b)))

def _sf12_wins(cp: int, *, ply: int) -> int:
    # https://github.com/official-stockfish/Stockfish/blob/sf_12/src/uci.cpp#L198-L218
    m = min(240, max(ply, 0)) / 64
    a = (((-8.24404295 * m + 64.23892342) * m + -95.73056462) * m) + 153.86478679
    b = (((-3.37154371 * m + 28.44489198) * m + -56.67657741) * m) + 72.05858751
    x = min(1000, max(cp, -1000))
    return int(0.5 + 1000 / (1 + math.exp((a - x) / b)))

def _licchess_raw_wins(cp: int) -> int:
    # https://github.com/licchess-org/lila/pull/11148
    # https://github.com/licchess-org/lila/blob/2242b0a08faa06e7be5508d338ede7bb09049777/modules/analyse/src/main/WinPercent.scala#L26-L30
    return round(1000 / (1 + math.exp(-0.00368208 * cp)))


class Cp(Score):
    """Centi-pawn score."""

    def __init__(self, cp: int) -> None:
        self.cp = cp

    def mate(self) -> None:
        return None

    def score(self, *, mate_score: Optional[int] = None) -> int:
        return self.cp

    def wdl(self, *, model: WdlModel = "sf", ply: int = 30) -> Wdl:
        if model == "licchess":
            wins = _licchess_raw_wins(max(-1000, min(self.cp, 1000)))
            losses = 1000 - wins
        elif model == "sf12":
            wins = _sf12_wins(self.cp, ply=ply)
            losses = _sf12_wins(-self.cp, ply=ply)
        elif model == "sf14":
            wins = _sf14_wins(self.cp, ply=ply)
            losses = _sf14_wins(-self.cp, ply=ply)
        elif model == "sf15":
            wins = _sf15_wins(self.cp, ply=ply)
            losses = _sf15_wins(-self.cp, ply=ply)
        elif model == "sf15.1":
            wins = _sf15_1_wins(self.cp, ply=ply)
            losses = _sf15_1_wins(-self.cp, ply=ply)
        elif model == "sf16":
            wins = _sf16_wins(self.cp, ply=ply)
            losses = _sf16_wins(-self.cp, ply=ply)
        else:
            wins = _sf16_1_wins(self.cp, ply=ply)
            losses = _sf16_1_wins(-self.cp, ply=ply)
        draws = 1000 - wins - losses
        return Wdl(wins, draws, losses)

    def __str__(self) -> str:
        return f"+{self.cp:d}" if self.cp > 0 else str(self.cp)

    def __repr__(self) -> str:
        return f"Cp({self})"

    def __neg__(self) -> Cp:
        return Cp(-self.cp)

    def __pos__(self) -> Cp:
        return Cp(self.cp)

    def __abs__(self) -> Cp:
        return Cp(abs(self.cp))


class Mate(Score):
    """Mate score."""

    def __init__(self, moves: int) -> None:
        self.moves = moves

    def mate(self) -> int:
        return self.moves

    @typing.overload
    def score(self, *, mate_score: int) -> int: ...
    @typing.overload
    def score(self, *, mate_score: Optional[int] = None) -> Optional[int]: ...
    def score(self, *, mate_score: Optional[int] = None) -> Optional[int]:
        if mate_score is None:
            return None
        elif self.moves > 0:
            return mate_score - self.moves
        else:
            return -mate_score - self.moves

    def wdl(self, *, model: WdlModel = "sf", ply: int = 30) -> Wdl:
        if model == "licchess":
            cp = (21 - min(10, abs(self.moves))) * 100
            wins = _licchess_raw_wins(cp)
            return Wdl(wins, 0, 1000 - wins) if self.moves > 0 else Wdl(1000 - wins, 0, wins)
        else:
            return Wdl(1000, 0, 0) if self.moves > 0 else Wdl(0, 0, 1000)

    def __str__(self) -> str:
        return f"#+{self.moves}" if self.moves > 0 else f"#-{abs(self.moves)}"

    def __repr__(self) -> str:
        return "Mate({})".format(str(self).lstrip("#"))

    def __neg__(self) -> Union[MateGivenType, Mate]:
        return MateGiven if not self.moves else Mate(-self.moves)

    def __pos__(self) -> Mate:
        return Mate(self.moves)

    def __abs__(self) -> Union[MateGivenType, Mate]:
        return MateGiven if not self.moves else Mate(abs(self.moves))


class MateGivenType(Score):
    """Winning mate score, equivalent to ``-Mate(0)``."""

    def mate(self) -> int:
        return 0

    @typing.overload
    def score(self, *, mate_score: int) -> int: ...
    @typing.overload
    def score(self, *, mate_score: Optional[int] = None) -> Optional[int]: ...
    def score(self, *, mate_score: Optional[int] = None) -> Optional[int]:
        return mate_score

    def wdl(self, *, model: WdlModel = "sf", ply: int = 30) -> Wdl:
        return Wdl(1000, 0, 0)

    def __neg__(self) -> Mate:
        return Mate(0)

    def __pos__(self) -> MateGivenType:
        return self

    def __abs__(self) -> MateGivenType:
        return self

    def __repr__(self) -> str:
        return "MateGiven"

    def __str__(self) -> str:
        return "#+0"

MateGiven = MateGivenType()


class PovWdl:
    """
    Relative :class:`win/draw/loss statistics <cchess.engine.Wdl>` and the point
    of view.

    .. deprecated:: 1.2
        Behaves like a tuple
        ``(wdl.relative.wins, wdl.relative.draws, wdl.relative.losses)``
        for backwards compatibility. But it is recommended to use the provided
        fields and methods instead.
    """

    relative: Wdl
    """The relative :class:`~cchess.engine.Wdl`."""

    turn: Color
    """The point of view (``cchess.RED`` or ``cchess.BLACK``)."""

    def __init__(self, relative: Wdl, turn: Color) -> None:
        self.relative = relative
        self.turn = turn

    def red(self) -> Wdl:
        """Gets the :class:`~cchess.engine.Wdl` from Red's point of view."""
        return self.pov(cchess.RED)

    def black(self) -> Wdl:
        """Gets the :class:`~cchess.engine.Wdl` from Black's point of view."""
        return self.pov(cchess.BLACK)

    def pov(self, color: Color) -> Wdl:
        """
        Gets the :class:`~cchess.engine.Wdl` from the point of view of the given
        *color*.
        """
        return self.relative if self.turn == color else -self.relative

    def __bool__(self) -> bool:
        return bool(self.relative)

    def __repr__(self) -> str:
        return "PovWdl({!r}, {})".format(self.relative, "RED" if self.turn else "BLACK")

    # Unfortunately in python-cchess v1.1.0, info["wdl"] was a simple tuple
    # of the relative permille values, so we have to support __iter__,
    # __len__, __getitem__, and equality comparisons with other tuples.
    # Never mind the ordering, because that's not a sensible operation, anyway.

    def __iter__(self) -> Iterator[int]:
        yield self.relative.wins
        yield self.relative.draws
        yield self.relative.losses

    def __len__(self) -> int:
        return 3

    def __getitem__(self, idx: int) -> int:
        return (self.relative.wins, self.relative.draws, self.relative.losses)[idx]

    def __eq__(self, other: object) -> bool:
        if isinstance(other, PovWdl):
            return self.red() == other.red()
        elif isinstance(other, tuple):
            return (self.relative.wins, self.relative.draws, self.relative.losses) == other
        else:
            return NotImplemented


@dataclasses.dataclass
class Wdl:
    """Win/draw/loss statistics."""

    wins: int
    """The number of wins."""

    draws: int
    """The number of draws."""

    losses: int
    """The number of losses."""

    def total(self) -> int:
        """
        Returns the total number of games. Usually, ``wdl`` reported by engines
        is scaled to 1000 games.
        """
        return self.wins + self.draws + self.losses

    def winning_chance(self) -> float:
        """Returns the relative frequency of wins."""
        return self.wins / self.total()

    def drawing_chance(self) -> float:
        """Returns the relative frequency of draws."""
        return self.draws / self.total()

    def losing_chance(self) -> float:
        """Returns the relative frequency of losses."""
        return self.losses / self.total()

    def expectation(self) -> float:
        """
        Returns the expectation value, where a win is valued 1, a draw is
        valued 0.5, and a loss is valued 0.
        """
        return (self.wins + 0.5 * self.draws) / self.total()

    def __bool__(self) -> bool:
        return bool(self.total())

    def __iter__(self) -> Iterator[int]:
        yield self.wins
        yield self.draws
        yield self.losses

    def __reversed__(self) -> Iterator[int]:
        yield self.losses
        yield self.draws
        yield self.wins

    def __pos__(self) -> Wdl:
        return self

    def __neg__(self) -> Wdl:
        return Wdl(self.losses, self.draws, self.wins)


class MockTransport(asyncio.SubprocessTransport, asyncio.WriteTransport):
    def __init__(self, protocol: Protocol) -> None:
        super().__init__()
        self.protocol = protocol
        self.expectations: Deque[Tuple[str, List[str]]] = collections.deque()
        self.expected_pings = 0
        self.stdin_buffer = bytearray()
        self.protocol.connection_made(self)

    def expect(self, expectation: str, responses: List[str] = []) -> None:
        self.expectations.append((expectation, responses))

    def expect_ping(self) -> None:
        self.expected_pings += 1

    def assert_done(self) -> None:
        assert not self.expectations, f"pending expectations: {self.expectations}"

    def get_pipe_transport(self, fd: int) -> Optional[asyncio.BaseTransport]:
        assert fd == 0, f"expected 0 for stdin, got {fd}"
        return self

    def write(self, data: bytes) -> None:
        self.stdin_buffer.extend(data)
        while b"\n" in self.stdin_buffer:
            line_bytes, self.stdin_buffer = self.stdin_buffer.split(b"\n", 1)
            line = line_bytes.decode("utf-8")

            if line.startswith("ping ") and self.expected_pings:
                self.expected_pings -= 1
                self.protocol.pipe_data_received(1, (line.replace("ping ", "pong ") + "\n").encode("utf-8"))
            else:
                assert self.expectations, f"unexpected: {line!r}"
                expectation, responses = self.expectations.popleft()
                assert expectation == line, f"expected {expectation}, got: {line}"
                if responses:
                    self.protocol.loop.call_soon(self.protocol.pipe_data_received, 1, "\n".join(responses + [""]).encode("utf-8"))

    def get_pid(self) -> int:
        return id(self)

    def get_returncode(self) -> Optional[int]:
        return None if self.expectations else 0


class Protocol(asyncio.SubprocessProtocol, metaclass=abc.ABCMeta):
    """Protocol for communicating with a cchess engine process."""

    options: MutableMapping[str, Option]
    """Dictionary of available options."""

    id: Dict[str, str]
    """
    Dictionary of information about the engine. Common keys are ``name``
    and ``author``.
    """

    returncode: asyncio.Future[int]
    """Future: Exit code of the process."""

    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.transport: Optional[asyncio.SubprocessTransport] = None

        self.buffer = {
            1: bytearray(),  # stdout
            2: bytearray(),  # stderr
        }

        self.command: Optional[BaseCommand[Any]] = None
        self.next_command: Optional[BaseCommand[Any]] = None

        self.initialized = False
        self.returncode: asyncio.Future[int] = asyncio.Future()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        # SubprocessTransport expected, but not checked to allow duck typing.
        self.transport = transport  # type: ignore
        LOGGER.debug("%s: Connection made", self)

    def connection_lost(self, exc: Optional[Exception]) -> None:
        assert self.transport is not None
        code = self.transport.get_returncode()
        assert code is not None, "connect lost, but got no returncode"
        LOGGER.debug("%s: Connection lost (exit code: %d, error: %s)", self, code, exc)

        # Terminate commands.
        command, self.command = self.command, None
        next_command, self.next_command = self.next_command, None
        if command:
            command._engine_terminated(code)
        if next_command:
            next_command._engine_terminated(code)

        self.returncode.set_result(code)

    def process_exited(self) -> None:
        LOGGER.debug("%s: Process exited", self)

    def send_line(self, line: str) -> None:
        LOGGER.debug("%s: << %s", self, line)
        assert self.transport is not None, "cannot send line before connection is made"
        stdin = self.transport.get_pipe_transport(0)
        # WriteTransport expected, but not checked to allow duck typing.
        stdin.write((line + "\n").encode("utf-8"))  # type: ignore

    def pipe_data_received(self, fd: int, data: Union[bytes, str]) -> None:
        self.buffer[fd].extend(data)  # type: ignore
        while b"\n" in self.buffer[fd]:
            line_bytes, self.buffer[fd] = self.buffer[fd].split(b"\n", 1)
            if line_bytes.endswith(b"\r"):
                line_bytes = line_bytes[:-1]
            try:
                line = line_bytes.decode("utf-8")
            except UnicodeDecodeError as err:
                LOGGER.warning("%s: >> %r (%s)", self, bytes(line_bytes), err)
            else:
                if fd == 1:
                    self._line_received(line)
                else:
                    self.error_line_received(line)

    def error_line_received(self, line: str) -> None:
        LOGGER.warning("%s: stderr >> %s", self, line)

    def _line_received(self, line: str) -> None:
        LOGGER.debug("%s: >> %s", self, line)

        self.line_received(line)

        if self.command:
            self.command._line_received(line)

    def line_received(self, line: str) -> None:
        pass

    async def communicate(self, command_factory: Callable[[Self], BaseCommand[T]]) -> T:
        command = command_factory(self)

        if self.returncode.done():
            raise EngineTerminatedError(f"engine process dead (exit code: {self.returncode.result()})")

        assert command.state == CommandState.NEW, command.state

        if self.next_command is not None:
            self.next_command.result.cancel()
            self.next_command.finished.cancel()
            self.next_command.set_finished()

        self.next_command = command

        def previous_command_finished() -> None:
            self.command, self.next_command = self.next_command, None
            if self.command is not None:
                cmd = self.command

                def cancel_if_cancelled(result: asyncio.Future[T]) -> None:
                    if result.cancelled():
                        cmd._cancel()

                cmd.result.add_done_callback(cancel_if_cancelled)
                cmd._start()
                cmd.add_finished_callback(previous_command_finished)

        if self.command is None:
            previous_command_finished()
        elif not self.command.result.done():
            self.command.result.cancel()
        elif not self.command.result.cancelled():
            self.command._cancel()

        return await command.result

    def __repr__(self) -> str:
        pid = self.transport.get_pid() if self.transport is not None else "?"
        return f"<{type(self).__name__} (pid={pid})>"

    @abc.abstractmethod
    async def initialize(self) -> None:
        """Initializes the engine."""

    @abc.abstractmethod
    async def ping(self) -> None:
        """
        Pings the engine and waits for a response. Used to ensure the engine
        is still alive and idle.
        """

    @abc.abstractmethod
    async def configure(self, options: ConfigMapping) -> None:
        """
        Configures global engine options.

        :param options: A dictionary of engine options where the keys are
            names of :data:`~cchess.engine.Protocol.options`. Do not set options
            that are managed automatically
            (:func:`cchess.engine.Option.is_managed()`).
        """

    @abc.abstractmethod
    async def send_opponent_information(self, *, opponent: Optional[Opponent] = None, engine_rating: Optional[int] = None) -> None:
        """
        Sends the engine information about its opponent. The information will
        be sent after a new game is announced and before the first move. This
        method should be called before the first move of a game--i.e., the
        first call to :func:`cchess.engine.Protocol.play()`.

        :param opponent: Optional. An instance of :class:`cchess.engine.Opponent` that has the opponent's information.
        :param engine_rating: Optional. This engine's own rating. Only used by XBoard engines.
        """

    @abc.abstractmethod
    async def play(self, board: cchess.Board, limit: Limit, *, game: object = None, info: Info = INFO_NONE, ponder: bool = False, draw_offered: bool = False, root_moves: Optional[Iterable[cchess.Move]] = None, options: ConfigMapping = {}, opponent: Optional[Opponent] = None) -> PlayResult:
        """
        Plays a position.

        :param board: The position. The entire move stack will be sent to the
            engine.
        :param limit: An instance of :class:`cchess.engine.Limit` that
            determines when to stop thinking.
        :param game: Optional. An arbitrary object that identifies the game.
            Will automatically inform the engine if the object is not equal
            to the previous game (e.g., ``ucinewgame``, ``new``).
        :param info: Selects which additional information to retrieve from the
            engine. ``INFO_NONE``, ``INFO_BASIC`` (basic information that is
            trivial to obtain), ``INFO_SCORE``, ``INFO_PV``,
            ``INFO_REFUTATION``, ``INFO_CURRLINE``, ``INFO_ALL`` or any
            bitwise combination. Some overhead is associated with parsing
            extra information.
        :param ponder: Whether the engine should keep analysing in the
            background even after the result has been returned.
        :param draw_offered: Whether the engine's opponent has offered a draw.
            Ignored by UCI engines.
        :param root_moves: Optional. Consider only root moves from this list.
        :param options: Optional. A dictionary of engine options for the
            analysis. The previous configuration will be restored after the
            analysis is complete. You can permanently apply a configuration
            with :func:`~cchess.engine.Protocol.configure()`.
        :param opponent: Optional. Information about a new opponent. Information
            about the original opponent will be restored once the move is
            complete. New opponent information can be made permanent with
            :func:`~cchess.engine.Protocol.send_opponent_information()`.
        """

    @typing.overload
    async def analyse(self, board: cchess.Board, limit: Limit, *, game: object = None, info: Info = INFO_ALL, root_moves: Optional[Iterable[cchess.Move]] = None, options: ConfigMapping = {}) -> InfoDict: ...
    @typing.overload
    async def analyse(self, board: cchess.Board, limit: Limit, *, multipv: int, game: object = None, info: Info = INFO_ALL, root_moves: Optional[Iterable[cchess.Move]] = None, options: ConfigMapping = {}) -> List[InfoDict]: ...
    @typing.overload
    async def analyse(self, board: cchess.Board, limit: Limit, *, multipv: Optional[int] = None, game: object = None, info: Info = INFO_ALL, root_moves: Optional[Iterable[cchess.Move]] = None, options: ConfigMapping = {}) -> Union[List[InfoDict], InfoDict]: ...
    async def analyse(self, board: cchess.Board, limit: Limit, *, multipv: Optional[int] = None, game: object = None, info: Info = INFO_ALL, root_moves: Optional[Iterable[cchess.Move]] = None, options: ConfigMapping = {}) -> Union[List[InfoDict], InfoDict]:
        """
        Analyses a position and returns a dictionary of
        :class:`information <cchess.engine.InfoDict>`.

        :param board: The position to analyse. The entire move stack will be
            sent to the engine.
        :param limit: An instance of :class:`cchess.engine.Limit` that
            determines when to stop the analysis.
        :param multipv: Optional. Analyse multiple root moves. Will return
            a list of at most *multipv* dictionaries rather than just a single
            info dictionary.
        :param game: Optional. An arbitrary object that identifies the game.
            Will automatically inform the engine if the object is not equal
            to the previous game (e.g., ``ucinewgame``, ``new``).
        :param info: Selects which information to retrieve from the
            engine. ``INFO_NONE``, ``INFO_BASIC`` (basic information that is
            trivial to obtain), ``INFO_SCORE``, ``INFO_PV``,
            ``INFO_REFUTATION``, ``INFO_CURRLINE``, ``INFO_ALL`` or any
            bitwise combination. Some overhead is associated with parsing
            extra information.
        :param root_moves: Optional. Limit analysis to a list of root moves.
        :param options: Optional. A dictionary of engine options for the
            analysis. The previous configuration will be restored after the
            analysis is complete. You can permanently apply a configuration
            with :func:`~cchess.engine.Protocol.configure()`.
        """
        analysis = await self.analysis(board, limit, multipv=multipv, game=game, info=info, root_moves=root_moves, options=options)

        with analysis:
            await analysis.wait()

        return analysis.info if multipv is None else analysis.multipv

    @abc.abstractmethod
    async def analysis(self, board: cchess.Board, limit: Optional[Limit] = None, *, multipv: Optional[int] = None, game: object = None, info: Info = INFO_ALL, root_moves: Optional[Iterable[cchess.Move]] = None, options: ConfigMapping = {}) -> AnalysisResult:
        """
        Starts analysing a position.

        :param board: The position to analyse. The entire move stack will be
            sent to the engine.
        :param limit: Optional. An instance of :class:`cchess.engine.Limit`
            that determines when to stop the analysis. Analysis is infinite
            by default.
        :param multipv: Optional. Analyse multiple root moves.
        :param game: Optional. An arbitrary object that identifies the game.
            Will automatically inform the engine if the object is not equal
            to the previous game (e.g., ``ucinewgame``, ``new``).
        :param info: Selects which information to retrieve from the
            engine. ``INFO_NONE``, ``INFO_BASIC`` (basic information that is
            trivial to obtain), ``INFO_SCORE``, ``INFO_PV``,
            ``INFO_REFUTATION``, ``INFO_CURRLINE``, ``INFO_ALL`` or any
            bitwise combination. Some overhead is associated with parsing
            extra information.
        :param root_moves: Optional. Limit analysis to a list of root moves.
        :param options: Optional. A dictionary of engine options for the
            analysis. The previous configuration will be restored after the
            analysis is complete. You can permanently apply a configuration
            with :func:`~cchess.engine.Protocol.configure()`.

        Returns :class:`~cchess.engine.AnalysisResult`, a handle that allows
        asynchronously iterating over the information sent by the engine
        and stopping the analysis at any time.
        """

    @abc.abstractmethod
    async def send_game_result(self, board: cchess.Board, winner: Optional[Color] = None, game_ending: Optional[str] = None, game_complete: bool = True) -> None:
        """
        Sends the engine the result of the game.

        XBoard engines receive the final moves and a line of the form
        ``result <winner> {<ending>}``. The ``<winner>`` field is one of ``1-0``,
        ``0-1``, ``1/2-1/2``, or ``*`` to indicate red won, black won, draw,
        or adjournment, respectively. The ``<ending>`` field is a description
        of the specific reason for the end of the game: "Red mates",
        "Time forfeiture", "Stalemate", etc.

        UCI engines do not expect end-of-game information and so are not
        sent anything.

        :param board: The final state of the board.
        :param winner: Optional. Specify the winner of the game. This is useful
            if the result of the game is not evident from the board--e.g., time
            forfeiture or draw by agreement. If not ``None``, this parameter
            overrides any winner derivable from the board.
        :param game_ending: Optional. Text describing the reason for the game
            ending. Similarly to the winner parameter, this overrides any game
            result derivable from the board.
        :param game_complete: Optional. Whether the game reached completion.
        """

    @abc.abstractmethod
    async def quit(self) -> None:
        """Asks the engine to shut down."""

    @classmethod
    async def popen(cls: Type[ProtocolT], command: Union[str, List[str]], *, setpgrp: bool = False, **popen_args: Any) -> Tuple[asyncio.SubprocessTransport, ProtocolT]:
        if not isinstance(command, list):
            command = [command]

        if setpgrp:
            try:
                # Windows.
                popen_args["creationflags"] = popen_args.get("creationflags", 0) | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore
            except AttributeError:
                # Unix.
                if sys.version_info >= (3, 11):
                    popen_args["process_group"] = 0
                else:
                    # Before Python 3.11
                    popen_args["start_new_session"] = True

        return await asyncio.get_running_loop().subprocess_exec(cls, *command, **popen_args)


class CommandState(enum.Enum):
    NEW = enum.auto()
    ACTIVE = enum.auto()
    CANCELLING = enum.auto()
    DONE = enum.auto()


class BaseCommand(Generic[T]):
    def __init__(self, engine: Protocol) -> None:
        self._engine = engine

        self.state = CommandState.NEW

        self.result: asyncio.Future[T] = asyncio.Future()
        self.finished: asyncio.Future[None] = asyncio.Future()

        self._finished_callbacks: List[Callable[[], None]] = []

    def add_finished_callback(self, callback: Callable[[], None]) -> None:
        self._finished_callbacks.append(callback)
        self._dispatch_finished()

    def _dispatch_finished(self) -> None:
        if self.finished.done():
            while self._finished_callbacks:
                self._finished_callbacks.pop()()

    def _engine_terminated(self, code: int) -> None:
        hint = ", binary not compatible with cpu?" if code in [-4, 0xc000001d] else ""
        exc = EngineTerminatedError(f"engine process died unexpectedly (exit code: {code}{hint})")
        if self.state == CommandState.ACTIVE:
            self.engine_terminated(exc)
        elif self.state == CommandState.CANCELLING:
            self.finished.set_result(None)
            self._dispatch_finished()
        elif self.state == CommandState.NEW:
            self._handle_exception(exc)

    def _handle_exception(self, exc: Exception) -> None:
        if not self.result.done():
            self.result.set_exception(exc)
        else:
            self._engine.loop.call_exception_handler({ # XXX
                "message": f"{type(self).__name__} failed after returning preliminary result ({self.result!r})",
                "exception": exc,
                "protocol": self._engine,
                "transport": self._engine.transport,
            })

        if not self.finished.done():
            self.finished.set_result(None)
            self._dispatch_finished()

    def set_finished(self) -> None:
        assert self.state in [CommandState.ACTIVE, CommandState.CANCELLING], self.state
        if not self.result.done():
            self.result.set_exception(EngineError(f"engine command finished before returning result: {self!r}"))
        self.state = CommandState.DONE
        self.finished.set_result(None)
        self._dispatch_finished()

    def _cancel(self) -> None:
        if self.state != CommandState.CANCELLING and self.state != CommandState.DONE:
            assert self.state == CommandState.ACTIVE, self.state
            self.state = CommandState.CANCELLING
            self.cancel()

    def _start(self) -> None:
        assert self.state == CommandState.NEW, self.state
        self.state = CommandState.ACTIVE
        try:
            self.check_initialized()
            self.start()
        except EngineError as err:
            self._handle_exception(err)

    def _line_received(self, line: str) -> None:
        assert self.state in [CommandState.ACTIVE, CommandState.CANCELLING], self.state
        try:
            self.line_received(line)
        except EngineError as err:
            self._handle_exception(err)

    def cancel(self) -> None:
        pass

    def check_initialized(self) -> None:
        if not self._engine.initialized:
            raise EngineError("tried to run command, but engine is not initialized")

    def start(self) -> None:
        raise NotImplementedError

    def line_received(self, line: str) -> None:
        pass

    def engine_terminated(self, exc: Exception) -> None:
        self._handle_exception(exc)

    def __repr__(self) -> str:
        return "<{} at {:#x} (state={}, result={}, finished={}>".format(type(self).__name__, id(self), self.state, self.result, self.finished)


class UciProtocol(Protocol):
    """
    An implementation of the
    `Universal cchess Interface <https://www.cchessprogramming.org/UCI>`_
    protocol.
    """

    def __init__(self) -> None:
        super().__init__()
        self.options: UciOptionMap[Option] = UciOptionMap()
        self.config: UciOptionMap[ConfigValue] = UciOptionMap()
        self.target_config: UciOptionMap[ConfigValue] = UciOptionMap()
        self.id = {}
        self.board = cchess.Board()
        self.game: object = None
        self.first_game = True
        self.may_ponderhit: Optional[cchess.Board] = None
        self.ponderhit = False

    async def initialize(self) -> None:
        class UciInitializeCommand(BaseCommand[None]):
            def __init__(self, engine: UciProtocol):
                super().__init__(engine)
                self.engine = engine

            @override
            def check_initialized(self) -> None:
                if self.engine.initialized:
                    raise EngineError("engine already initialized")

            @override
            def start(self) -> None:
                self.engine.send_line("uci")

            @override
            def line_received(self, line: str) -> None:
                token, remaining = _next_token(line)
                if line.strip() == "uciok" and not self.result.done():
                    self.engine.initialized = True
                    self.result.set_result(None)
                    self.set_finished()
                elif token == "option":
                    self._option(remaining)
                elif token == "id":
                    self._id(remaining)

            def _option(self, arg: str) -> None:
                current_parameter = None
                option_parts: dict[str, str] = {k: "" for k in ["name", "type", "default", "min", "max"]}
                var = []

                parameters = list(option_parts.keys()) + ['var']
                inner_regex = '|'.join([fr"\b{parameter}\b" for parameter in parameters])
                option_regex = fr"\s*({inner_regex})\s*"
                for token in re.split(option_regex, arg.strip()):
                    if token == "var" or (token in option_parts and not option_parts[token]):
                        current_parameter = token
                    elif current_parameter == "var":
                        var.append(token)
                    elif current_parameter:
                        option_parts[current_parameter] = token

                def parse_min_max_value(option_parts: dict[str, str], which: Literal["min", "max"]) -> Optional[int]:
                    try:
                        number = option_parts[which]
                        return int(number) if number else None
                    except ValueError:
                        LOGGER.exception(f"Exception parsing option {which}")
                        return None

                name = option_parts["name"]
                type = option_parts["type"]
                default = option_parts["default"]
                min = parse_min_max_value(option_parts, "min")
                max = parse_min_max_value(option_parts, "max")

                without_default = Option(name, type, None, min, max, var)
                option = Option(without_default.name, without_default.type, without_default.parse(default), min, max, var)
                self.engine.options[option.name] = option

                if option.default is not None:
                    self.engine.config[option.name] = option.default
                if option.default is not None and not option.is_managed() and option.name.lower() != "uci_analysemode":
                    self.engine.target_config[option.name] = option.default

            def _id(self, arg: str) -> None:
                key, value = _next_token(arg)
                self.engine.id[key] = value.strip()

        return await self.communicate(UciInitializeCommand)

    def _isready(self) -> None:
        self.send_line("isready")

    def _opponent_info(self) -> None:
        opponent_info = self.config.get("UCI_Opponent") or self.target_config.get("UCI_Opponent")
        if opponent_info:
            self.send_line(f"setoption name UCI_Opponent value {opponent_info}")

    def _ucinewgame(self) -> None:
        self.send_line("ucinewgame")
        self._opponent_info()
        self.first_game = False
        self.ponderhit = False

    def debug(self, on: bool = True) -> None:
        """
        Switches debug mode of the engine on or off. This does not interrupt
        other ongoing operations.
        """
        if on:
            self.send_line("debug on")
        else:
            self.send_line("debug off")

    async def ping(self) -> None:
        class UciPingCommand(BaseCommand[None]):
            def __init__(self, engine: UciProtocol) -> None:
                super().__init__(engine)
                self.engine =  engine

            def start(self) -> None:
                self.engine._isready()

            @override
            def line_received(self, line: str) -> None:
                if line.strip() == "readyok":
                    self.result.set_result(None)
                    self.set_finished()
                else:
                    LOGGER.warning("%s: Unexpected engine output: %r", self.engine, line)

        return await self.communicate(UciPingCommand)

    def _changed_options(self, options: ConfigMapping) -> bool:
        return any(value is None or value != self.config.get(name) for name, value in _chain_config(options, self.target_config))

    def _setoption(self, name: str, value: ConfigValue) -> None:
        try:
            value = self.options[name].parse(value)
        except KeyError:
            raise EngineError("engine does not support option {} (available options: {})".format(name, ", ".join(self.options)))

        if value is None or value != self.config.get(name):
            builder = ["setoption name", name]
            if value is False:
                builder.append("value false")
            elif value is True:
                builder.append("value true")
            elif value is not None:
                builder.append("value")
                builder.append(str(value))

            if name != "UCI_Opponent":  # sent after ucinewgame
                self.send_line(" ".join(builder))
            self.config[name] = value

    def _configure(self, options: ConfigMapping) -> None:
        for name, value in _chain_config(options, self.target_config):
            if name.lower() in MANAGED_OPTIONS:
                raise EngineError("cannot set {} which is automatically managed".format(name))
            self._setoption(name, value)

    async def configure(self, options: ConfigMapping) -> None:
        class UciConfigureCommand(BaseCommand[None]):
            def __init__(self, engine: UciProtocol):
                super().__init__(engine)
                self.engine = engine

            def start(self) -> None:
                self.engine._configure(options)
                self.engine.target_config.update({name: value for name, value in options.items() if value is not None})
                self.result.set_result(None)
                self.set_finished()

        return await self.communicate(UciConfigureCommand)

    def _opponent_configuration(self, *, opponent: Optional[Opponent] = None) -> ConfigMapping:
        if opponent and opponent.name and "UCI_Opponent" in self.options:
            rating = opponent.rating or "none"
            title = opponent.title or "none"
            player_type = "computer" if opponent.is_engine else "human"
            return {"UCI_Opponent": f"{title} {rating} {player_type} {opponent.name}"}
        else:
            return {}

    async def send_opponent_information(self, *, opponent: Optional[Opponent] = None, engine_rating: Optional[int] = None) -> None:
        return await self.configure(self._opponent_configuration(opponent=opponent))

    def _position(self, board: cchess.Board) -> None:
        # Send starting position.
        builder = ["position"]
        safe_history = all(board.move_stack)
        root = board.root() if safe_history else board
        fen = root.fen()
        if fen == cchess.STARTING_FEN:
            builder.append("startpos")
        else:
            builder.append("fen")
            builder.append(fen)

        # Send moves.
        if not safe_history:
            LOGGER.warning("Not transmitting history with null moves to UCI engine")
        elif board.move_stack:
            builder.append("moves")
            builder.extend(move.uci() for move in board.move_stack)

        self.send_line(" ".join(builder))
        self.board = board.copy(stack=False)

    def _go(self, limit: Limit, *, root_moves: Optional[Iterable[cchess.Move]] = None, ponder: bool = False, infinite: bool = False) -> None:
        builder = ["go"]
        if ponder:
            builder.append("ponder")
        if limit.red_clock is not None:
            builder.append("wtime")
            builder.append(str(max(1, round(limit.red_clock * 1000))))
        if limit.black_clock is not None:
            builder.append("btime")
            builder.append(str(max(1, round(limit.black_clock * 1000))))
        if limit.red_inc is not None:
            builder.append("winc")
            builder.append(str(round(limit.red_inc * 1000)))
        if limit.black_inc is not None:
            builder.append("binc")
            builder.append(str(round(limit.black_inc * 1000)))
        if limit.remaining_moves is not None and int(limit.remaining_moves) > 0:
            builder.append("movestogo")
            builder.append(str(int(limit.remaining_moves)))
        if limit.depth is not None:
            builder.append("depth")
            builder.append(str(max(1, int(limit.depth))))
        if limit.nodes is not None:
            builder.append("nodes")
            builder.append(str(max(1, int(limit.nodes))))
        if limit.mate is not None:
            builder.append("mate")
            builder.append(str(max(1, int(limit.mate))))
        if limit.time is not None:
            builder.append("movetime")
            builder.append(str(max(1, round(limit.time * 1000))))
        if infinite:
            builder.append("infinite")
        if root_moves is not None:
            builder.append("searchmoves")
            if root_moves:
                builder.extend(move.uci() for move in root_moves)
            else:
                # Work around searchmoves followed by nothing.
                builder.append("0000")
        self.send_line(" ".join(builder))

    async def play(self, board: cchess.Board, limit: Limit, *, game: object = None, info: Info = INFO_NONE, ponder: bool = False, draw_offered: bool = False, root_moves: Optional[Iterable[cchess.Move]] = None, options: ConfigMapping = {}, opponent: Optional[Opponent] = None) -> PlayResult:
        new_options: Dict[str, ConfigValue] = {}
        for name, value in options.items():
            new_options[name] = value
        new_options.update(self._opponent_configuration(opponent=opponent))

        engine = self

        class UciPlayCommand(BaseCommand[PlayResult]):
            def __init__(self, engine: UciProtocol):
                super().__init__(engine)
                self.engine = engine

                # May ponderhit only in the same game and with unchanged target
                # options. The managed options UCI_AnalyseMode, Ponder, and
                # MultiPV never change between pondering play commands.
                engine.may_ponderhit = board if ponder and not engine.first_game and game == engine.game and not engine._changed_options(new_options) else None

            @override
            def start(self) -> None:
                self.info: InfoDict = {}
                self.pondering: Optional[cchess.Board] = None
                self.sent_isready = False
                self.start_time = time.perf_counter()

                if self.engine.ponderhit:
                    self.engine.ponderhit = False
                    self.engine.send_line("ponderhit")
                    return

                if "UCI_AnalyseMode" in self.engine.options and "UCI_AnalyseMode" not in self.engine.target_config and all(name.lower() != "uci_analysemode" for name in new_options):
                    self.engine._setoption("UCI_AnalyseMode", False)
                if "Ponder" in self.engine.options:
                    self.engine._setoption("Ponder", ponder)
                if "MultiPV" in self.engine.options:
                    self.engine._setoption("MultiPV", self.engine.options["MultiPV"].default)

                new_opponent = new_options.get("UCI_Opponent") or self.engine.target_config.get("UCI_Opponent")
                opponent_changed = new_opponent != self.engine.config.get("UCI_Opponent")
                self.engine._configure(new_options)

                if self.engine.first_game or self.engine.game != game or opponent_changed:
                    self.engine.game = game
                    self.engine._ucinewgame()
                    self.sent_isready = True
                    self.engine._isready()
                else:
                    self._readyok()

            @override
            def line_received(self, line: str) -> None:
                token, remaining = _next_token(line)
                if token == "info":
                    self._info(remaining)
                elif token == "bestmove":
                    self._bestmove(remaining)
                elif line.strip() == "readyok" and self.sent_isready:
                    self._readyok()
                else:
                    LOGGER.warning("%s: Unexpected engine output: %r", self.engine, line)

            def _readyok(self) -> None:
                self.sent_isready = False
                engine._position(board)
                engine._go(limit, root_moves=root_moves)

            def _info(self, arg: str) -> None:
                if not self.pondering:
                    self.info.update(_parse_uci_info(arg, self.engine.board, info))

            def _bestmove(self, arg: str) -> None:
                if self.pondering:
                    self.pondering = None
                elif not self.result.cancelled():
                    best = _parse_uci_bestmove(self.engine.board, arg)
                    self.result.set_result(PlayResult(best.move, best.ponder, self.info))

                    if ponder and best.move and best.ponder:
                        self.pondering = board.copy()
                        self.pondering.push(best.move)
                        self.pondering.push(best.ponder)
                        self.engine._position(self.pondering)

                        # Adjust clocks for pondering.
                        time_used = time.perf_counter() - self.start_time
                        ponder_limit = copy.copy(limit)
                        if ponder_limit.red_clock is not None:
                            ponder_limit.red_clock += (ponder_limit.red_inc or 0.0)
                            if self.pondering.turn == cchess.RED:
                                ponder_limit.red_clock -= time_used
                        if ponder_limit.black_clock is not None:
                            ponder_limit.black_clock += (ponder_limit.black_inc or 0.0)
                            if self.pondering.turn == cchess.BLACK:
                                ponder_limit.black_clock -= time_used
                        if ponder_limit.remaining_moves:
                            ponder_limit.remaining_moves -= 1

                        self.engine._go(ponder_limit, ponder=True)

                if not self.pondering:
                    self.end()

            def end(self) -> None:
                engine.may_ponderhit = None
                self.set_finished()

            @override
            def cancel(self) -> None:
                if self.engine.may_ponderhit and self.pondering and self.engine.may_ponderhit.move_stack == self.pondering.move_stack and self.engine.may_ponderhit == self.pondering:
                    self.engine.ponderhit = True
                    self.end()
                else:
                    self.engine.send_line("stop")

            @override
            def engine_terminated(self, exc: Exception) -> None:
                # Allow terminating engine while pondering.
                if not self.result.done():
                    super().engine_terminated(exc)

        return await self.communicate(UciPlayCommand)

    async def analysis(self, board: cchess.Board, limit: Optional[Limit] = None, *, multipv: Optional[int] = None, game: object = None, info: Info = INFO_ALL, root_moves: Optional[Iterable[cchess.Move]] = None, options: ConfigMapping = {}) -> AnalysisResult:
        class UciAnalysisCommand(BaseCommand[AnalysisResult]):
            def __init__(self, engine: UciProtocol):
                super().__init__(engine)
                self.engine = engine

            def start(self) -> None:
                self.analysis = AnalysisResult(stop=lambda: self.cancel())
                self.sent_isready = False

                if "Ponder" in self.engine.options:
                    self.engine._setoption("Ponder", False)
                if "UCI_AnalyseMode" in self.engine.options and "UCI_AnalyseMode" not in self.engine.target_config and all(name.lower() != "uci_analysemode" for name in options):
                    self.engine._setoption("UCI_AnalyseMode", True)
                if "MultiPV" in self.engine.options or (multipv and multipv > 1):
                    self.engine._setoption("MultiPV", 1 if multipv is None else multipv)

                self.engine._configure(options)

                if self.engine.first_game or self.engine.game != game:
                    self.engine.game = game
                    self.engine._ucinewgame()
                    self.sent_isready = True
                    self.engine._isready()
                else:
                    self._readyok()

            @override
            def line_received(self, line: str) -> None:
                token, remaining = _next_token(line)
                if token == "info":
                    self._info(remaining)
                elif token == "bestmove":
                    self._bestmove(remaining)
                elif line.strip() == "readyok" and self.sent_isready:
                    self._readyok()
                else:
                    LOGGER.warning("%s: Unexpected engine output: %r", self.engine, line)

            def _readyok(self) -> None:
                self.sent_isready = False
                self.engine._position(board)

                if limit:
                    self.engine._go(limit, root_moves=root_moves)
                else:
                    self.engine._go(Limit(), root_moves=root_moves, infinite=True)

                self.result.set_result(self.analysis)

            def _info(self, arg: str) -> None:
                self.analysis.post(_parse_uci_info(arg, self.engine.board, info))

            def _bestmove(self, arg: str) -> None:
                if not self.result.done():
                    raise EngineError("was not searching, but engine sent bestmove")
                best = _parse_uci_bestmove(self.engine.board, arg)
                self.set_finished()
                self.analysis.set_finished(best)

            @override
            def cancel(self) -> None:
                self.engine.send_line("stop")

            @override
            def engine_terminated(self, exc: Exception) -> None:
                LOGGER.debug("%s: Closing analysis because engine has been terminated (error: %s)", self.engine, exc)
                self.analysis.set_exception(exc)

        return await self.communicate(UciAnalysisCommand)

    async def send_game_result(self, board: cchess.Board, winner: Optional[Color] = None, game_ending: Optional[str] = None, game_complete: bool = True) -> None:
        pass

    async def quit(self) -> None:
        self.send_line("quit")
        await asyncio.shield(self.returncode)


UCI_REGEX = re.compile(r"^[a-i][0-9][a-i][0-9][pnbrack]?|[PNBRACK]@[a-i][0-9]|0000\Z")

def _create_variation_line(root_board: cchess.Board, line: str) -> tuple[list[cchess.Move], str]:
    board = root_board.copy(stack=False)
    currline: list[cchess.Move] = []
    while True:
        next_move, remaining_line_after_move = _next_token(line)
        if UCI_REGEX.match(next_move):
            currline.append(board.push_uci(next_move))
            line = remaining_line_after_move
        else:
            return currline, line


def _parse_uci_info(arg: str, root_board: cchess.Board, selector: Info = INFO_ALL) -> InfoDict:
    info: InfoDict = {}
    if not selector:
        return info

    remaining_line = arg
    while remaining_line:
        parameter, remaining_line = _next_token(remaining_line)

        if parameter == "string":
            info["string"] = remaining_line
            break
        elif parameter in ["depth", "seldepth", "nodes", "multipv", "currmovenumber", "hashfull", "nps", "tbhits", "cpuload"]:
            try:
                number, remaining_line = _next_token(remaining_line)
                info[parameter] = int(number)  # type: ignore
            except (ValueError, IndexError):
                LOGGER.error("Exception parsing %s from info: %r", parameter, arg)
        elif parameter == "time":
            try:
                time_ms, remaining_line = _next_token(remaining_line)
                info["time"] = int(time_ms) / 1000.0
            except (ValueError, IndexError):
                LOGGER.error("Exception parsing %s from info: %r", parameter, arg)
        elif parameter == "ebf":
            try:
                number, remaining_line = _next_token(remaining_line)
                info["ebf"] = float(number)
            except (ValueError, IndexError):
                LOGGER.error("Exception parsing %s from info: %r", parameter, arg)
        elif parameter == "score" and selector & INFO_SCORE:
            try:
                kind, remaining_line = _next_token(remaining_line)
                value, remaining_line = _next_token(remaining_line)
                token, remaining_after_token = _next_token(remaining_line)
                if token in ["lowerbound", "upperbound"]:
                    info[token] = True  # type: ignore
                    remaining_line = remaining_after_token
                if kind == "cp":
                    info["score"] = PovScore(Cp(int(value)), root_board.turn)
                elif kind == "mate":
                    info["score"] = PovScore(Mate(int(value)), root_board.turn)
                else:
                    LOGGER.error("Unknown score kind %r in info (expected cp or mate): %r", kind, arg)
            except (ValueError, IndexError):
                LOGGER.error("Exception parsing score from info: %r", arg)
        elif parameter == "currmove":
            try:
                current_move, remaining_line = _next_token(remaining_line)
                info["currmove"] = cchess.Move.from_uci(current_move)
            except (ValueError, IndexError):
                LOGGER.error("Exception parsing currmove from info: %r", arg)
        elif parameter == "currline" and selector & INFO_CURRLINE:
            try:
                if "currline" not in info:
                    info["currline"] = {}

                cpunr_text, remaining_line = _next_token(remaining_line)
                cpunr = int(cpunr_text)
                currline, remaining_line = _create_variation_line(root_board, remaining_line)
                info["currline"][cpunr] = currline
            except (ValueError, IndexError):
                LOGGER.error("Exception parsing currline from info: %r, position at root: %s", arg, root_board.fen())
        elif parameter == "refutation" and selector & INFO_REFUTATION:
            try:
                if "refutation" not in info:
                    info["refutation"] = {}

                board = root_board.copy(stack=False)
                refuted_text, remaining_line = _next_token(remaining_line)
                refuted = board.push_uci(refuted_text)

                refuted_by, remaining_line = _create_variation_line(board, remaining_line)
                info["refutation"][refuted] = refuted_by
            except (ValueError, IndexError):
                LOGGER.error("Exception parsing refutation from info: %r, position at root: %s", arg, root_board.fen())
        elif parameter == "pv" and selector & INFO_PV:
            try:
                pv, remaining_line = _create_variation_line(root_board, remaining_line)
                info["pv"] = pv
            except (ValueError, IndexError):
                LOGGER.error("Exception parsing pv from info: %r, position at root: %s", arg, root_board.fen())
        elif parameter == "wdl":
            try:
                wins, remaining_line = _next_token(remaining_line)
                draws, remaining_line = _next_token(remaining_line)
                losses, remaining_line = _next_token(remaining_line)
                info["wdl"] = PovWdl(Wdl(int(wins), int(draws), int(losses)), root_board.turn)
            except (ValueError, IndexError):
                LOGGER.error("Exception parsing wdl from info: %r", arg)

    return info

def _parse_uci_bestmove(board: cchess.Board, args: str) -> BestMove:
    tokens = args.split()

    move = None
    ponder = None

    if tokens and tokens[0] not in ["(none)", "NULL"]:
        try:
            # AnMon 5.75 uses uppercase letters to denote promotion types.
            move = board.push_uci(tokens[0].lower())
        except ValueError as err:
            raise EngineError(err)

        try:
            # Houdini 1.5 sends NULL instead of skipping the token.
            if len(tokens) >= 3 and tokens[1] == "ponder" and tokens[2] not in ["(none)", "NULL"]:
                ponder = board.parse_uci(tokens[2].lower())
        except ValueError:
            LOGGER.exception("Engine sent invalid ponder move")
        finally:
            board.pop()

    return BestMove(move, ponder)


def _chain_config(a: ConfigMapping, b: ConfigMapping) -> Iterator[Tuple[str, ConfigValue]]:
    for name, value in a.items():
        yield name, value
    for name, value in b.items():
        if name not in a:
            yield name, value


class UciOptionMap(MutableMapping[str, T]):
    """Dictionary with case-insensitive keys."""

    def __init__(self, data: Optional[Iterable[Tuple[str, T]]] = None, **kwargs: T) -> None:
        self._store: Dict[str, Tuple[str, T]] = {}
        if data is None:
            data = {}
        self.update(data, **kwargs)

    def __setitem__(self, key: str, value: T) -> None:
        self._store[key.lower()] = (key, value)

    def __getitem__(self, key: str) -> T:
        return self._store[key.lower()][1]

    def __delitem__(self, key: str) -> None:
        del self._store[key.lower()]

    def __iter__(self) -> Iterator[str]:
        return (casedkey for casedkey, _ in self._store.values())

    def __len__(self) -> int:
        return len(self._store)

    def __eq__(self, other: object) -> bool:
        try:
            for key, value in self.items():
                if key not in other or other[key] != value:  # type: ignore
                    return False

            for key, value in other.items():  # type: ignore
                if key not in self or self[key] != value:
                    return False

            return True
        except (TypeError, AttributeError):
            return NotImplemented

    def copy(self) -> UciOptionMap[T]:
        return type(self)(self._store.values())

    def __copy__(self) -> UciOptionMap[T]:
        return self.copy()

    def __repr__(self) -> str:
        return f"{type(self).__name__}({dict(self.items())!r})"


XBOARD_ERROR_REGEX = re.compile(r"^\s*(Error|Illegal move)(\s*\([^()]+\))?\s*:")


class XBoardProtocol(Protocol):
    """
    An implementation of the
    `XBoard protocol <http://hgm.nubati.net/CECP.html>`__ (CECP).
    """

    def __init__(self) -> None:
        super().__init__()
        self.features: Dict[str, Union[int, str]] = {}
        self.id = {}
        self.options = {
            "random": Option("random", "check", False, None, None, None),
            "computer": Option("computer", "check", False, None, None, None),
            "name": Option("name", "string", "", None, None, None),
            "engine_rating": Option("engine_rating", "spin", 0, None, None, None),
            "opponent_rating": Option("opponent_rating", "spin", 0, None, None, None)
        }
        self.config: Dict[str, ConfigValue] = {}
        self.target_config: Dict[str, ConfigValue] = {}
        self.board = cchess.Board()
        self.game: object = None
        self.clock_id: object = None
        self.first_game = True

    async def initialize(self) -> None:
        class XBoardInitializeCommand(BaseCommand[None]):
            def __init__(self, engine: XBoardProtocol):
                super().__init__(engine)
                self.engine = engine

            @override
            def check_initialized(self) -> None:
                if self.engine.initialized:
                    raise EngineError("engine already initialized")

            @override
            def start(self) -> None:
                self.engine.send_line("xboard")
                self.engine.send_line("protover 2")
                self.timeout_handle = self.engine.loop.call_later(2.0, lambda: self.timeout())

            def timeout(self) -> None:
                LOGGER.error("%s: Timeout during initialization", self.engine)
                self.end()

            @override
            def line_received(self, line: str) -> None:
                token, remaining = _next_token(line)
                if token.startswith("#"):
                    pass
                elif token == "feature":
                    self._feature(remaining)
                elif XBOARD_ERROR_REGEX.match(line):
                    raise EngineError(line)

            def _feature(self, arg: str) -> None:
                for feature in shlex.split(arg):
                    key, value = feature.split("=", 1)
                    if key == "option":
                        option = _parse_xboard_option(value)
                        if option.name not in ["random", "computer", "cores", "memory"]:
                            self.engine.options[option.name] = option
                    else:
                        try:
                            self.engine.features[key] = int(value)
                        except ValueError:
                            self.engine.features[key] = value

                if "done" in self.engine.features:
                    self.timeout_handle.cancel()
                if self.engine.features.get("done"):
                    self.end()

            def end(self) -> None:
                if not self.engine.features.get("ping", 0):
                    self.result.set_exception(EngineError("xboard engine did not declare required feature: ping"))
                    self.set_finished()
                    return
                if not self.engine.features.get("setboard", 0):
                    self.result.set_exception(EngineError("xboard engine did not declare required feature: setboard"))
                    self.set_finished()
                    return

                if not self.engine.features.get("reuse", 1):
                    LOGGER.warning("%s: Rejecting feature reuse=0", self.engine)
                    self.engine.send_line("rejected reuse")
                if not self.engine.features.get("sigterm", 1):
                    LOGGER.warning("%s: Rejecting feature sigterm=0", self.engine)
                    self.engine.send_line("rejected sigterm")
                if self.engine.features.get("san", 0):
                    LOGGER.warning("%s: Rejecting feature san=1", self.engine)
                    self.engine.send_line("rejected san")

                if "myname" in self.engine.features:
                    self.engine.id["name"] = str(self.engine.features["myname"])

                if self.engine.features.get("memory", 0):
                    self.engine.options["memory"] = Option("memory", "spin", 16, 1, None, None)
                    self.engine.send_line("accepted memory")
                if self.engine.features.get("smp", 0):
                    self.engine.options["cores"] = Option("cores", "spin", 1, 1, None, None)
                    self.engine.send_line("accepted smp")
                if self.engine.features.get("egt"):
                    for egt in str(self.engine.features["egt"]).split(","):
                        name = f"egtpath {egt}"
                        self.engine.options[name] = Option(name, "path", None, None, None, None)
                    self.engine.send_line("accepted egt")

                for option in self.engine.options.values():
                    if option.default is not None:
                        self.engine.config[option.name] = option.default
                    if option.default is not None and not option.is_managed():
                        self.engine.target_config[option.name] = option.default

                self.engine.initialized = True
                self.result.set_result(None)
                self.set_finished()

        return await self.communicate(XBoardInitializeCommand)

    def _ping(self, n: int) -> None:
        self.send_line(f"ping {n}")

    def _variant(self, variant: Optional[str]) -> None:
        variants = str(self.features.get("variants", "")).split(",")
        if not variant or variant not in variants:
            raise EngineError("unsupported xboard variant: {} (available: {})".format(variant, ", ".join(variants)))

        self.send_line(f"variant {variant}")

    def _new(self, board: cchess.Board, game: object, options: ConfigMapping, opponent: Optional[Opponent] = None) -> None:
        self._configure(options)
        self._configure(self._opponent_configuration(opponent=opponent))

        # Set up starting position.
        root = board.root()
        new_options = any(param in options for param in ("random", "computer"))
        new_game = self.first_game or self.game != game or new_options or opponent or root != self.board.root()
        self.game = game
        self.first_game = False
        if new_game:
            self.board = root
            self.send_line("new")

            variant = type(board).xboard_variant
            if variant == "normal" and board.cchess960:
                self._variant("fischerandom")
            elif variant != "normal":
                self._variant(variant)

            if self.config.get("random"):
                self.send_line("random")

            opponent_name = self.config.get("name")
            if opponent_name and self.features.get("name", True):
                self.send_line(f"name {opponent_name}")

            opponent_rating = self.config.get("opponent_rating")
            engine_rating = self.config.get("engine_rating")
            if engine_rating or opponent_rating:
                self.send_line(f"rating {engine_rating or 0} {opponent_rating or 0}")

            if self.config.get("computer"):
                self.send_line("computer")

            self.send_line("force")

            fen = root.fen(shredder=board.cchess960, en_passant="fen")
            if variant != "normal" or fen != cchess.STARTING_FEN or board.cchess960:
                self.send_line(f"setboard {fen}")
        else:
            self.send_line("force")

        # Undo moves until common position.
        common_stack_len = 0
        if not new_game:
            for left, right in zip(self.board.move_stack, board.move_stack):
                if left == right:
                    common_stack_len += 1
                else:
                    break

            while len(self.board.move_stack) > common_stack_len + 1:
                self.send_line("remove")
                self.board.pop()
                self.board.pop()

            while len(self.board.move_stack) > common_stack_len:
                self.send_line("undo")
                self.board.pop()

        # Play moves from board stack.
        for move in board.move_stack[common_stack_len:]:
            if not move:
                LOGGER.warning("Null move (in %s) may not be supported by all XBoard engines", self.board.fen())
            prefix = "usermove " if self.features.get("usermove", 0) else ""
            self.send_line(prefix + self.board.xboard(move))
            self.board.push(move)

    async def ping(self) -> None:
        class XBoardPingCommand(BaseCommand[None]):
            def __init__(self, engine: XBoardProtocol):
                super().__init__(engine)
                self.engine = engine

            @override
            def start(self) -> None:
                n = id(self) & 0xffff
                self.pong = f"pong {n}"
                self.engine._ping(n)

            @override
            def line_received(self, line: str) -> None:
                if line == self.pong:
                    self.result.set_result(None)
                    self.set_finished()
                elif not line.startswith("#"):
                    LOGGER.warning("%s: Unexpected engine output: %r", self.engine, line)
                elif XBOARD_ERROR_REGEX.match(line):
                    raise EngineError(line)

        return await self.communicate(XBoardPingCommand)

    async def play(self, board: cchess.Board, limit: Limit, *, game: object = None, info: Info = INFO_NONE, ponder: bool = False, draw_offered: bool = False, root_moves: Optional[Iterable[cchess.Move]] = None, options: ConfigMapping = {}, opponent: Optional[Opponent] = None) -> PlayResult:
        if root_moves is not None:
            raise EngineError("play with root_moves, but xboard supports 'include' only in analysis mode")

        class XBoardPlayCommand(BaseCommand[PlayResult]):
            def __init__(self, engine: XBoardProtocol):
                super().__init__(engine)
                self.engine = engine

            @override
            def start(self) -> None:
                self.play_result = PlayResult(None, None)
                self.stopped = False
                self.pong_after_move: Optional[str] = None
                self.pong_after_ponder: Optional[str] = None

                # Set game, position and configure.
                self.engine._new(board, game, options, opponent)

                # Limit or time control.
                clock = limit.red_clock if board.turn else limit.black_clock
                increment = limit.red_inc if board.turn else limit.black_inc
                if limit.clock_id is None or limit.clock_id != self.engine.clock_id:
                    self._send_time_control(clock, increment)
                self.engine.clock_id = limit.clock_id
                if limit.nodes is not None:
                    if limit.time is not None or limit.red_clock is not None or limit.black_clock is not None or increment is not None:
                        raise EngineError("xboard does not support mixing node limits with time limits")

                    if "nps" not in self.engine.features:
                        LOGGER.warning("%s: Engine did not explicitly declare support for node limits (feature nps=?)")
                    elif not self.engine.features["nps"]:
                        raise EngineError("xboard engine does not support node limits (feature nps=0)")

                    self.engine.send_line("nps 1")
                    self.engine.send_line(f"st {max(1, int(limit.nodes))}")
                if limit.depth is not None:
                    self.engine.send_line(f"sd {max(1, int(limit.depth))}")
                if limit.red_clock is not None:
                    self.engine.send_line("{} {}".format("time" if board.turn else "otim", max(1, round(limit.red_clock * 100))))
                if limit.black_clock is not None:
                    self.engine.send_line("{} {}".format("otim" if board.turn else "time", max(1, round(limit.black_clock * 100))))

                if draw_offered and self.engine.features.get("draw", 1):
                    self.engine.send_line("draw")

                # Start thinking.
                self.engine.send_line("post" if info else "nopost")
                self.engine.send_line("hard" if ponder else "easy")
                self.engine.send_line("go")

            @override
            def line_received(self, line: str) -> None:
                token, remaining = _next_token(line)
                if token == "move":
                    self._move(remaining.strip())
                elif token == "Hint:":
                    self._hint(remaining.strip())
                elif token == "pong":
                    pong_line = f"{token} {remaining.strip()}"
                    if pong_line == self.pong_after_move:
                        if not self.result.done():
                            self.result.set_result(self.play_result)
                        if not ponder:
                            self.set_finished()
                    elif pong_line == self.pong_after_ponder:
                        if not self.result.done():
                            self.result.set_result(self.play_result)
                        self.set_finished()
                elif f"{token} {remaining.strip()}" == "offer draw":
                    if not self.result.done():
                        self.play_result.draw_offered = True
                    self._ping_after_move()
                elif line.strip() == "resign":
                    if not self.result.done():
                        self.play_result.resigned = True
                    self._ping_after_move()
                elif token in ["1-0", "0-1", "1/2-1/2"]:
                    if "resign" in line and not self.result.done():
                        self.play_result.resigned = True
                    self._ping_after_move()
                elif token.startswith("#"):
                    pass
                elif XBOARD_ERROR_REGEX.match(line):
                    self.engine.first_game = True  # Board state might no longer be in sync
                    raise EngineError(line)
                elif len(line.split()) >= 4 and line.lstrip()[0].isdigit():
                    self._post(line)
                else:
                    LOGGER.warning("%s: Unexpected engine output: %r", self.engine, line)

            def _send_time_control(self, clock: Optional[float], increment: Optional[float]) -> None:
                if limit.remaining_moves or clock is not None or increment is not None:
                    base_mins, base_secs = divmod(int(clock or 0), 60)
                    self.engine.send_line(f"level {limit.remaining_moves or 0} {base_mins}:{base_secs:02d} {increment or 0}")
                if limit.time is not None:
                    self.engine.send_line(f"st {max(0.01, limit.time)}")

            def _post(self, line: str) -> None:
                if not self.result.done():
                    self.play_result.info = _parse_xboard_post(line, self.engine.board, info)

            def _move(self, arg: str) -> None:
                if not self.result.done() and self.play_result.move is None:
                    try:
                        self.play_result.move = self.engine.board.push_xboard(arg)
                    except ValueError as err:
                        self.result.set_exception(EngineError(err))
                    else:
                        self._ping_after_move()
                else:
                    try:
                        self.engine.board.push_xboard(arg)
                    except ValueError:
                        LOGGER.exception("Exception playing unexpected move")

            def _hint(self, arg: str) -> None:
                if not self.result.done() and self.play_result.move is not None and self.play_result.ponder is None:
                    try:
                        self.play_result.ponder = self.engine.board.parse_xboard(arg)
                    except ValueError:
                        LOGGER.exception("Exception parsing hint")
                else:
                    LOGGER.warning("Unexpected hint: %r", arg)

            def _ping_after_move(self) -> None:
                if self.pong_after_move is None:
                    n = id(self) & 0xffff
                    self.pong_after_move = f"pong {n}"
                    self.engine._ping(n)

            @override
            def cancel(self) -> None:
                if self.stopped:
                    return
                self.stopped = True

                if self.result.cancelled():
                    self.engine.send_line("?")

                if ponder:
                    self.engine.send_line("easy")

                    n = (id(self) + 1) & 0xffff
                    self.pong_after_ponder = f"pong {n}"
                    self.engine._ping(n)

            @override
            def engine_terminated(self, exc: Exception) -> None:
                # Allow terminating engine while pondering.
                if not self.result.done():
                    super().engine_terminated(exc)

        return await self.communicate(XBoardPlayCommand)

    async def analysis(self, board: cchess.Board, limit: Optional[Limit] = None, *, multipv: Optional[int] = None, game: object = None, info: Info = INFO_ALL, root_moves: Optional[Iterable[cchess.Move]] = None, options: ConfigMapping = {}) -> AnalysisResult:
        if multipv is not None:
            raise EngineError("xboard engine does not support multipv")

        if limit is not None and (limit.red_clock is not None or limit.black_clock is not None):
            raise EngineError("xboard analysis does not support clock limits")

        class XBoardAnalysisCommand(BaseCommand[AnalysisResult]):
            def __init__(self, engine: XBoardProtocol):
                super().__init__(engine)
                self.engine = engine

            @override
            def start(self) -> None:
                self.stopped = False
                self.best_move: Optional[cchess.Move] = None
                self.analysis = AnalysisResult(stop=lambda: self.cancel())
                self.final_pong: Optional[str] = None

                self.engine._new(board, game, options)

                if root_moves is not None:
                    if not self.engine.features.get("exclude", 0):
                        raise EngineError("xboard engine does not support root_moves (feature exclude=0)")

                    self.engine.send_line("exclude all")
                    for move in root_moves:
                        self.engine.send_line(f"include {self.engine.board.xboard(move)}")

                self.engine.send_line("post")
                self.engine.send_line("analyze")

                self.result.set_result(self.analysis)

                if limit is not None and limit.time is not None:
                    self.time_limit_handle: Optional[asyncio.Handle] = self.engine.loop.call_later(limit.time, lambda: self.cancel())
                else:
                    self.time_limit_handle = None

            @override
            def line_received(self, line: str) -> None:
                token, remaining = _next_token(line)
                if token.startswith("#"):
                    pass
                elif len(line.split()) >= 4 and line.lstrip()[0].isdigit():
                    self._post(line)
                elif f"{token} {remaining.strip()}" == self.final_pong:
                    self.end()
                elif XBOARD_ERROR_REGEX.match(line):
                    self.engine.first_game = True  # Board state might no longer be in sync
                    raise EngineError(line)
                else:
                    LOGGER.warning("%s: Unexpected engine output: %r", self.engine, line)

            def _post(self, line: str) -> None:
                post_info = _parse_xboard_post(line, self.engine.board, info)
                self.analysis.post(post_info)

                pv = post_info.get("pv")
                if pv:
                    self.best_move = pv[0]

                if limit is not None:
                    if limit.time is not None and post_info.get("time", 0) >= limit.time:
                        self.cancel()
                    elif limit.nodes is not None and post_info.get("nodes", 0) >= limit.nodes:
                        self.cancel()
                    elif limit.depth is not None and post_info.get("depth", 0) >= limit.depth:
                        self.cancel()
                    elif limit.mate is not None and "score" in post_info:
                        if post_info["score"].relative >= Mate(limit.mate):
                            self.cancel()

            def end(self) -> None:
                if self.time_limit_handle:
                    self.time_limit_handle.cancel()

                self.set_finished()
                self.analysis.set_finished(BestMove(self.best_move, None))

            @override
            def cancel(self) -> None:
                if self.stopped:
                    return
                self.stopped = True

                self.engine.send_line(".")
                self.engine.send_line("exit")

                n = id(self) & 0xffff
                self.final_pong = f"pong {n}"
                self.engine._ping(n)

            @override
            def engine_terminated(self, exc: Exception) -> None:
                LOGGER.debug("%s: Closing analysis because engine has been terminated (error: %s)", self.engine, exc)

                if self.time_limit_handle:
                    self.time_limit_handle.cancel()

                self.analysis.set_exception(exc)

        return await self.communicate(XBoardAnalysisCommand)

    def _setoption(self, name: str, value: ConfigValue) -> None:
        if value is not None and value == self.config.get(name):
            return

        try:
            option = self.options[name]
        except KeyError:
            raise EngineError(f"unsupported xboard option or command: {name}")

        self.config[name] = value = option.parse(value)

        if name in ["random", "computer", "name", "engine_rating", "opponent_rating"]:
            # Applied in _new.
            pass
        elif name in ["memory", "cores"] or name.startswith("egtpath "):
            self.send_line(f"{name} {value}")
        elif value is None:
            self.send_line(f"option {name}")
        elif value is True:
            self.send_line(f"option {name}=1")
        elif value is False:
            self.send_line(f"option {name}=0")
        else:
            self.send_line(f"option {name}={value}")

    def _configure(self, options: ConfigMapping) -> None:
        for name, value in _chain_config(options, self.target_config):
            if name.lower() in MANAGED_OPTIONS:
                raise EngineError(f"cannot set {name} which is automatically managed")
            self._setoption(name, value)

    async def configure(self, options: ConfigMapping) -> None:
        class XBoardConfigureCommand(BaseCommand[None]):
            def __init__(self, engine: XBoardProtocol):
                super().__init__(engine)
                self.engine = engine

            @override
            def start(self) -> None:
                self.engine._configure(options)
                self.engine.target_config.update({name: value for name, value in options.items() if value is not None})
                self.result.set_result(None)
                self.set_finished()

        return await self.communicate(XBoardConfigureCommand)

    def _opponent_configuration(self, *, opponent: Optional[Opponent] = None, engine_rating: Optional[int] = None) -> ConfigMapping:
        if opponent is None:
            return {}

        opponent_info: Dict[str, Union[int, bool, str]] = {"engine_rating": engine_rating or self.target_config.get("engine_rating") or 0,
                                                           "opponent_rating": opponent.rating or 0,
                                                           "computer": opponent.is_engine or False}

        if opponent.name and self.features.get("name", True):
            opponent_info["name"] = f"{opponent.title or ''} {opponent.name}".strip()

        return opponent_info

    async def send_opponent_information(self, *, opponent: Optional[Opponent] = None, engine_rating: Optional[int] = None) -> None:
        return await self.configure(self._opponent_configuration(opponent=opponent, engine_rating=engine_rating))

    async def send_game_result(self, board: cchess.Board, winner: Optional[Color] = None, game_ending: Optional[str] = None, game_complete: bool = True) -> None:
        class XBoardGameResultCommand(BaseCommand[None]):
            def __init__(self, engine: XBoardProtocol):
                super().__init__(engine)
                self.engine = engine

            @override
            def start(self) -> None:
                if game_ending and any(c in game_ending for c in "{}\n\r"):
                    raise EngineError(f"invalid line break or curly braces in game ending message: {game_ending!r}")

                self.engine._new(board, self.engine.game, {})  # Send final moves to engine.

                outcome = board.outcome(claim_draw=True)

                if not game_complete:
                    result = "*"
                    ending = game_ending or ""
                elif winner is not None or game_ending:
                    result = "1-0" if winner == cchess.RED else "0-1" if winner == cchess.BLACK else "1/2-1/2"
                    ending = game_ending or ""
                elif outcome is not None and outcome.winner is not None:
                    result = outcome.result()
                    winning_color = "Red" if outcome.winner == cchess.RED else "Black"
                    is_checkmate = outcome.termination == cchess.Termination.CHECKMATE
                    ending = f"{winning_color} {'mates' if is_checkmate else 'variant win'}"
                elif outcome is not None:
                    result = outcome.result()
                    ending = outcome.termination.name.capitalize().replace("_", " ")
                else:
                    result = "*"
                    ending = ""

                ending_text = f"{{{ending}}}" if ending else ""
                self.engine.send_line(f"result {result} {ending_text}".strip())
                self.result.set_result(None)
                self.set_finished()

        return await self.communicate(XBoardGameResultCommand)

    async def quit(self) -> None:
        self.send_line("quit")
        await asyncio.shield(self.returncode)


def _parse_xboard_option(feature: str) -> Option:
    params = feature.split()

    name = params[0]
    type = params[1][1:]
    default: Optional[ConfigValue] = None
    min = None
    max = None
    var = None

    if type == "combo":
        var = []
        choices = params[2:]
        for choice in choices:
            if choice == "///":
                continue
            elif choice[0] == "*":
                default = choice[1:]
                var.append(choice[1:])
            else:
                var.append(choice)
    elif type == "check":
        default = int(params[2])
    elif type in ["string", "file", "path"]:
        if len(params) > 2:
            default = params[2]
        else:
            default = ""
    elif type == "spin":
        default = int(params[2])
        min = int(params[3])
        max = int(params[4])

    return Option(name, type, default, min, max, var)


def _parse_xboard_post(line: str, root_board: cchess.Board, selector: Info = INFO_ALL) -> InfoDict:
    # Format: depth score time nodes [seldepth [nps [tbhits]]] pv
    info: InfoDict = {}

    # Split leading integer tokens from pv.
    pv_tokens = line.split()
    integer_tokens = []
    while pv_tokens:
        token = pv_tokens.pop(0)
        try:
            integer_tokens.append(int(token))
        except ValueError:
            pv_tokens.insert(0, token)
            break

    if len(integer_tokens) < 4:
        return info

    # Required integer tokens.
    info["depth"] = integer_tokens.pop(0)
    cp = integer_tokens.pop(0)
    info["time"] = int(integer_tokens.pop(0)) / 100
    info["nodes"] = int(integer_tokens.pop(0))

    # Score.
    if cp <= -100000:
        score: Score = Mate(cp + 100000)
    elif cp == 100000:
        score = MateGiven
    elif cp >= 100000:
        score = Mate(cp - 100000)
    else:
        score = Cp(cp)
    info["score"] = PovScore(score, root_board.turn)

    # Optional integer tokens.
    if integer_tokens:
        info["seldepth"] = integer_tokens.pop(0)
    if integer_tokens:
        info["nps"] = integer_tokens.pop(0)

    while len(integer_tokens) > 1:
        # Reserved for future extensions.
        integer_tokens.pop(0)

    if integer_tokens:
        info["tbhits"] = integer_tokens.pop(0)

    # Principal variation.
    pv = []
    board = root_board.copy(stack=False)
    for token in pv_tokens:
        if token.rstrip(".").isdigit():
            continue

        try:
            pv.append(board.push_xboard(token))
        except ValueError:
            break

        if not (selector & INFO_PV):
            break
    info["pv"] = pv

    return info


def _next_token(line: str) -> tuple[str, str]:
    """
    Get the next token in a whitespace-delimited line of text.

    The result is returned as a 2-part tuple of strings.

    If the input line is empty or all whitespace, then the result is two
    empty strings.

    If the input line is not empty and not completely whitespace, then
    the first element of the returned tuple is a single word with
    leading and trailing whitespace removed. The second element is the
    unchanged rest of the line.
    """
    parts = line.split(maxsplit=1)
    return parts[0] if parts else "", parts[1] if len(parts) == 2 else ""


class BestMove:
    """Returned by :func:`cchess.engine.AnalysisResult.wait()`."""

    move: Optional[cchess.Move]
    """The best move according to the engine, or ``None``."""

    ponder: Optional[cchess.Move]
    """The response that the engine expects after *move*, or ``None``."""

    def __init__(self, move: Optional[cchess.Move], ponder: Optional[cchess.Move]):
        self.move = move
        self.ponder = ponder

    def __repr__(self) -> str:
        return "<{} at {:#x} (move={}, ponder={}>".format(
            type(self).__name__, id(self), self.move, self.ponder)


class AnalysisResult:
    """
    Handle to ongoing engine analysis.
    Returned by :func:`cchess.engine.Protocol.analysis()`.

    Can be used to asynchronously iterate over information sent by the engine.

    Automatically stops the analysis when used as a context manager.
    """

    multipv: List[InfoDict]
    """
    A list of dictionaries with aggregated information sent by the engine.
    One item for each root move.
    """

    def __init__(self, stop: Optional[Callable[[], None]] = None):
        self._stop = stop
        self._queue: asyncio.Queue[InfoDict] = asyncio.Queue()
        self._posted_kork = False
        self._seen_kork = False
        self._finished: asyncio.Future[BestMove] = asyncio.Future()
        self.multipv = [{}]

    def post(self, info: InfoDict) -> None:
        # Empty dictionary reserved for kork.
        if not info:
            return

        multipv = info.get("multipv", 1)
        while len(self.multipv) < multipv:
            self.multipv.append({})
        self.multipv[multipv - 1].update(info)

        self._queue.put_nowait(info)

    def _kork(self) -> None:
        if not self._posted_kork:
            self._posted_kork = True
            self._queue.put_nowait({})

    def set_finished(self, best: BestMove) -> None:
        if not self._finished.done():
            self._finished.set_result(best)
        self._kork()

    def set_exception(self, exc: Exception) -> None:
        self._finished.set_exception(exc)
        self._kork()

    @property
    def info(self) -> InfoDict:
        """
        A dictionary of aggregated information sent by the engine. This is
        actually an alias for ``multipv[0]``.
        """
        return self.multipv[0]

    def stop(self) -> None:
        """Stops the analysis as soon as possible."""
        if self._stop and not self._posted_kork:
            self._stop()
            self._stop = None

    async def wait(self) -> BestMove:
        """Waits until the analysis is finished."""
        return await self._finished

    async def get(self) -> InfoDict:
        """
        Waits for the next dictionary of information from the engine and
        returns it.

        It might be more convenient to use ``async for info in analysis: ...``.

        :raises: :exc:`cchess.engine.AnalysisComplete` if the analysis is
            complete (or has been stopped) and all information has been
            consumed. Use :func:`~cchess.engine.AnalysisResult.next()` if you
            prefer to get ``None`` instead of an exception.
        """
        if self._seen_kork:
            raise AnalysisComplete()

        info = await self._queue.get()
        if not info:
            # Empty dictionary marks end.
            self._seen_kork = True
            await self._finished
            raise AnalysisComplete()

        return info

    def would_block(self) -> bool:
        """
        Checks if calling :func:`~cchess.engine.AnalysisResult.get()`,
        calling :func:`~cchess.engine.AnalysisResult.next()`,
        or advancing the iterator one step would require waiting for the
        engine.

        These functions would return immediately if information is
        pending (queue is not
        :func:`empty <cchess.engine.AnalysisResult.empty()>`) or if the search
        is finished.
        """
        return not self._seen_kork and self._queue.empty()

    def empty(self) -> bool:
        """
        Checks if all current information has been consumed.

        If the queue is empty, but the analysis is still ongoing, then further
        information can become available in the future.
        """
        return self._seen_kork or self._queue.qsize() <= self._posted_kork

    async def next(self) -> Optional[InfoDict]:
        try:
            return await self.get()
        except AnalysisComplete:
            return None

    def __aiter__(self) -> AnalysisResult:
        return self

    async def __anext__(self) -> InfoDict:
        try:
            return await self.get()
        except AnalysisComplete:
            raise StopAsyncIteration

    def __enter__(self) -> AnalysisResult:
        return self

    def __exit__(self, exc_type: Optional[Type[BaseException]], exc_value: Optional[BaseException], traceback: Optional[TracebackType]) -> None:
        self.stop()


async def popen_uci(command: Union[str, List[str]], *, setpgrp: bool = False, **popen_args: Any) -> Tuple[asyncio.SubprocessTransport, UciProtocol]:
    """
    Spawns and initializes a UCI engine.

    :param command: Path of the engine executable, or a list including the
        path and arguments.
    :param setpgrp: Open the engine process in a new process group. This will
        stop signals (such as keyboard interrupts) from propagating from the
        parent process. Defaults to ``False``.
    :param popen_args: Additional arguments for
        `popen <https://docs.python.org/3/library/subprocess.html#popen-constructor>`_.
        Do not set ``stdin``, ``stdout``, ``bufsize`` or
        ``universal_newlines``.

    Returns a subprocess transport and engine protocol pair.
    """
    transport, protocol = await UciProtocol.popen(command, setpgrp=setpgrp, **popen_args)
    try:
        await protocol.initialize()
    except:
        transport.close()
        raise
    return transport, protocol


async def popen_xboard(command: Union[str, List[str]], *, setpgrp: bool = False, **popen_args: Any) -> Tuple[asyncio.SubprocessTransport, XBoardProtocol]:
    """
    Spawns and initializes an XBoard engine.

    :param command: Path of the engine executable, or a list including the
        path and arguments.
    :param setpgrp: Open the engine process in a new process group. This will
        stop signals (such as keyboard interrupts) from propagating from the
        parent process. Defaults to ``False``.
    :param popen_args: Additional arguments for
        `popen <https://docs.python.org/3/library/subprocess.html#popen-constructor>`_.
        Do not set ``stdin``, ``stdout``, ``bufsize`` or
        ``universal_newlines``.

    Returns a subprocess transport and engine protocol pair.
    """
    transport, protocol = await XBoardProtocol.popen(command, setpgrp=setpgrp, **popen_args)
    try:
        await protocol.initialize()
    except:
        transport.close()
        raise
    return transport, protocol


async def _async(sync: Callable[[], T]) -> T:
    return sync()


class SimpleEngine:
    """
    Synchronous wrapper around a transport and engine protocol pair. Provides
    the same methods and attributes as :class:`cchess.engine.Protocol`
    with blocking functions instead of coroutines.

    You may not concurrently modify objects passed to any of the methods. Other
    than that, :class:`~cchess.engine.SimpleEngine` is thread-safe. When sending
    a new command to the engine, any previous running command will be cancelled
    as soon as possible.

    Methods will raise :class:`asyncio.TimeoutError` if an operation takes
    *timeout* seconds longer than expected (unless *timeout* is ``None``).

    Automatically closes the transport when used as a context manager.
    """

    def __init__(self, transport: asyncio.SubprocessTransport, protocol: Protocol, *, timeout: Optional[float] = 10.0) -> None:
        self.transport = transport
        self.protocol = protocol
        self.timeout = timeout

        self._shutdown_lock = threading.Lock()
        self._shutdown = False
        self.shutdown_event = asyncio.Event()

        self.returncode: concurrent.futures.Future[int] = concurrent.futures.Future()

    def _timeout_for(self, limit: Optional[Limit]) -> Optional[float]:
        if self.timeout is None or limit is None or limit.time is None:
            return None
        return self.timeout + limit.time

    @contextlib.contextmanager
    def _not_shut_down(self) -> Generator[None, None, None]:
        with self._shutdown_lock:
            if self._shutdown:
                raise EngineTerminatedError("engine event loop dead")
            yield

    @property
    def options(self) -> MutableMapping[str, Option]:
        with self._not_shut_down():
            coro = _async(lambda: copy.copy(self.protocol.options))
            future = asyncio.run_coroutine_threadsafe(coro, self.protocol.loop)
        return future.result()

    @property
    def id(self) -> Mapping[str, str]:
        with self._not_shut_down():
            coro = _async(lambda: self.protocol.id.copy())
            future = asyncio.run_coroutine_threadsafe(coro, self.protocol.loop)
        return future.result()

    def communicate(self, command_factory: Callable[[Protocol], BaseCommand[T]]) -> T:
        with self._not_shut_down():
            coro = self.protocol.communicate(command_factory)
            future = asyncio.run_coroutine_threadsafe(coro, self.protocol.loop)
        return future.result()

    def configure(self, options: ConfigMapping) -> None:
        with self._not_shut_down():
            coro = asyncio.wait_for(self.protocol.configure(options), self.timeout)
            future = asyncio.run_coroutine_threadsafe(coro, self.protocol.loop)
        return future.result()

    def send_opponent_information(self, *, opponent: Optional[Opponent] = None, engine_rating: Optional[int] = None) -> None:
        with self._not_shut_down():
            coro = asyncio.wait_for(
                self.protocol.send_opponent_information(opponent=opponent, engine_rating=engine_rating),
                self.timeout)
            future = asyncio.run_coroutine_threadsafe(coro, self.protocol.loop)
        return future.result()

    def ping(self) -> None:
        with self._not_shut_down():
            coro = asyncio.wait_for(self.protocol.ping(), self.timeout)
            future = asyncio.run_coroutine_threadsafe(coro, self.protocol.loop)
        return future.result()

    def play(self, board: cchess.Board, limit: Limit, *, game: object = None, info: Info = INFO_NONE, ponder: bool = False, draw_offered: bool = False, root_moves: Optional[Iterable[cchess.Move]] = None, options: ConfigMapping = {}, opponent: Optional[Opponent] = None) -> PlayResult:
        with self._not_shut_down():
            coro = asyncio.wait_for(
                self.protocol.play(board, limit, game=game, info=info, ponder=ponder, draw_offered=draw_offered, root_moves=root_moves, options=options, opponent=opponent),
                self._timeout_for(limit))
            future = asyncio.run_coroutine_threadsafe(coro, self.protocol.loop)
        return future.result()

    @typing.overload
    def analyse(self, board: cchess.Board, limit: Limit, *, game: object = None, info: Info = INFO_ALL, root_moves: Optional[Iterable[cchess.Move]] = None, options: ConfigMapping = {}) -> InfoDict: ...
    @typing.overload
    def analyse(self, board: cchess.Board, limit: Limit, *, multipv: int, game: object = None, info: Info = INFO_ALL, root_moves: Optional[Iterable[cchess.Move]] = None, options: ConfigMapping = {}) -> List[InfoDict]: ...
    @typing.overload
    def analyse(self, board: cchess.Board, limit: Limit, *, multipv: Optional[int] = None, game: object = None, info: Info = INFO_ALL, root_moves: Optional[Iterable[cchess.Move]] = None, options: ConfigMapping = {}) -> Union[InfoDict, List[InfoDict]]: ...
    def analyse(self, board: cchess.Board, limit: Limit, *, multipv: Optional[int] = None, game: object = None, info: Info = INFO_ALL, root_moves: Optional[Iterable[cchess.Move]] = None, options: ConfigMapping = {}) -> Union[InfoDict, List[InfoDict]]:
        with self._not_shut_down():
            coro = asyncio.wait_for(
                self.protocol.analyse(board, limit, multipv=multipv, game=game, info=info, root_moves=root_moves, options=options),
                self._timeout_for(limit))
            future = asyncio.run_coroutine_threadsafe(coro, self.protocol.loop)
        return future.result()

    def analysis(self, board: cchess.Board, limit: Optional[Limit] = None, *, multipv: Optional[int] = None, game: object = None, info: Info = INFO_ALL, root_moves: Optional[Iterable[cchess.Move]] = None, options: ConfigMapping = {}) -> SimpleAnalysisResult:
        with self._not_shut_down():
            coro = asyncio.wait_for(
                self.protocol.analysis(board, limit, multipv=multipv, game=game, info=info, root_moves=root_moves, options=options),
                self.timeout)  # Timeout until analysis is *started*
            future = asyncio.run_coroutine_threadsafe(coro, self.protocol.loop)
        return SimpleAnalysisResult(self, future.result())

    def send_game_result(self, board: cchess.Board, winner: Optional[Color] = None, game_ending: Optional[str] = None, game_complete: bool = True) -> None:
        with self._not_shut_down():
            coro = asyncio.wait_for(self.protocol.send_game_result(board, winner, game_ending, game_complete), self.timeout)
            future = asyncio.run_coroutine_threadsafe(coro, self.protocol.loop)
        return future.result()

    def quit(self) -> None:
        with self._not_shut_down():
            coro = asyncio.wait_for(self.protocol.quit(), self.timeout)
            future = asyncio.run_coroutine_threadsafe(coro, self.protocol.loop)
        return future.result()

    def close(self) -> None:
        """
        Closes the transport and the background event loop as soon as possible.
        """
        def _shutdown() -> None:
            self.transport.close()
            self.shutdown_event.set()

        with self._shutdown_lock:
            if not self._shutdown:
                self._shutdown = True
                self.protocol.loop.call_soon_threadsafe(_shutdown)

    @classmethod
    def popen(cls, Protocol: Type[Protocol], command: Union[str, List[str]], *, timeout: Optional[float] = 10.0, debug: Optional[bool] = None, setpgrp: bool = False, **popen_args: Any) -> SimpleEngine:
        async def background(future: concurrent.futures.Future[SimpleEngine]) -> None:
            transport, protocol = await Protocol.popen(command, setpgrp=setpgrp, **popen_args)
            threading.current_thread().name = f"{cls.__name__} (pid={transport.get_pid()})"
            simple_engine = cls(transport, protocol, timeout=timeout)
            try:
                await asyncio.wait_for(protocol.initialize(), timeout)
                future.set_result(simple_engine)
                returncode = await protocol.returncode
                simple_engine.returncode.set_result(returncode)
            finally:
                simple_engine.close()
            await simple_engine.shutdown_event.wait()

        return run_in_background(background, name=f"{cls.__name__} (command={command!r})", debug=debug)

    @classmethod
    def popen_uci(cls, command: Union[str, List[str]], *, timeout: Optional[float] = 10.0, debug: Optional[bool] = None, setpgrp: bool = False, **popen_args: Any) -> SimpleEngine:
        """
        Spawns and initializes a UCI engine.
        Returns a :class:`~cchess.engine.SimpleEngine` instance.
        """
        return cls.popen(UciProtocol, command, timeout=timeout, debug=debug, setpgrp=setpgrp, **popen_args)

    @classmethod
    def popen_xboard(cls, command: Union[str, List[str]], *, timeout: Optional[float] = 10.0, debug: Optional[bool] = None, setpgrp: bool = False, **popen_args: Any) -> SimpleEngine:
        """
        Spawns and initializes an XBoard engine.
        Returns a :class:`~cchess.engine.SimpleEngine` instance.
        """
        return cls.popen(XBoardProtocol, command, timeout=timeout, debug=debug, setpgrp=setpgrp, **popen_args)

    def __enter__(self) -> SimpleEngine:
        return self

    def __exit__(self, exc_type: Optional[Type[BaseException]], exc_value: Optional[BaseException], traceback: Optional[TracebackType]) -> None:
        self.close()

    def __repr__(self) -> str:
        pid = self.transport.get_pid()  # This happens to be thread-safe
        return f"<{type(self).__name__} (pid={pid})>"


class SimpleAnalysisResult:
    """
    Synchronous wrapper around :class:`~cchess.engine.AnalysisResult`. Returned
    by :func:`cchess.engine.SimpleEngine.analysis()`.
    """

    def __init__(self, simple_engine: SimpleEngine, inner: AnalysisResult) -> None:
        self.simple_engine = simple_engine
        self.inner = inner

    @property
    def info(self) -> InfoDict:
        with self.simple_engine._not_shut_down():
            coro = _async(lambda: self.inner.info.copy())
            future = asyncio.run_coroutine_threadsafe(coro, self.simple_engine.protocol.loop)
        return future.result()

    @property
    def multipv(self) -> List[InfoDict]:
        with self.simple_engine._not_shut_down():
            coro = _async(lambda: [info.copy() for info in self.inner.multipv])
            future = asyncio.run_coroutine_threadsafe(coro, self.simple_engine.protocol.loop)
        return future.result()

    def stop(self) -> None:
        with self.simple_engine._not_shut_down():
            self.simple_engine.protocol.loop.call_soon_threadsafe(self.inner.stop)

    def wait(self) -> BestMove:
        with self.simple_engine._not_shut_down():
            future = asyncio.run_coroutine_threadsafe(self.inner.wait(), self.simple_engine.protocol.loop)
        return future.result()

    def would_block(self) -> bool:
        with self.simple_engine._not_shut_down():
            future = asyncio.run_coroutine_threadsafe(_async(self.inner.would_block), self.simple_engine.protocol.loop)
        return future.result()

    def empty(self) -> bool:
        with self.simple_engine._not_shut_down():
            future = asyncio.run_coroutine_threadsafe(_async(self.inner.empty), self.simple_engine.protocol.loop)
        return future.result()

    def get(self) -> InfoDict:
        with self.simple_engine._not_shut_down():
            future = asyncio.run_coroutine_threadsafe(self.inner.get(), self.simple_engine.protocol.loop)
        return future.result()

    def next(self) -> Optional[InfoDict]:
        with self.simple_engine._not_shut_down():
            future = asyncio.run_coroutine_threadsafe(self.inner.next(), self.simple_engine.protocol.loop)
        return future.result()

    def __iter__(self) -> Iterator[InfoDict]:
        with self.simple_engine._not_shut_down():
            self.simple_engine.protocol.loop.call_soon_threadsafe(self.inner.__aiter__)
        return self

    def __next__(self) -> InfoDict:
        try:
            with self.simple_engine._not_shut_down():
                future = asyncio.run_coroutine_threadsafe(self.inner.__anext__(), self.simple_engine.protocol.loop)
            return future.result()
        except StopAsyncIteration:
            raise StopIteration

    def __enter__(self) -> SimpleAnalysisResult:
        return self

    def __exit__(self, exc_type: Optional[Type[BaseException]], exc_value: Optional[BaseException], traceback: Optional[TracebackType]) -> None:
        self.stop()
