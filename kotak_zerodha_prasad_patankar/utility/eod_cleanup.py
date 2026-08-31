import time
import logging
import pandas as pd
from datetime import datetime, timedelta
from utility.common_functions import safe_execute, send_telegram_message
from utility.ledger import log_trade
from data.instruments import get_ltp
from utility.orders import place_limit_order, round_to_tick

def eod_cleanup(ctx):

    open_positions, order_df = eod_build_positions(ctx)

    eod_cancel_sl(ctx, order_df)

    time.sleep(1)

    # rebuild state after cancellation
    open_positions, order_df = eod_build_positions(ctx)

    open_positions, order_df = eod_reconcile(ctx, open_positions, order_df)

    time.sleep(10)

    final_confirmation(ctx)

def eod_build_positions(ctx):

    client = ctx.client
    open_positions = {}
    order_df = pd.DataFrame()  # ALWAYS define

    STRATS = [
    {"name":"CEB","main":"CEB","sl":None,"ind":"IND_CEB","hedge":False},
    {"name":"C2EB","main":"C2EB","sl":None,"ind":"IND_C2EB","hedge":False},
    {"name":"PEB","main":"PEB","sl":None,"ind":"IND_PEB","hedge":False}, # this is for reference, if in future we need any strategy without premium stoploss
    # {"name": "PEB", "main": "PEB", "sl": "SL_PEB", "ind": "IND_PEB", "hedge": False}, # this is for reference, if in future we need to add premium stoploss to any strategy
    # {"name":"SPE_HDG","main":"SPE_HDG","sl":"ORPH_SPE_HDG","ind":None,"hedge":True}, # removed because we dont want hedge for sold leg
    # {"name":"SPE","main":"SPE","sl":"SL_SPE","ind":"IND_SPE","hedge":False},
    {"name":"SPE","main":"SPE","sl":None,"ind":"IND_SPE","hedge":False},
    {"name":"RCE_HDG","main":"RCE_HDG","sl":"ORPH_RCE_HDG","ind":None,"hedge":True},
    {"name":"RCE","main":"RCE","sl":"SL_RCE","ind":"IND_RCE","hedge":False},]

    # understand here that key is "lowercase letters" and actual tags are "uppercase letters"
    # sl, ind are keys and s[sl] => SL, s[ind] => IND
    # dictionary keys → lowercase
    # constants / tags → uppercase

    time.sleep(1)
    orderbook = client.order_report()  

    if not orderbook or 'data' not in orderbook or not orderbook['data']:
        logging.info("No order data found.")
    else:
        order_df = pd.DataFrame(orderbook.get('data', []))
        order_df["GuiOrdId"] = order_df["GuiOrdId"].astype(str).str.strip()
        order_df["ordSt"] = order_df["ordSt"].astype(str).str.lower()
        order_df["exCfmTm"] = pd.to_datetime(order_df["exCfmTm"],  format="%d-%b-%Y %H:%M:%S", errors="coerce")
        if order_df.empty:
            logging.info("Order book is empty.")
        else:
            logging.info("Order book loaded successfully.")
                       
        rows = {}
        for s in STRATS:
            if s["hedge"]:
                main_all = order_df[order_df["GuiOrdId"].str.startswith(s["main"], na=False)]
            else:
                main_all = order_df[
                    order_df["GuiOrdId"].str.startswith(s["main"], na=False) &
                    ~order_df["GuiOrdId"].str.startswith(s["main"]+"_HDG", na=False)]
            sl_all = order_df[order_df["GuiOrdId"].str.startswith(s["sl"], na=False)] if s["sl"] else pd.DataFrame()
            ind_all = order_df[order_df["GuiOrdId"].str.startswith(s["ind"], na=False)] if s["ind"] else pd.DataFrame()
            row = pd.DataFrame()
            if not main_all.empty:
                main_row = main_all[main_all["nOrdNo"] == main_all["nOrdNo"].max()]
                row = main_row
                if s["sl"] is None and not ind_all.empty:
                    ind_row = ind_all[ind_all['nOrdNo'] == ind_all['nOrdNo'].max()]
                    ind_status = ind_row.iloc[0]['ordSt']
                    if ind_status == "complete":
                        if ind_row.iloc[0]['exCfmTm'] >= main_row.iloc[0]['exCfmTm']:
                            row = pd.DataFrame()  # position closed via IND               
                elif s["sl"] is not None and not sl_all.empty:
                    sl_row = sl_all[sl_all['nOrdNo'] == sl_all['nOrdNo'].max()] # ordebook madhali SL_CEB tag asnari latest row:
                    sl_status = sl_row.iloc[0]['ordSt'] # SL madhlya pahilya ordercha status bagh
                    if sl_status == "complete": # ata jar te status completed asel tar premium SL udala ahe
                        if sl_row.iloc[0]['exCfmTm'] > main_row.iloc[0]['exCfmTm'] + timedelta(minutes=1): # SL main orderchya kiman 1 minute tari nantar udala ka?
                            row = pd.DataFrame() # SL asa nantar udala mhanje khara SL gela, open_position tayar karaychi garaj nahi
                    elif sl_status == "cancelled" and not ind_all.empty: # ani status cancelled asel tar indicator SL udawtana apan to SL cancel kelay
                        ind_row = ind_all[ind_all['nOrdNo'] == ind_all['nOrdNo'].max()] # ordebook madhali IND_CEB tag asnari latest row:
                        if ind_row.iloc[0]['exCfmTm'] >= sl_row.iloc[0]['exCfmTm']: # IND_CEB ch timing SL_CEB peksha jasti asel tar to nantar udalay
                            row = pd.DataFrame() # mhanje regular IND SL gelay, so open position tayar karaychi garaj nahi
            rows[s["name"]] = row
       
        ## at eod we dont want to suppress hedges. we want to clear everything
        
        # if not rows.get("SPE", pd.DataFrame()).empty:
        #     rows["SPE_HDG"] = pd.DataFrame()
        # if not rows.get("RCE", pd.DataFrame()).empty:
        #     rows["RCE_HDG"] = pd.DataFrame()

        valid_rows = [r for r in rows.values() if not r.empty]
        if valid_rows:
            filtered_orders = pd.concat(valid_rows, ignore_index=True)
        else:
            filtered_orders = pd.DataFrame()
        if filtered_orders.empty:
            logging.info("No old open orders found.")
            
        # Fetch position book
        position_res = client.positions()

        if not position_res.get('stat') or not position_res.get('data'):
            logging.info("No positions found.")
        else:
            position_df = pd.DataFrame(position_res.get('data', []))
                            
            # Loop through each filtered order
            for _, order_row in filtered_orders.iterrows():
                order_tag = order_row.get('GuiOrdId')
                order_symbol = order_row.get('trdSym')

                # Find matching position where strikeprice matches and netqty != 0
                match = position_df[
                    (position_df['trdSym'] == order_symbol) &
                    ((position_df['flBuyQty'].astype(int) - position_df['flSellQty'].astype(int)) != 0)]
                
                if match.empty:
                    continue
                pos_row = match.iloc[0]

                for s in STRATS:
                    if order_tag.startswith(s["main"]):

                        open_positions[s["name"]] = {
                            'trdSym': order_row.get('trdSym'),
                            'tok': order_row.get('tok'),
                            'GuiOrdId': order_row.get('GuiOrdId'),
                            'nOrdNo': order_row.get('nOrdNo'),
                            'trnsTp': order_row.get('trnsTp'),
                            'avgPrc': float(order_row.get('avgPrc')),
                            # 'productType': order_row.get('prod'),
                            'strike': pos_row.get('stkPrc')}

                        logging.info(
                            f"Running {s['name']} {open_positions[s['name']]['trdSym'][-7:]} "
                            f"at {round(open_positions[s['name']]['avgPrc'],2)}")  

                        break

    return open_positions, order_df

