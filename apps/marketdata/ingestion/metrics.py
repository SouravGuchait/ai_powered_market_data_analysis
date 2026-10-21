"""Pure, chunk-aware market metric calculations."""

from collections import deque
from collections.abc import Iterable
from decimal import Decimal
from math import sqrt
from typing import Protocol

import pandas as pd

from apps.marketdata.models import IngestionJob


class MetricCalculator(Protocol):
    def on_job_completed(self, *, job: IngestionJob) -> None:
        """Persist or refresh metrics derived from the job's accepted rows."""


def calculate_daily_returns(
    frame: pd.DataFrame,
    previous_closes: dict[str, Decimal] | None = None,
) -> tuple[pd.Series, dict[str, Decimal]]:
    """Return fractions in original row order and updated per-symbol state.

    Rows with missing or non-positive closes do not advance state. Duplicate
    (symbol, timestamp) keys are rejected; callers should exclude invalid rows
    before calculation. Sorting is stable so ties in input order are preserved.
    """
    state = dict(previous_closes or {})
    returns = pd.Series(float("nan"), index=frame.index, dtype="float64")
    if frame.empty:
        return returns, state

    ordered = frame.assign(_position=range(len(frame))).sort_values(
        ["symbol", "timestamp"], kind="stable"
    )
    if ordered.duplicated(["symbol", "timestamp"]).any():
        raise ValueError("duplicate symbol/timestamp observations")

    for symbol_value, timestamp, close_value, position in ordered[
        ["symbol", "timestamp", "close", "_position"]
    ].itertuples(index=False, name=None):
        symbol = str(symbol_value)
        close = _positive_decimal(close_value)
        if close is None:
            continue
        prior = state.get(symbol)
        if prior is not None and prior > 0:
            returns.iloc[int(position)] = float((close - prior) / prior)
        state[symbol] = close
    return returns, state


def _positive_decimal(value: object) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (ValueError, ArithmeticError):
        return None
    return result if result.is_finite() and result > 0 else None


def iter_daily_returns(
    chunks: Iterable[pd.DataFrame],
) -> Iterable[tuple[pd.DataFrame, pd.Series]]:
    """Yield each input chunk with returns continuous across chunk boundaries."""
    previous: dict[str, Decimal] = {}
    for chunk in chunks:
        returns, previous = calculate_daily_returns(chunk, previous)
        yield chunk, returns


def calculate_moving_average(
    frame: pd.DataFrame,
    window: int = 5,
    previous_closes: dict[str, list[Decimal]] | None = None,
) -> tuple[pd.Series, dict[str, list[Decimal]]]:
    """Calculate an observation-based full-window SMA in original row order."""
    if window < 1:
        raise ValueError("window must be positive")
    state = {
        symbol: deque(values[-(window - 1) :] if window > 1 else [], maxlen=window)
        for symbol, values in (previous_closes or {}).items()
    }
    values = pd.Series(float("nan"), index=frame.index, dtype="float64")
    if frame.empty:
        return values, {symbol: list(closes) for symbol, closes in state.items()}

    ordered = frame.assign(_position=range(len(frame))).sort_values(
        ["symbol", "timestamp"], kind="stable"
    )
    if ordered.duplicated(["symbol", "timestamp"]).any():
        raise ValueError("duplicate symbol/timestamp observations")
    for symbol_value, close_value, position in ordered[
        ["symbol", "close", "_position"]
    ].itertuples(index=False, name=None):
        symbol = str(symbol_value)
        close = _positive_decimal(close_value)
        if close is None:
            continue
        closes = state.setdefault(symbol, deque(maxlen=window))
        closes.append(close)
        if len(closes) == window:
            values.iloc[int(position)] = float(sum(closes) / Decimal(window))
    return values, {symbol: list(closes) for symbol, closes in state.items()}


def iter_moving_averages(
    chunks: Iterable[pd.DataFrame], window: int = 5
) -> Iterable[tuple[pd.DataFrame, pd.Series]]:
    """Yield SMA values with at most window-1 closes retained per symbol."""
    previous: dict[str, list[Decimal]] = {}
    for chunk in chunks:
        averages, previous = calculate_moving_average(chunk, window, previous)
        yield chunk, averages


