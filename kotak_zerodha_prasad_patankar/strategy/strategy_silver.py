import logging
import time
import pandas as pd
from datetime import datetime
from utility.orders import place_cmdty_entry, place_cmdty_exit, round_to_tick
from data.instruments import get_cmdty_symbol_token, get_cmdty_ltp
from utility.common_functions import safe_execute, send_telegram_message


def silver_entry(ctx):
    """
    SILVER is FUTURE entry
    """

    days_to_expiry = (ctx.nearest_silver_expiry_date - datetime.now().date()).days

    if days_to_expiry <= 7:
        expiry = ctx.next_silver_expiry_date
        ctx.silver_lot = ctx.silver_next_lot
    else:
        expiry = ctx.nearest_silver_expiry_date
        ctx.silver_lot = ctx.silver_near_lot

    instrument = get_cmdty_symbol_token( ctx, ctx.silver_instrument, expiry )

    if not instrument:
        logging.info("SILVER: No SILVER found")
        return None
    
    logging.info(f"SILVER future {instrument['trdSym']}, {instrument['tok']}")

    main_leg = {
        "trdSym": instrument["trdSym"],
        "tok" : instrument["tok"],
        "side": "B",                     # BUY = B
        "qty": ctx.silver_lot,
        "productType": "NRML",
        "tag": f"SILVER_{time.strftime('%H%M%S')}"}

    ltp = get_cmdty_ltp( ctx, ctx.silver_instrument, instrument["tok"] )
    logging.info(f"LTP = {ltp}")
    
    if ltp is None:
        logging.error("LTP is None — skipping..")
        return None 
    buffer = ltp * ctx.silver_limitorder

    if main_leg["side"] == 'B':
        price = round(ltp + buffer)
    else:
        price = round(ltp - buffer)
    
    logging.info(f"Placing order for {instrument['trdSym']}, {instrument['tok']}")

    return place_cmdty_entry(ctx=ctx, main_leg=main_leg, price=price)

def silver_exit(ctx, silver_open_positions):
    """
    Indicator exit for SILVER
    """

    silver = silver_open_positions.get("SILVER")

    if silver is None:
        logging.error("SILVER exit requested but it's missing")
        return None
    
    main_leg = {
        "trdSym": silver["trdSym"],
        "tok" : int(silver["tok"]),
        "productType": "NRML",
        "qty": int(silver["fldQty"])}

    # flip B <-> S
    exit_side = "S" if silver["trnsTp"] == "B" else "B"

    ltp = get_cmdty_ltp( ctx, ctx.silver_instrument, silver["tok"] )
    if ltp is None:
        logging.error("LTP is None — skipping..")
        return None 
    buffer = max(ltp * ctx.silver_limitorder,1)

    if exit_side == "B":
        price = round(ltp + buffer)
    else:
        price = round(ltp - buffer)
    
    logging.info(f"Placing order for {silver['trdSym'][-7:]} @ {price}")

    return place_cmdty_exit(
        ctx=ctx,
        main_leg=main_leg,
        exit_tag=f"IND_SILVER_{time.strftime('%H%M%S')}",
        exit_side=exit_side,
        price = price)
