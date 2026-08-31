import logging
import time
import pandas as pd
import datetime
from datetime import datetime
from utility.orders import place_synthfut_entry_without_hedge, place_synthfut_exit_without_hedge, round_to_tick
from data.instruments import get_nifty_100strike,get_nifty_option_symbol_token, get_next_nifty_option_symbol_token, get_ltp
from utility.reconcile import ncl_build_positions
from utility.common_functions import safe_execute, send_telegram_message


def ncl_entry(ctx):
    """
    If today expiry day, then NCL position for next expiry
    Else near expiry
    """
    today = datetime.now().date()

    # synthfut_strike_price = get_synthfut_strike(ctx)    
    synthfut_strike_price = get_nifty_100strike(ctx, 0)

    # Kotak strike normalization
    # Ksynthfut_strike_price = synthfut_strike_price * 100

    main_s_strike = synthfut_strike_price
    # main_h_strike = synthfut_strike_price + (ctx.ncl_hdg_dist)
    main_b_strike = synthfut_strike_price

    if ctx.nearest_nifty_expiry_date == today:
        main_s_inst = get_next_nifty_option_symbol_token(ctx, main_s_strike, "PE")
        # main_h_inst = get_next_nifty_option_symbol_token(ctx, main_h_strike, "PE")
        main_b_inst = get_next_nifty_option_symbol_token(ctx, main_b_strike, "CE")
    else:
        main_s_inst = get_nifty_option_symbol_token(ctx, main_s_strike, "PE")
        # main_h_inst = get_nifty_option_symbol_token(ctx, main_h_strike, "PE")
        main_b_inst = get_nifty_option_symbol_token(ctx, main_b_strike, "CE")

    if not main_s_inst or not main_b_inst: # or not main_h_inst:
        logging.info("NCL: Options not found")
        return None

    s_ltp = get_ltp(ctx, main_s_inst["tok"])
    if s_ltp is None:
        logging.error("LTP is None — skipping..")
        return None 
    # h_ltp = get_ltp(ctx, main_h_inst["tok"])
    b_ltp = get_ltp(ctx, main_b_inst["tok"])
    if b_ltp is None:
        logging.error("LTP is None — skipping..")
        return None 

    s_buffer = max(s_ltp * ctx.limitorder, 0.10)
    # h_buffer = max(h_ltp * ctx.limitorder, 0.10)
    b_buffer = max(b_ltp * ctx.limitorder, 0.10)

    if ctx.nearest_nifty_expiry_date == today:
        main_s_leg = { "trdSym": main_s_inst["trdSym"], "tok": main_s_inst["tok"], "side": "S", "qty": ctx.ncl_next_lot, "productType": "NRML", "tag": f"NCL_PE_{int(time.time())}"}
        # main_h_leg = { "trdSym": main_h_inst["trdSym"], "tok": main_h_inst["tok"], "side": "B", "qty": ctx.ncl_next_lot, "productType": "NRML", "tag": f"NCL_HPE_{int(time.time())}"}
        main_b_leg = { "trdSym": main_b_inst["trdSym"], "tok": main_b_inst["tok"], "side": "B", "qty": ctx.ncl_next_lot, "productType": "NRML", "tag": f"NCL_CE_{int(time.time())}" }
    else:
        main_s_leg = { "trdSym": main_s_inst["trdSym"], "tok": main_s_inst["tok"], "side": "S", "qty": ctx.ncl_near_lot, "productType": "NRML", "tag": f"NCL_PE_{int(time.time())}"}
        # main_h_leg = { "trdSym": main_h_inst["trdSym"], "tok": main_h_inst["tok"], "side": "B", "qty": ctx.ncl_near_lot, "productType": "NRML", "tag": f"NCL_HPE_{int(time.time())}"}
        main_b_leg = { "trdSym": main_b_inst["trdSym"], "tok": main_b_inst["tok"], "side": "B", "qty": ctx.ncl_near_lot, "productType": "NRML", "tag": f"NCL_CE_{int(time.time())}" }

    # MAIN leg → SELL
    if main_b_leg["side"] == "B":
        buyprice = round_to_tick(b_ltp + b_buffer)
    else:
        buyprice = round_to_tick(b_ltp - b_buffer)

    if main_s_leg["side"] == "B":
        sellprice = round_to_tick(s_ltp + s_buffer)
    else:
        sellprice = round_to_tick(s_ltp - s_buffer)

    # if main_h_leg["side"] == "B":
    #     hedgeprice = round_to_tick(h_ltp + h_buffer)
    # else:
    #     hedgeprice = round_to_tick(h_ltp - h_buffer)

    logging.info(
        f"Placing order for CE {main_b_inst['trdSym'][-7:]} | PE {main_s_inst['trdSym'][-7:]}"
    )
    

    return place_synthfut_entry_without_hedge(
        ctx=ctx,
        main_s_leg=main_s_leg,
        main_b_leg=main_b_leg,
        buyprice=buyprice,
        sellprice=sellprice
        )

