import os
import csv
import pandas as pd
import logging
from datetime import datetime
import re

def init_ledger(log_directory, clientname):
    """
    Initializes and returns the ledger file path for a client.
    Creates the file with headers if it does not exist.
    """

    global ledger

    ledger = os.path.join(
        log_directory,
        f"A_Ledger_for_{clientname}.csv"
    )

    # Ensure Future_Ledger.csv exists with headers at startup
    if not os.path.isfile(ledger):
        with open(ledger, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp", "trdSym", "tok", "expDt", "GuiOrdId",
                "optTp", "fldQty", "trnsTp", "entry_price", "status",
                "exit_price", "exit_time", "pnl", "total_trades", "total_pnl"
            ])

    return ledger

def log_trade(orderdata):

    order = orderdata.iloc[0]

    dtype_map = {
        "timestamp": "object",
        "trdSym": "object",
        "tok": "object",
        "expDt": "object",
        "GuiOrdId": "object",
        "optTp": "object",
        "fldQty": "float64",
        "trnsTp": "object",
        "entry_price": "float64",
        "status": "object",
        "exit_price": "float64",
        "exit_time": "object",
        "pnl": "float64",
        "total_trades": "float64",
        "total_pnl": "float64",
    }

    trades = pd.read_csv(ledger, dtype=dtype_map)
    # trades = pd.read_csv(ledger) # this dtype map was added because of futurewarning

    # Drop summary row if present
    trades = trades[
        pd.isna(trades['total_trades']) &
        pd.isna(trades['total_pnl'])
    ]

    incoming_gui = str(order.get('GuiOrdId', '')).strip()
    trdSym = order.get('trdSym', '').strip()
    expDt = order.get("expDt", "").strip()


    prefix_map = {
    # Main-only strategies
    "CEB": ["SL_CEB", "IND_CEB", "REC_CEB", "EOD_CEB"],
    "C2EB": ["SL_C2EB", "IND_C2EB", "REC_C2EB", "EOD_C2EB"],
    "PEB": ["SL_PEB", "IND_PEB", "REC_PEB", "EOD_PEB"],

    # SCE (main + hedge)
    "SPE": ["SL_SPE", "IND_SPE", "REC_SPE", "EOD_SPE"],
    # SPE_HDG mapping retained for legacy reference — currently unused
    "SPE_HDG": ["ORPH_SPE_HDG", "IND_SPE_HDG", "REC_SPE_HDG", "EOD_SPE_HDG"], # SL_SCE_HDG was earlier called ORPH_SCE_HDG

    # RCE (main + hedge)
    "RCE": ["SL_RCE", "IND_RCE", "REC_RCE", "EOD_RCE"],
    # RCE_HDG mapping retained for legacy reference — currently unused
    "RCE_HDG": ["ORPH_RCE_HDG", "IND_RCE_HDG", "REC_RCE_HDG", "EOD_RCE_HDG"], # SL_RCE_HDG was earlier called ORPH_RCE_HDG
    
    # Expiry
    "EX_CE": ["EX_SLCE"],
    "EX_HCE": ["EX_HCE"],
    "EX_PE": ["EX_SLPE"],
    "EX_HPE": ["EX_HPE"],

    # NHF
    "NHF_CE": ["IND_NHF_CE"],
    "NHF_PE": ["IND_NHF_PE"],
    "NHF_HPE": ["IND_NHF_HPE"],

    # NCL
    "NCL_CE": ["IND_NCL_CE"],
    "NCL_PE": ["IND_NCL_PE"],
    "NCL_HPE": ["IND_NCL_HPE"],

    # NCS
    "NCS_CE": ["IND_NCS_CE", "TGT_NCS_CE"],
    "NCS_PE": ["IND_NCS_PE", "TGT_NCS_PE"],
    "NCS_HPE": ["IND_NCS_HPE", "TGT_NCS_HPE"],

    # N2CS
    "N2CS_CE": ["IND_N2CS_CE", "TGT_N2CS_CE"],
    "N2CS_PE": ["IND_N2CS_PE", "TGT_N2CS_PE"],
    "N2CS_HPE": ["IND_N2CS_HPE", "TGT_N2CS_HPE"],
    
    # GOLD
    "GOLD": ["IND_GOLD", "FC_GOLD"],
    
    # SILVER
    "SILVER": ["IND_SILVER", "FC_SILVER"],

    # CRUDE
    "CRUDE": ["IND_CRUDE", "FC_CRUDE"],    
    }

    matched_exit = False

    matching_entry_prefixes = [
        entry_prefix
        for entry_prefix, exit_prefixes in prefix_map.items()
        if any(incoming_gui.startswith(pfx) for pfx in exit_prefixes)
    ]

    if matching_entry_prefixes:
        for entry_prefix in matching_entry_prefixes:
            possible_entries = trades[
                (trades['status'] == 'ENTRY') &
                (trades['trdSym'].str.strip() == trdSym) &
                (trades['GuiOrdId'].astype(str).str.startswith(entry_prefix))
            ]

            if not possible_entries.empty:
                idx = possible_entries.index[-1]
                exit_price = round(float(order.get('avgPrc', 0)), 2)

                trades.loc[idx, 'status'] = 'EXIT'
                trades.loc[idx, 'exit_price'] = exit_price
                trades.loc[idx, 'exit_time'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                qty = int(trades.loc[idx, 'fldQty'])
                entry_price = float(trades.loc[idx, 'entry_price'])
                transactiontype = trades.loc[idx, 'trnsTp']

                if transactiontype == 'B':
                    pnl = qty * (exit_price - entry_price)
                else:
                    pnl = qty * (entry_price - exit_price)

                trades.loc[idx, 'pnl'] = round(pnl, 2)

                # logging.info(f"EXIT logged for {trdSym} (GuiOrdId={incoming_gui})")
                
                matched_exit = True

    if not matched_exit:
        # ---------------- ENTRY LOGIC ----------------
        new_row = {
                "timestamp": order.get('ordDtTm', ''),
                "trdSym": trdSym,
                "tok":order.get('tok',''),
                "expDt": expDt,
                "GuiOrdId": incoming_gui,
                "optTp": order.get('optTp', ''),
                "fldQty": order.get('fldQty', ''),
                "trnsTp": order.get('trnsTp', ''),
                "entry_price": round(float(order.get('avgPrc', 0)), 2),
                "status": "ENTRY",
                "exit_price": '',
                "exit_time": '',
                "pnl": ''
            }

        # trades = pd.concat([trades, pd.DataFrame([new_row])],ignore_index=True) # removed this line for pandas error which rahul faced on 2 mar

        new_df = pd.DataFrame([new_row])

        if trades.empty:
            trades = new_df.copy()
        else:
            trades = pd.concat([trades, new_df], ignore_index=True)

        # logging.info(f"ENTRY logged for {trdSym} (GuiOrdId={incoming_gui})")

    # Flush logs immediately
    for handler in logging.getLogger().handlers:
        handler.flush()

    # ---------------- SUMMARY ROW ----------------
    total_trades = trades['status'].count()
    total_pnl = trades['pnl'].apply(
        pd.to_numeric, errors='coerce'
    ).sum()

    summary_row = {
            "timestamp": "",
            "trdSym": "",
            "tok": "",
            "expDt": "",
            "GuiOrdId": "",
            "optTp": "",
            "fldQty": "",
            "trnsTp": "",
            "entry_price": "",
            "status": "",
            "exit_price": "",
            "exit_time": "",
            "pnl": "",
            "total_trades": total_trades,
            "total_pnl": round(total_pnl, 2)
        }

    # trades = pd.concat([trades.iloc[:0], summary_row, trades.iloc[0:]], ignore_index=True)

    summary_df = pd.DataFrame([summary_row])

    if trades.empty:
        trades = summary_df.copy()
    else:
        trades = pd.concat([summary_df, trades], ignore_index=True)

    trades.to_csv(ledger, index=False)

def disable_strategy_in_ledger(strategy_tag):

    trades = pd.read_csv(ledger)

    # Remove summary row
    summary = trades[
        ~(pd.isna(trades["total_trades"]) &
          pd.isna(trades["total_pnl"]))
    ]

    mask = (
        trades["GuiOrdId"].astype(str).str.startswith(strategy_tag)
        &
        (trades["status"] == "ENTRY")
    )

    if not mask.any():
        return False

    trades.loc[mask, "status"] = "EXIT"

    # Write back
    trades.to_csv(ledger, index=False)

    logging.info(f"{strategy_tag} manually disabled from dashboard")

    return True