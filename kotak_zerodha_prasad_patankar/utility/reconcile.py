# by default, we create openposition if order with any main tag is found in orderbook
# then we check corresponding SL tag's status

# we don't have to do anything if said status is "trigger pending"
# which mean SL is intact and thus position is intact. 
# as we already have created openposition for the same.

# we have to explicitely make that openposition blank if SL status is completed or cancelled
# status change = main leg is gone
# so if SL status is completed that means premium sl is hit and main leg is already gone. 
# since we create openposition (because we found main tag in orderbook) we have to make it empty now because no valid leg exists


# if on the other hand SL status cancelled that means indicator sl was hit and we ourselves cancelled that SL leg
# in this case, we have to check if indicator exit order timing > premium sl timing
# if that indicator order timing is more than premiumSL order cancellation timing, we make openposition blank
# as we cancel the premium sl first and then go on to place indicator exit order, ind(timing) will always > than sl(timing)

# when code crashes and restarts, if trigger pending>>> no issue we anyways create the openposition
# if SL was cancelled and if no IND order found or OLD IND order found, we anyways create the openposition

# so in all this, the basic idea is, if you find order with entry tag, create the openposition
# and if certain conditions are true then only make those openpositions empty so that no ghost reconciling is done


import os
import pandas as pd
import logging
import time
from datetime import datetime, timedelta
from utility.common_functions import safe_execute, send_telegram_message
from utility.orders import wait_for_order_complete, place_limit_order, round_to_tick, place_short_target, cancel_order
from data.instruments import get_ltp
from utility.ledger import log_trade
from strategy.strategy_gold import gold_exit
from strategy.strategy_silver import silver_exit
from strategy.strategy_crude import crude_exit
from utility.order_counter import get_count, increment_count

SL_DETECT_WINDOW_SECONDS = 181   # 3 min + buffer