def ncl_exit(ctx, ncl_open_positions):
    """
    Indicator exit for RCE (main + hedge)
    """

    main_b = ncl_open_positions.get("NCL_CE")
    # main_h = ncl_open_positions.get("NCL_HPE")
    main_s = ncl_open_positions.get("NCL_PE")

    # if not main_b or not main_s:
    #     logging.error("NCL exit requested but one or more legs missing")
    #     return None

    # IMPORTANT → explicit None check (Series cannot be used in boolean context)
    if main_b is None or main_s is None:# or main_h is None:
        logging.error("NCL exit requested but one or more legs missing")
        return None
    
    main_b_leg = {
        "trdSym": main_b["trdSym"],
        "tok": int(main_b["tok"]),
        "productType": "NRML",
        "qty": int(main_b["fldQty"])
    }

    main_s_leg = {
        "trdSym": main_s["trdSym"],
        "tok": int(main_s["tok"]),
        "productType": "NRML",
        "qty": int(main_s["fldQty"])
    }

    # main_h_leg = {
    #     "trdSym": main_h["trdSym"],
    #     "tok": int(main_h["tok"]),
    #     "productType": "NRML",
    #     "qty": int(main_h["fldQty"])
    # }

     # Flip sides using broker truth
    main_b_exit_side = "S" if main_b["trnsTp"] == "B" else "B"
    main_s_exit_side = "S" if main_s["trnsTp"] == "B" else "B"
    # main_h_exit_side = "S" if main_h["trnsTp"] == "B" else "B"

    b_ltp = get_ltp(ctx, int(main_b["tok"]))
    if b_ltp is None:
        logging.error("LTP is None — skipping..")
        return None 
    s_ltp = get_ltp(ctx, int(main_s["tok"]))
    if s_ltp is None:
        logging.error("LTP is None — skipping..")
        return None 
    # h_ltp = get_ltp(ctx, int(main_h["tok"]))

    b_buffer = max(b_ltp * ctx.limitorder, 1)
    s_buffer = max(s_ltp * ctx.limitorder, 1)
    # h_buffer = max(h_ltp * ctx.limitorder, 1)
    
    if main_b_exit_side == "B":
        buyprice = round_to_tick(b_ltp + b_buffer)
    else:
        buyprice = round_to_tick(b_ltp - b_buffer)

    if main_s_exit_side == "B":
        sellprice = round_to_tick(s_ltp + s_buffer)
    else:
        sellprice = round_to_tick(s_ltp - s_buffer)

    # if main_h_exit_side == "B":
    #     hedgeprice = round_to_tick(h_ltp + h_buffer)
    # else:
    #     hedgeprice = round_to_tick(h_ltp - h_buffer)

    logging.info(f"Placing order for CE {main_b['trdSym'][-7:]} | PE {main_s['trdSym'][-7:]}")

    return place_synthfut_exit_without_hedge(
        ctx=ctx,
        main_s_leg=main_s_leg,
        main_b_leg=main_b_leg,
        main_s_exit_side=main_s_exit_side,
        main_s_exit_tag=f"IND_NCL_PE_{int(time.time())}",
        main_b_exit_side=main_b_exit_side,
        main_b_exit_tag=f"IND_NCL_CE_{int(time.time())}",
        buyprice=buyprice,
        sellprice=sellprice)

