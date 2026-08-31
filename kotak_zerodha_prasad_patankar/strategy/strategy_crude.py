import logging
import time
import pandas as pd
from datetime import datetime
from utility.orders import place_cmdty_entry, place_cmdty_exit, round_to_tick
from data.instruments import get_cmdty_symbol_token, get_cmdty_ltp
from utility.common_functions import safe_execute, send_telegram_message


def crude_entry(ctx):
    """
    CRUDE is FUTURE entry
    """

    days_to_expiry = (ctx.nearest_crude_expiry_date - datetime.now().date()).days

    if days_to_expiry <= 7:
        expiry = ctx.next_crude_expiry_date
        ctx.crude_lot = ctx.crude_next_lot
    else:
        expiry = ctx.nearest_crude_expiry_date
        ctx.crude_lot = ctx.crude_near_lot

    instrument = get_cmdty_symbol_token( ctx, ctx.crude_instrument, expiry )

    if not instrument:
        logging.info("CRUDE: No CRUDE found")
        return None
    
    logging.info(f"CRUDE future {instrument['trdSym']}, {instrument['tok']}")

    main_leg = {
        "trdSym": instrument["trdSym"],
        "tok" : instrument["tok"],
        "side": "B",                     # BUY = B
        "qty": ctx.crude_lot,
        "productType": "NRML",
        "tag": f"CRUDE_{time.strftime('%H%M%S')}"}

    ltp = get_cmdty_ltp( ctx, ctx.crude_instrument, instrument["tok"] )
    logging.info(f"LTP = {ltp}")

    if ltp is None:
        logging.error("LTP is None — skipping..")
        return None 
    buffer = ltp * ctx.crude_limitorder

    if main_leg["side"] == 'B':
        price = round(ltp + buffer)
    else:
        price = round(ltp - buffer)

    logging.info(f"Placing order for {instrument['trdSym']}, {instrument['tok']}")

    return place_cmdty_entry(ctx=ctx, main_leg=main_leg, price=price)

def crude_exit(ctx, crude_open_positions):
    """
    Indicator exit for CRUDE
    """

    crude = crude_open_positions.get("CRUDE")

    if crude is None:
        logging.error("CRUDE exit requested but it's missing")
        return None
    
    main_leg = {
        "trdSym": crude["trdSym"],
        "tok" : int(crude["tok"]),
        "productType": "NRML",
        "qty": int(crude["fldQty"])}

    # flip B <-> S
    exit_side = "S" if crude["trnsTp"] == "B" else "B"

    ltp = get_cmdty_ltp( ctx, ctx.crude_instrument, crude["tok"] )
    if ltp is None:
        logging.error("LTP is None — skipping..")
        return None 
    buffer = max(ltp * ctx.crude_limitorder,1)

    if exit_side == "B":
        price = round(ltp + buffer)
    else:
        price = round(ltp - buffer)

    logging.info(f"Placing order for {crude['trdSym'][-7:]} @ {price}")

    return place_cmdty_exit(
        ctx=ctx,
        main_leg=main_leg,
        exit_tag=f"IND_CRUDE_{time.strftime('%H%M%S')}",
        exit_side=exit_side,
        price = price)