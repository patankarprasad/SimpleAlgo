import logging
import time
import pandas as pd
import datetime
from datetime import datetime
from utility.orders import place_entry_without_sl, place_exit_without_sl, round_to_tick
from data.instruments import get_next_nifty_strike_by_premium, get_next_nifty_option_symbol_token, get_ltp
from utility.reconcile import n2cs_build_positions, cancel_target, get_order_df
from utility.common_functions import safe_execute, send_telegram_message

# in this is NCS we sell call only (next week closest premium 300 ).. no hedge, no synthetic future

def n2cs_entry(ctx):
    """
    If today expiry day, then NCS position for next expiry
    Else near expiry
    """
    today = datetime.now().date()

    # if ctx.nifty_monthend_expiry_date == today:
    #     synthfut_strike_price = get_nifty_next_monthend_strike_by_premium(ctx, "CE", ctx.ncs_cp)
    #     main_s_strike = synthfut_strike_price
    #     main_s_inst = get_next_monthend_option_symbol_token(ctx, main_s_strike, "CE")
    # else:
    #     synthfut_strike_price = get_nifty_monthend_strike_by_premium(ctx, "CE", ctx.ncs_cp)
    #     main_s_strike = synthfut_strike_price
    #     main_s_inst = get_monthend_option_symbol_token(ctx, main_s_strike, "CE")
    
    synthfut_strike_price = get_next_nifty_strike_by_premium(ctx, "CE", ctx.n2cs_cp)
    main_s_strike = synthfut_strike_price
    main_s_inst = get_next_nifty_option_symbol_token(ctx, main_s_strike, "CE")
    
    if not main_s_inst:
        logging.info("N2CS: Options not found")
        return None

    s_ltp = get_ltp(ctx, main_s_inst["tok"])
    if s_ltp is None:
        logging.error("LTP is None — skipping..")
        return None 
    s_buffer = max(s_ltp * ctx.limitorder, 0.10)

    main_s_leg = { "trdSym": main_s_inst["trdSym"], "tok": main_s_inst["tok"], "side": "S", "qty": ctx.n2cs_next_lot, "productType": "NRML", "tag": f"N2CS_CE_{int(time.time())}"}

    # MAIN leg → SELL
    if main_s_leg["side"] == "B":
        sellprice = round_to_tick(s_ltp + s_buffer)
    else:
        sellprice = round_to_tick(s_ltp - s_buffer)

    logging.info(f"Placing order for | CE {main_s_inst['trdSym'][-7:]}")
    

    return place_entry_without_sl(
        ctx=ctx,
        main_leg=main_s_leg,
        price=sellprice,
        )

def n2cs_exit(ctx, ncs_open_positions):
    """
    Indicator exit for N2CS (main + hedge)
    """

    main_s = ncs_open_positions.get("N2CS_CE")

    # IMPORTANT → explicit None check (Series cannot be used in boolean context)
    if main_s is None :
        logging.error("NCS exit requested but one or more legs missing")
        return None
    
    order_df = get_order_df(ctx)
    safe_execute( cancel_target, ctx, main_s, "TGT_N2CS_CE", order_df )

    # # # by adding order_df in function definition, we skipped fetching of orderbook repeatedly
    # # Cancel active target first
    # safe_execute( cancel_target, ctx, main_s, "TGT_N2CS_CE", order_df )
    
    main_s_leg = {
        "trdSym": main_s["trdSym"],
        "tok": int(main_s["tok"]),
        "productType": "NRML",
        "qty": int(main_s["fldQty"])
    }
     # Flip sides using broker truth
    main_s_exit_side = "S" if main_s["trnsTp"] == "B" else "B"

    s_ltp = get_ltp(ctx, int(main_s["tok"]))
    if s_ltp is None:
        logging.error("LTP is None — skipping..")
        return None 
    s_buffer = max(s_ltp * ctx.limitorder, 1)

    if main_s_exit_side == "B":
        sellprice = round_to_tick(s_ltp + s_buffer)
    else:
        sellprice = round_to_tick(s_ltp - s_buffer)

    logging.info(f"Placing order for N2CS | CE {main_s['trdSym'][-7:]}" )

    return place_exit_without_sl(
        ctx=ctx,
        main_leg=main_s_leg,
        exit_side=main_s_exit_side,
        exit_tag=f"IND_N2CS_CE_{int(time.time())}",
        price=sellprice)


def n2cs_expiry_clearing(ctx):
    
    n2cs_open_positions = n2cs_build_positions(ctx)

    if n2cs_open_positions is None:
        logging.info("N2CS No open N2CS position found..\n")
        return False

    # All legs have same expiry, pick any
    exp_dt_str = n2cs_open_positions["N2CS_CE"]["expDt"]
    exp_date = pd.to_datetime(exp_dt_str).date()
    today = datetime.now().date()

    # if next expiry date is exp_date
    if today != exp_date :
        logging.info(f"N2CS Open position found but no need of expiry_clearing today, expiry={exp_date}, today={today}")
        return False

    logging.warning(f"N2CS EXPIRY CLEARING!!: Expiry today ({exp_date}) hence clearing the position")

    # 1. Force EXIT of current expiry
    n2cs_exit(ctx, n2cs_open_positions)

    # Give broker + ledger some time to update
    time.sleep(1)

    # 2. Reconcile again to ensure position is closed
    n2cs_after_exit = n2cs_build_positions(ctx)

    if n2cs_after_exit is not None:
        logging.error("N2CS expiry_clearing: Exit not confirmed")
        # safe_execute (send_telegram_message, f" N2CS Expiry_Clearing: Exit not confirmed for {ctx.clientname}.")
        return False

    logging.info("N2CS expiry_clearing done")

    return True