def calculate_drawdowns(
    frame: pd.DataFrame,
    peaks: dict[str, Decimal] | None = None,
    minimum_drawdowns: dict[str, Decimal] | None = None,
) -> tuple[pd.Series, dict[str, Decimal], dict[str, Decimal]]:
    """Return cumulative running maximum drawdown per symbol, in input order."""
    running_peaks = dict(peaks or {})
    minima = dict(minimum_drawdowns or {})
    values = pd.Series(float("nan"), index=frame.index, dtype="float64")
    if frame.empty:
        return values, running_peaks, minima

    ordered = frame.assign(_position=range(len(frame))).sort_values(
        ["symbol", "timestamp"], kind="stable"
    )
    if ordered.duplicated(["symbol", "timestamp"]).any():
        raise ValueError("duplicate symbol/timestamp observations")
    for symbol_value, close_value, position in ordered[
        ["symbol", "close", "_position"]
    ].itertuples(index=False, name=None):
        symbol = str(symbol_value)
        close = _positive_decimal(close_value)
        if close is None:
            continue
        peak = max(running_peaks.get(symbol, close), close)
        drawdown = (close - peak) / peak
        running_peaks[symbol] = peak
        minima[symbol] = min(minima.get(symbol, drawdown), drawdown)
        values.iloc[int(position)] = float(minima[symbol])
    return values, running_peaks, minima


def iter_drawdowns(
    chunks: Iterable[pd.DataFrame],
) -> Iterable[tuple[pd.DataFrame, pd.Series]]:
    """Yield cumulative drawdowns with peak and minimum state across chunks."""
    peaks: dict[str, Decimal] = {}
    minima: dict[str, Decimal] = {}
    for chunk in chunks:
        values, peaks, minima = calculate_drawdowns(chunk, peaks, minima)
        yield chunk, values


def calculate_rolling_volatility(
    frame: pd.DataFrame,
    window: int = 20,
    previous_state: dict[str, tuple[Decimal | None, list[float]]] | None = None,
) -> tuple[pd.Series, dict[str, tuple[Decimal | None, list[float]]]]:
    """Return annualized sample volatility from full valid-return windows.

    State holds each symbol's last valid close and up to ``window - 1`` prior
    fractional returns, allowing callers to continue across chunks.
    """
    if window < 2:
        raise ValueError("window must be at least 2")
    state = {
        symbol: (close, deque(returns[-(window - 1) :], maxlen=window))
        for symbol, (close, returns) in (previous_state or {}).items()
    }
    values = pd.Series(float("nan"), index=frame.index, dtype="float64")
    if frame.empty:
        return values, {
            symbol: (close, list(returns)) for symbol, (close, returns) in state.items()
        }

    ordered = frame.assign(_position=range(len(frame))).sort_values(
        ["symbol", "timestamp"], kind="stable"
    )
    if ordered.duplicated(["symbol", "timestamp"]).any():
        raise ValueError("duplicate symbol/timestamp observations")
    for symbol_value, close_value, position in ordered[
        ["symbol", "close", "_position"]
    ].itertuples(index=False, name=None):
        symbol = str(symbol_value)
        close = _positive_decimal(close_value)
        if close is None:
            continue
        prior, returns = state.setdefault(symbol, (None, deque(maxlen=window)))
        if prior is not None and prior > 0:
            returns.append(float((close - prior) / prior))
            if len(returns) == window:
                mean = sum(returns) / window
                daily_sample_std = sqrt(sum((value - mean) ** 2 for value in returns) / (window - 1))
                values.iloc[int(position)] = daily_sample_std * sqrt(252)
        state[symbol] = (close, returns)
    return values, {
        symbol: (close, list(returns)) for symbol, (close, returns) in state.items()
    }


def iter_rolling_volatility(
    chunks: Iterable[pd.DataFrame], window: int = 20
) -> Iterable[tuple[pd.DataFrame, pd.Series]]:
    """Yield annualized volatility with bounded state across input chunks."""
    previous: dict[str, tuple[Decimal | None, list[float]]] = {}
    for chunk in chunks:
        volatility, previous = calculate_rolling_volatility(chunk, window, previous)
        yield chunk, volatility
