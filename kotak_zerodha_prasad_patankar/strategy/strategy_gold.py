import logging
import time
import pandas as pd
from datetime import datetime
from utility.orders import place_cmdty_entry, place_cmdty_exit, round_to_tick
from data.instruments import get_cmdty_symbol_token, get_cmdty_ltp
from utility.common_functions import safe_execute, send_telegram_message


def gold_entry(ctx):
    """
    GOLD is FUTURE entry
    """

    days_to_expiry = (ctx.nearest_gold_expiry_date - datetime.now().date()).days

    if days_to_expiry <= 7:
        expiry = ctx.next_gold_expiry_date
        ctx.gold_lot = ctx.gold_next_lot
    else:
        expiry = ctx.nearest_gold_expiry_date
        ctx.gold_lot = ctx.gold_near_lot

    instrument = get_cmdty_symbol_token( ctx, ctx.gold_instrument, expiry )

    if not instrument:
        logging.info("GOLD: No GOLD found")
        return None
    
    logging.info(f"GOLD future {instrument['trdSym']}, {instrument['tok']}")

    main_leg = {
        "trdSym": instrument["trdSym"],
        "tok" : instrument["tok"],
        "side": "B",                     # BUY = B
        "qty": ctx.gold_lot,
        "productType": "NRML",
        "tag": f"GOLD_{time.strftime('%H%M%S')}"}

    ltp = get_cmdty_ltp( ctx, ctx.gold_instrument, instrument["tok"] )
    logging.info(f"LTP = {ltp}")
    
    if ltp is None:
        logging.error("LTP is None — skipping..")
        return None 
    buffer = ltp * ctx.gold_limitorder
   
    if main_leg["side"] == 'B':
        price = round(ltp + buffer)
    else:
        price = round(ltp - buffer)
    
    logging.info(f"Placing order for {instrument['trdSym']}, {instrument['tok']}")

    return place_cmdty_entry(ctx=ctx, main_leg=main_leg, price=price)

def gold_exit(ctx, gold_open_positions):
    """
    Indicator exit for GOLD
    """

    gold = gold_open_positions.get("GOLD")

    if gold is None:
        logging.error("GOLD exit requested but it's missing")
        return None
    
    main_leg = {
        "trdSym": gold["trdSym"],
        "tok" : int(gold["tok"]),
        "productType": "NRML",
        "qty": int(gold["fldQty"])}

    # flip B <-> S
    exit_side = "S" if gold["trnsTp"] == "B" else "B"

    ltp = get_cmdty_ltp( ctx, ctx.gold_instrument, gold["tok"] )
    if ltp is None:
        logging.error("LTP is None — skipping..")
        return None 
    buffer = max(ltp * ctx.gold_limitorder,1)

    if exit_side == "B":
        price = round(ltp + buffer)
    else:
        price = round(ltp - buffer)

    logging.info(f"Placing order for {gold['trdSym'][-7:]} @ {price}")

    return place_cmdty_exit(
        ctx=ctx,
        main_leg=main_leg,
        exit_tag=f"IND_GOLD_{time.strftime('%H%M%S')}",
        exit_side=exit_side,
        price = price)
