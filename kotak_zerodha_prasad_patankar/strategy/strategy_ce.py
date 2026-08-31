import logging
import time
import pandas as pd
from data.instruments import get_nifty_strike_by_premium, get_nifty_option_symbol_token, get_ltp
from utility.orders import place_entry_without_sl, place_exit_without_sl, round_to_tick
from utility.reconcile import build_positions

# we used to place PSL at 80% but broker denies that far price. so SL is not placed
# and order used to get reconciled.. thus we are now placing entries without premium stoploss

def ceb_entry(ctx):

    CEitm_strike_price = get_nifty_strike_by_premium(ctx, "CE", ctx.ceb_cp)

    instrument = get_nifty_option_symbol_token(ctx, CEitm_strike_price, "CE")
    logging.info(f"CEB symbol {instrument['trdSym']}, {instrument['tok']}")

    if not instrument:
        logging.info("CEB: No CE options found")
        return None

    main_leg = {
        "trdSym": instrument["trdSym"],
        "tok" : instrument["tok"],
        "side": "B",                     # BUY = B
        "qty": ctx.ceb_lot,
        "productType": ctx.productType,
        "tag": f"CEB_{time.strftime('%H%M%S')}"}

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

def ceb_exit(ctx, open_positions):
    """
    Indicator exit for CEB
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
    ceb = open_positions.get("CEB")
    if not ceb:
        return None
    
    main_leg = {
        "trdSym": ceb["trdSym"],
        "tok" : int(ceb["tok"]),
        "productType": ctx.productType,
        "qty": ctx.ceb_lot}

    # flip B <-> S
    exit_side = "S" if ceb["trnsTp"] == "B" else "B"

    ltp = get_ltp(ctx, ceb["tok"])
    if ltp is None:
        logging.error("LTP is None — skipping..")
        return None 
    buffer = ltp * ctx.limitorder

    if exit_side == "B":
        price = round_to_tick(ltp + buffer)
    else:
        price = round_to_tick(ltp - buffer)

    logging.info(f"Placing order for {ceb['trdSym'][-7:]} @ {price}")

    return place_exit_without_sl(
        ctx=ctx,
        main_leg=main_leg,
        exit_tag=f"IND_CEB_{time.strftime('%H%M%S')}",
        exit_side=exit_side,
        price = price)
