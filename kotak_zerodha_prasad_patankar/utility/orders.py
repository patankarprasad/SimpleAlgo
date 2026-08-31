import logging
import time
import pandas as pd
import math
from utility.ledger import log_trade
from utility.common_functions import safe_execute, send_telegram_message
from data.instruments import get_ltp

def round_to_tick(price, tick=0.05):
    return round(round(price / tick) * tick, 2)

def wait_for_order_complete(ctx, order_id, max_attempts=10):  
    client = ctx.client
    for attempt in range(max_attempts):
        time.sleep(1)
        ob = client.order_report()
        if not ob or not ob.get("data"):
            continue
        df = pd.DataFrame(ob["data"])
        rows = df[df["nOrdNo"] == order_id]
        if rows.empty:
            logging.warning(f"Order {order_id} not found yet")
            continue
        row = rows.iloc[0]
        status = str(row["ordSt"]).lower().strip()
        if status == "complete":
            return row
        elif status in ["rejected", "cancelled"]:
            logging.error(f"Order {order_id} failed: {status}")
            return None
        else:
            logging.warning(f"attempt [{attempt+1}] for Order {order_id} pending: {status}")
            continue
    # Not completed → cancel
    logging.error(f"Order {order_id} not completed. Cancelling.")
    safe_execute(cancel_order, ctx, order_id)
    return None

def place_limit_order(
    ctx,
    trdSym: str,
    transaction_type: str,   # "B" or "S"
    quantity: int,
    order_type="L",
    exchange_segment = 'nse_fo',
    limitPrice=0,
    trigger_price=0,
    productType="NRML",
    tag=""):
    #logging.info(f"PLACING ORDER FOR SYMBOL: {trdSym} | QTY: {quantity} | SIDE: {transaction_type}")
    try:
        ordeRes = ctx.client.place_order(
            exchange_segment=exchange_segment,
            product=productType,
            price=str(limitPrice),
            order_type=order_type,
            quantity=str(quantity),
            validity="DAY",
            trading_symbol=trdSym,
            transaction_type=transaction_type,
            amo="NO",
            disclosed_quantity="0",
            market_protection="0",
            pf="N",
            trigger_price=str(trigger_price),
            tag=tag)
        if ordeRes.get("stCode") == 200 and ordeRes.get("nOrdNo"):
            logging.info(f"Order placed {tag} | SYMBOL:{trdSym} | QTY:{quantity} | SIDE:{transaction_type}")
            return ordeRes["nOrdNo"]
        else:
            raise Exception(f"Order placement failed: {ordeRes}")
    except Exception as e:
        logging.error(f"Error in order placement: {e}", exc_info=True)
        # safe_execute(send_telegram_message,f"❌ Error in order placement for {trdSym}")
        return None
    
def cancel_order(ctx, order_id):
    try:
        res = ctx.client.cancel_order(order_id=order_id)
        # logging.info(f"Order cancelled: {order_id}")
        time.sleep(0.5)
        return res
    except Exception as e:
        logging.exception(f"Error cancelling order {order_id}: {e}")
        return None



def place_entry_with_sl(ctx, main_leg, sl_config, price):
    """
    main_leg:
        {
            "trdSym": str,
            "side": "B" or "S",
            "qty": int,
            "tag": str
        }

    sl_config:
        {
            "sl_pct": float,
            "sl_tag": str
        }
    """
    client = ctx.client
    # 1. Place MAIN order
    main_order_id = safe_execute(
        place_limit_order,
        ctx,
        trdSym=main_leg["trdSym"],
        transaction_type=main_leg["side"],
        quantity=main_leg["qty"],
        order_type="L",
        limitPrice=price,
        trigger_price=0,
        productType=main_leg["productType"],
        tag=main_leg["tag"])
    if not main_order_id:
        return None
    main_row = wait_for_order_complete(ctx, main_order_id)
    if main_row is None:
        return None 
    entry_price = float(main_row["avgPrc"])
    if entry_price <= 0:
        logging.error(f"Invalid entry price for {main_order_id}")
        return None
    try:
        safe_execute(log_trade, pd.DataFrame([main_row]))
    except Exception as e:
        logging.error(f"Trade logging failed for entry {main_order_id}: {e}")
    # 2. Calculate SL prices and side
    if main_leg["side"] == "B":
        # long → SL is SELL
        sl_trigger = entry_price * (1 - sl_config["sl_pct"])
        sl_limit   = sl_trigger * 0.98
        sl_side = "S"
    else:
        # short → SL is BUY
        sl_trigger = entry_price * (1 + sl_config["sl_pct"])
        sl_limit   = sl_trigger * 1.02
        sl_side = "B"
    sl_trigger = math.floor(round_to_tick(sl_trigger) / 0.05) * 0.05
    sl_limit   = math.floor(round_to_tick(sl_limit)   / 0.05) * 0.05
    # 6. Place SL order
    sl_order_id = safe_execute(
        place_limit_order,
        ctx,
        trdSym=main_leg["trdSym"],
        transaction_type=sl_side,
        quantity=main_leg["qty"],
        order_type="SL",
        limitPrice=sl_limit,
        trigger_price=sl_trigger,
        productType=main_leg["productType"],
        tag=f"{sl_config['sl_tag']}_{int(time.time())}")
    if not sl_order_id:
        logging.error(f"CRITICAL: SL placement FAILED for {main_leg['trdSym']}")
    return {
        "entry_order_id": main_order_id,
        "sl_order_id": sl_order_id,
        "entry_price": entry_price
    }