def build_positions(ctx):

    client = ctx.client
    open_positions = {}
    order_df = pd.DataFrame()  # ALWAYS define

    STRATS = [
    {"name": "CEB", "main": "CEB", "sl": None, "ind": "IND_CEB", "hedge": False},
    {"name": "C2EB", "main": "C2EB", "sl": None, "ind": "IND_C2EB", "hedge": False},
    {"name": "PEB", "main": "PEB", "sl": None, "ind": "IND_PEB", "hedge": False}, 
    # {"name": "PEB", "main": "PEB", "sl": "SL_PEB", "ind": "IND_PEB", "hedge": False}, # this is for reference, if in future we need to add premium stoploss to any strategy
    # {"name": "SPE_HDG", "main": "SPE_HDG", "sl": "ORPH_SPE_HDG", "ind": None, "hedge": True}, 
    # {"name": "SPE", "main": "SPE", "sl": "SL_SPE", "ind": "IND_SPE", "hedge": False},
    {"name": "SPE", "main": "SPE", "sl": None, "ind": "IND_SPE", "hedge": False},
    {"name": "RCE_HDG", "main": "RCE_HDG", "sl": "ORPH_RCE_HDG", "ind": None, "hedge": True},
    {"name": "RCE", "main": "RCE", "sl": "SL_RCE", "ind": "IND_RCE", "hedge": False},]
    
    # understand here that key is "lowercase letters" and actual tags are "uppercase letters"
    # sl, ind are keys and s[sl] => SL, s[ind] => IND
    # dictionary keys → lowercase
    # constants / tags → uppercase
    
    time.sleep(0.1)
    orderbook = client.order_report()

    if not orderbook or 'data' not in orderbook or not orderbook['data']:
        logging.info("No order data found.")
    else:
        order_df = pd.DataFrame(orderbook.get('data', []))
        order_df["GuiOrdId"] = order_df["GuiOrdId"].astype(str).str.strip() # convert all order tags to string
        order_df["ordSt"] = order_df["ordSt"].astype(str).str.lower() # convert all orderstatus to string
        order_df["exCfmTm"] = pd.to_datetime(order_df["exCfmTm"],  format="%d-%b-%Y %H:%M:%S", errors="coerce") # convert excfmtm to date format
        if order_df.empty:
            logging.info("Order book is empty.")
        else:
            logging.info("Order book loaded successfully.")
                       
        rows = {}
        for s in STRATS:
            if s["hedge"]:
                main_all = order_df[order_df['GuiOrdId'].str.startswith(s["main"], na=False)] # hedge true asnarya orders gola mhanjech SCE_HDG and RCE_HDG
            else:
                main_all = order_df[
                    order_df['GuiOrdId'].str.startswith(s["main"], na=False) &
                    ~order_df['GuiOrdId'].str.startswith(s["main"] + "_HDG", na=False)] # CEB tag wale sagle gola kar, pan tyat HEDGE tag wale gheu nako RCE_HDG asa tag asnarya yenar nahit
            sl_all = order_df[order_df['GuiOrdId'].str.startswith(s["sl"], na=False)]  if s["sl"] else pd.DataFrame() # SL tag wale sagle gola kar
            ind_all = order_df[order_df['GuiOrdId'].str.startswith(s["ind"], na=False)] if s["ind"] else pd.DataFrame() # IND tag wale sagle gola kar,
            row = pd.DataFrame()
            if not main_all.empty: # CEB tag asnari ektari order orderbook sapadli tar:
                main_row = main_all[main_all['nOrdNo'] == main_all['nOrdNo'].max()] # ordebook madhali CEB tag asnari latest row:
                # row = main_row # row_ceb mhanjech latest asnari CEB order
                main_order_no = str(main_row.iloc[0]["nOrdNo"]) # tya order cha order number ghe
                rec_tag = f"REC_{s['name']}_{main_order_no}" # REC tag mhanje REC_CEB_ordernumber
                rec_done = order_df[ (order_df["GuiOrdId"].astype(str).str.strip() == rec_tag) & (order_df["ordSt"].astype(str).str.lower() == "complete") ]
                # jar rec_tag sapadla ani order status complete asel tar rec_done navacha box tayar kar
                if not rec_done.empty: # to box empty nahi mhanjech tyala juna rec_tag sapadla, tar tya row cha vichar karu nako.. ti blank karun tak
                    logging.info( f"{s['name']} already REC-closed | MainOrder={main_order_no}" )
                    row = pd.DataFrame()
                else:
                    row = main_row
                if s["sl"] is None and not ind_all.empty: # tya strategy la stoploss nasel ani IND wali ektari order asel
                    ind_row = ind_all[ind_all['nOrdNo'] == ind_all['nOrdNo'].max()] # tar highest ordernumber wali IND row uchal
                    ind_status = ind_row.iloc[0]['ordSt'] # tya order ch status bagh
                    if ind_status == "complete": # jar te complete asel
                        if ind_row.iloc[0]['exCfmTm'] >= main_row.iloc[0]['exCfmTm']: # tar tyacha time bagh, jar IND row cha time main peksha jasti asel tar ti nantar udali ahe
                            row = pd.DataFrame()  # IND stoploss udalela ahe mhanun ti position blank karun tak               
                elif s["sl"] is not None and not sl_all.empty: # tya strategy la stoploss nasel SL wali ektari order asel
                    sl_row = sl_all[sl_all['nOrdNo'] == sl_all['nOrdNo'].max()] # ordebook madhali SL_CEB tag asnari latest row:
                    sl_status = sl_row.iloc[0]['ordSt'] # SL madhlya pahilya ordercha status bagh
                    if sl_status == "complete": # ata jar te status completed asel tar premium SL udala ahe
                        if sl_row.iloc[0]['exCfmTm'] > main_row.iloc[0]['exCfmTm'] + timedelta(minutes=1): # SL main orderchya kiman 1 minute tari nantar udala ka?
                            row = pd.DataFrame() # SL asa nantar udala mhanje khara SL gela, open_position tayar karaychi garaj nahi
                    elif sl_status == "cancelled" and not ind_all.empty: # ani status cancelled asel tar indicator SL udawtana apan to SL cancel kelay
                        ind_row = ind_all[ind_all['nOrdNo'] == ind_all['nOrdNo'].max()] # ordebook madhali IND_CEB tag asnari latest row:
                        ind_row_status = ind_row.iloc[0]['ordSt'] #indicator walya order cha status bagh
                        if ind_row_status == "complete": # jar to status complete asel tar ch timing check karayla ja
                            if ind_row.iloc[0]['exCfmTm'] >= sl_row.iloc[0]['exCfmTm']: # IND_CEB ch timing SL_CEB peksha jasti asel tar to nantar udalay
                                row = pd.DataFrame() # mhanje regular IND SL gelay, so open position tayar karaychi garaj nahi
            rows[s["name"]] = row
        
        # if parent leg exists, dont consider the hedge legs while building open_positions, so they are not orphaned out accidently
        # but, we commented this section because there are no hedges for any intraday strategies as of now
        
        # MODULE 1- Fresh hedge every trade - if you need hedges taken and cleared with main leg everytime, then, 
        # comment out the 4 lines starting from "if not rows["SPE"].. etc" 
        # and in reconcile function, UNcomment lines starting from "HEDGE_STRATS.." (orphan hedge reconcile block)
        
        # MODULE 2- Persistent hedge for whole day- if you need single hedge survive the whole day, then
        # UNcomment out the "if not rows["SPE"].. etc 4 lines" and in reconcile function, comment out lines 240 to 276 (orphan hedge reconcile block)
        
        # And if you dont want hedge altogether, then make changes in strategy page, use corresponding place order functions
        # and comment out everything related to hedges including in build_positions and reconcile ORPH block

        # if not rows["SPE"].empty: # removed because we dont want hedge for sold leg
        #     rows["SPE_HDG"] = pd.DataFrame() # removed because we dont want hedge for sold leg
        # if not rows["RCE"].empty: # removed this because we dont want to clear the hedges during the day
        #     rows["RCE_HDG"] = pd.DataFrame() # removed this because we dont want to clear the hedges during the day

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
                        # in open position you could add 'productType': order_row.get('prod'),which fetches NRML or MIS from orderbook for that specific order
                        logging.info(
                            f"Running {s['name']} {open_positions[s['name']]['trdSym'][-7:]} "
                            f"at {round(open_positions[s['name']]['avgPrc'],2)}")
                        
                        break
                    
    return open_positions, order_df