def rollover_ncl(ctx):
    
    # applicable where we buy synthetic future of next expiry
    
    """
    NCL Rollover:
    If NCL position is open and its expiry is today:
        1. Force exit current NCL
        2. Immediately re-enter NCL using far expiry
    Indicators are ignored.
    """

    ncl_open_positions = ncl_build_positions(ctx)

    if ncl_open_positions is None:
        logging.info("NCL No open NCL position found..\n")
        return False

    # All legs have same expiry, pick any
    exp_dt_str = ncl_open_positions["NCL_CE"]["expDt"]
    exp_date = pd.to_datetime(exp_dt_str).date()
    today = datetime.now().date()

    # if today = exp_date ---> rollover
    # expiries on 17 feb, 24 feb, 3 mar, 10 mar and 17 mar

    # say today is 18 feb, if NHF triggers today, we buy synthfut of 3mar i.e. exp_dt = 3 mar
    # near expiry is 24 feb, next expiry is 3 mar 

    # on 24 feb,  near expiry is 24 feb, next expiry is 3 mar
    # so on 24 feb,  today !== exp_dt (3 mar)
    # so on 24 feb, we dont roll

    # on 25 feb,  near expiry is 3 mar, next expiry is 10 mar
    # so on 25 feb,  today != exp_dt (3 mar)
    # so on 25 feb, we dont roll

    # on 3 mar,  near expiry is 3 mar, next expiry is 10 mar
    # so on 3 mar,  today == nexp_dt (3 mar)
    # so on 3 mar, we roll to expiry of 10 mar

    # say today is 17 feb, if NHF triggers today, we buy synthfut of next expiry i.e. 24feb (exp_dt = 24 feb)
    # on 24 feb,  today !== exp_dt (24 feb)
    # so on 24 feb, we roll to expiry of 3 mar


    # if next expiry date is exp_date
    if today != exp_date :
        logging.info(f"NCL Open position found but no need of rollover today, expiry={exp_date}, today={today}")
        return False

    logging.warning(f"NCL ROLLOVER ALERT!!: Expiry today ({exp_date}) hence rolling to next expiry")

    # 1. Force EXIT of current expiry
    ncl_exit(ctx, ncl_open_positions)

    # Give broker + ledger some time to update
    time.sleep(1)

    # 2. Reconcile again to ensure position is closed
    ncl_after_exit = ncl_build_positions(ctx)

    if ncl_after_exit is not None:
        logging.error("NCL ROLLOVER: Exit not confirmed, aborting re-entry")
        # safe_execute (send_telegram_message, f" NCL ROLLOVER: Exit not confirmed, aborting re-entry for {ctx.clientname}.")
        return False

    logging.info("NCL ROLLOVER: Exit confirmed")

    # 3. Fresh ENTRY (will use far_nifty_expiry_date automatically)
    ncl_entry(ctx)

    # 4. Reconcile again to ensure new position is created
    ncl_reentry = ncl_build_positions(ctx)

    if ncl_reentry is None:
        logging.error("NCL ROLLOVER: Re-entry not confirmed..\n")
        # safe_execute (send_telegram_message, f" NCL ROLLOVER: Re-entry not confirmed for {ctx.clientname}.")
        return False

    logging.info("NCL ROLLOVER: New NCL position opened for next expiry..\n")
    # safe_execute (send_telegram_message, f" NCL ROLLOVER: New NCL position created successfully for {ctx.clientname}.")

    return True



# if exp_dt = ctx.next_expiry_date and today = ctx.near_expirydate --- rollover

# def rollover_nhf_a_week_before(ctx):
    
