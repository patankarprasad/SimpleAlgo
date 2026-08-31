import numpy as np
import pandas as pd
import logging



def ATR(df, period):
    high = df["High"].values
    low = df["Low"].values
    close = df["Close"].values

    n = len(df)

    tr = np.zeros(n)
    tr[1:] = np.maximum.reduce([
        high[1:] - low[1:],
        np.abs(high[1:] - close[:-1]),
        np.abs(low[1:] - close[:-1])
    ])

    atr = np.full(n, np.nan)

    if n > period:
        atr[period] = tr[1:period+1].mean()

        for i in range(period + 1, n):
            atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period

    df["TR_PY"] = np.round(tr, 2)
    
    return tr, atr

def calculate_supertrend(df, period=10, multiplier=2, j="ST"):
    df = df.copy()
    n = len(df)

    tr, atr = ATR(df, period)

    high = df["High"].values
    low = df["Low"].values
    close = df["Close"].values

    mid = (high + low) / 2
    ub = mid + multiplier * atr
    lb = mid - multiplier * atr

    fub = np.zeros(n)
    flb = np.zeros(n)
    st = np.zeros(n)

    if n > period:
        fub[period] = ub[period]
        flb[period] = lb[period]
        st[period] = fub[period]

        for i in range(period + 1, n):

            fub[i] = ub[i] if (ub[i] < fub[i-1] or close[i-1] > fub[i-1]) else fub[i-1]
            flb[i] = lb[i] if (lb[i] > flb[i-1] or close[i-1] < flb[i-1]) else flb[i-1]

            if st[i-1] == fub[i-1]:
                st[i] = fub[i] if close[i] <= fub[i] else flb[i]
            else:
                st[i] = flb[i] if close[i] >= flb[i] else fub[i]

    df[f"ATR_{period}"] = np.round(atr, 2)
    df[j] = np.round(st, 2)

    return df

def ADX(df, period=14):

    df = df.copy()

    high = df["High"].values
    low = df["Low"].values
    # close = df["Close"].values

    n = len(df)

    # ==================================================
    # USE SAME ATR ENGINE AS SUPERTREND
    # ==================================================

    tr, atr = ATR(df, period)

    # ==================================================
    # DIRECTIONAL MOVEMENT
    # ==================================================

    upmove = np.diff(high, prepend=np.nan)
    downmove = -np.diff(low, prepend=np.nan)

    dm_plus = np.where(
        (upmove > downmove) & (upmove > 0),
        upmove,
        0.0
    )

    dm_minus = np.where(
        (downmove > upmove) & (downmove > 0),
        downmove,
        0.0
    )

    # ==================================================
    # WILDER SMOOTHING OF DM
    # ==================================================

    sm_dm_plus = np.full(n, np.nan)
    sm_dm_minus = np.full(n, np.nan)

    if n > period:

        sm_dm_plus[period] = np.sum(dm_plus[1:period+1])
        sm_dm_minus[period] = np.sum(dm_minus[1:period+1])

        for i in range(period + 1, n):

            sm_dm_plus[i] = (
                sm_dm_plus[i-1]
                - (sm_dm_plus[i-1] / period)
                + dm_plus[i]
            )

            sm_dm_minus[i] = (
                sm_dm_minus[i-1]
                - (sm_dm_minus[i-1] / period)
                + dm_minus[i]
            )

    # ==================================================
    # DI
    # ==================================================

    di_plus = 100 * (sm_dm_plus / atr)
    di_minus = 100 * (sm_dm_minus / atr)

    # ==================================================
    # DX
    # ==================================================

    dx = 100 * (
        np.abs(di_plus - di_minus)
        / (di_plus + di_minus)
    )

    # ==================================================
    # ADX
    # ==================================================

    adx = np.full(n, np.nan)

    if n > (period * 2):

        adx[period * 2] = np.nanmean(
            dx[period+1:period*2+1]
        )

        for i in range(period * 2 + 1, n):

            adx[i] = (
                (
                    adx[i-1] * (period - 1)
                ) + dx[i]
            ) / period

    # ==================================================
    # SAVE COLUMNS
    # ==================================================

    # df[f"DI_PLUS_{period}"] = np.round(di_plus, 2)
    # df[f"DI_MINUS_{period}"] = np.round(di_minus, 2)
    df["ADX"] = np.round(adx, 2)

    return df

def calculate_pivots_from_candles(df_daily, df_3m):

    if df_daily is None or df_3m is None:
        return None

    if len(df_daily) < 2:
        return None

    df_daily = df_daily.sort_values("timestamp")
    df_3m = df_3m.sort_values("timestamp")

    # logging.info( "Daily rows=%s, 3m rows=%s", len(df_daily), len(df_3m) )
    # logging.info( "Last daily date=%s", df_daily.iloc[-1]["timestamp"] )
    # logging.info( "Second last daily date=%s", df_daily.iloc[-2]["timestamp"] )

    # # we had encountered an issue where pivot calculation went wrong
    # # possible reason was it skipped the daily candle unnecessarily
    # # its possible at 9:15:02, when we get the candledata, new day candledata is not formed, so no reason to skip 2 candles
    # # we will decide with the logging if it is really the case
    # # otherwise we will have to harden the daily candle selection like shown below
    # today = datetime.now().date()
    # if df_daily.iloc[-1]["timestamp"].date() == today:
    #     prev_day = df_daily.iloc[-2]
    # else:
    #     prev_day = df_daily.iloc[-1]

    prev_day = df_daily.iloc[-2]

    # logging.info( f"SELECTED PREV_DAY => " f"Date={prev_day['timestamp']}, " f"High={prev_day['High']}, " f"Low={prev_day['Low']}, " f"Close={prev_day['Close']}" )
    
    high = prev_day["High"]
    low = prev_day["Low"]
    pivot_date = prev_day["timestamp"].date()

    day_3m = df_3m[df_3m["timestamp"].dt.date == pivot_date]

    # logging.info( "3m candles found for pivot date=%s : %s", pivot_date, len(day_3m) )
    # logging.info( "\n%s", day_3m[["timestamp", "Close"]].tail(10) )

    if day_3m.empty:
        return None

    close = day_3m.iloc[-1]["Close"]

    PP = round((high + low + close) / 3, 2)
    R1 = round((2 * PP) - low, 2)
    S1 = round((2 * PP) - high, 2)

    # logging.info(
    #                 f"\nPIVOT DEBUG\n"
    #                 f"LastDaily      = {df_daily.iloc[-1]['timestamp']}\n"
    #                 f"SecondLastDaily= {df_daily.iloc[-2]['timestamp']}\n"
    #                 f"SelectedDate   = {prev_day['timestamp']}\n"
    #                 f"High           = {high}\n"
    #                 f"Low            = {low}\n"
    #                 f"DailyClose     = {prev_day['Close']}\n"
    #                 f"Last3mClose    = {day_3m.iloc[-1]['Close']}\n"
    #                 f"PP             = {PP}\n"
    #                 f"R1             = {R1}\n"
    #                 f"S1             = {S1}"
    #             )
    
    return {
        "PP": PP,
        "R1": R1,
        "S1": S1,
        "pivot_date": pivot_date,
    }