def reconcile(ctx, open_positions, order_df):
    # exit_ids = []
    if order_df is None or order_df.empty:
        return open_positions, order_df
    df = order_df.copy()
    df["GuiOrdId"] = df["GuiOrdId"].astype(str).str.strip()
    df["ordSt"] = df["ordSt"].astype(str).str.lower()
    updated_positions = open_positions.copy()
    SLfiltered_orders = df[  df["GuiOrdId"].str.startswith("SL_", na=False) ].copy()
    if SLfiltered_orders.empty:
        logging.info("No old SL orders found.")

    REC_STRATS = [
    # {"name": "CEB", "sl": "SL_CEB", "lot": "ceb_lot"},
    # {"name": "C2EB", "sl": "SL_C2EB", "lot": "c2eb_lot"},
    # {"name": "SPE", "sl": "SL_SPE", "lot": "spe_lot"},
    # {"name": "PEB", "sl": "SL_PEB", "lot": "peb_lot"}, 
    # if we add premium sl to PEB, then for indicator exit, we cancel the premium sl and then place exit of actual position
    # but during crash, we might end up with cancelled premium sl but no actual indicator exit of that position
    # for such scenarios only, we have created the RECONCILE function.
    # so, if we use PSL for PEB, we need to add PEB in REC_STRATS
    {"name": "RCE", "sl": "SL_RCE", "lot": "rce_lot"},]

    for s in REC_STRATS:
        SLorderid = None
        if not SLfiltered_orders.empty:
            sl_pending = SLfiltered_orders[
                SLfiltered_orders["GuiOrdId"].str.startswith(s["sl"], na=False) &
                (SLfiltered_orders["ordSt"] == "trigger pending")]
            if not sl_pending.empty:
                SLorderid = sl_pending["nOrdNo"].iloc[0]
        if SLorderid is None and s["name"] in updated_positions:
            logging.warning(f"REC exit triggered for {s['name']} | No active SL found")
            pos = updated_positions[s["name"]]

            # REC ATTEMPT PROTECTION - Counter belongs to THIS specific original order
            rec_key = f"REC_{s['name']}_{pos['nOrdNo']}"
            if get_count(rec_key, "EXIT") >= 2:
                logging.error( f"REC attempt limit reached | {rec_key}" )
                continue
            increment_count(rec_key, "EXIT")
            
            ltp = get_ltp(ctx, pos["tok"])
            buffer = ltp * ctx.limitorder
            exit_side = "S" if pos["trnsTp"] == "B" else "B"
            if exit_side == "B":
                price = round_to_tick(ltp + buffer)
            else:
                price = round_to_tick(ltp - buffer)
            #qty = getattr(ctx, s["lot"])
            qty = pos.get("qty", getattr(ctx, s["lot"]))
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
                # tag=f"REC_{s['name']}_{int(time.time())}")
                # tag=f"REC_{s['name']}_{pos['nOrdNo']}")
                tag=rec_key)
            if exit_id:
                row = wait_for_order_complete(ctx, exit_id)
                if row is not None:
                    safe_execute(log_trade, pd.DataFrame([row]))
                    updated_positions.pop(s["name"], None)
                    logging.info(f"REC exit SUCCESS {s['name']} {pos['trdSym'][-7:]}")
                else:
                    logging.error(f"REC exit FAILED {s['name']} — will retry next cycle")
    
    # ORHAN HEDGE RECONCILE BLOCK
    # comment out if you need persistent hedge for whole day
    HEDGE_STRATS = [
        # {"parent": "SPE", "hedge": "SPE_HDG", "lot": "spe_lot"},
        {"parent": "RCE", "hedge": "RCE_HDG", "lot": "rce_lot"},]
    for h in HEDGE_STRATS:
        if h["parent"] not in updated_positions and h["hedge"] in updated_positions:
            logging.warning(f"ORPH exit triggered for {h['hedge']} | No parent found")
            pos = updated_positions[h["hedge"]]
            ltp = get_ltp(ctx, pos["tok"])
            buffer = ltp * ctx.limitorder
            exit_side = "S" if pos["trnsTp"] == "B" else "B"
            if exit_side == "B":
                price = round_to_tick(ltp + buffer)
            else:
                price = round_to_tick(ltp - buffer)
            # qty = getattr(ctx, h["lot"])
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
                tag=f"ORPH_{h['hedge']}_{int(time.time())}")
            if exit_id:
                row = wait_for_order_complete(ctx, exit_id)
                if row is not None:
                    safe_execute(log_trade, pd.DataFrame([row]))
                    updated_positions.pop(h["hedge"], None)
                    logging.warning(f"ORPH exit SUCCESS {h['hedge']} {pos['trdSym'][-7:]}")
                else:
                    logging.error(f"ORPH exit FAILED {h['hedge']} — will retry next cycle")
   
    # If no exits happened → return original snapshot
    return updated_positions, order_df

