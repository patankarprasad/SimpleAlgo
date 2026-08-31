import pandas as pd
from data.indicators import calculate_supertrend,ADX
import logging

def get_indicator_values(candledf2):

    # logging.info("LAST 10 CANDLES RECEIVED FROM API")
    # logging.info( "\n%s", candledf2[["timestamp", "Open", "High", "Low", "Close"]].tail(10) )

    candles = candledf2.iloc[:-1].copy()

    logging.info(
                    f"LATEST CANDLE USED FOR INDICATORS => "
                    f"{candles.iloc[-1]['timestamp']} | "
                    f"Close={candles.iloc[-1]['Close']}"
                )

    # -------- PRICE --------
    latest_close   = candles['Close'].iloc[-1]
    previous_close = candles['Close'].iloc[-2]

    # -------- SMA --------
    sma_period = 50
    sma_series = candles['Close'].rolling(sma_period).mean()

    latest_sma   = round(float(sma_series.iloc[-1]), 2)
    previous_sma = round(float(sma_series.iloc[-2]), 2)

    # -------- SUPER TREND (full series, temp only) --------
    st102_series = calculate_supertrend( candles, period=10, multiplier=2, j='ST102' )['ST102']
    st103_series = calculate_supertrend( candles, period=10, multiplier=3, j='ST103' )['ST103']
    st104_series = calculate_supertrend( candles, period=10, multiplier=4, j='ST104' )['ST104']

    latest_st102   = round(float(st102_series.iloc[-1]), 2)
    previous_st102 = round(float(st102_series.iloc[-2]), 2)

    latest_st103   = round(float(st103_series.iloc[-1]), 2)
    previous_st103 = round(float(st103_series.iloc[-2]), 2)

    latest_st104   = round(float(st104_series.iloc[-1]), 2)
    previous_st104 = round(float(st104_series.iloc[-2]), 2)

    # -------- ADX (limited lookback, aligned) --------
    adx_df = ADX(candles.copy().reset_index(drop=True),14)
    latest_adx   = round(float(adx_df['ADX'].iloc[-1]), 2)
    previous_adx = round(float(adx_df['ADX'].iloc[-2]), 2)
    
    # logging.info(f"Previous Close = {previous_close}")
    # logging.info(f"Previous ADX = {previous_adx}")
    # logging.info(f"Previous ST102 = {previous_st102}")
    # logging.info(f"Previous ST103 = {previous_st103}")
    # logging.info(f"Previous ST104 = {previous_st104}")
    # logging.info(f"Previous SMA = {previous_sma}")

    logging.info(f"Latest Close = {latest_close}")
    logging.info(f"Latest ADX = {latest_adx}")
    logging.info(f"Latest ST102 = {latest_st102}")
    logging.info(f"Latest ST103 = {latest_st103}")
    logging.info(f"Latest ST104 = {latest_st104}")
    logging.info(f"Latest SMA = {latest_sma}")

    return {"previous_close" : previous_close,
            "previous_adx" : previous_adx,
            "previous_st102" : previous_st102,
            "previous_st103" : previous_st103,
            "previous_st104" : previous_st104,
            "previous_sma" : previous_sma,
            "latest_close" : latest_close,
            "latest_adx" : latest_adx,
            "latest_st102" : latest_st102,
            "latest_st103" : latest_st103,
            "latest_st104" : latest_st104,
            "latest_sma" : latest_sma}

    