import logging
import time
from utility.orders import place_entry_with_hdg_and_sl, place_exit_with_sl, round_to_tick, place_entry_with_sl
from data.instruments import get_nifty_strike, get_nifty_option_symbol_token, get_ltp
from utility.reconcile import build_positions


def rce_entry(ctx):
    """
    Reversal Call Entry (SELL CE with BUY hedge)
    Hedge first, SL only on main.
    """

    CEitm_strike_price = get_nifty_strike(ctx, ctx.rce_itm)

    main_strike = CEitm_strike_price
    hedge_strike = CEitm_strike_price + (ctx.rce_hdg_dist)

    main_instrument = get_nifty_option_symbol_token(ctx, main_strike, "CE")
    hedge_instrument = get_nifty_option_symbol_token(ctx, hedge_strike, "CE")

    if not main_instrument or not hedge_instrument:
        logging.info("RCE: CE main or hedge option not found")
        return None

    # MAIN leg → SELL
    main_leg = {
        "trdSym": main_instrument["trdSym"],
        "tok": main_instrument["tok"],
        "side": "S",
        "qty": ctx.rce_lot,
        "productType": ctx.productType,
        "tag": f"RCE_{time.strftime('%H%M%S')}"
    }

    main_ltp= get_ltp(ctx, main_instrument["tok"])
    if main_ltp is None:
        logging.error("LTP is None — skipping..")
        return None 
    
    buffer = main_ltp * ctx.limitorder

    if main_leg["side"] == 'B':
        main_price = round_to_tick(main_ltp + buffer)
    else:
        main_price = round_to_tick(main_ltp - buffer)

    # HEDGE leg → BUY
    hedge_leg = {
        "trdSym": hedge_instrument["trdSym"],
        "tok": hedge_instrument["tok"],
        "side": "B",
        "qty": ctx.rce_lot,
        "productType": ctx.productType,
        "tag": f"RCE_HDG_{time.strftime('%H%M%S')}"
    }

    hedge_ltp= get_ltp(ctx, hedge_instrument["tok"])
    if hedge_ltp is None:
        logging.error("LTP is None — skipping..")
        return None 
    
    buffer = max(hedge_ltp * ctx.limitorder, 1)

    if hedge_leg["side"] == 'B':
        hedge_price = round_to_tick(hedge_ltp + buffer)
    else:
        hedge_price = round_to_tick(hedge_ltp - buffer)

    sl_config = {
        "sl_pct": ctx.rce_psl,
        "sl_tag": f"SL_RCE_{time.strftime('%H%M%S')}"
    }

    logging.info(f"Placing order for | MAIN {main_instrument['trdSym'][-7:]} @ {main_price}")

    return place_entry_with_hdg_and_sl(
        ctx=ctx,
        main_leg=main_leg,
        hedge_leg=hedge_leg,
        sl_config=sl_config,
        main_price = main_price,
        hedge_price = hedge_price,
    )

def rce_exit(ctx, open_positions):
    """
    Indicator exit for RCE (main + hedge)
    """
    # WE ARE REMOVING HEDGES FROM EXIT AS WE DONT CREATE HEDGE OPEN POSITION VIA RECONCILE
    # HEDGES WILL BE TAKEN CARE BY handle_oprhan_hedges FUNCTION
    # open_positions is created at the beginning of the 3-minute cycle.
    # By the time control reaches this indicator exit, the broker-side SL may
    # already have closed the position, making our snapshot stale.
    #
    # Therefore, refresh open_positions here and verify that the CEB open_position still exists 
    # before placing an indicator exit. 
    # This prevents sending a naked exit order if the position was already closed.
    #
    open_positions, _ = build_positions(ctx) # this new value to open_positions, has nothing to do with same created in main.py
    main = open_positions.get("RCE")
    if not main:
        return None

    main_leg = {
        "trdSym": main["trdSym"],
        "tok" : int(main["tok"]),
        "productType": ctx.productType,
        "qty": ctx.rce_lot
    }


    main_exit_side = "S" if main["trnsTp"] == "B" else "B"

    ltp = get_ltp(ctx, main["tok"])
    if ltp is None:
        logging.error("LTP is None — skipping..")
        return None 
    buffer = ltp * ctx.limitorder

    if main_exit_side == "B":
        price = round_to_tick(ltp + buffer)
    else:
        price = round_to_tick(ltp - buffer)

    logging.info(f"Placing order for MAIN {main['trdSym'][-7:]}")
    
    return place_exit_with_sl(
        ctx=ctx,
        main_leg=main_leg,
        sl_tag="SL_RCE",
        exit_tag=f"IND_RCE_{time.strftime('%H%M%S')}",
        exit_side=main_exit_side,
        price= price)

def rce_entry_without_hedge(ctx):
    """
    Reversal Call Entry (SELL CE)
    Hedge first, SL only on main.
    """

    CEitm_strike_price = get_nifty_strike(ctx, ctx.rce_itm)

    main_strike = CEitm_strike_price
    #hedge_strike = CEitm_strike_price + (ctx.rce_hdg_dist)

    main_instrument = get_nifty_option_symbol_token(ctx, main_strike, "CE")
    #hedge_instrument = get_nifty_option_symbol_token(ctx, hedge_strike, "CE")

    if not main_instrument : #or not hedge_instrument
        logging.info("RCE: CE main or hedge option not found")
        return None

    # MAIN leg → SELL
    main_leg = {
        "trdSym": main_instrument["trdSym"],
        "tok": main_instrument["tok"],
        "side": "S",
        "qty": ctx.rce_lot,
        "productType": ctx.productType,
        "tag": f"RCE_{time.strftime('%H%M%S')}"
    }

    main_ltp= get_ltp(ctx, main_instrument["tok"])
    if main_ltp is None:
        logging.error("LTP is None — skipping..")
        return None 
    
    buffer = main_ltp * ctx.limitorder

    if main_leg["side"] == 'B':
        main_price = round_to_tick(main_ltp + buffer)
    else:
        main_price = round_to_tick(main_ltp - buffer)

    # # HEDGE leg → BUY
    # hedge_leg = {
    #     "trdSym": hedge_instrument["trdSym"],
    #     "tok": hedge_instrument["tok"],
    #     "side": "B",
    #     "qty": ctx.rce_lot,
    #     "productType": ctx.productType,
    #     "tag": f"RCE_HDG_{time.strftime('%H%M%S')}"
    # }

    # hedge_ltp= get_ltp(ctx, hedge_instrument["tok"])
    # logging.info(hedge_ltp)
    
    # buffer = max(hedge_ltp * ctx.limitorder, 1)

    # if hedge_leg["side"] == 'B':
    #     hedge_price = round_to_tick(hedge_ltp + buffer)
    # else:
    #     hedge_price = round_to_tick(hedge_ltp - buffer)

    sl_config = {
        "sl_pct": ctx.rce_psl,
        "sl_tag": f"SL_RCE_{time.strftime('%H%M%S')}"
    }

    logging.info(f"Placing order for | MAIN {main_instrument['trdSym'][-7:]} @ {main_price}")

    return place_entry_with_sl(
        ctx=ctx,
        main_leg=main_leg,
        sl_config=sl_config,
        price = main_price,
    )