def detect_completed_sl(order_df):
    completed_sl_rows = []
    seen_sl_orders = set()
    if order_df is None or order_df.empty:
        return completed_sl_rows
    df = order_df.copy()
    # df["GuiOrdId"] = df["GuiOrdId"].astype(str).str.strip()
    # df["ordSt"] = df["ordSt"].astype(str).str.lower()
    # df["exCfmTm"] = pd.to_datetime(df["exCfmTm"], errors="coerce")
    now = datetime.now()
    cutoff = now - timedelta(seconds=SL_DETECT_WINDOW_SECONDS)
    sl_df = df[df["GuiOrdId"].str.startswith("SL_", na=False) 
               & (df["ordSt"] == "complete")
               & (df["exCfmTm"] >= cutoff)]
    if sl_df.empty:
        return completed_sl_rows
    for _, row in sl_df.iterrows():
        
        if row["nOrdNo"] in seen_sl_orders: # done this to avoid repeated ledgering of same order if it comes second time
            continue # done this to avoid repeated ledgering of same order if it comes second time
        seen_sl_orders.add(row["nOrdNo"]) # done this to avoid repeated ledgering of same order if it comes second time
        
        logging.info(f"RECENT SL EXECUTED | {row['trdSym']}")
        completed_sl_rows.append(row)
    return completed_sl_rows

def update_ledger_for_sl_exits(completed_sl_rows):
    if not completed_sl_rows:
        return
    for row in completed_sl_rows:
        logging.info(f"Broker SL executed hence logging {row['GuiOrdId']}")
        safe_execute(log_trade, row.to_frame().T)