def place_entry_with_hdg_and_sl(ctx, main_leg, hedge_leg, sl_config, main_price, hedge_price):
    """
    main_leg / hedge_leg:
        {
            "trdSym": str,
            "side": "B" or "S",
            "qty": int,
            "tag": str
        }
    """
    client = ctx.client
    # 1. Place HEDGE first
    hedge_order_id = safe_execute(
        place_limit_order,
        ctx,
        trdSym=hedge_leg["trdSym"],
        transaction_type=hedge_leg["side"],
        quantity=hedge_leg["qty"],
        order_type="L",
        limitPrice=hedge_price,
        trigger_price=0,
        productType=hedge_leg["productType"],
        tag=hedge_leg["tag"])
    if not hedge_order_id:
        return None
    hedge_row = wait_for_order_complete(ctx, hedge_order_id)
    if hedge_row is None:
        return None
    try:
        safe_execute(log_trade, pd.DataFrame([hedge_row]))
    except Exception as e:
        logging.error(f"Hedge log failed {hedge_order_id}: {e}")
    # 2. Place MAIN order
    main_order_id = safe_execute(
        place_limit_order,
        ctx,
        trdSym=main_leg["trdSym"],
        transaction_type=main_leg["side"],
        quantity=main_leg["qty"],
        order_type="L",
        limitPrice=main_price,
        trigger_price=0,
        productType=main_leg["productType"],
        tag=main_leg["tag"])
    if not main_order_id:
        return None
    main_row = wait_for_order_complete(ctx, main_order_id)
    if main_row is None:
        # >>> IMPORTANT: hedge left open
        logging.error("MAIN failed after hedge. Consider REC exit.")
        return None
    entry_price = float(main_row["avgPrc"])
    if entry_price <= 0:
        logging.error(f"Invalid entry price for {main_order_id}")
        return None
    try:
        safe_execute(log_trade, pd.DataFrame([main_row]))
    except Exception as e:
        logging.error(f"Trade logging failed for entry {main_order_id}: {e}")
    # 6. Calculate SL for MAIN
    if main_leg["side"] == "B":
        sl_trigger = entry_price * (1 - sl_config["sl_pct"])
        sl_limit   = sl_trigger * 0.98
        sl_side = "S"
    else:
        sl_trigger = entry_price * (1 + sl_config["sl_pct"])
        sl_limit   = sl_trigger * 1.02
        sl_side = "B"
    sl_trigger = math.floor(round_to_tick(sl_trigger) / 0.05) * 0.05
    sl_limit   = math.floor(round_to_tick(sl_limit)   / 0.05) * 0.05
    # 7. Place SL
    sl_order_id = safe_execute(
        place_limit_order,
        ctx,
        trdSym=main_leg["trdSym"],
        transaction_type=sl_side,
        quantity=main_leg["qty"],
        order_type="SL",
        limitPrice=sl_limit,
        trigger_price=sl_trigger,
        productType=main_leg["productType"],
        tag=f"{sl_config['sl_tag']}_{int(time.time())}")
    if not sl_order_id:
        logging.error(f"CRITICAL: SL placement FAILED {main_leg['trdSym']}")
    return {
        "main_entry_order_id": main_order_id,
        "hedge_entry_order_id": hedge_order_id,
        "sl_order_id": sl_order_id,
        "entry_price": entry_price
    }

