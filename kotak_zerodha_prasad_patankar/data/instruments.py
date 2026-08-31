import pandas as pd
import requests
import logging
from datetime import date, datetime, timedelta
from dateutil.relativedelta import relativedelta
import json
import time
import sys

# as per discussion with avadhoot, we need to select 100 multiple strikes for carry-fwd positions viz. NCL, NCS and NHF
# for 100 multiples of strikes, we can simply get nifty_ltp -> multiply and divide by 100 and not by 50
# for CP300 etc, we multiply and divide by 100, but strike range shoould be 1200 instead of 400


def load_kotak_scrip_master(ctx):
    """
    Loads Kotak scrip master and stores token_df + expiry helpers in ctx
    """

    today_date = datetime.now().strftime('%Y-%m-%d')
    # today_date = (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d')
    
    # -----------------------------
    # NIFTY OPTIONS
    # -----------------------------
    knfoUrl = f"https://lapi.kotaksecurities.com/wso2-scripmaster/v1/prod/{today_date}/transformed/nse_fo.csv"
    # knfoUrl = f"https://lapi.kotaksecurities.com/wso2-scripmaster/v1/prod/2026-06-09/transformed/nse_fo.csv"
    # logging.info("Downloading Kotak scrip master...")
    knfodf = pd.read_csv(knfoUrl, usecols=lambda c: c.strip().replace(";", "") in ["pSymbol","pInstType","pSymbolName","lExpiryDate","lLotSize","dStrikePrice","pOptionType","pTrdSymbol"]) # select wanted columns in the beginning
    knfodf.columns = [c.strip().replace(";", "") for c in knfodf.columns.values.tolist()] # cleanup the column names
    nifty_df = knfodf[(knfodf["pInstType"] == "OPTIDX") & (knfodf["pSymbolName"] == "NIFTY")].copy() # filter out the things you want 
    # nifty_df["lExpiryDate"] = (pd.to_datetime(nifty_df["lExpiryDate"], unit="s").apply(lambda x: x.date() + relativedelta(years=10))) # rationalize the expiry date column


    # THIS SECTIONS NEEDS TO BE ADDED SPECIFICALLY FOR NSE AS THE EXPIRY DAY MOVE 1 DAY FORWARD. THUS WE HAVE TI GET "REAL_EXPIRY" BY SUBTRACTING 1 DAY
    expiry_ts = pd.to_datetime(nifty_df["lExpiryDate"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    expiry_plus10 = expiry_ts + pd.DateOffset(years=10)
    real_expiry = (expiry_plus10 - pd.Timedelta(days=0)).dt.date
    
    nifty_df["lExpiryDate"] = real_expiry
 
    ctx.token_df = nifty_df

    if nifty_df.empty:
        raise RuntimeError("No NIFTY instruments found in Kotak scrip master")

    nifty_expiry_list = sorted(nifty_df["lExpiryDate"].dropna().unique())

    if len(nifty_expiry_list) < 1:
        raise RuntimeError("No NIFTY expiries found in scrip master")
    
    ctx.nearest_nifty_expiry_date = nifty_expiry_list[0]
    ctx.next_nifty_expiry_date = nifty_expiry_list[1] if len(nifty_expiry_list) > 1 else None
    ctx.far_nifty_expiry_date = nifty_expiry_list[2] if len(nifty_expiry_list) > 2 else None
  
    today = date.today()
    # only future/active expiries
    future_expiries = [ d for d in nifty_expiry_list if d >= today ]
    # current active expiry month
    active_month = future_expiries[0].month
    active_year = future_expiries[0].year
    month_expiries = [ d for d in future_expiries if d.month == active_month and d.year == active_year ]
    ctx.nifty_monthend_expiry_date = max(month_expiries)

    # next active expiry month
    remaining_expiries = [ d for d in future_expiries if not ( d.month == active_month and d.year == active_year ) ]
    if remaining_expiries:
        next_month = remaining_expiries[0].month
        next_year = remaining_expiries[0].year
        next_month_expiries = [ d for d in remaining_expiries if d.month == next_month and d.year == next_year ]
        ctx.nifty_next_monthend_expiry_date = max(next_month_expiries)
    else:
        ctx.nifty_next_monthend_expiry_date = None

    # Lot size
    nifty_near_lot = int(nifty_df.loc[nifty_df["lExpiryDate"] == ctx.nearest_nifty_expiry_date,"lLotSize"].iloc[0])
    ctx.nifty_near_lot = nifty_near_lot

    nifty_next_lot = int(nifty_df.loc[nifty_df["lExpiryDate"] == ctx.next_nifty_expiry_date,"lLotSize"].iloc[0])
    ctx.nifty_next_lot = nifty_next_lot

    nifty_monthend_lot = int(nifty_df.loc[nifty_df["lExpiryDate"] == ctx.nifty_monthend_expiry_date,"lLotSize"].iloc[0])
    ctx.nifty_monthend_lot = nifty_monthend_lot

    nifty_next_monthend_lot = int(nifty_df.loc[nifty_df["lExpiryDate"] == ctx.nifty_next_monthend_expiry_date,"lLotSize"].iloc[0])
    ctx.nifty_next_monthend_lot = nifty_next_monthend_lot

    logging.info(f"Nearest NIFTY expiry: {ctx.nearest_nifty_expiry_date} and Lot size: {ctx.nifty_near_lot}")
    logging.info(f"Next NIFTY expiry: {ctx.next_nifty_expiry_date} and Lot size: {ctx.nifty_next_lot}")
    logging.info(f"Monthend NIFTY expiry: {ctx.nifty_monthend_expiry_date} and Lot size: {ctx.nifty_monthend_lot}")
    logging.info(f"Next Monthend NIFTY expiry: {ctx.nifty_next_monthend_expiry_date} and Lot size: {ctx.nifty_next_monthend_lot}")

    # -----------------------------
    # SENSEX OPTIONS
    # -----------------------------
    kbfoUrl = f"https://lapi.kotaksecurities.com/wso2-scripmaster/v1/prod/{today_date}/transformed/bse_fo.csv"
    # kbfoUrl = f"https://lapi.kotaksecurities.com/wso2-scripmaster/v1/prod/2026-06-09/transformed/bse_fo.csv"

    # logging.info("Downloading Kotak scrip master...")
    kbfodf = pd.read_csv(kbfoUrl, usecols=lambda c: c.strip().replace(";", "") in ["pSymbol","pInstType","pSymbolName","lExpiryDate","lLotSize","dStrikePrice","pOptionType","pTrdSymbol"]) # select wanted columns in the beginning
    kbfodf.columns = [c.strip().replace(";", "") for c in kbfodf.columns.values.tolist()] # cleanup the column names
    sensex_df = kbfodf[ (kbfodf["pInstType"] == "IO") & (kbfodf["pSymbolName"] == "SENSEX")].copy() # filter out the things you want 
    sensex_df["lExpiryDate"] = (pd.to_datetime(sensex_df["lExpiryDate"], unit="s").apply(lambda x: x.date() + relativedelta(years=0))) # rationalize the expiry date column

    ctx.sensextoken_df = sensex_df

    if sensex_df.empty:
        raise RuntimeError("No SENSEX instruments found in Kotak scrip master")

    sensex_expiry_list = sorted(sensex_df["lExpiryDate"].dropna().unique())

    if len(sensex_expiry_list) < 1:
        raise RuntimeError("No SENSEX expiries found in scrip master")

    ctx.nearest_sensex_expiry_date = sensex_expiry_list[0]
    # ctx.next_sensex_expiry_date = sensex_expiry_list[1] if len(expiry_list) > 1 else None
    # ctx.far_sensex_expiry_date = sensex_expiry_list[2] if len(expiry_list) > 2 else None

    # Lot size from nearest expiry
    sensex_lot = int(sensex_df.loc[sensex_df["lExpiryDate"] == ctx.nearest_sensex_expiry_date, "lLotSize"].iloc[0])
    
    ctx.sensex_lot = sensex_lot

    logging.info(f"Nearest SENSEX expiry: {ctx.nearest_sensex_expiry_date} and Lot size: {ctx.sensex_lot}")

    # # -----------------------------
    # # COMMODITY OPTIONS
    # # -----------------------------
    kcmdtyUrl = f"https://lapi.kotaksecurities.com/wso2-scripmaster/v1/prod/{today_date}/transformed/mcx_fo.csv"
    
    # logging.info("Downloading Kotak scrip master...")
    kcmdtydf = pd.read_csv(kcmdtyUrl, usecols=lambda c: c.strip().replace(";", "") in ["pSymbol","pInstType","pSymbolName","lExpiryDate","lLotSize","dStrikePrice","pOptionType","pTrdSymbol"]) # select wanted columns in the beginning
    kcmdtydf.columns = [c.strip().replace(";", "") for c in kcmdtydf.columns.values.tolist()] # cleanup the column names
    kcmdtydf["lExpiryDate"] = ( pd.to_datetime(kcmdtydf["lExpiryDate"], unit="s", errors="coerce").dt.date)

    ctx.cmdtytoken_df = kcmdtydf[ kcmdtydf["pInstType"] == "FUTCOM" ].copy()
    def load_cmdty(symbol, prefix):
        df = ctx.cmdtytoken_df[ ctx.cmdtytoken_df["pSymbolName"] == symbol ].copy()
        if df.empty:
            logging.warning(f"{symbol} not found in Kotak scrip master")
            return
        expiry_list = sorted(df["lExpiryDate"].dropna().unique())
        setattr(ctx, f"nearest_{prefix}_expiry_date", expiry_list[0])
        setattr(ctx, f"next_{prefix}_expiry_date", expiry_list[1] if len(expiry_list) > 1 else None)
        near_row = df[df["lExpiryDate"] == expiry_list[0]].iloc[0]
        setattr(ctx, f"{prefix}scripmaster_near_lot", int(near_row["lLotSize"]))
        if len(expiry_list) > 1:
            next_row = df[df["lExpiryDate"] == expiry_list[1]].iloc[0]
            setattr(ctx, f"{prefix}scripmaster_next_lot", int(next_row["lLotSize"]))
        logging.info( f"{symbol}: Near={expiry_list[0]} Next={expiry_list[1] if len(expiry_list)>1 else None}" )
    load_cmdty(ctx.gold_instrument, "gold")
    load_cmdty(ctx.silver_instrument, "silver")
    load_cmdty(ctx.crude_instrument, "crude")

def load_zerodha_scrip_master(ctx):
    logging.info("Downloading Zerodha instrument master...")
    symboldf = pd.read_csv( "https://api.kite.trade/instruments", low_memory=False )
    symboldf["expiry"] = pd.to_datetime( symboldf["expiry"], errors="coerce" ).dt.date
    nifty_df = symboldf[ (symboldf["name"] == "NIFTY") & (symboldf["segment"] == "NFO-OPT") ].copy()
    sensex_df = symboldf[ (symboldf["name"] == "SENSEX") & (symboldf["segment"].str.contains("OPT", na=False)) ].copy()
    kite_df = pd.concat( [nifty_df, sensex_df], ignore_index=True )
    kite_df["option_type"] = kite_df["instrument_type"]
    ctx.kite_df = kite_df[ [ "expiry", "strike", "option_type", "instrument_token", "tradingsymbol", "name" ] ].copy()
    logging.info( f"Loaded {len(ctx.kite_df)} Zerodha option contracts" )

    ctx.kite_cmdty_df = symboldf[ symboldf["segment"] == "MCX-FUT" ].copy()
    def load_kite_cmdty(symbol, prefix):
        df = ctx.kite_cmdty_df[ ctx.kite_cmdty_df["name"] == symbol ].copy()
        if df.empty:
            logging.warning(f"{symbol} missing in Kite master")
            return
        expiry_list = sorted(df["expiry"].dropna().unique())
        setattr(ctx, f"nearest_kite_{prefix}_expiry_date", expiry_list[0])
        setattr(ctx, f"next_kite_{prefix}_expiry_date", expiry_list[1] if len(expiry_list) > 1 else None)
        setattr( ctx, f"nearest_kite_{prefix}_token", df.loc[ df["expiry"] == expiry_list[0], "instrument_token" ].iloc[0])
        if len(expiry_list) > 1:
            setattr( ctx, f"next_kite_{prefix}_token", df.loc[ df["expiry"] == expiry_list[1], "instrument_token" ].iloc[0] )
        logging.info( f"Kite {symbol}: Near={expiry_list[0]} Next={expiry_list[1] if len(expiry_list)>1 else None}" )
    load_kite_cmdty(ctx.gold_instrument, "gold")
    load_kite_cmdty(ctx.silver_instrument, "silver")
    load_kite_cmdty(ctx.crude_instrument, "crude")

def get_ltp(ctx, token):
    try:
        row = ctx.token_df[ ctx.token_df["pSymbol"].astype(str) == str(token) ]
        if row.empty:
            logging.error( f"Kotak token not found: {token}" )
            return None
        row = row.iloc[0]
        expiry = row["lExpiryDate"]
        # safety
        if not isinstance(expiry, date):
            expiry = pd.to_datetime(expiry).date()
        
        strike = row["dStrikePrice"] / 100
        option_type = row["pOptionType"]
        kite_match = ctx.kite_df[ (ctx.kite_df["expiry"] == expiry) & (ctx.kite_df["strike"] == strike) & (ctx.kite_df["option_type"] == option_type) ]

        if kite_match.empty:
            logging.error( f"No Zerodha mapping for token {token}" )
            return None
        kite_token = str( int( kite_match.iloc[0]["instrument_token"] ) )
        logging.info( f"kite token = {kite_token}" )
        res = ctx.kiteobj.ltp(kite_token)
        return res[kite_token]["last_price"]
    except Exception as e:
        logging.error( f"get_ltp failed: {e}", exc_info=True )
        return None


def get_nifty_ltp(ctx):
    try:
        res = ctx.kiteobj.ltp("256265")
        return res["256265"]["last_price"]
    except Exception as e:
        logging.error(f"get_nifty_ltp failed: {e}")
        return None

def get_sensex_ltp(ctx):
    try:
        res = ctx.kiteobj.ltp("265")
        return res["265"]["last_price"]
    except Exception as e:
        logging.error(f"get_sensex_ltp failed: {e}")
        return None
    

def get_nifty_strike(ctx, itm_offset=0): 
    # For ATM → itm_offset = 0 
    # For OTM call and ITM put → itm_offset = positive 
    # For ITM call and OTM put → itm_offset = negative
    
    nifty_ltp = get_nifty_ltp(ctx)

    if nifty_ltp is None:
        logging.error("Failed to get NIFTY LTP")
        sys.exit(0)
        return None

    atmstrike = round(nifty_ltp / 50) * 50
    # logging.info(f"NIFTY ATM strike selected: {atmstrike}")
    strike = atmstrike + int(itm_offset)

    logging.info(f"NIFTY strike selected: {strike}")

    return strike

def get_nifty_100strike(ctx, itm_offset=0): 
    # For ATM → itm_offset = 0 
    # For OTM call and ITM put → itm_offset = positive 
    # For ITM call and OTM put → itm_offset = negative
    
    nifty_ltp = get_nifty_ltp(ctx)

    if nifty_ltp is None:
        logging.error("Failed to get NIFTY LTP")
        sys.exit(0)
        return None

    atmstrike = round(nifty_ltp / 100) * 100
    # logging.info(f"NIFTY ATM strike selected: {atmstrike}")
    strike = atmstrike + int(itm_offset)

    logging.info(f"NIFTY strike selected: {strike}")

    return strike

def get_sensex_strike(ctx, itm_offset=0):
    # For ATM → itm_offset = 0 
    # For OTM call and ITM put → itm_offset = positive 
    # For ITM call and OTM put → itm_offset = negative
    
    sensex_ltp = get_sensex_ltp(ctx)

    if sensex_ltp is None:
        logging.error("Failed to get SENSEX LTP")
        sys.exit(0)
        return None

    atmstrike = round(sensex_ltp / 100) * 100
    # logging.info(f"SENSEX ATM strike selected: {atmstrike}")
    strike = atmstrike + int(itm_offset)

    logging.info(f"SENSEX strike selected: {strike}")

    return strike


def get_nifty_option_symbol_token(ctx, strike, option_type):
    df = ctx.token_df
    row = df[
        (df["pInstType"] == "OPTIDX") &
        (df["pSymbolName"] == "NIFTY") &
        (df["dStrikePrice"] == strike*100) &
         (df["pOptionType"] == option_type) &
        (df["lExpiryDate"] == ctx.nearest_nifty_expiry_date)]
    if row.empty:
        return None
    r = row.iloc[0]

    return {
        "trdSym": r["pTrdSymbol"],
        "tok": r["pSymbol"]
    }

def get_sensex_option_symbol_token(ctx, strike, option_type):
    df = ctx.sensextoken_df
    row = df[
        (df["pInstType"] == "IO") &
        (df["pSymbolName"] == "SENSEX") &
        (df["dStrikePrice"] == strike*100) &
         (df["pOptionType"] == option_type) &
        (df["lExpiryDate"] == ctx.nearest_sensex_expiry_date)]
    if row.empty:
        return None
    r = row.iloc[0]

    return {
        "trdSym": r["pTrdSymbol"],
        "tok": r["pSymbol"]
    }

def get_next_nifty_option_symbol_token(ctx, strike, option_type):
    df = ctx.token_df
    row = df[
        (df["pInstType"] == "OPTIDX") &
        (df["pSymbolName"] == "NIFTY") &
        (df["dStrikePrice"] == strike*100) &
         (df["pOptionType"] == option_type) &
        (df["lExpiryDate"] == ctx.next_nifty_expiry_date)]
    if row.empty:
        return None
    r = row.iloc[0]

    return {
        "trdSym": r["pTrdSymbol"],
        "tok": r["pSymbol"]
    }

def get_monthend_option_symbol_token(ctx, strike, option_type):
    df = ctx.token_df
    row = df[
        (df["pInstType"] == "OPTIDX") &
        (df["pSymbolName"] == "NIFTY") &
        (df["dStrikePrice"] == strike*100) &
         (df["pOptionType"] == option_type) &
        (df["lExpiryDate"] == ctx.nifty_monthend_expiry_date)]
    if row.empty:
        return None
    r = row.iloc[0]

    return {
        "trdSym": r["pTrdSymbol"],
        "tok": r["pSymbol"]
    }

def get_next_monthend_option_symbol_token(ctx, strike, option_type):
    df = ctx.token_df
    row = df[
        (df["pInstType"] == "OPTIDX") &
        (df["pSymbolName"] == "NIFTY") &
        (df["dStrikePrice"] == strike*100) &
         (df["pOptionType"] == option_type) &
        (df["lExpiryDate"] == ctx.nifty_next_monthend_expiry_date)]
    if row.empty:
        return None
    r = row.iloc[0]

    return {
        "trdSym": r["pTrdSymbol"],
        "tok": r["pSymbol"]
    }


def get_nifty_strike_by_premium(ctx, option_type, target_premium, strike_range_pts=800):

    nifty_ltp = get_nifty_ltp(ctx)
   
    if nifty_ltp is None:
        logging.error("NIFTY LTP not available")
        sys.exit(0)
        return None
    atm = round(nifty_ltp / 50) * 50
    strike_range = range( atm - strike_range_pts, atm + strike_range_pts + 1, 50 )
    df = ctx.token_df[
        (ctx.token_df["pSymbolName"] == "NIFTY") &
        (ctx.token_df["pInstType"] == "OPTIDX") &
        (ctx.token_df["pOptionType"] == option_type) &
        (ctx.token_df["lExpiryDate"] == ctx.nearest_nifty_expiry_date) &
        ((ctx.token_df["dStrikePrice"] / 100).isin(strike_range)) ].copy()
    if df.empty:
        logging.error("No options found for premium selection")
        return None

    expiry = ctx.nearest_nifty_expiry_date
    def find_kite_token(row):
        strike = row["dStrikePrice"] / 100
        match = ctx.kite_df[
            (ctx.kite_df["expiry"] == expiry) &
            (ctx.kite_df["strike"] == strike) &
            (ctx.kite_df["option_type"] == row["pOptionType"]) &
            (ctx.kite_df["name"] == "NIFTY") ]
        if match.empty:
            return None
        return int(match.iloc[0]["instrument_token"])

    df["kite_token"] = df.apply(find_kite_token, axis =1)
    df = df.dropna(subset=["kite_token"])
    if df.empty:
        logging.error("No Zerodha mappings found")
        return None
    tokens = [str(int(t)) for t in df["kite_token"]]
    logging.info( f"Resolved {len(tokens)} Zerodha contracts" )
    try: 
        res = ctx.kiteobj.ltp(tokens)
    except Exception as e:
        logging.error( f"Zerodha LTP fetch failed: {e}", exc_info=True )
        return None
   
    df["LTP"] = df["kite_token"].apply( lambda t: res.get( str(int(t)), {} ).get("last_price") )
    df = df.dropna(subset=["LTP"])
    if df.empty:
        logging.error("No option LTPs received from Zerodha")
        return None
    
    # closest to target_premium
    df["diff"] = abs(df["LTP"] - target_premium)
    chosen = df.loc[df["diff"].idxmin()]

    # # # closest to target_premium but above target_premium
    # df = df[df["LTP"] >= target_premium]
    # if df.empty:
    #     logging.error( f"No {option_type} strikes found with premium >= {target_premium}" )
    #     return None
    # df["diff"] = df["LTP"] - target_premium
    # chosen = df.loc[df["diff"].idxmin()]

    strike = int(chosen["dStrikePrice"] / 100)
    if not hasattr(ctx, "last_premium_selection"):
        ctx.last_premium_selection = {}
    token = str(chosen["pSymbol"])
    ctx.last_premium_selection[token] = { "ltp": float(chosen["LTP"]), "ts": time.time() }
    logging.info( f"{option_type} strike by premium {target_premium}: " f"{strike} (LTP={chosen['LTP']})" )

    return strike

def get_sensex_strike_by_premium(ctx, option_type, target_premium, strike_range_pts=2000):
    
    sensex_ltp = get_sensex_ltp(ctx)
   
    if sensex_ltp is None:
        logging.error("SENSEX LTP not available")
        sys.exit(0)
        return None
    atm = round(sensex_ltp / 100) * 100
    strike_range = range( atm - strike_range_pts, atm + strike_range_pts + 1, 100 )
    df = ctx.sensextoken_df[
        (ctx.sensextoken_df["pSymbolName"] == "SENSEX") &
        (ctx.sensextoken_df["pInstType"] == "IO") &
        (ctx.sensextoken_df["pOptionType"] == option_type) &
        (ctx.sensextoken_df["lExpiryDate"] == ctx.nearest_sensex_expiry_date) &
        ((ctx.sensextoken_df["dStrikePrice"] / 100).isin(strike_range)) ].copy()
    if df.empty:
        logging.error("No options found for premium selection")
        return None

    expiry = ctx.nearest_sensex_expiry_date
    def find_kite_token(row):
        strike = row["dStrikePrice"] / 100
        match = ctx.kite_df[
            (ctx.kite_df["expiry"] == expiry) &
            (ctx.kite_df["strike"] == strike) &
            (ctx.kite_df["option_type"] == row["pOptionType"]) &
            (ctx.kite_df["name"] == "SENSEX") ]
        if match.empty:
            return None
        return int(match.iloc[0]["instrument_token"])

    df["kite_token"] = df.apply(find_kite_token, axis =1)
    df = df.dropna(subset=["kite_token"])
    if df.empty:
        logging.error("No Zerodha mappings found")
        return None
    tokens = [str(int(t)) for t in df["kite_token"]]
    logging.info( f"Resolved {len(tokens)} Zerodha contracts" )
    try: 
        res = ctx.kiteobj.ltp(tokens)
    except Exception as e:
        logging.error( f"Zerodha LTP fetch failed: {e}", exc_info=True )
        return None
   
    df["LTP"] = df["kite_token"].apply( lambda t: res.get( str(int(t)), {} ).get("last_price") )
    df = df.dropna(subset=["LTP"])
    if df.empty:
        logging.error("No option LTPs received from Zerodha")
        return None
    
    # # closest to target_premium
    # df["diff"] = abs(df["LTP"] - target_premium)
    # chosen = df.loc[df["diff"].idxmin()]

    # # closest to target_premium but above target_premium
    df = df[df["LTP"] >= target_premium]
    if df.empty:
        logging.error( f"No {option_type} strikes found with premium >= {target_premium}" )
        return None
    df["diff"] = df["LTP"] - target_premium
    chosen = df.loc[df["diff"].idxmin()]

    strike = int(chosen["dStrikePrice"] / 100)
    if not hasattr(ctx, "last_premium_selection"):
        ctx.last_premium_selection = {}
    token = str(chosen["pSymbol"])
    ctx.last_premium_selection[token] = { "ltp": float(chosen["LTP"]), "ts": time.time() }
    logging.info( f"{option_type} strike by premium {target_premium}: " f"{strike} (LTP={chosen['LTP']})" )

    return strike

def get_next_nifty_strike_by_premium(ctx, option_type, target_premium, strike_range_pts=3000):
    nifty_ltp = get_nifty_ltp(ctx)
   
    if nifty_ltp is None:
        logging.error("NIFTY LTP not available")
        sys.exit(0)
        return None
    atm = round(nifty_ltp / 100) * 100
    strike_range = range( atm - strike_range_pts, atm + strike_range_pts + 1, 100 )
    df = ctx.token_df[
        (ctx.token_df["pSymbolName"] == "NIFTY") &
        (ctx.token_df["pInstType"] == "OPTIDX") &
        (ctx.token_df["pOptionType"] == option_type) &
        (ctx.token_df["lExpiryDate"] == ctx.next_nifty_expiry_date) &
        ((ctx.token_df["dStrikePrice"] / 100).isin(strike_range)) ].copy()
    if df.empty:
        logging.error("No options found for premium selection")
        return None

    expiry = ctx.next_nifty_expiry_date
    def find_kite_token(row):
        strike = row["dStrikePrice"] / 100
        match = ctx.kite_df[
            (ctx.kite_df["expiry"] == expiry) &
            (ctx.kite_df["strike"] == strike) &
            (ctx.kite_df["option_type"] == row["pOptionType"]) &
            (ctx.kite_df["name"] == "NIFTY") ]
        if match.empty:
            return None
        return int(match.iloc[0]["instrument_token"])

    df["kite_token"] = df.apply(find_kite_token, axis =1)
    df = df.dropna(subset=["kite_token"])
    if df.empty:
        logging.error("No Zerodha mappings found")
        return None
    tokens = [str(int(t)) for t in df["kite_token"]]
    logging.info( f"Resolved {len(tokens)} Zerodha contracts" )
    try: 
        res = ctx.kiteobj.ltp(tokens)
    except Exception as e:
        logging.error( f"Zerodha LTP fetch failed: {e}", exc_info=True )
        return None
   
    df["LTP"] = df["kite_token"].apply( lambda t: res.get( str(int(t)), {} ).get("last_price") )
    df = df.dropna(subset=["LTP"])
    if df.empty:
        logging.error("No option LTPs received from Zerodha")
        return None
    
    # # closest to target_premium
    # df["diff"] = abs(df["LTP"] - target_premium)
    # chosen = df.loc[df["diff"].idxmin()]

    # # closest to target_premium but above target_premium
    df = df[df["LTP"] >= target_premium]
    if df.empty:
        logging.error( f"No {option_type} strikes found with premium >= {target_premium}" )
        return None
    df["diff"] = df["LTP"] - target_premium
    chosen = df.loc[df["diff"].idxmin()]

    strike = int(chosen["dStrikePrice"] / 100)
    if not hasattr(ctx, "last_premium_selection"):
        ctx.last_premium_selection = {}
    token = str(chosen["pSymbol"])
    ctx.last_premium_selection[token] = { "ltp": float(chosen["LTP"]), "ts": time.time() }
    logging.info( f"{option_type} strike by premium {target_premium}: " f"{strike} (LTP={chosen['LTP']})" )

    return strike

def get_nifty_monthend_strike_by_premium(ctx, option_type, target_premium, strike_range_pts=3000):
    nifty_ltp = get_nifty_ltp(ctx)
   
    if nifty_ltp is None:
        logging.error("NIFTY LTP not available")
        sys.exit(0)
        return None
    atm = round(nifty_ltp / 100) * 100
    strike_range = range( atm - strike_range_pts, atm + strike_range_pts + 1, 100 )
    df = ctx.token_df[
        (ctx.token_df["pSymbolName"] == "NIFTY") &
        (ctx.token_df["pInstType"] == "OPTIDX") &
        (ctx.token_df["pOptionType"] == option_type) &
        (ctx.token_df["lExpiryDate"] == ctx.nifty_monthend_expiry_date) &
        ((ctx.token_df["dStrikePrice"] / 100).isin(strike_range)) ].copy()
    if df.empty:
        logging.error("No options found for premium selection")
        return None

    expiry = ctx.nifty_monthend_expiry_date

    def find_kite_token(row):
        strike = row["dStrikePrice"] / 100
        match = ctx.kite_df[
            (ctx.kite_df["expiry"] == expiry) &
            (ctx.kite_df["strike"] == strike) &
            (ctx.kite_df["option_type"] == row["pOptionType"]) &
            (ctx.kite_df["name"] == "NIFTY") ]
        if match.empty:
            return None
        return int(match.iloc[0]["instrument_token"])

    df["kite_token"] = df.apply(find_kite_token, axis =1)
    df = df.dropna(subset=["kite_token"])
    if df.empty:
        logging.error("No Zerodha mappings found")
        return None
    tokens = [str(int(t)) for t in df["kite_token"]]
    logging.info( f"Resolved {len(tokens)} Zerodha contracts" )
    try: 
        res = ctx.kiteobj.ltp(tokens)
    except Exception as e:
        logging.error( f"Zerodha LTP fetch failed: {e}", exc_info=True )
        return None
   
    df["LTP"] = df["kite_token"].apply( lambda t: res.get( str(int(t)), {} ).get("last_price") )
    df = df.dropna(subset=["LTP"])
    if df.empty:
        logging.error("No option LTPs received from Zerodha")
        return None

    # # closest to target_premium
    # df["diff"] = abs(df["LTP"] - target_premium)
    # chosen = df.loc[df["diff"].idxmin()]

    # # closest to target_premium but above target_premium
    df = df[df["LTP"] >= target_premium]
    if df.empty:
        logging.error( f"No {option_type} strikes found with premium >= {target_premium}" )
        return None
    df["diff"] = df["LTP"] - target_premium
    chosen = df.loc[df["diff"].idxmin()]

    strike = int(chosen["dStrikePrice"] / 100)
    if not hasattr(ctx, "last_premium_selection"):
        ctx.last_premium_selection = {}
    token = str(chosen["pSymbol"])
    ctx.last_premium_selection[token] = { "ltp": float(chosen["LTP"]), "ts": time.time() }
    logging.info( f"{option_type} strike by premium {target_premium}: " f"{strike} (LTP={chosen['LTP']})" )

    return strike

def get_nifty_next_monthend_strike_by_premium(ctx, option_type, target_premium, strike_range_pts=3000):
    nifty_ltp = get_nifty_ltp(ctx)
   
    if nifty_ltp is None:
        logging.error("NIFTY LTP not available")
        sys.exit(0)
        return None
    atm = round(nifty_ltp / 100) * 100
    strike_range = range( atm - strike_range_pts, atm + strike_range_pts + 1, 100 )
    df = ctx.token_df[
        (ctx.token_df["pSymbolName"] == "NIFTY") &
        (ctx.token_df["pInstType"] == "OPTIDX") &
        (ctx.token_df["pOptionType"] == option_type) &
        (ctx.token_df["lExpiryDate"] == ctx.nifty_next_monthend_expiry_date) &
        ((ctx.token_df["dStrikePrice"] / 100).isin(strike_range)) ].copy()
    if df.empty:
        logging.error("No options found for premium selection")
        return None

    expiry = ctx.nifty_next_monthend_expiry_date
    def find_kite_token(row):
        strike = row["dStrikePrice"] / 100
        match = ctx.kite_df[
            (ctx.kite_df["expiry"] == expiry) &
            (ctx.kite_df["strike"] == strike) &
            (ctx.kite_df["option_type"] == row["pOptionType"]) &
            (ctx.kite_df["name"] == "NIFTY") ]
        if match.empty:
            return None
        return int(match.iloc[0]["instrument_token"])

    df["kite_token"] = df.apply(find_kite_token, axis =1)
    df = df.dropna(subset=["kite_token"])
    if df.empty:
        logging.error("No Zerodha mappings found")
        return None
    tokens = [str(int(t)) for t in df["kite_token"]]
    logging.info( f"Resolved {len(tokens)} Zerodha contracts" )
    try: 
        res = ctx.kiteobj.ltp(tokens)
    except Exception as e:
        logging.error( f"Zerodha LTP fetch failed: {e}", exc_info=True )
        return None
   
    df["LTP"] = df["kite_token"].apply( lambda t: res.get( str(int(t)), {} ).get("last_price") )
    df = df.dropna(subset=["LTP"])
    if df.empty:
        logging.error("No option LTPs received from Zerodha")
        return None
    
    # # closest to target_premium
    # df["diff"] = abs(df["LTP"] - target_premium)
    # chosen = df.loc[df["diff"].idxmin()]

    # # closest to target_premium but above target_premium
    df = df[df["LTP"] >= target_premium]
    if df.empty:
        logging.error( f"No {option_type} strikes found with premium >= {target_premium}" )
        return None
    df["diff"] = df["LTP"] - target_premium
    chosen = df.loc[df["diff"].idxmin()]

    strike = int(chosen["dStrikePrice"] / 100)
    if not hasattr(ctx, "last_premium_selection"):
        ctx.last_premium_selection = {}
    token = str(chosen["pSymbol"])
    ctx.last_premium_selection[token] = { "ltp": float(chosen["LTP"]), "ts": time.time() }
    logging.info( f"{option_type} strike by premium {target_premium}: " f"{strike} (LTP={chosen['LTP']})" )

    return strike

def get_cmdty_symbol_token(ctx, symbol_name, expiry):

    row = ctx.cmdtytoken_df[
        (ctx.cmdtytoken_df["pInstType"] == "FUTCOM") &
        (ctx.cmdtytoken_df["pSymbolName"] == symbol_name) &
        (ctx.cmdtytoken_df["lExpiryDate"] == expiry)
    ]
    if row.empty:
        logging.error( f"{symbol_name}: contract not found for {expiry}" )
        return None
    row = row.iloc[0]
    return {
        "trdSym": row["pTrdSymbol"],
        "tok": row["pSymbol"] }

def get_cmdty_ltp(ctx, symbol_name, kotak_token):

    try:
        kotak_token = int(float(kotak_token))
        row = ctx.cmdtytoken_df[
            ctx.cmdtytoken_df["pSymbol"] == kotak_token ]
        if row.empty:
            logging.error(f"{symbol_name}: Kotak token missing")
            return None
        row = row.iloc[0]
        expiry = row["lExpiryDate"]
        if not isinstance(expiry, date):
            expiry = pd.to_datetime(expiry).date()
        kite_row = ctx.kite_cmdty_df[ (ctx.kite_cmdty_df["name"] == symbol_name) & (ctx.kite_cmdty_df["expiry"] == expiry) ]
        if kite_row.empty:
            logging.error(f"{symbol_name}: Kite mapping missing")
            return None
        kite_token = str( int(kite_row.iloc[0]["instrument_token"]) )
        logging.info( f"{symbol_name} Kite token = {kite_token}" )
        res = ctx.kiteobj.ltp(kite_token)
        return res[kite_token]["last_price"]
    except Exception as e:
        logging.error( f"get_future_ltp failed: {e}", exc_info=True )
        return None