def nhf_build_positions(ctx):
    """
    NHF Build Positions from ledger.
    NHF is open if all three legs exist with status == ENTRY:
        - NHF_CE  (Main CE Buy)
        - NHF_PE  (Main PE Sell)
        - NHF_HPE (Hedge PE Buy)
    """
    ledger = ctx.ledger_path 

    if not os.path.isfile(ledger):
        logging.info("NHF Build Positions: Ledger file not found.")
        return None

    trades = pd.read_csv(ledger)

    # Drop summary row
    trades = trades[
        pd.isna(trades['total_trades']) &
        pd.isna(trades['total_pnl'])
    ]

     # Directly filter only NHF + ENTRY rows
    nhf_df = trades[
        (trades["status"] == "ENTRY") &
        (
            trades["GuiOrdId"].astype(str).str.startswith("NHF_CE") |
            # trades["GuiOrdId"].astype(str).str.startswith("NHF_HPE") |
            trades["GuiOrdId"].astype(str).str.startswith("NHF_PE")
        )
    ]

    if nhf_df.empty:
        logging.info("NHF Build Positions: No NHF entries found.")
        return None

    nhf_open_positions = {
        "NHF_CE": None,
        # "NHF_HPE": None,
        "NHF_PE": None
    }

    for _, row in nhf_df.iterrows():
        gui = str(row["GuiOrdId"]).strip()

        if gui.startswith("NHF_CE"):
            nhf_open_positions["NHF_CE"] = row
        # elif gui.startswith("NHF_HPE"):
        #     nhf_open_positions["NHF_HPE"] = row
        elif gui.startswith("NHF_PE"):
            nhf_open_positions["NHF_PE"] = row

    # Check if full NHF structure exists
    if any(v is None for v in nhf_open_positions.values()):
        logging.info("NHF Build Positions: No complete NHF position found in ledger.")
        return None

    logging.info(
        f"NHF OPEN detected | "
        f"CE {nhf_open_positions['NHF_CE']['trdSym'][-7:]} | "
        f"PE {nhf_open_positions['NHF_PE']['trdSym'][-7:]} | "
        # f"HEDGE {nhf_open_positions['NHF_HPE']['trdSym'][-7:]}"
    )

    return nhf_open_positions

def ncl_build_positions(ctx):
    """
    NCL Build Positions from ledger.
    NCL is open if all three legs exist with status == ENTRY:
        - NCL_CE  (Main CE Buy)
        - NCL_PE  (Main PE Sell)
        - NCL_HPE (Hedge PE Buy)
    """
    ledger = ctx.ledger_path 

    if not os.path.isfile(ledger):
        logging.info("NCL Build Positions: Ledger file not found.")
        return None

    trades = pd.read_csv(ledger)

    # Drop summary row
    trades = trades[
        pd.isna(trades['total_trades']) &
        pd.isna(trades['total_pnl'])
    ]

     # Directly filter only NHF + ENTRY rows
    ncl_df = trades[
        (trades["status"] == "ENTRY") &
        (
            trades["GuiOrdId"].astype(str).str.startswith("NCL_CE") |
            # trades["GuiOrdId"].astype(str).str.startswith("NCL_HPE") |
            trades["GuiOrdId"].astype(str).str.startswith("NCL_PE")
        )
    ]

    if ncl_df.empty:
        logging.info("NCL Build Positions: No NCL entries found.")
        return None

    ncl_open_positions = {
        "NCL_CE": None,
        # "NCL_HPE": None,
        "NCL_PE": None
    }

    for _, row in ncl_df.iterrows():
        gui = str(row["GuiOrdId"]).strip()

        if gui.startswith("NCL_CE"):
            ncl_open_positions["NCL_CE"] = row
        # elif gui.startswith("NCL_HPE"):
        #     ncl_open_positions["NCL_HPE"] = row
        elif gui.startswith("NCL_PE"):
            ncl_open_positions["NCL_PE"] = row

    # Check if full NHF structure exists
    if any(v is None for v in ncl_open_positions.values()):
        logging.info("NCL Build Positions: No complete NCL position found in ledger.")
        return None

    logging.info(
        f"NCL OPEN detected | "
        f"CE {ncl_open_positions['NCL_CE']['trdSym'][-7:]} | "
        f"PE {ncl_open_positions['NCL_PE']['trdSym'][-7:]} | "
        # f"HEDGE {nhf_open_positions['NHF_HPE']['trdSym'][-7:]}"
    )

    return ncl_open_positions