def place_exit_with_sl(ctx, main_leg, sl_tag, exit_tag, exit_side, price):
    """
    main_leg:
        {
            "trdSym": str,
            "tok" : int(ceb["tok"]),
            "qty": int
        }

    exit_side: "B" or "S"
    """
    client = ctx.client
    # 1. Cancel SL orders + VERIFY cancel
    sl_ordernos = []
    try:
        orderbook = client.order_report()
        if orderbook and orderbook.get("data"):
            order_df = pd.DataFrame(orderbook["data"])
            sl_orders = order_df[
                order_df["GuiOrdId"].str.startswith(sl_tag, na=False) &
                order_df["ordSt"].isin(["open", "trigger pending", "partial"])]
            # Capture ordNos for verification
            sl_ordernos = sl_orders["nOrdNo"].tolist()
            # if premium sl is already hit, skip exit of leg
            if not sl_ordernos:
                logging.info(f"No active SL found for {sl_tag}. Skipping exit.")
                return None          
            for _, row in sl_orders.iterrows():
                safe_execute(cancel_order, ctx, row["nOrdNo"])
                logging.info(f"Cancel requested SL {row['GuiOrdId']}")
    except Exception as e:
        logging.error(f"SL cancel failed for {sl_tag}: {e}")
        return None
    # ---- VERIFY cancel ----
    cancel_verified = False
    if sl_ordernos:
        for _ in range(5):  
            time.sleep(1)
            orderbook = client.order_report()
            if not orderbook or not orderbook.get("data"):
                continue
            order_df = pd.DataFrame(orderbook["data"])
            remaining = order_df[
                order_df["nOrdNo"].isin(sl_ordernos) &
                order_df["ordSt"].isin(["open", "trigger pending", "partial"])]
            if remaining.empty:
                cancel_verified = True
                break
            else:
                logging.warning(f"Waiting SL cancel confirmation {remaining[['GuiOrdId','ordSt']].to_dict('records')}")
        if not cancel_verified:
            logging.error(f"SL cancel NOT confirmed for {sl_tag}, aborting exit")
            return None
    # 2. Exit MAIN
    exit_order_id = safe_execute(
        place_limit_order,
        ctx,
        trdSym=main_leg["trdSym"],
        transaction_type=exit_side,
        quantity=main_leg["qty"],
        order_type="L",
        limitPrice=price,
        trigger_price=0,
        productType=main_leg["productType"],
        tag=exit_tag)
    if not exit_order_id:
        logging.error(f"Exit failed for {main_leg['trdSym']}")
        return None
    exit_row = wait_for_order_complete(ctx, exit_order_id)
    if exit_row is None:
        logging.error(f"Exit not completed. Order {exit_order_id} cancelled or failed.")
        return None
    try:
        safe_execute(log_trade, pd.DataFrame([exit_row]))
    except Exception as e:
        logging.error(f"Trade logging failed for exit {exit_order_id}: {e}")
    return exit_order_id


def place_entry_without_sl(ctx, main_leg, price):
    """
    main_leg:
        {
            "trdSym": str,
            "side": "B" or "S",
            "qty": int,
            "tag": str
        }
    """
    client = ctx.client
    # 1. Place MAIN order
    main_order_id = safe_execute(
        place_limit_order,
        ctx,
        trdSym=main_leg["trdSym"],
        transaction_type=main_leg["side"],
        quantity=main_leg["qty"],
        order_type="L",
        limitPrice=price,
        trigger_price=0,
        productType=main_leg["productType"],
        tag=main_leg["tag"])
    if not main_order_id:
        return None
    main_row = wait_for_order_complete(ctx, main_order_id)
    if main_row is None:
        return None 
    entry_price = float(main_row["avgPrc"])
    if entry_price <= 0:
        logging.error(f"Invalid entry price for {main_order_id}")
        return None
    try:
        safe_execute(log_trade, pd.DataFrame([main_row]))
    except Exception as e:
        logging.error(f"Trade logging failed for entry {main_order_id}: {e}")
    
    return {
        "entry_order_id": main_order_id,
        "entry_price": entry_price
    }

def place_exit_without_sl(ctx, main_leg, exit_tag, exit_side, price):
    """
    main_leg:
        {
            "trdSym": str,
            "tok" : int(ceb["tok"]),
            "qty": int
        }

    exit_side: "B" or "S"
    """
    client = ctx.client
    
    exit_order_id = safe_execute(
        place_limit_order,
        ctx,
        trdSym=main_leg["trdSym"],
        transaction_type=exit_side,
        quantity=main_leg["qty"],
        order_type="L",
        limitPrice=price,
        trigger_price=0,
        productType=main_leg["productType"],
        tag=exit_tag)
    if not exit_order_id:
        logging.error(f"Exit failed for {main_leg['trdSym']}")
        return None
    exit_row = wait_for_order_complete(ctx, exit_order_id)
    if exit_row is None:
        logging.error(f"Exit not completed. Order {exit_order_id} cancelled or failed.")
        return None
    try:
        safe_execute(log_trade, pd.DataFrame([exit_row]))
    except Exception as e:
        logging.error(f"Trade logging failed for exit {exit_order_id}: {e}")
    return exit_order_id


