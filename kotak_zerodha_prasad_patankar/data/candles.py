import pandas as pd
import time
import logging
from datetime import datetime, timedelta
from utility.common_functions import safe_execute
import sys
import os
import threading
# ---------------- CONFIG ---------------- #

MAX_RETRIES = 5
RETRY_DELAY = 1

def get_candles( ctx, instrument_token, interval, days, name="" ):
    to_date = datetime.now()
    from_date = to_date - timedelta(days=days)
    res = fetch_candle_data( ctx, instrument_token, interval, from_date, to_date, name=name )
    if not res:
        logging.warning( f"{name}: No candle data received" )
        return None
    df = pd.DataFrame(res)
    df.rename(
        columns={ "date": "timestamp", "open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume" }, inplace=True)
    df["timestamp"] = pd.to_datetime( df["timestamp"] )
    df = ( df.sort_values("timestamp") .reset_index(drop=True) )
    if len(df) < 2:
        return None
    return df

def fetch_candle_data(ctx, instrument_token, interval, from_date, to_date, name="" ):  
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logging.info( f"CANDLE API REQUEST | " f"{interval} | " f"{instrument_token} | " f"Attempt={attempt}" )
            res = ctx.kiteobj.historical_data( instrument_token, from_date, to_date, interval )
            if res:
                return res
            logging.warning( f"{name} candle empty, retry {attempt}" )
        except Exception as e:
            logging.error( f"{name} candle fetch failed: {e}", exc_info=True )
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY)
    return None