"""Pure, chunk-aware market metric calculations."""

from collections.abc import Iterable
from decimal import Decimal
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