def place_entry_with_hdg_without_sl(ctx, main_leg, hedge_leg, main_price, hedge_price):
    """
    main_leg / hedge_leg:
        {
            "trdSym": str,
            "side": "B" or "S",
            "qty": int,
            "tag": str
        }
    """
    client = ctx.client
    # 1. Place HEDGE first
    hedge_order_id = safe_execute(
        place_limit_order,
        ctx,
        trdSym=hedge_leg["trdSym"],
        transaction_type=hedge_leg["side"],
        quantity=hedge_leg["qty"],
        order_type="L",
        limitPrice=hedge_price,
        trigger_price=0,
        productType=hedge_leg["productType"],
        tag=hedge_leg["tag"])
    if not hedge_order_id:
        return None
    hedge_row = wait_for_order_complete(ctx, hedge_order_id)
    if hedge_row is None:
        return None
    try:
        safe_execute(log_trade, pd.DataFrame([hedge_row]))
    except Exception as e:
        logging.error(f"Hedge log failed {hedge_order_id}: {e}")
    # 2. Place MAIN order
    main_order_id = safe_execute(
        place_limit_order,
        ctx,
        trdSym=main_leg["trdSym"],
        transaction_type=main_leg["side"],
        quantity=main_leg["qty"],
        order_type="L",
        limitPrice=main_price,
        trigger_price=0,
        productType=main_leg["productType"],
        tag=main_leg["tag"])
    if not main_order_id:
        return None
    main_row = wait_for_order_complete(ctx, main_order_id)
    if main_row is None:
        # >>> IMPORTANT: hedge left open
        logging.error("MAIN failed after hedge. Consider REC exit.")
        return None
    entry_price = float(main_row["avgPrc"])
    if entry_price <= 0:
        logging.error(f"Invalid entry price for {main_order_id}")
        return None
    try:
        safe_execute(log_trade, pd.DataFrame([main_row]))
    except Exception as e:
        logging.error(f"Trade logging failed for entry {main_order_id}: {e}")
    
    return {
        "main_entry_order_id": main_order_id,
        "hedge_entry_order_id": hedge_order_id,
        "entry_price": entry_price
    }


def place_short_target(
    ctx,
    trdSym,
    quantity,
    target_price,
    productType="NRML",
    tag=""
):
    
    target_order_id = safe_execute(
        place_limit_order,
        ctx,
        trdSym=trdSym,
        transaction_type="B",     # cover short
        quantity=quantity,
        order_type="L",
        limitPrice=target_price,
        trigger_price=0,
        productType=productType,
        tag=tag
    )

    if not target_order_id:
        logging.error(
            f"Failed to place target order for {trdSym}"
        )
        return None

    logging.info(
        f"Target order placed | {trdSym[-7:]} | "
        f"Target={target_price} | Tag={tag}"
    )

    return target_order_id


def place_synthfut_entry(ctx, main_s_leg, main_h_leg, main_b_leg, buyprice, sellprice, hedgeprice):
    """
    main_leg / hedge_leg:
        {
            "trdSym": str,
            "side": "B" or "S",
            "qty": int,
            "tag": str
        }
    """
    main_s_order_id = None
    main_b_order_id = None
    main_h_order_id = None
    # 1. Place MAIN buy order
    main_b_order_id = safe_execute(
        place_limit_order,
        ctx,
        trdSym=main_b_leg["trdSym"],
        transaction_type=main_b_leg["side"],
        quantity=main_b_leg["qty"],
        order_type="L",
        limitPrice=buyprice,
        trigger_price=0,
        productType=main_b_leg["productType"],
        tag=main_b_leg["tag"])
    if not main_b_order_id:
        logging.error("MAIN BUY failed")
        return None   
    main_b_row = wait_for_order_complete(ctx, main_b_order_id)
    if main_b_row is None:
        logging.error("MAIN BUY not completed")
        return None   
    
    # 2. Place MAIN hedge order
    main_h_order_id = safe_execute(
        place_limit_order,
        ctx,
        trdSym=main_h_leg["trdSym"],
        transaction_type=main_h_leg["side"],
        quantity=main_h_leg["qty"],
        order_type="L",
        limitPrice=hedgeprice,
        trigger_price=0,
        productType=main_h_leg["productType"],
        tag=main_h_leg["tag"])