def ncs_build_positions(ctx):
    """
    NCS Build Positions from ledger.
    NCS is open if all three legs exist with status == ENTRY:
        - NCS_CE  (Main CE Sell)
    """
    ledger = ctx.ledger_path 

    if not os.path.isfile(ledger):
        logging.info("NCS Build Positions: Ledger file not found.")
        return None

    trades = pd.read_csv(ledger)

    # Drop summary row
    trades = trades[
        pd.isna(trades['total_trades']) &
        pd.isna(trades['total_pnl'])
    ]

     # Directly filter only NHF + ENTRY rows
    ncs_df = trades[
        (trades["status"] == "ENTRY") &
        (
            trades["GuiOrdId"].astype(str).str.startswith("NCS_CE")
        )
    ]

    if ncs_df.empty:
        logging.debug("NCS Build Positions: No NCS entries found.")
        return None

    ncs_open_positions = {
        "NCS_CE": None
    }

    for _, row in ncs_df.iterrows():
        gui = str(row["GuiOrdId"]).strip()

        if gui.startswith("NCS_CE"):
            ncs_open_positions["NCS_CE"] = row

    # Check if full NHF structure exists
    if any(v is None for v in ncs_open_positions.values()):
        logging.info("NCS Build Positions: No complete NCS position found in ledger.")
        return None

    logging.info(
        f"NCS OPEN detected | CE {ncs_open_positions['NCS_CE']['trdSym'][-7:]} | "

        # f"HEDGE {nhf_open_positions['NHFPEH']['trdSym'][-7:]}"
    )

    return ncs_open_positions

def n2cs_build_positions(ctx):
    """
    N2CS Build Positions from ledger.
    N2CS is open if all three legs exist with status == ENTRY:
        - N2CS_CE  (Main CE Sell)
    """
    ledger = ctx.ledger_path 

    if not os.path.isfile(ledger):
        logging.info("N2CS Build Positions: Ledger file not found.")
        return None

    trades = pd.read_csv(ledger)

    # Drop summary row
    trades = trades[
        pd.isna(trades['total_trades']) &
        pd.isna(trades['total_pnl'])
    ]

     # Directly filter only NHF + ENTRY rows
    n2cs_df = trades[
        (trades["status"] == "ENTRY") &
        (
            trades["GuiOrdId"].astype(str).str.startswith("N2CS_CE")
        )
    ]

    if n2cs_df.empty:
        logging.debug("N2CS Build Positions: No N2CS entries found.")
        return None

    n2cs_open_positions = {
        "N2CS_CE": None
    }

    for _, row in n2cs_df.iterrows():
        gui = str(row["GuiOrdId"]).strip()

        if gui.startswith("N2CS_CE"):
            n2cs_open_positions["N2CS_CE"] = row

    # Check if full NHF structure exists
    if any(v is None for v in n2cs_open_positions.values()):
        logging.info("N2CS Build Positions: No complete NCS position found in ledger.")
        return None

    logging.info(
        f"N2CS OPEN detected | CE {n2cs_open_positions['N2CS_CE']['trdSym'][-7:]} | "

        # f"HEDGE {nhf_open_positions['NHFPEH']['trdSym'][-7:]}"
    )

    return n2cs_open_positions



OVERNIGHT_TARGETS = {
    "NCS_CE": { "target_prefix": "TGT_NCS_CE", "target_decay": 150, "build_fn": ncs_build_positions, "position_key": "NCS_CE" }, #"target_pct": 0.50,
    "N2CS_CE": { "target_prefix": "TGT_N2CS_CE", "target_decay": 75, "build_fn": n2cs_build_positions, "position_key": "N2CS_CE" }
    }

ACTIVE_TARGET_STATES = { "open", "trigger pending", "partial" }

def overnight_reconcile(ctx, order_df):

    if order_df is None or order_df.empty:
        return
    for _, cfg in OVERNIGHT_TARGETS.items():
        positions = cfg["build_fn"](ctx)
        if positions is None:
            continue
        position_row = positions.get(cfg["position_key"])
        if position_row is None:
            continue
        reconcile_target(
            ctx,
            position_row,
            cfg["target_prefix"],
            cfg["target_decay"],
            order_df
        )

