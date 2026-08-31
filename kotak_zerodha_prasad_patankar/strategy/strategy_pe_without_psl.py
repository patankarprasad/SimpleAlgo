import logging
import time
import pandas as pd
from data.instruments import  get_nifty_strike, get_nifty_option_symbol_token, get_ltp
from utility.orders import place_entry_without_sl, place_exit_without_sl, round_to_tick
from utility.reconcile import build_positions

# This PEB places order without premium stoploss

def peb_entry(ctx):
    
    PEitm_strike_price = get_nifty_strike(ctx, ctx.peb_itm)
    
    instrument = get_nifty_option_symbol_token(ctx, PEitm_strike_price, "PE")
    logging.info(f"PEB symbol {instrument['trdSym']}, {instrument['tok']}")
    
    if not instrument:
        logging.info("PEB: No PE options found")
        return None

    main_leg = {
        "trdSym": instrument["trdSym"],
        "tok" : instrument["tok"],
        "side": "B",                      # Buy
        "qty": ctx.peb_lot,
        "productType": ctx.productType,
        "tag": f"PEB_{time.strftime('%H%M%S')}"}

    ltp= get_ltp(ctx, instrument["tok"])
    if ltp is None:
        logging.error("LTP is None — skipping..")
        return None 
    
    buffer = ltp * ctx.limitorder

    if main_leg["side"] == 'B':
        price = round_to_tick(ltp + buffer)
    else:
        price = round_to_tick(ltp - buffer)
    
    logging.info(f"Placing order for {instrument['trdSym']}, {instrument['tok']}")

    return place_entry_without_sl(ctx=ctx, main_leg=main_leg, price=price)

def peb_exit(ctx, open_positions):
    """
    Indicator exit for PEB
    """
    # open_positions is created at the beginning of the 3-minute cycle.
    # By the time control reaches this indicator exit, the broker-side SL may
    # already have closed the position, making our snapshot stale.
    #
    # Therefore, refresh open_positions here and verify that the CEB open_position still exists 
    # before placing an indicator exit. 
    # This prevents sending a naked exit order if the position was already closed.
    #
    open_positions, _ = build_positions(ctx) # this new value to open_positions, has nothing to do with same created in main.py
    peb = open_positions.get("PEB")
    if not peb:
        return None

    main_leg = {
        "trdSym": peb["trdSym"],
        "tok" : int(peb["tok"]),
        "productType": ctx.productType,
        "qty": ctx.peb_lot}

    # Flip side: B → S, S → B
    exit_side = "S" if peb["trnsTp"] == "B" else "B"

    ltp = get_ltp(ctx, peb["tok"])
    if ltp is None:
        logging.error("LTP is None — skipping..")
        return None 
    buffer = ltp * ctx.limitorder

    if exit_side == "B":
        price = round_to_tick(ltp + buffer)
    else:
        price = round_to_tick(ltp - buffer)

    logging.info(f"Placing order for {peb['trdSym'][-7:]} @ {price}")

    return place_exit_without_sl(
        ctx=ctx,
        main_leg=main_leg,
        exit_tag=f"IND_PEB_{time.strftime('%H%M%S')}",
        exit_side=exit_side,
        price = price)
