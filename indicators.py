"""
Indicator calculations that replicate TradingView's Pine Script behaviour.

Supertrend:
  - ATR via Wilder's RMA  (alpha = 1/period), same as Pine ta.rma()
  - Bands use hl2 = (high + low) / 2

SMA:
  - Simple moving average on close
"""
import numpy as np
import pandas as pd


def rma(series: pd.Series, period: int) -> pd.Series:
    """
    Wilder's Smoothing / RMA – equivalent to Pine ta.rma().
    alpha = 1/period, seed with simple average of first `period` values.
    """
    if period <= 0:
        raise ValueError(f"rma() period must be > 0, got {period}")
    result = np.full(len(series), np.nan)
    values = series.values.astype(float)
    for i in range(len(values)):
        if i < period:
            if i == period - 1:
                result[i] = np.mean(values[:period])
        else:
            result[i] = (values[i] + (period - 1) * result[i - 1]) / period
    return pd.Series(result, index=series.index)


def atr(df: pd.DataFrame, period: int) -> pd.Series:
    """Average True Range using RMA."""
    high, low, prev_close = df["high"], df["low"], df["close"].shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return rma(tr, period)


def supertrend(df: pd.DataFrame, period: int, multiplier: float) -> pd.Series:
    """
    Supertrend – replicates Pine ta.supertrend().
    Returns a Series of supertrend values (same index as df).
    """
    hl2         = (df["high"] + df["low"]) / 2
    atr_values  = atr(df, period)

    basic_upper = hl2 + multiplier * atr_values
    basic_lower = hl2 - multiplier * atr_values

    upper = basic_upper.copy()
    lower = basic_lower.copy()
    st    = pd.Series(np.nan, index=df.index)
    close = df["close"].values

    for i in range(1, len(df)):
        # Skip candles where ATR has not yet seeded (basic bands still NaN)
        if np.isnan(basic_upper.iloc[i]):
            continue

        # ── First valid candle: previous bands were NaN, just plant the seed ──
        if np.isnan(upper.iloc[i - 1]):
            upper.iloc[i] = basic_upper.iloc[i]
            lower.iloc[i] = basic_lower.iloc[i]
            # Neutral start: treat as bearish so first real bar decides direction
            st.iloc[i] = upper.iloc[i]
            continue

        # ── Normal band locking (Pine Script logic) ───────────────────────────
        # Upper band only tightens; resets when previous close breaks above it
        upper.iloc[i] = (
            basic_upper.iloc[i]
            if basic_upper.iloc[i] < upper.iloc[i - 1] or close[i - 1] > upper.iloc[i - 1]
            else upper.iloc[i - 1]
        )
        # Lower band only rises; resets when previous close breaks below it
        lower.iloc[i] = (
            basic_lower.iloc[i]
            if basic_lower.iloc[i] > lower.iloc[i - 1] or close[i - 1] < lower.iloc[i - 1]
            else lower.iloc[i - 1]
        )

        # ── Supertrend direction ──────────────────────────────────────────────
        prev_st = st.iloc[i - 1]
        if np.isnan(prev_st) or prev_st == upper.iloc[i - 1]:
            # Was on upper band (downtrend): stay unless close breaks above
            st.iloc[i] = upper.iloc[i] if close[i] <= upper.iloc[i] else lower.iloc[i]
        else:
            # Was on lower band (uptrend): stay unless close breaks below
            st.iloc[i] = lower.iloc[i] if close[i] >= lower.iloc[i] else upper.iloc[i]

    return st


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def compute_signals(df: pd.DataFrame, st1_period: int, st1_factor: float,
                    st2_period: int, st2_factor: float, ma_length: int) -> pd.DataFrame:
    """
    Add indicator columns to df and return the last row's signal summary.
    Returns the full df with added columns: st1, st2, ma, signal
      signal: 'BUY' | 'SELL' | 'EXIT_LONG' | 'EXIT_SHORT' | None
    """
    df = df.copy()
    df["st1"] = supertrend(df, st1_period, st1_factor)
    df["st2"] = supertrend(df, st2_period, st2_factor)
    df["ma"]  = sma(df["close"], ma_length)

    c   = df["close"]
    st1 = df["st1"]
    st2 = df["st2"]
    ma  = df["ma"]

    buy_cond  = (c > st1) & (c > st2) & (c > ma)
    sell_cond = (c < st1) & (c < st2) & (c < ma)
    exit_long  = c < st1
    exit_short = c > st1

    conditions = [buy_cond, sell_cond, exit_long, exit_short]
    choices    = ["BUY",    "SELL",    "EXIT_LONG", "EXIT_SHORT"]
    df["signal"] = np.select(conditions, choices, default=None)

    return df