def reconcile_target( ctx, position_row, target_prefix, target_decay, order_df ):

    if position_row is None:
        return

    entry_time = pd.to_datetime( position_row["timestamp"], format="%d-%b-%Y %H:%M:%S", errors="coerce")
    
    target_orders = order_df[
        (order_df["trdSym"].astype(str).str.strip()
            == str(position_row["trdSym"]).strip())
        &
        (order_df["GuiOrdId"].astype(str)
            .str.startswith(target_prefix, na=False))
    ]

    # Only consider targets belonging to this trade
    target_orders = target_orders[ target_orders["exCfmTm"] >= entry_time ]

    # -------------------------------------------------
    # CASE 1 : TARGET FILLED
    # -------------------------------------------------
    
    completed = target_orders[ target_orders["ordSt"] == "complete" ]
    # completed = target_orders[ (target_orders["ordSt"] == "complete") & (target_orders["exCfmTm"] >= entry_time) ]
    
    if not completed.empty:
        target_row = completed.loc[ completed["nOrdNo"].idxmax() ]
        logging.info( f"TARGET FILLED | " f"{target_row['trdSym'][-7:]}" )
        safe_execute( log_trade, target_row.to_frame().T )
        return

    # -------------------------------------------------
    # CASE 2 : TARGET ACTIVE
    # -------------------------------------------------
    
    active = target_orders[ target_orders["ordSt"].isin( ACTIVE_TARGET_STATES ) ]
    # active = target_orders[ (target_orders["ordSt"].isin(ACTIVE_TARGET_STATES)) & (target_orders["exCfmTm"] >= entry_time) ]
    if not active.empty:
        return

    # -------------------------------------------------
    # CASE 3 : TARGET MISSING
    # -------------------------------------------------
    logging.warning(f"TARGET MISSING | " f"{position_row['trdSym'][-7:]}" )
    ensure_target_exists( ctx, position_row, target_prefix, target_decay, order_df )

def ensure_target_exists( ctx, position_row, target_prefix, target_decay, order_df):

    if position_row is None:
        return None
    
    if order_df is None or order_df.empty:
        active_target = pd.DataFrame()
    else:
        active_target = order_df[ 
            (order_df["trdSym"].astype(str).str.strip() == str(position_row["trdSym"]).strip()) 
            & 
            (order_df["GuiOrdId"].astype(str) .str.startswith(target_prefix, na=False)) 
            & 
            (order_df["ordSt"].astype(str).str.lower() .isin(ACTIVE_TARGET_STATES)) ]

    if not active_target.empty:
        return None

    entry_price = float(position_row["entry_price"])

    target_price = round_to_tick( max(0.05, entry_price - target_decay) )

    target_order_id = safe_execute(
        place_short_target,
        ctx,
        trdSym=position_row["trdSym"],
        quantity=int(position_row["fldQty"]),
        target_price=target_price,
        productType="NRML",
        tag=f"{target_prefix}_{int(time.time())}" )

    if target_order_id:
        logging.info( f"TARGET ARMED|" f"{position_row['trdSym'][-7:]} | " f"Entry={entry_price} | " f"Target={target_price}" )

    return target_order_id

def get_order_df(ctx):

    time.sleep(1)

    orderbook = ctx.client.order_report()

    if not orderbook or not orderbook.get("data"):
        return pd.DataFrame()

    order_df = pd.DataFrame(orderbook["data"])

    order_df["GuiOrdId"] = ( order_df["GuiOrdId"] .astype(str) .str.strip() )

    order_df["ordSt"] = ( order_df["ordSt"] .astype(str) .str.lower() )

    return order_df

def cancel_target( ctx, position_row, target_prefix, order_df ):

    if position_row is None:
        return

    if order_df is None or order_df.empty:
        active_target = pd.DataFrame()
    else:
        active_target = order_df[
            (order_df["trdSym"].astype(str).str.strip()
                == str(position_row["trdSym"]).strip())
            &
            (order_df["GuiOrdId"].astype(str)
                .str.startswith(target_prefix, na=False))
            &
            (order_df["ordSt"].astype(str).str.lower()
                .isin(ACTIVE_TARGET_STATES))
    ]
        
    if active_target.empty:
        return

    target_row = active_target.iloc[-1]

    order_no = str(target_row["nOrdNo"])

    res = safe_execute( cancel_order, ctx, order_no )

    if res:
        logging.info( f"TARGET CANCELLED | {position_row['trdSym'][-7:]} | Order={order_no}" )