# if hedge is not PLACED, cleanup the main leg also instead of merely returing none
    if not main_h_order_id:
        logging.error("HEDGE order placement failed — unwinding BUY")
        
        ltp = get_ltp(ctx, main_b_leg["tok"])  
        buffer = max(ltp * ctx.limitorder, 0.10)
        if main_b_leg["side"] == "B":
            # original was BUY → now we SELL
            unwind_buy_price = round_to_tick(ltp - buffer)
        else:
            # original was SELL → now we BUY
            unwind_buy_price = round_to_tick(ltp + buffer)       
        exit_id = safe_execute(
            place_limit_order, ctx,
            trdSym=main_b_leg["trdSym"],
            transaction_type="S" if main_b_leg["side"] == "B" else "B",
            quantity=main_b_leg["qty"],
            order_type="L",
            limitPrice=unwind_buy_price,
            trigger_price=0,
            productType=main_b_leg["productType"],
            tag=f"MAIN_BUY_UNWIND_{int(time.time())}")
        if exit_id:
            wait_for_order_complete(ctx, exit_id)
        return None
    main_h_row = wait_for_order_complete(ctx, main_h_order_id)

# if hedge is not COMPLETED, cleanup the main leg also instead of merely returing none
    if main_h_row is None:
        logging.error("HEDGE failed — unwinding BUY")
        
        ltp = get_ltp(ctx, main_b_leg["tok"])  
        buffer = max(ltp * ctx.limitorder, 0.10)
        if main_b_leg["side"] == "B":
            # original was BUY → now we SELL
            unwind_buy_price = round_to_tick(ltp - buffer)
        else:
            # original was SELL → now we BUY
            unwind_buy_price = round_to_tick(ltp + buffer) 
        exit_id = safe_execute(
            place_limit_order, ctx,
            trdSym=main_b_leg["trdSym"],
            transaction_type="S" if main_b_leg["side"] == "B" else "B",
            quantity=main_b_leg["qty"],
            order_type="L",
            limitPrice=unwind_buy_price,
            trigger_price=0,
            productType=main_b_leg["productType"],
            tag=f"MAIN_BUY_UNWIND_{int(time.time())}")
        if exit_id:
            wait_for_order_complete(ctx, exit_id)
        return None
    
    # 2. Place MAIN sell order
    main_s_order_id = safe_execute(
        place_limit_order,
        ctx,
        trdSym=main_s_leg["trdSym"],
        transaction_type=main_s_leg["side"],
        quantity=main_s_leg["qty"],
        order_type="L",
        limitPrice=sellprice,
        trigger_price=0,
        productType=main_s_leg["productType"],
        tag=main_s_leg["tag"])

# if main buy and hedge are COMPLETED, 
# but if main sell is not PLACED, cleanup them instead of merely returing none    
    if not main_s_order_id:
        logging.error("MAIN SELL placement failed — unwinding BUY + HEDGE")
        
        # unwind MAIN
        ltp = get_ltp(ctx, main_b_leg["tok"])  
        buffer = max(ltp * ctx.limitorder, 0.10)
        if main_b_leg["side"] == "B":
            # original was BUY → now we SELL
            unwind_buy_price = round_to_tick(ltp - buffer)
        else:
            # original was SELL → now we BUY
            unwind_buy_price = round_to_tick(ltp + buffer)
        exit_b = safe_execute(
            place_limit_order, ctx,
            trdSym=main_b_leg["trdSym"],
            transaction_type="S" if main_b_leg["side"] == "B" else "B",
            quantity=main_b_leg["qty"],
            order_type="L",
            limitPrice=unwind_buy_price,
            trigger_price=0,
            productType=main_b_leg["productType"],
            tag=f"MAIN_BUY_UNWIND_{int(time.time())}")
        if exit_b:
            wait_for_order_complete(ctx, exit_b)

        # unwind HEDGE
        ltp = get_ltp(ctx, main_h_leg["tok"])  
        buffer = max(ltp * ctx.limitorder, 0.10)
        if main_h_leg["side"] == "B":
            # original was BUY → now we SELL
            unwind_hedge_price = round_to_tick(ltp - buffer)
        else:
            # original was SELL → now we BUY
            unwind_hedge_price = round_to_tick(ltp + buffer)
        exit_h = safe_execute(
            place_limit_order, ctx,
            trdSym=main_h_leg["trdSym"],
            transaction_type="S" if main_h_leg["side"] == "B" else "B",
            quantity=main_h_leg["qty"],
            order_type="L",
            limitPrice=unwind_hedge_price,
            trigger_price=0,
            productType=main_h_leg["productType"],
            tag=f"HEDGE_UNWIND_{int(time.time())}")
        if exit_h:
            wait_for_order_complete(ctx, exit_h)
        
        return None

    main_s_row = wait_for_order_complete(ctx, main_s_order_id)

