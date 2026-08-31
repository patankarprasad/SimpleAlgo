######################## add the following in user_selection ########################
# CEB_ENTRIES_ALLOWED,5,
# CEB_ENTRIES_ALLOWED,5,

######################## add the following in config.py ########################
# ctx.ceb_entries_allowed = user_cfg.get("CEB_ENTRIES_ALLOWED", 5)
# ctx.ceb_exits_allowed = user_cfg.get("CEB_ENTRIES_ALLOWED", 5)

######################## add the following in context.py ########################
# self.ceb_entries_allowed = None
# self.ceb_exits_allowed = None

######################## add the following in strategy_ce ########################
# from utility.reconcile import build_positions
# open_positions, _ = build_positions(ctx)

######################## add this before indicator entry in main ########################
# if get_count("CEB","ENTRY") < ctx.ceb_entries_allowed:
#     increment_count("CEB","ENTRY")

######################## add this before indicator exit in main ########################
# if get_count("CEB","EXIT") < ctx.ceb_exits_allowed:
#     increment_count("CEB","EXIT")


import os
import pandas as pd
from datetime import datetime

counter = None
counter_file = None


STRATEGIES = [
    "CEB",
    "C2EB",
    "PEB",
    "SPE",
    "RCE",
    "NCL",
    "NCS",
    "N2CS",
    "NHF",
    "GOLD",
    "SILVER",
    "CRUDE"
    ]


def init_order_counter(log_directory):
    global counter
    global counter_file
    today = datetime.now().strftime("%Y%m%d")
    counter_file = os.path.join( log_directory, f"OrderCounter_{today}.csv" )
    if os.path.isfile(counter_file):
        counter = pd.read_csv(counter_file)
    else:
        counter = pd.DataFrame({ "Strategy": STRATEGIES, "EntryCalls": [0] * len(STRATEGIES), "ExitCalls": [0] * len(STRATEGIES) })
        counter.to_csv(counter_file, index=False)

def get_count(strategy, call_type):
    column = "EntryCalls" if call_type == "ENTRY" else "ExitCalls"
    row = counter.loc[ counter["Strategy"] == strategy, column ]
    if row.empty:
        raise ValueError(f"Unknown strategy : {strategy}")
    return int(row.iloc[0])

def increment_count(strategy, call_type):
    column = "EntryCalls" if call_type == "ENTRY" else "ExitCalls"
    idx = counter.index[ counter["Strategy"] == strategy ]
    if len(idx) == 0:
        raise ValueError(f"Unknown strategy : {strategy}")
    counter.at[idx[0], column] += 1
    counter.to_csv(counter_file, index=False)