#     # applicable where synthfut expiry chosen is == ctx.far_nifty_expiry_date
    
#     """
#     NHF Rollover (1 week before contract expiry)

#     Trigger condition:
#         - NHF position expiry == ctx.next_nifty_expiry_date
#         - Today == ctx.nearest_nifty_expiry_date

#     Action:
#         1. Force exit current NHF
#         2. Immediately re-enter NHF using far expiry
#     Indicators are ignored.
#     """

#     nhf_open_positions = nhf_hedge_reconcile(ctx)

#     if nhf_open_positions is None:
#         logging.info("NHF Rollover: No open NHF position found")
#         return False

#     # All legs have same expiry, pick any
#     exp_dt_str = nhf_open_positions["NHFCE"]["expDt"]
#     exp_date = pd.to_datetime(exp_dt_str).date()
#     today = datetime.now().date()

#     near_exp = ctx.nearest_nifty_expiry_date
#     next_exp = ctx.next_nifty_expiry_date


#     # if today = ctx.near_expirydate  and exp_dt = ctx.next_expiry_date ---> rollover
#     # expiries on 17 feb, 24 feb, 3 mar, 10 mar and 17 mar

#     # say today is 18 feb, if NHF triggers today, we buy synthfut of 10mar i.e. exp_dt = 10 mar
#     # near expiry is 24 feb, next expiry is 3 mar and far expiry is 10 mar

#     # on 24 feb also,  near expiry is 24 feb, next expiry is 3 mar and far expiry is 10 mar
#     # so on 24 feb,  today == near expiry  but exp_dt (10 mar) != next expiry (3 mar)
#     # so on 24 feb, we dont roll

#     # on 25 feb,  near expiry is 3 mar, next expiry is 10 mar and far expiry is 17 mar
#     # so on 25 feb,  today != near expiry  but exp_dt (10 mar) == next expiry (10 mar)
#     # so on 25 feb, we dont roll

#     # on 3 mar,  near expiry is 3 mar, next expiry is 10 mar and far expiry is 17 mar
#     # so on 3 mar,  today == near expiry  but exp_dt (10 mar) == next expiry (10 mar)
#     # so on 3 mar, we roll

#     if not (today == near_exp and exp_date == next_exp):
#         logging.info(f"NHF Rollover: No action needed | pos_exp={exp_date} | near={near_exp} | next={next_exp} | today={today}")
#         return False

#     logging.warning(f"NHF ROLLOVER ALERT (A WEEK BEFORE): pos_exp={exp_date} | today={today} (near expiry)")

#     # 1. Force EXIT of current expiry
#     nhf_hedge_exit(ctx, nhf_open_positions)

#     # Give broker + ledger some time to update
#     time.sleep(1)

#     # 2. Reconcile again to ensure position is closed
#     nhf_after_exit = nhf_hedge_reconcile(ctx)

#     if nhf_after_exit is not None:
#         logging.error("NHF ROLLOVER: Exit not confirmed, aborting re-entry")
#         safe_execute(send_telegram_message,f"NHF ROLLOVER: Exit not confirmed, aborting re-entry for {ctx.clientname}.")
#         return False

#     logging.info("NHF ROLLOVER: Exit confirmed")

#     # 3. Fresh ENTRY (will use far_nifty_expiry_date automatically)
#     nhf_hedge_entry(ctx)

#     # 4. Reconcile again to ensure new position is created
#     nhf_reentry = nhf_hedge_reconcile(ctx)

#     if nhf_reentry is None:
#         logging.error("NHF ROLLOVER: Re-entry not confirmed")
#         safe_execute( send_telegram_message, f"NHF ROLLOVER: Re-entry not confirmed for {ctx.clientname}.")
#         return False

#     logging.info("NHF ROLLOVER: New NHF position opened for far expiry")
#     safe_execute(send_telegram_message, f"NHF ROLLOVER: New NHF position created successfully for {ctx.clientname}.")

#     return True