# if main buy and hedge are COMPLETED, 
# but if main sell is not COMPLETED, cleanup them instead of merely returing none
    if main_s_row is None:
        logging.error("MAIN SELL failed — unwinding BUY + HEDGE")
        
        # unwind BUY
        ltp = get_ltp(ctx, main_b_leg["tok"])  
        buffer = max(ltp * ctx.limitorder, 0.10)
        if main_b_leg["side"] == "B":
            # original was BUY → now we SELL
            unwind_buy_price = round_to_tick(ltp - buffer)
        else:
            # original was SELL → now we BUY
            unwind_buy_price = round_to_tick(ltp + buffer)
        exit_b = safe_execute(
            place_limit_order, ctx,
            trdSym=main_b_leg["trdSym"],
            transaction_type="S" if main_b_leg["side"] == "B" else "B",
            quantity=main_b_leg["qty"],
            order_type="L",
            limitPrice=unwind_buy_price,
            trigger_price=0,
            productType=main_b_leg["productType"],
            tag=f"MAIN_BUY_UNWIND_{int(time.time())}")
        if exit_b:
            wait_for_order_complete(ctx, exit_b)

        # unwind HEDGE
        ltp = get_ltp(ctx, main_h_leg["tok"])  
        buffer = max(ltp * ctx.limitorder, 0.10)
        if main_h_leg["side"] == "B":
            # original was BUY → now we SELL
            unwind_hedge_price = round_to_tick(ltp - buffer)
        else:
            # original was SELL → now we BUY
            unwind_hedge_price = round_to_tick(ltp + buffer)
        exit_h = safe_execute(
            place_limit_order, ctx,
            trdSym=main_h_leg["trdSym"],
            transaction_type="S" if main_h_leg["side"] == "B" else "B",
            quantity=main_h_leg["qty"],
            order_type="L",
            limitPrice=unwind_hedge_price,
            trigger_price=0,
            productType=main_h_leg["productType"],
            tag=f"HEDGE_UNWIND_{int(time.time())}")
        if exit_h:
            wait_for_order_complete(ctx, exit_h)

        return None

    main_b_entry_price = float(main_b_row["avgPrc"])
    main_h_entry_price = float(main_h_row["avgPrc"])
    main_s_entry_price = float(main_s_row["avgPrc"])
    if any(p <= 0 for p in [main_b_entry_price, main_h_entry_price, main_s_entry_price]):
        logging.error("Invalid entry prices in synthetic entry")
        return None
    for row, oid in [
        (main_b_row, main_b_order_id),
        (main_h_row, main_h_order_id),
        (main_s_row, main_s_order_id)]:
        try:
            safe_execute(log_trade, pd.DataFrame([row]))
        except Exception as e:
            logging.error(f"Log failed {oid}: {e}")
    # 6. Return metadata (optional but powerful)
    return {
        "main_s_order_id": main_s_order_id,
        "main_h_order_id": main_h_order_id,
        "main_b_order_id": main_b_order_id,
        "main_s_entry_price": main_s_entry_price,
        "main_h_entry_price": main_h_entry_price,
        "main_b_entry_price": main_b_entry_price }

def place_synthfut_exit(ctx, main_s_leg, main_b_leg, main_h_leg, 
                            main_s_exit_side, main_s_exit_tag,
                            main_h_exit_side, main_h_exit_tag,
                            main_b_exit_side, main_b_exit_tag,
                            buyprice, sellprice, hedgeprice):
    """
    main_leg / hedge_leg:
        {
            "trdSym": str,
            "qty": int
        }

    main_exit_side, hedge_exit_side:
        "B" or "S"
    """
    exit_data = []
    s_id = safe_execute(place_limit_order, ctx,
        trdSym=main_s_leg["trdSym"],
        transaction_type=main_s_exit_side,
        quantity=main_s_leg["qty"],
        limitPrice=sellprice,
        tag=main_s_exit_tag)
    if s_id:
        s_row = wait_for_order_complete(ctx, s_id)
        if s_row is not None:
            exit_data.append((s_id, s_row))
    h_id = safe_execute(place_limit_order, ctx,
        trdSym=main_h_leg["trdSym"],
        transaction_type=main_h_exit_side,
        quantity=main_h_leg["qty"],
        limitPrice=hedgeprice,
        tag=main_h_exit_tag)
    if h_id:
        h_row = wait_for_order_complete(ctx, h_id)
        if h_row is not None:
            exit_data.append((h_id, h_row))   
    b_id = safe_execute(place_limit_order, ctx,
        trdSym=main_b_leg["trdSym"],
        transaction_type=main_b_exit_side,
        quantity=main_b_leg["qty"],
        limitPrice=buyprice,
        tag=main_b_exit_tag)
    if b_id:
        b_row = wait_for_order_complete(ctx, b_id)
        if b_row is not None:
            exit_data.append((b_id, b_row))
    for oid, row in exit_data:
        try:
            safe_execute(log_trade, pd.DataFrame([row]))
        except Exception as e:
            logging.error(f"Exit log failed {oid}: {e}")
    logging.info("Synthetic exit completed")
    return [x[0] for x in exit_data]