def gold_build_positions(ctx):
    """
    GOLD Build Positions from ledger.
    """
    ledger = ctx.ledger_path 

    if not os.path.isfile(ledger):
        logging.info("GOLD Build Positions: Ledger file not found.")
        return None

    trades = pd.read_csv(ledger)

    # Drop summary row
    trades = trades[ pd.isna(trades['total_trades']) & pd.isna(trades['total_pnl'])]

     # Directly filter only NHF + ENTRY rows
    gold_df = trades[ (trades["status"] == "ENTRY") &  ( trades["GuiOrdId"].astype(str).str.startswith("GOLD") ) ]

    if gold_df.empty:
        logging.debug("GOLD Build Positions: No GOLD entries found.")
        return None

    gold_open_positions = { "GOLD": None }

    for _, row in gold_df.iterrows():
        gui = str(row["GuiOrdId"]).strip()

        if gui.startswith("GOLD"):
            gold_open_positions["GOLD"] = row

    # Check if full NHF structure exists
    if any(v is None for v in gold_open_positions.values()):
        logging.info("GOLD Build Positions: No complete GOLD position found in ledger.")
        return None

    logging.info( f"GOLD OPEN detected | {gold_open_positions['GOLD']['trdSym'][-7:]} | " )

    return gold_open_positions

def silver_build_positions(ctx):
    """
    SILVER Build Positions from ledger.
    """
    ledger = ctx.ledger_path 

    if not os.path.isfile(ledger):
        logging.info("SILVER Build Positions: Ledger file not found.")
        return None

    trades = pd.read_csv(ledger)

    # Drop summary row
    trades = trades[ pd.isna(trades['total_trades']) & pd.isna(trades['total_pnl'])]

     # Directly filter only NHF + ENTRY rows
    silver_df = trades[ (trades["status"] == "ENTRY") &  ( trades["GuiOrdId"].astype(str).str.startswith("SILVER") ) ]

    if silver_df.empty:
        logging.debug("SILVER Build Positions: No SILVER entries found.")
        return None

    silver_open_positions = { "SILVER": None }

    for _, row in silver_df.iterrows():
        gui = str(row["GuiOrdId"]).strip()

        if gui.startswith("SILVER"):
            silver_open_positions["SILVER"] = row

    # Check if full NHF structure exists
    if any(v is None for v in silver_open_positions.values()):
        logging.info("SILVER Build Positions: No complete SILVER position found in ledger.")
        return None

    logging.info( f"SILVER OPEN detected | {silver_open_positions['SILVER']['trdSym'][-7:]} | " )

    return silver_open_positions

def crude_build_positions(ctx):
    """
    CRUDE Build Positions from ledger.
    """
    ledger = ctx.ledger_path 

    if not os.path.isfile(ledger):
        logging.info("CRUDE Build Positions: Ledger file not found.")
        return None

    trades = pd.read_csv(ledger)

    # Drop summary row
    trades = trades[ pd.isna(trades['total_trades']) & pd.isna(trades['total_pnl'])]

     # Directly filter only NHF + ENTRY rows
    crude_df = trades[ (trades["status"] == "ENTRY") &  ( trades["GuiOrdId"].astype(str).str.startswith("CRUDE") ) ]

    if crude_df.empty:
        logging.debug("CRUDE Build Positions: No CRUDE entries found.")
        return None

    crude_open_positions = { "CRUDE": None }

    for _, row in crude_df.iterrows():
        gui = str(row["GuiOrdId"]).strip()

        if gui.startswith("CRUDE"):
            crude_open_positions["CRUDE"] = row

    # Check if full NHF structure exists
    if any(v is None for v in crude_open_positions.values()):
        logging.info("CRUDE Build Positions: No complete CRUDE position found in ledger.")
        return None

    logging.info( f"CRUDE OPEN detected | {crude_open_positions['CRUDE']['trdSym'][-7:]} | " )

    return crude_open_positions

def force_close_cmdty(ctx):
    today_date = datetime.now().date()
    commodities = [
        ("GOLD", gold_build_positions, gold_exit),
        ("SILVER", silver_build_positions, silver_exit),
        ("CRUDE", crude_build_positions, crude_exit),
    ]
    for name, build_fn, exit_fn in commodities:
        open_positions = build_fn(ctx)
        if not open_positions:
            continue
        position = open_positions.get(name)
        if position is None:
            continue
        expiry = pd.to_datetime(position["expDt"]).date()
        days = (expiry - today_date).days
        if days <= 7:
            logging.info( f"{name} contract expires in {days} days. Force closing commodity position." )
            exit_fn(ctx, open_positions)

    # EXPLAINATION
    # gold_open_positions = gold_build_positions(ctx)
    # if gold_open_positions:
    #     days = (gold_open_positions['expDt'] - today_date).days
    #     if days <= 7:
    #         logging.info( f"GOLD expiry is {days} days away. Force closing commodity position." )
    #         gold_exit(ctx, gold_open_positions)