def eod_cancel_sl(ctx, order_df):
    client = ctx.client
    if order_df is None or order_df.empty:
        return
    sl_orders = order_df[
        (order_df["GuiOrdId"].str.startswith("SL_", na=False)) &
        (order_df["ordSt"].isin(["open", "trigger pending", "partial"]))]

    cancelled_sl = 0
    for _, sl in sl_orders.iterrows():
        try:
            resp = client.cancel_order(order_id=sl["nOrdNo"])
            logging.info(f"[EOD] Cancelled SL {sl['nOrdNo']} | {resp}")
            cancelled_sl += 1
            time.sleep(0.5)
        except Exception as e:
            logging.error(f"[EOD] Failed to cancel SL {sl['nOrdNo']} | {e}")

    logging.info(f"[EOD] All SLs cancelled: {cancelled_sl}")

def eod_reconcile(ctx, open_positions, order_df):
    exit_ids = []
    if order_df is None or order_df.empty:
        return open_positions, order_df
    df = order_df.copy()
    df["GuiOrdId"] = df["GuiOrdId"].astype(str).str.strip()
    df["ordSt"] = df["ordSt"].astype(str).str.lower()
    df["exCfmTm"] = pd.to_datetime(df["exCfmTm"],  format="%d-%b-%Y %H:%M:%S", errors="coerce")
    updated_positions = open_positions.copy()
    SLfiltered_orders = df[  df["GuiOrdId"].str.startswith("SL_", na=False) ].copy()
    if SLfiltered_orders.empty:
        logging.info("No old SL orders found.")

    EOD_STRATS = [
    {"name": "CEB", "lot": "ceb_lot"},
    {"name": "C2EB", "lot": "c2eb_lot"},
    {"name": "PEB", "lot": "peb_lot"},
    {"name": "SPE", "lot": "spe_lot"},
    {"name": "RCE", "lot": "rce_lot"},]

    for s in EOD_STRATS:
        if s["name"] in updated_positions:
            logging.warning(f"EOD exit triggered for {s['name']}")
            pos = updated_positions[s["name"]]
            ltp = get_ltp(ctx, pos["tok"])
            buffer = ltp * ctx.limitorder
            exit_side = "S" if pos["trnsTp"] == "B" else "B"
            if exit_side == "B":
                price = round_to_tick(ltp + buffer)
            else:
                price = round_to_tick(ltp - buffer)
            qty = getattr(ctx, s["lot"])
            exit_id = safe_execute(
                place_limit_order,
                ctx,
                trdSym=pos["trdSym"],
                transaction_type=exit_side,
                quantity=qty,
                order_type="L",
                limitPrice=price,
                trigger_price=0,
                productType=ctx.productType, 
                # productType=pos["productType"], do this if you take producttype from broker truth in build_position
                tag=f"EOD_{s['name']}_{time.strftime('%H%M%S')}")
            if exit_id:
                exit_ids.append(exit_id)
                updated_positions.pop(s["name"], None)
                logging.info(f"EOD exit for {s['name']} {pos['trdSym'][-7:]}")

    # hedge logic disabled as we dont use hedges as of now
    # so running a loop for hedges is unnecessary at the moment
    HEDGE_STRATS = [
        # {"name":"SPE_HDG","entry":"SPE_HDG","orph":"ORPH_SPE_HDG","lot":"spe_lot"}, 
        {"name":"RCE_HDG","entry":"RCE_HDG","orph":"ORPH_RCE_HDG","lot":"rce_lot"},
        ]

    for h in HEDGE_STRATS:
        hedge_id = None
        entry_complete = df[
            df["GuiOrdId"].str.startswith(h["entry"], na=False) &
            (df["ordSt"] == "complete")].copy()
        orph_complete = df[
            df["GuiOrdId"].str.startswith(h["orph"], na=False) &
            (df["ordSt"] == "complete")].copy()
        latest_entry = pd.DataFrame()
        latest_orph = pd.DataFrame()
        if not entry_complete.empty:
            entry_complete = entry_complete.sort_values("exCfmTm", ascending=False)
            latest_entry = entry_complete.iloc[[0]]
        if not orph_complete.empty:
            orph_complete = orph_complete.sort_values("exCfmTm", ascending=False)
            latest_orph = orph_complete.iloc[[0]]
        if not latest_entry.empty:
            if latest_orph.empty:
                hedge_id = latest_entry.iloc[0]["nOrdNo"]
            elif latest_orph.iloc[0]["exCfmTm"] < latest_entry.iloc[0]["exCfmTm"]:
                hedge_id = latest_entry.iloc[0]["nOrdNo"]
        if hedge_id is not None and h["name"] in updated_positions:
            logging.warning(f"EOD exit triggered for {h['name']}")
            pos = updated_positions[h["name"]]
            ltp = get_ltp(ctx, pos["tok"])
            buffer = ltp * ctx.limitorder
            exit_side = "S" if pos["trnsTp"] == "B" else "B"
            if exit_side == "B":
                price = round_to_tick(ltp + buffer)
            else:
                price = round_to_tick(ltp - buffer)
            qty = pos.get("qty", getattr(ctx, h["lot"]))
            exit_id = safe_execute(
                place_limit_order,
                ctx,
                trdSym=pos["trdSym"],
                transaction_type=exit_side,
                quantity=qty,
                order_type="L",
                limitPrice=price,
                trigger_price=0,
                productType=ctx.productType,
                tag=f"EOD_{h['name']}_{time.strftime('%H%M%S')}")
            if exit_id:
                exit_ids.append(exit_id)
                updated_positions.pop(h["name"], None)
                logging.info(f"EOD exit for {h['name']} {pos['trdSym'][-7:]}")

    if exit_ids:
        time.sleep(1)
        ob = ctx.client.order_report()
        if ob and ob.get("data"):
            df2 = pd.DataFrame(ob["data"])
            df2["GuiOrdId"] = df2["GuiOrdId"].astype(str).str.strip()
            df2["ordSt"] = df2["ordSt"].astype(str).str.lower()
            df2["exCfmTm"] = pd.to_datetime(df2["exCfmTm"], errors="coerce", dayfirst=True)
            df2["nOrdNo"] = df2["nOrdNo"].astype(str)
            for eid in exit_ids:
                exit_row = df2[df2["nOrdNo"] == str(eid)]
                if not exit_row.empty:
                    safe_execute(log_trade, exit_row)
            return updated_positions, df2   
    return updated_positions, order_df

def final_confirmation(ctx):
    time.sleep(1)
    final_open_positions, _ = eod_build_positions(ctx) 
    # here we dont need that order_df from build_positions
    # thus instead of saying "final_open_positions, order_df = eod_build_positions(ctx)""
    # we are saying "final_open_positions, _ = eod_build_positions(ctx)""

    if final_open_positions:
        logging.warning(f"[EOD] Positions still open after cleanup: {final_open_positions}..\n")
        # safe_execute(
        #     send_telegram_message,
        #     f"[EOD] Cleanup incomplete for {ctx.clientname}\n"
        #     f"Open positions: {list(final_open_positions.keys())}"
        # )
    else:
        logging.info(f"[EOD] Verified: No open positions remain for {ctx.clientname}..\n")