def place_synthfut_entry_without_hedge(ctx, main_s_leg, main_b_leg, buyprice, sellprice):
    """
    main_leg / hedge_leg:
        {
            "trdSym": str,
            "side": "B" or "S",
            "qty": int,
            "tag": str
        }
    """
    main_s_order_id = None
    main_b_order_id = None

    # 1. Place MAIN buy order
    main_b_order_id = safe_execute(
        place_limit_order,
        ctx,
        trdSym=main_b_leg["trdSym"],
        transaction_type=main_b_leg["side"],
        quantity=main_b_leg["qty"],
        order_type="L",
        limitPrice=buyprice,
        trigger_price=0,
        productType=main_b_leg["productType"],
        tag=main_b_leg["tag"])
    if not main_b_order_id:
        logging.error("MAIN BUY failed")
        return None   
    main_b_row = wait_for_order_complete(ctx, main_b_order_id)
    if main_b_row is None:
        logging.error("MAIN BUY not completed")
        return None   
    
    # 2. Place MAIN sell order
    main_s_order_id = safe_execute(
        place_limit_order,
        ctx,
        trdSym=main_s_leg["trdSym"],
        transaction_type=main_s_leg["side"],
        quantity=main_s_leg["qty"],
        order_type="L",
        limitPrice=sellprice,
        trigger_price=0,
        productType=main_s_leg["productType"],
        tag=main_s_leg["tag"])

# if main buy is COMPLETED, 
# but if main sell is not PLACED, clean BUY LEG up instead of merely returing none    
    if not main_s_order_id:
        logging.error("MAIN SELL placement failed — unwinding BUY")
        
        # unwind MAIN
        ltp = get_ltp(ctx, main_b_leg["tok"])  
        buffer = max(ltp * ctx.limitorder, 0.10)
        if main_b_leg["side"] == "B":
            # original was BUY → now we SELL
            unwind_buy_price = round_to_tick(ltp - buffer)
        else:
            # original was SELL → now we BUY
            unwind_buy_price = round_to_tick(ltp + buffer)
        exit_b = safe_execute(
            place_limit_order, ctx,
            trdSym=main_b_leg["trdSym"],
            transaction_type="S" if main_b_leg["side"] == "B" else "B",
            quantity=main_b_leg["qty"],
            order_type="L",
            limitPrice=unwind_buy_price,
            trigger_price=0,
            productType=main_b_leg["productType"],
            tag=f"MAIN_BUY_UNWIND_{int(time.time())}")
        if exit_b:
            wait_for_order_complete(ctx, exit_b)
        return None
    
    # main_s_row = wait_for_order_complete(ctx, main_s_order_id)
    if main_s_order_id:
        main_s_row = wait_for_order_complete(ctx, main_s_order_id)
    else:
        return None

# if main buy is COMPLETED, 
# but if main sell is not COMPLETED, clean BUY LEG up instead of merely returing none
    if main_s_row is None:
        logging.error("MAIN SELL failed — unwinding BUY")
        
        # unwind BUY
        ltp = get_ltp(ctx, main_b_leg["tok"])  
        buffer = max(ltp * ctx.limitorder, 0.10)
        if main_b_leg["side"] == "B":
            # original was BUY → now we SELL
            unwind_buy_price = round_to_tick(ltp - buffer)
        else:
            # original was SELL → now we BUY
            unwind_buy_price = round_to_tick(ltp + buffer)
        exit_b = safe_execute(
            place_limit_order, ctx,
            trdSym=main_b_leg["trdSym"],
            transaction_type="S" if main_b_leg["side"] == "B" else "B",
            quantity=main_b_leg["qty"],
            order_type="L",
            limitPrice=unwind_buy_price,
            trigger_price=0,
            productType=main_b_leg["productType"],
            tag=f"MAIN_BUY_UNWIND_{int(time.time())}")
        if exit_b:
            wait_for_order_complete(ctx, exit_b)

        return None
    
    main_b_entry_price = float(main_b_row["avgPrc"])
    main_s_entry_price = float(main_s_row["avgPrc"])
    if any(p <= 0 for p in [main_b_entry_price, main_s_entry_price]):
        logging.error("Invalid entry prices in synthetic entry")
        return None
    for row, oid in [
        (main_b_row, main_b_order_id),
        (main_s_row, main_s_order_id)]:
        try:
            safe_execute(log_trade, pd.DataFrame([row]))
        except Exception as e:
            logging.error(f"Log failed {oid}: {e}")
    # 6. Return metadata (optional but powerful)
    return {
        "main_s_order_id": main_s_order_id,
        "main_b_order_id": main_b_order_id,
        "main_s_entry_price": main_s_entry_price,
        "main_b_entry_price": main_b_entry_price }

def place_synthfut_exit_without_hedge(ctx, main_s_leg, main_b_leg, 
                            main_s_exit_side, main_s_exit_tag,
                            main_b_exit_side, main_b_exit_tag,
                            buyprice, sellprice):
    """
    main_leg / hedge_leg:
        {
            "trdSym": str,
            "qty": int
        }

    main_exit_side, hedge_exit_side:
        "B" or "S"
    """
    exit_data = []
    s_id = safe_execute(place_limit_order, ctx,
        trdSym=main_s_leg["trdSym"],
        transaction_type=main_s_exit_side,
        quantity=main_s_leg["qty"],
        limitPrice=sellprice,
        tag=main_s_exit_tag)
    if s_id:
        s_row = wait_for_order_complete(ctx, s_id)
        if s_row is not None:
            exit_data.append((s_id, s_row))
    b_id = safe_execute(place_limit_order, ctx,
        trdSym=main_b_leg["trdSym"],
        transaction_type=main_b_exit_side,
        quantity=main_b_leg["qty"],
        limitPrice=buyprice,
        tag=main_b_exit_tag)
    if b_id:
        b_row = wait_for_order_complete(ctx, b_id)
        if b_row is not None:
            exit_data.append((b_id, b_row))
    for oid, row in exit_data:
        try:
            safe_execute(log_trade, pd.DataFrame([row]))
        except Exception as e:
            logging.error(f"Exit log failed {oid}: {e}")
    logging.info("Synthetic exit completed")
    return [x[0] for x in exit_data]



def place_cmdty_entry(ctx, main_leg, price):
    """
    main_leg:
        {
            "trdSym": str,
            "side": "B" or "S",
            "qty": int,
            "tag": str
        }
    """
    client = ctx.client
    # 1. Place MAIN order
    main_order_id = safe_execute(
        place_limit_order,
        ctx,
        trdSym=main_leg["trdSym"],
        transaction_type=main_leg["side"],
        quantity=main_leg["qty"],
        order_type="L",
        exchange_segment="mcx_fo",
        limitPrice=price,
        trigger_price=0,
        productType=main_leg["productType"],
        tag=main_leg["tag"])
    if not main_order_id:
        return None
    main_row = wait_for_order_complete(ctx, main_order_id)
    if main_row is None:
        return None 
    entry_price = float(main_row["avgPrc"])
    if entry_price <= 0:
        logging.error(f"Invalid entry price for {main_order_id}")
        return None
    try:
        safe_execute(log_trade, pd.DataFrame([main_row]))
    except Exception as e:
        logging.error(f"Trade logging failed for entry {main_order_id}: {e}")
    
    return {
        "entry_order_id": main_order_id,
        "entry_price": entry_price
    }

def place_cmdty_exit(ctx, main_leg, exit_tag, exit_side, price):
    """
    main_leg:
        {
            "trdSym": str,
            "tok" : int(ceb["tok"]),
            "qty": int
        }

    exit_side: "B" or "S"
    """
    client = ctx.client
    
    exit_order_id = safe_execute(
        place_limit_order,
        ctx,
        trdSym=main_leg["trdSym"],
        transaction_type=exit_side,
        quantity=main_leg["qty"],
        order_type="L",
        exchange_segment="mcx_fo",
        limitPrice=price,
        trigger_price=0,
        productType=main_leg["productType"],
        tag=exit_tag)
    if not exit_order_id:
        logging.error(f"Exit failed for {main_leg['trdSym']}")
        return None
    exit_row = wait_for_order_complete(ctx, exit_order_id)
    if exit_row is None:
        logging.error(f"Exit not completed. Order {exit_order_id} cancelled or failed.")
        return None
    try:
        safe_execute(log_trade, pd.DataFrame([exit_row]))
    except Exception as e:
        logging.error(f"Trade logging failed for exit {exit_order_id}: {e}")
    return exit_order_id



   