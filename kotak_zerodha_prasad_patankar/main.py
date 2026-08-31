import time
from datetime import date, time as dt_time, datetime, timedelta
import os
import sys
import logging
import pandas as pd
# →
from utility.common_functions import kotak_login, zerodha_login, send_telegram_message, safe_execute
from utility.context import Context
from utility.ledger import init_ledger

from data.instruments import load_kotak_scrip_master, load_zerodha_scrip_master
from utility.config import apply_login_config, apply_config, load_user_selection, reload_config_if_changed
from data.candles import get_candles
from data.indicators import calculate_pivots_from_candles
from data.get_indicator_values import get_indicator_values
from utility.eod_cleanup import eod_cleanup
from utility.reconcile import (build_positions, reconcile, detect_completed_sl, 
                               update_ledger_for_sl_exits, nhf_build_positions, 
                               ncl_build_positions, ncs_build_positions, n2cs_build_positions,
                               overnight_reconcile, get_order_df, ensure_target_exists, OVERNIGHT_TARGETS,
                               gold_build_positions, silver_build_positions, crude_build_positions, force_close_cmdty)

from strategy.strategy_ce import ceb_entry, ceb_exit
from strategy.strategy_c2e import c2eb_entry, c2eb_exit
from strategy.strategy_pe_without_psl import peb_entry, peb_exit
# from strategy.strategy_pe_without_psl import peb_entry, peb_exit
from strategy.strategy_spe_without_psl import spe_entry, spe_exit
from strategy.strategy_rce import rce_entry, rce_exit, rce_entry_without_hedge
# from strategy.strategy_exd_with_hedges import run_expiry_strangle, exd_ledgering

from strategy.strategy_nhf_without_hedge import nhf_entry, nhf_exit, rollover_nhf
from strategy.strategy_ncl_without_hedge import ncl_entry, ncl_exit, rollover_ncl
from strategy.strategy_ncs_monthend_cesell_no_rollover import ncs_entry, ncs_exit, ncs_expiry_clearing
from strategy.strategy_n2cs_weekly_cesell_no_rollover import n2cs_entry, n2cs_exit, n2cs_expiry_clearing

from strategy.strategy_gold import gold_entry, gold_exit
from strategy.strategy_silver import silver_entry, silver_exit
from strategy.strategy_crude import crude_entry, crude_exit

import utility.dashboard as dashboard
import threading

from utility.order_counter import init_order_counter, get_count, increment_count

# ================= CREDENTIALS =================
from user.credentials import CLIENT_NAME

today_date = datetime.now().date()

if today_date <= date(2030, 12, 31): 

    ctx = Context()
    ctx.clientname = CLIENT_NAME

    if getattr(sys, 'frozen', False):
        base_dir = os.path.dirname(sys.executable)
    else:
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
        except NameError:
            # __file__ is not defined in interactive environments like Jupyter
            base_dir = os.getcwd()

    def load_env_file():
        env_path = os.path.join(base_dir, ".env")
        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, val = line.split("=", 1)
                        os.environ[key.strip()] = val.strip()

    # Call the helper
    load_env_file()

    dashboard.USERNAME = ctx.clientname
    # dashboard.PASSWORD = f"{ctx.clientname}_1757051"
    dashboard.PASSWORD = os.getenv("DASHBOARD_PASS")#, f"{ctx.clientname}_1757051")
    dashboard.CLIENT_NAME = ctx.clientname

    #####################################################################################################
    #################### CREATE LOG FILE ####################
    #####################################################################################################

    log_directory = os.path.join(base_dir, "logs")
    os.makedirs(log_directory, exist_ok=True)
    dashboard.LOG_DIR = log_directory
    dashboard.CONFIG_PATH = os.path.join(base_dir, "user_selection.csv")

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_filename = os.path.join(log_directory, f"Logfile_{timestamp}.log")

     # Clear old handlers before configuring
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[logging.FileHandler(log_filename),logging.StreamHandler()])

    # ================= LEDGER INITIALIZATION =================
    ctx.ledger_path = init_ledger(log_directory, CLIENT_NAME)
    user_cfg = load_user_selection(base_dir)
    # ================= LOGIN =================
    
    apply_login_config(ctx, user_cfg)
    # angel_login(ctx)
    kotak_login(ctx)   
    zerodha_login(ctx)
    
    # ================= SCRIPMASTER =================
    load_kotak_scrip_master(ctx)
    load_zerodha_scrip_master(ctx)
    #load_angel_scrip_master(ctx)

    # ================= CONFIG =================
    apply_config(ctx, user_cfg)

    init_order_counter(log_directory)

    def start_dashboard():
        # dashboard.app.run(host="0.0.0.0", port=7070, debug=False, use_reloader=False)
        dashboard.app.run(host="127.0.0.1", port=7070, debug=False, use_reloader=False)

    dashboard_thread = threading.Thread(target=start_dashboard)
    dashboard_thread.daemon = True
    dashboard_thread.start()

    #####################################################################################################
    #################### MAIN LOOP ####################
    #####################################################################################################

    market_open = datetime.now().replace(hour=9, minute=15, second=30, microsecond=0) # 09:15:30
    phase1_exit_time = datetime.now().replace(hour=15, minute=30, second=50, microsecond=0) #market_open + timedelta(hours=6, minutes=14, seconds=30) # 15:30:00
    phase2_exit_time = datetime.now().replace(hour=23, minute=16, second=0, microsecond=0)
    intraday_entry_cutoff = datetime.now().replace(hour=15, minute=13, second=0, microsecond=0) #market_open + timedelta(hours=5, minutes=57, seconds=30) # 15:13:00

    exd_start = market_open + timedelta(minutes=3, seconds=40) # 9:19:40
    exd_end   = market_open + timedelta(minutes=6, seconds=10) # 9:21:40

    R1 = None
    S1 = None
    pivots = None
    spe_flag = "Down"
    # this flag thing was added when PEB had premium stoploss, 
    # when PSL hits, we did not want another entry till indicator exit was confirmed atleast once before fresh entry
    # otherwise PSL kept getting hit, and code kept on making fresh entries repeatedly, because entry conditions were true.
    # since we have completely removed PSL from PEB, we no longer need this PEB, but kept as a references
    expiry_executed = False
    last_run_hour = None

    # safe_execute(send_telegram_message, f"Login Successfull for {CLIENT_NAME}.")

    while True:
        now = datetime.now()
        reload_config_if_changed(ctx, base_dir)
        # now gives 2026-03-17 15:42:18.734921, now.year gives 2026, now.month gives 3, now.day gives 17, now.hour gives 15, now.minute gives 42, now.second gives 18

        if now < datetime.now().replace(hour=9, minute=14, second=30, microsecond=0): # 09:15:30
            force_close_cmdty(ctx)

        # ================= MARKET CLOSE =================
        if now >= phase1_exit_time:
            break
        # ================= MARKET OPEN CHECK =================
        if now < market_open:
            logging.info("Waiting for market open..\n")
            time.sleep(10)
            continue
        # ================= PIVOTS (RUN ONCE) =================
        if R1 is None and S1 is None:
            df_3m = get_candles(ctx=ctx, instrument_token=256265, interval="3minute", days=7, name="3min")
            df_day = get_candles( ctx=ctx, instrument_token=256265, interval="day", days=7, name="daily")
            if df_3m is not None and df_day is not None:
                pivots = calculate_pivots_from_candles(df_day, df_3m)
            else:
                logging.warning("Pivot calculation skipped due to missing candle data")
            if pivots is not None:
                R1 = pivots["R1"]
                S1 = pivots["S1"]
                logging.info(f"R1 = {R1},S1 = {S1}")
                
                # ================= ARM OVERNIGHT TARGETS =================
                order_df = get_order_df(ctx)
                for _, cfg in OVERNIGHT_TARGETS.items():
                    positions = cfg["build_fn"](ctx)
                    if positions is None:
                        continue
                    position_row = positions.get(cfg["position_key"])
                    if position_row is None:
                        continue
                    safe_execute( ensure_target_exists, ctx, position_row,
                                 cfg["target_prefix"], cfg["target_decay"], order_df)
            
            else:
                time.sleep(60)
                continue      

        # ================= EXD (EVENT WINDOW) =================
        # if ctx.strategy_exd == True and not expiry_executed:
        #     now = datetime.now()
        #     if exd_start <= now < exd_end:
        #         logging.info("Running EXD")
        #         run_expiry_strangle(ctx)
        #         expiry_executed = True
        #     elif now >= exd_end:
        #             expiry_executed = True   # Disabled for the day       

        # ================= IOB (3-MIN SCHEDULER) =================
       
        if now.minute % 3 == 0 and now >= market_open + timedelta(minutes=2):
            logging.info(f"3m scheduler fired")
            # ================= RECONCILE BLOCK =================          
            open_positions, order_df = build_positions(ctx)
            open_positions, order_df = reconcile(ctx, open_positions, order_df)
            
            completed_sl = detect_completed_sl(order_df)
            update_ledger_for_sl_exits(completed_sl)
            safe_execute( overnight_reconcile, ctx, order_df )
            
            
            
            time.sleep(2)  # broker candle publish buffer
            logging.info("From main: call to fetch 3 min candle data...")

            # # OLD LOGIC
            # candledf3m = get_candles(ctx=ctx, instrument_token=256265, interval="3minute", days=7, name="3min")
            # logging.info( f"NOW={datetime.now()} | " f"3M_LAST={candledf3m.iloc[-1]['timestamp']} | " f"3M_PREV={candledf3m.iloc[-2]['timestamp']}" )
            
            # # NEW LOGIC
            expected_timestamp = now.replace(second=0, microsecond=0) # Calculate timestamp we expect e.g., if it's 12:42:04, we expect a candle for 12:42:00
            max_candle_retries = 5 # Poll until the broker publishes the expected candle
            candledf3m = None
            latest_candle_time = None
            for attempt in range(1, max_candle_retries + 1):
                candledf3m = get_candles(ctx=ctx, instrument_token=256265, interval="3minute", days=7, name="3min")
                if candledf3m is not None and not candledf3m.empty:
                    # latest_candle_time = candledf3m.iloc[-1]['timestamp']
                    latest_candle_time = pd.to_datetime( candledf3m.iloc[-1]['timestamp'] ).tz_localize(None) 
                    if latest_candle_time >= expected_timestamp: # If the latest candle is newer than expected, break the loop
                        break 
                    logging.warning(f"Attempt {attempt}/{max_candle_retries}: Expected is {expected_timestamp} while Last is {latest_candle_time}. Retrying in 2s...")
                else:
                    logging.warning(f"Attempt {attempt}/{max_candle_retries}: DataFrame empty or None. Retrying in 2s...")
                time.sleep(1) # Wait 2 seconds before requesting again
            if latest_candle_time is None or latest_candle_time < expected_timestamp:
                logging.error(f"Expected candle {expected_timestamp} not available after {max_candle_retries} retries. Skipping this cycle." )
                candledf3m = None
            if candledf3m is not None and len(candledf3m) >= 2:
                logging.info(f"NOW={datetime.now()} | 3M_LAST={candledf3m.iloc[-1]['timestamp']} | 3M_PREV={candledf3m.iloc[-2]['timestamp']}")
            else:
                logging.error("Failed to fetch valid candle data after all retries.")
            
            if candledf3m is not None:
                ts3m = pd.to_datetime(candledf3m.iloc[-1]['timestamp'])
                if ts3m.minute % 3 == 0:# and now.second >= 3: # this seconds >=3 is added to remove the error of access rate
                    logging.info(f"Checking for IOB entries or exits")
                    ind_val = get_indicator_values(candledf3m)
                    # ----- CEB -----
                    if ctx.strategy_ceb == True:
                        if (now < intraday_entry_cutoff 
                            and ind_val['latest_close'] > (R1)
                            and ind_val['latest_close'] > ind_val['latest_st103']
                            and open_positions.get('CEB') is None):
                            logging.info("FROM MAIN: CEB indicator entry triggered")
                            if get_count("CEB","ENTRY") < ctx.ceb_entry_limit:
                                increment_count("CEB","ENTRY")
                                safe_execute(ceb_entry, ctx)
                        if open_positions.get('CEB') is not None:
                            if ind_val['latest_close'] < ind_val['latest_st103']:
                                logging.info("FROM MAIN: CEB indicator exit triggered")
                                if get_count("CEB","EXIT") < ctx.ceb_exit_limit:
                                    increment_count("CEB","EXIT")
                                    safe_execute(ceb_exit, ctx, positions)
                            elif now.time() >= dt_time(15, 15, 0):
                                logging.info("FROM MAIN: CEB time based exit triggered")
                                safe_execute(ceb_exit, ctx, open_positions)

                    # ----- C2EB -----
                    if ctx.strategy_c2eb == True:
                        if (now < intraday_entry_cutoff 
                            and ind_val['latest_close'] > (R1)
                            and ind_val['latest_close'] > ind_val['latest_st103']
                            and ind_val['latest_adx'] > 25
                            and open_positions.get('C2EB') is None):
                            logging.info("FROM MAIN: C2EB indicator entry triggered")
                            if get_count("C2EB","ENTRY") < ctx.c2eb_entry_limit:
                                increment_count("C2EB","ENTRY")
                                safe_execute(c2eb_entry, ctx)
                        if open_positions.get('C2EB') is not None:
                            if ind_val['latest_close'] < ind_val['latest_st103']:
                                logging.info("FROM MAIN: C2EB indicator exit triggered")
                                if get_count("C2EB","EXIT") < ctx.c2eb_exit_limit:
                                    increment_count("C2EB","EXIT")
                                    safe_execute(c2eb_exit, ctx, open_positions)
                            elif now.time() >= dt_time(15, 15, 0):
                                logging.info("FROM MAIN: C2EB time based exit triggered")
                                safe_execute(c2eb_exit, ctx, open_positions)
                    # ----- PEB -----
                    if ctx.strategy_peb == True :
                        if (now < intraday_entry_cutoff 
                            and ind_val['latest_close'] < (S1) 
                            and ind_val['latest_close'] < ind_val['latest_st102'] 
                            and ind_val['latest_close'] < ind_val['latest_st104']
                            and open_positions.get('PEB') is None):
                            logging.info("FROM MAIN: PEB indicator entry triggered")
                            if get_count("PEB","ENTRY") < ctx.peb_entry_limit:
                                increment_count("PEB","ENTRY")
                                safe_execute(peb_entry,ctx)
                        if open_positions.get('PEB') is not None:
                            if ind_val['latest_close'] > ind_val['latest_st102']:
                                logging.info("FROM MAIN: PEB indicator exit triggered")
                                if get_count("PEB","EXIT") < ctx.peb_exit_limit:
                                    increment_count("PEB","EXIT")
                                    safe_execute(peb_exit, ctx, open_positions)
                            elif now.time() >= dt_time(15, 15, 0):
                                logging.info("FROM MAIN: PEB time based exit triggered")
                                safe_execute(peb_exit, ctx, open_positions)
                    
                    # ----- SPE -----
                    if ctx.strategy_spe == True :
                        if (now < intraday_entry_cutoff 
                            and ind_val['latest_close'] < (S1) 
                            and ind_val['latest_close'] < ind_val['latest_st102'] 
                            and ind_val['latest_close'] < ind_val['latest_st104'] 
                            and open_positions.get('SPE') is None):
                            if ctx.re_entry == False: 
                                if spe_flag == "Down":
                                    logging.info("FROM MAIN: SPE indicator entry triggered")
                                    if get_count("SPE","ENTRY") < ctx.spe_entry_limit:
                                        increment_count("SPE","ENTRY")
                                        safe_execute(spe_entry, ctx) 
                                        spe_flag = "Up"
                            else:
                                logging.info("FROM MAIN: SPE indicator entry triggered")
                                safe_execute(spe_entry, ctx)           
                        if open_positions.get('SPE') is not None:
                            if ind_val['latest_close'] > ind_val['latest_st102']:
                                logging.info("FROM MAIN: SPE indicator exit triggered")
                                if get_count("SPE","EXIT") < ctx.spe_exit_limit:
                                    increment_count("SPE","EXIT")
                                    safe_execute(spe_exit, ctx, open_positions)
                                    spe_flag = "Down"
                            elif now.time() >= dt_time(15, 15, 0):
                                logging.info("FROM MAIN: SPE time based exit triggered")
                                safe_execute(spe_exit, ctx, open_positions)
                        # If premium SL is hit in between, that makes open_positions.get('SPE') None via build_positions.
                        # So, above mentioned if statement could never lower the spe_flag.
                        # We need to lower the flag if indicator exit happens after premium SL was hit earlier and position was made none.
                        if (ind_val['latest_close'] > ind_val['latest_st102'] 
                            and open_positions.get('SPE') is None
                            and spe_flag == "Up"):
                            logging.info("MAIN: SPE reset after SL via indicator condition")
                            spe_flag = "Down"

                    # ----- RCE -----
                    if ctx.strategy_rce == True :
                        if (now < intraday_entry_cutoff 
                            and ind_val['previous_close'] > ind_val['previous_st104'] 
                            and ind_val['latest_close'] < ind_val['latest_st104'] 
                            and ind_val['latest_adx'] < 30 
                            and open_positions.get('RCE') is None):
                            logging.info("FROM MAIN: RCE entry WITH hedge")
                            if get_count("RCE","ENTRY") < ctx.rce_entry_limit:
                                increment_count("RCE","ENTRY")
                                safe_execute(rce_entry,ctx)# for RCE with HEDGE
                            # logging.info("FROM MAIN: RCE entry WITHOUT hedge")
                            # safe_execute(rce_entry_without_hedge, ctx) # for RCE without HEDGE

                            # we had this when we were placing hedge for RCE, now we dont place hedge, thus we dont need if else thing
                            # the idea was, if we want hedge only once for the day, we take first entry as hedged entry, and later entries as unhedged entries 
                            # if open_positions.get("RCE_HDG") is None:
                            #     logging.info("FROM MAIN: RCE entry WITH hedge")
                            #     rce_entry(ctx)
                            # else:
                            #     logging.info("FROM MAIN: RCE entry WITHOUT hedge")
                            #     rce_entry_without_hedge(ctx)
                        if open_positions.get('RCE') is not None:
                            if ind_val['latest_close'] > ind_val['latest_st104']:
                                logging.info("FROM MAIN: RCE indicator exit triggered")
                                if get_count("RCE","EXIT") < ctx.rce_exit_limit:
                                    increment_count("RCE","EXIT")
                                    safe_execute(rce_exit, ctx, open_positions)
                            elif now.time() >= dt_time(15, 15, 0):
                                logging.info("FROM MAIN: RCE time based exit triggered")
                                safe_execute(rce_exit, ctx, open_positions)

                    # ----- INTRADAY_EOD_CLEANUP -----
                    if dt_time(15, 15, 0) <= now.time() < dt_time(15, 16, 0):
                        logging.info("Intraday EOD Clean-up Time!..\n")
                        safe_execute(eod_cleanup, ctx)
        
        # ================= NCL (Nifty Chirag Long system MIN SCHEDULER) =================
        if ctx.strategy_ncl == True or ctx.strategy_ncs == True:
            if now.minute % 15 == 0 and now >= market_open + timedelta(minutes=2):
                logging.info(f"NCL 15m scheduler fired {now}")
                ncl_open_positions = ncl_build_positions(ctx)
                ncs_open_positions = ncs_build_positions(ctx)
                n2cs_open_positions = n2cs_build_positions(ctx)
                time.sleep(0.5)
                logging.info("From main: call to fetch 15 min candle data...")
                
                # # OLD LOGIC
                # candledf_qr = get_candles(ctx=ctx, instrument_token=256265, interval="15minute", days=10, name="15min")
                # logging.info( f"NOW={datetime.now()} | " f"15M_LAST={candledf_qr.iloc[-1]['timestamp']} | " f"15M_PREV={candledf_qr.iloc[-2]['timestamp']}" )

                # # NEW LOGIC
                expectedqr_timestamp = now.replace(second=0, microsecond=0) # Calculate timestamp we expect e.g., if it's 12:42:04, we expect a candle for 12:42:00
                max_candleqr_retries = 5 # Poll until the broker publishes the expected candle
                candledf_qr = None
                latest_candleqr_time = None
                for attempt in range(1, max_candleqr_retries + 1):
                    candledf_qr = get_candles(ctx=ctx, instrument_token=256265, interval="15minute", days=10, name="15min")
                    if candledf_qr is not None and not candledf_qr.empty:
                        # latest_candle_time = candledf3m.iloc[-1]['timestamp']
                        latest_candleqr_time = pd.to_datetime( candledf_qr.iloc[-1]['timestamp'] ).tz_localize(None) 
                        if latest_candleqr_time >= expectedqr_timestamp: # If the latest candle is newer than expected, break the loop
                            break 
                        logging.warning(f"Attempt {attempt}/{max_candleqr_retries}: Expected is {expectedqr_timestamp} while Last is {latest_candleqr_time}. Retrying in 2s...")
                    else:
                        logging.warning(f"Attempt {attempt}/{max_candleqr_retries}: DataFrame empty or None. Retrying in 2s...")
                    time.sleep(1) # Wait 2 seconds before requesting again
                if latest_candleqr_time is None or latest_candleqr_time < expectedqr_timestamp:
                    logging.error(f"Expected candle {expectedqr_timestamp} not available after {max_candleqr_retries} retries. Skipping this cycle." )
                    candledf_qr = None
                if candledf_qr is not None and len(candledf_qr) >= 2:
                    logging.info(f"NOW={datetime.now()} | 15M_LAST={candledf_qr.iloc[-1]['timestamp']} | 15M_PREV={candledf_qr.iloc[-2]['timestamp']}")
                else:
                    logging.error("Failed to fetch valid candle data after all retries.")
            
                if candledf_qr is not None and len(candledf_qr) > 1:
                    ts15m =  pd.to_datetime(candledf_qr.iloc[-1]['timestamp'])
                    if ts15m.minute % 15 == 0:# and now.second >= 3: # this seconds >=3 is added to remove the error of access rate
                        logging.info(f"Checking for NCL entries or exits")
                        ind_val_qr = get_indicator_values(candledf_qr)

                        # ----- NCL -----
                        if ctx.strategy_ncl == True:
                            if (ind_val_qr['latest_close'] > ind_val_qr['latest_st103']
                                    and ind_val_qr['latest_close'] > ind_val_qr['latest_sma']
                                    and ncl_open_positions is None):
                                logging.info("FROM MAIN: NCL indicator entry triggered")
                                if get_count("NCL","ENTRY") < ctx.ncl_entry_limit:
                                    increment_count("NCL","ENTRY")
                                    safe_execute(ncl_entry, ctx)
                            if (ind_val_qr['latest_close'] < ind_val_qr['latest_st103']
                                    and ncl_open_positions is not None):
                                logging.info("FROM MAIN: NCL indicator exit triggered")
                                if get_count("NCL","EXIT") < ctx.ncl_exit_limit:
                                    increment_count("NCL","EXIT")
                                    safe_execute(ncl_exit, ctx, ncl_open_positions)     

                        # ----- NCS -----
                        if ctx.strategy_ncs == True:
                            if (ind_val_qr['latest_close'] < ind_val_qr['latest_st102']
                                    and ind_val_qr['latest_close'] < ind_val_qr['latest_st103']
                                    and ind_val_qr['latest_close'] < ind_val_qr['latest_sma']
                                    and ncs_open_positions is None):
                                logging.info("FROM MAIN: NCS indicator entry triggered")
                                if get_count("NCS","ENTRY") < ctx.ncs_entry_limit:
                                    increment_count("NCS","ENTRY")
                                    safe_execute(ncs_entry, ctx)
                            if (ind_val_qr['latest_close'] > ind_val_qr['latest_st102']
                                    and ncs_open_positions is not None):
                                logging.info("FROM MAIN: NCS indicator exit triggered")
                                if get_count("NCS","EXIT") < ctx.ncs_exit_limit:
                                    increment_count("NCS","EXIT")
                                    safe_execute(ncs_exit, ctx, ncs_open_positions)

                        # ----- N2CS -----
                        if ctx.strategy_n2cs == True:
                            if (ind_val_qr['latest_close'] < ind_val_qr['latest_st102']
                                    and ind_val_qr['latest_close'] < ind_val_qr['latest_st103']
                                    and ind_val_qr['latest_close'] < ind_val_qr['latest_sma']
                                    and n2cs_open_positions is None):
                                logging.info("FROM MAIN: N2CS indicator entry triggered")
                                if get_count("N2CS","ENTRY") < ctx.n2cs_entry_limit:
                                    increment_count("N2CS","ENTRY")
                                    safe_execute(n2cs_entry, ctx)
                            if (ind_val_qr['latest_close'] > ind_val_qr['latest_st102']
                                    and n2cs_open_positions is not None):
                                logging.info("FROM MAIN: N2CS indicator exit triggered")
                                if get_count("N2CS","EXIT") < ctx.n2cs_exit_limit:
                                    increment_count("N2CS","EXIT")
                                    safe_execute(n2cs_exit, ctx, n2cs_open_positions)
        
        # ================= NHF (60-MIN SCHEDULER) =================
        if now.minute == 15 and now.hour != 9:
            if ctx.strategy_nhf == True:
                logging.info(f"NHF scheduler fired {now}")
                nhf_open_positions = nhf_build_positions(ctx)
                time.sleep(0.5)
                logging.info("From main: call to fetch 60 min candle data...")
                
                # # OLD LOGIC
                # candledf_hr = get_candles(ctx=ctx, instrument_token=256265, interval="60minute", days=30, name="1hr")
                # logging.info(  f"NOW={datetime.now()} | " f"60M_LAST={candledf_hr.iloc[-1]['timestamp']} | " f"60M_PREV={candledf_hr.iloc[-2]['timestamp']}" )
                
                # # NEW LOGIC
                expectedhr_timestamp = now.replace(second=0, microsecond=0) # Calculate timestamp we expect e.g., if it's 12:42:04, we expect a candle for 12:42:00
                max_candlehr_retries = 5 # Poll until the broker publishes the expected candle
                candledf_hr = None
                latest_candlehr_time = None
                for attempt in range(1, max_candlehr_retries + 1):
                    candledf_hr = get_candles(ctx=ctx, instrument_token=256265, interval="60minute", days=30, name="60min")
                    if candledf_hr is not None and not candledf_hr.empty:
                        # latest_candle_time = candledf3m.iloc[-1]['timestamp']
                        latest_candlehr_time = pd.to_datetime( candledf_hr.iloc[-1]['timestamp'] ).tz_localize(None) 
                        if latest_candlehr_time >= expectedhr_timestamp: # If the latest candle is newer than expected, break the loop
                            break 
                        logging.warning(f"Attempt {attempt}/{max_candlehr_retries}: Expected is {expectedhr_timestamp} while Last is {latest_candlehr_time}. Retrying in 2s...")
                    else:
                        logging.warning(f"Attempt {attempt}/{max_candlehr_retries}: DataFrame empty or None. Retrying in 2s...")
                    time.sleep(1) # Wait 2 seconds before requesting again
                if latest_candlehr_time is None or latest_candlehr_time < expectedhr_timestamp:
                    logging.error(f"Expected candle {expectedhr_timestamp} not available after {max_candlehr_retries} retries. Skipping this cycle." )
                    candledf_hr = None
                if candledf_hr is not None and len(candledf_hr) >= 2:
                    logging.info(f"NOW={datetime.now()} | 60M_LAST={candledf_hr.iloc[-1]['timestamp']} | 60M_PREV={candledf_hr.iloc[-2]['timestamp']}")
                else:
                    logging.error("Failed to fetch valid candle data after all retries.")
                
                if candledf_hr is not None and len(candledf_hr) > 1:
                    ts_hr = pd.to_datetime(candledf_hr.iloc[-1]['timestamp'])
                    if ts_hr.minute == 15:# and now.second >= 3: # this seconds >=3 is added to remove the error of access rate
                        logging.info(f"Checking for NHF entries or exits")
                        ind_val_hr = get_indicator_values(candledf_hr)
                        # ----- NHF -----
                        if (ind_val_hr['latest_close'] > ind_val_hr['latest_st102']
                                and ind_val_hr['latest_close'] > ind_val_hr['latest_st103'] 
                                and ind_val_hr['latest_close'] > ind_val_hr['latest_sma']
                                and nhf_open_positions is None):
                            logging.info("FROM MAIN: NHF indicator entry triggered")
                            if get_count("NHF","ENTRY") < ctx.nhf_entry_limit:
                                increment_count("NHF","ENTRY")
                                safe_execute(nhf_entry, ctx)
                        if (ind_val_hr['latest_close'] < ind_val_hr['latest_st102'] 
                                and nhf_open_positions is not None):
                            logging.info("FROM MAIN: NHF indicator exit triggered")
                            if get_count("NHF","EXIT") < ctx.nhf_exit_limit:
                                increment_count("NHF","EXIT")
                                safe_execute(nhf_exit, ctx, nhf_open_positions)
        
        # ================= ROLL-OVER SECTION =================
        if today_date == ctx.nearest_nifty_expiry_date:
            if now.minute == 28 and now.hour == 14:
                logging.info(f"Rollover time!")               
                # NCL ROLLOVER (expiry day ONLY)
                if today_date == ctx.nearest_nifty_expiry_date:
                    logging.info("Expiry day detected — checking for NCL ROLLOVER")
                    safe_execute(rollover_ncl, ctx)             
                    time.sleep(1)
                # NHF ROLLOVER (expiry day ONLY)
                if today_date == ctx.nearest_nifty_expiry_date:
                    logging.info("Expiry day detected — checking for NHF ROLLOVER")
                    safe_execute(rollover_nhf, ctx)             
                    time.sleep(1)

        # ================= GOLD (15-MIN SIGNAL) =================
        if ctx.strategy_gold == True :
            if now.minute % 15 == 0 and now >= market_open:
                logging.info(f" 15m scheduler fired {now}")
                gold_open_positions = gold_build_positions(ctx)
                time.sleep(2)
                logging.info("From main: call to fetch 15 min candle data...")
                expectedgqr_timestamp = now.replace(second=0, microsecond=0) # Calculate timestamp we expect e.g., if it's 12:42:04, we expect a candle for 12:42:00
                max_candlegqr_retries = 5 # Poll until the broker publishes the expected candle
                candledf_gqr = None
                latest_candlegqr_time = None

                days_to_expiry = (ctx.nearest_kite_gold_expiry_date - today_date).days
                if days_to_expiry <= 7:
                    gold_token = ctx.next_kite_gold_token
                else:
                    gold_token = ctx.nearest_kite_gold_token

                for attempt in range(1, max_candlegqr_retries + 1):
                    candledf_gqr = get_candles(ctx=ctx, instrument_token=gold_token, interval="15minute", days=10, name="15min")
                    if candledf_gqr is not None and not candledf_gqr.empty:
                        latest_candlegqr_time = pd.to_datetime( candledf_gqr.iloc[-1]['timestamp'] ).tz_localize(None) 
                        if latest_candlegqr_time >= expectedgqr_timestamp: # If the latest candle is newer than expected, break the loop
                            break 
                        logging.warning(f"Attempt {attempt}/{max_candlegqr_retries}: Expected is {expectedgqr_timestamp} while Last is {latest_candlegqr_time}. Retrying in 2s...")
                    else:
                        logging.warning(f"Attempt {attempt}/{max_candlegqr_retries}: DataFrame empty or None. Retrying in 2s...")
                    time.sleep(1) # Wait 2 seconds before requesting again
                if latest_candlegqr_time is None or latest_candlegqr_time < expectedgqr_timestamp:
                    logging.error(f"Expected candle {expectedgqr_timestamp} not available after {max_candlegqr_retries} retries. Skipping this cycle." )
                    candledf_gqr = None
                if candledf_gqr is not None and len(candledf_gqr) >= 2:
                    logging.info(f"NOW={datetime.now()} | 15M_LAST={candledf_gqr.iloc[-1]['timestamp']} | 15M_PREV={candledf_gqr.iloc[-2]['timestamp']}")
                else:
                    logging.error("Failed to fetch valid candle data after all retries.")

                if candledf_gqr is not None and len(candledf_gqr) > 1:
                    tsg15 = pd.to_datetime(candledf_gqr.iloc[-1]['timestamp'])
                    if tsg15.minute % 15 == 0:
                        logging.info("Checking for GOLD entries or exits")
                        ind_val_gqr = get_indicator_values(candledf_gqr)
                        # ---- SAME STYLE AS NCS (you can tweak later) ----
                        if ctx.strategy_gold == True:
                            if (ind_val_gqr['latest_close'] > ind_val_gqr['latest_st103']
                                    and ind_val_gqr['latest_close'] > ind_val_gqr['latest_sma']
                                    and gold_open_positions is None):
                                logging.info("FROM MAIN: GOLD indicator entry triggered")
                                if get_count("GOLD","ENTRY") < ctx.gold_entry_limit:
                                    increment_count("GOLD","ENTRY")
                                    safe_execute(gold_entry, ctx)
                            if (ind_val_gqr['latest_close'] < ind_val_gqr['latest_st103']
                                    and gold_open_positions is not None):
                                logging.info("FROM MAIN: GOLD indicator exit triggered")
                                if get_count("GOLD","EXIT") < ctx.gold_exit_limit:
                                    increment_count("GOLD","EXIT")
                                    safe_execute(gold_exit, ctx, gold_open_positions) 

        # ================= SILVER (15-MIN SIGNAL) =================
        if ctx.strategy_silver == True :
            if now.minute % 15 == 0 and now >= market_open:
                logging.info(f" 15m scheduler fired {now}")
                silver_open_positions = silver_build_positions(ctx)
                time.sleep(2)
                logging.info("From main: call to fetch 15 min candle data...")
                expectedsqr_timestamp = now.replace(second=0, microsecond=0) # Calculate timestamp we expect e.g., if it's 12:42:04, we expect a candle for 12:42:00
                max_candlesqr_retries = 10 # Poll until the broker publishes the expected candle
                candledf_sqr = None
                latest_candlesqr_time = None

                days_to_expiry = (ctx.nearest_kite_silver_expiry_date - today_date).days
                if days_to_expiry <= 7:
                    silver_token = ctx.next_kite_silver_token
                else:
                    silver_token = ctx.nearest_kite_silver_token

                for attempt in range(1, max_candlesqr_retries + 1):
                    candledf_sqr = get_candles(ctx=ctx, instrument_token=silver_token, interval="15minute", days=10, name="15min")
                    if candledf_sqr is not None and not candledf_sqr.empty:
                        latest_candlesqr_time = pd.to_datetime( candledf_sqr.iloc[-1]['timestamp'] ).tz_localize(None) 
                        if latest_candlesqr_time >= expectedsqr_timestamp: # If the latest candle is newer than expected, break the loop
                            break 
                        logging.warning(f"Attempt {attempt}/{max_candlesqr_retries}: Expected is {expectedsqr_timestamp} while Last is {latest_candlesqr_time}. Retrying in 2s...")
                    else:
                        logging.warning(f"Attempt {attempt}/{max_candlesqr_retries}: DataFrame empty or None. Retrying in 2s...")
                    time.sleep(1) # Wait 2 seconds before requesting again
                if latest_candlesqr_time is None or latest_candlesqr_time < expectedsqr_timestamp:
                    logging.error(f"Expected candle {expectedsqr_timestamp} not available after {max_candlesqr_retries} retries. Skipping this cycle." )
                    candledf_sqr = None
                if candledf_sqr is not None and len(candledf_sqr) >= 2:
                    logging.info(f"NOW={datetime.now()} | 15M_LAST={candledf_sqr.iloc[-1]['timestamp']} | 15M_PREV={candledf_sqr.iloc[-2]['timestamp']}")
                else:
                    logging.error("Failed to fetch valid candle data after all retries.")

                if candledf_sqr is not None and len(candledf_sqr) > 1:
                    tss15 = pd.to_datetime(candledf_sqr.iloc[-1]['timestamp'])
                    if tss15.minute % 15 == 0:
                        logging.info("Checking for SILVER entries or exits")
                        ind_val_sqr = get_indicator_values(candledf_sqr)
                        # ---- SAME STYLE AS NCS (you can tweak later) ----
                        if ctx.strategy_silver == True:
                            if (ind_val_sqr['latest_close'] > ind_val_sqr['latest_st103']
                                    and ind_val_sqr['latest_close'] > ind_val_sqr['latest_sma']
                                    and silver_open_positions is None):
                                logging.info("FROM MAIN: SILVER indicator entry triggered")
                                if get_count("SILVER","ENTRY") < ctx.silver_entry_limit:
                                    increment_count("SILVER","ENTRY")
                                    safe_execute(silver_entry, ctx)
                            if (ind_val_sqr['latest_close'] < ind_val_sqr['latest_st103']
                                    and silver_open_positions is not None):
                                logging.info("FROM MAIN: SILVER indicator exit triggered")
                                if get_count("SILVER","EXIT") < ctx.silver_exit_limit:
                                    increment_count("SILVER","EXIT")
                                    safe_execute(silver_exit, ctx, silver_open_positions)

        # ================= CRUDE (15-MIN SIGNAL) =================
        if ctx.strategy_crude == True :
            if now.minute % 15 == 0 and now >= market_open:
                logging.info(f" 15m scheduler fired {now}")
                crude_open_positions = crude_build_positions(ctx)
                time.sleep(2)
                logging.info("From main: call to fetch 15 min candle data...")
                expectedcqr_timestamp = now.replace(second=0, microsecond=0) # Calculate timestamp we expect e.g., if it's 12:42:04, we expect a candle for 12:42:00
                max_candlecqr_retries = 10 # Poll until the broker publishes the expected candle
                candledf_cqr = None
                latest_candlecqr_time = None

                days_to_expiry = (ctx.nearest_kite_crude_expiry_date - today_date).days
                if days_to_expiry <= 7:
                    crude_token = ctx.next_kite_crude_token
                else:
                    crude_token = ctx.nearest_kite_crude_token

                for attempt in range(1, max_candlecqr_retries + 1):
                    candledf_cqr = get_candles(ctx=ctx, instrument_token=crude_token, interval="15minute", days=10, name="15min")
                    if candledf_cqr is not None and not candledf_cqr.empty:
                        latest_candlecqr_time = pd.to_datetime( candledf_cqr.iloc[-1]['timestamp'] ).tz_localize(None) 
                        if latest_candlecqr_time >= expectedcqr_timestamp: # If the latest candle is newer than expected, break the loop
                            break 
                        logging.warning(f"Attempt {attempt}/{max_candlecqr_retries}: Expected is {expectedcqr_timestamp} while Last is {latest_candlecqr_time}. Retrying in 2s...")
                    else:
                        logging.warning(f"Attempt {attempt}/{max_candlecqr_retries}: DataFrame empty or None. Retrying in 2s...")
                    time.sleep(1) # Wait 2 seconds before requesting again
                if latest_candlecqr_time is None or latest_candlecqr_time < expectedcqr_timestamp:
                    logging.error(f"Expected candle {expectedcqr_timestamp} not available after {max_candlecqr_retries} retries. Skipping this cycle." )
                    candledf_cqr = None
                if candledf_cqr is not None and len(candledf_cqr) >= 2:
                    logging.info(f"NOW={datetime.now()} | 15M_LAST={candledf_cqr.iloc[-1]['timestamp']} | 15M_PREV={candledf_cqr.iloc[-2]['timestamp']}")
                else:
                    logging.error("Failed to fetch valid candle data after all retries.")

                if candledf_cqr is not None and len(candledf_cqr) > 1:
                    tss15 = pd.to_datetime(candledf_cqr.iloc[-1]['timestamp'])
                    if tss15.minute % 15 == 0:
                        logging.info("Checking for CRUDE entries or exits")
                        ind_val_cqr = get_indicator_values(candledf_cqr)
                        # ---- SAME STYLE AS NCS (you can tweak later) ----
                        if ctx.strategy_crude == True:
                            if (ind_val_cqr['latest_close'] > ind_val_cqr['latest_st103']
                                    and ind_val_cqr['latest_close'] > ind_val_cqr['latest_sma']
                                    and crude_open_positions is None):
                                logging.info("FROM MAIN: CRUDE indicator entry triggered")
                                if get_count("CRUDE","ENTRY") < ctx.crude_entry_limit:
                                    increment_count("CRUDE","ENTRY")
                                    safe_execute(crude_entry, ctx)
                            if (ind_val_cqr['latest_close'] < ind_val_cqr['latest_st103']
                                    and crude_open_positions is not None):
                                logging.info("FROM MAIN: CRUDE indicator exit triggered")
                                if get_count("CRUDE","EXIT") < ctx.crude_exit_limit:
                                    increment_count("CRUDE","EXIT")
                                    safe_execute(crude_exit, ctx, crude_open_positions)
                                 
        # # ================= ANY OTHER EVENTS =================
        # fixed_times = [(9, 20),(10, 28),(11, 32),(13, 26),]
        # for h, m in fixed_times:
        #     key = (h, m)
        #     if (now.hour == h
        #         and now.minute == m
        #         and not ctx.executed_times.get(key, False)): # at 9:20, strategy runs, but only once. next time it wont run even if loop run several times in that minute
        #         logging.info(f"Running arbitrary timed strategy for {h:02d}:{m:02d}")
        #         # >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
        #         # PUT YOUR STRATEGY CALL HERE
        #         # example:
        #         # run_my_strategy(ctx)
        #         # >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
        #         ctx.executed_times[key] = True
        # if ts_hr.minute == 15 and last_run_hour != ts_hr.hour: # this helps to avoid double entries when we dont use open positions
        #     last_run_hour = ts_hr.hour 
        #     # after this run your strategy, it wont run twice in the same hour
        
        # ----- Expiry day cleanup for NCS and N2CS -----
        if dt_time(15, 15, 0) <= now.time() < dt_time(15, 16, 0):       
            logging.info("Expiry day Clean-up Time!..\n")

            # NCS EXPIRY CLEARING (expiry day ONLY)
            if today_date == ctx.nearest_nifty_expiry_date:
                logging.info("Expiry day detected — checking for NCS ROLLOVER")
                safe_execute(ncs_expiry_clearing, ctx)             
                time.sleep(1)

            # N2CS EXPIRY CLEARING (expiry day ONLY)
            if today_date == ctx.nearest_nifty_expiry_date:
                logging.info("Expiry day detected — checking for N2CS ROLLOVER")
                safe_execute(n2cs_expiry_clearing, ctx)             
                time.sleep(1)

        # ================= 1-MIN HEARTBEAT =================
        now = datetime.now()
        # Don't sleep past market close
        if now < phase1_exit_time:
            next_minute = 60 - (now.second + now.microsecond / 1e6) # time remaining till start of next minute
            seconds_to_close = (phase1_exit_time - now).total_seconds() # time remaining till market close
            sleep_seconds = min(next_minute, seconds_to_close)

            logging.info(f"1 min cycle ends.. in {sleep_seconds:.1f} seconds\n")
            time.sleep(max(0.5, sleep_seconds))

    ####################################################################################################
    ###################                    END OF DAY CONDITIONS                    ####################
    ####################################################################################################

    # ================= NHF Clean-Up =================
    while True:
        now = datetime.now()
        
        if now >= phase1_exit_time :
            logging.info("NHF Clean-up time!")
            if now.minute >= 29 and now.hour == 15:
                if ctx.strategy_nhf == True:
                    logging.info(f"NHF scheduler fired {now}")
                    nhf_open_positions = nhf_build_positions(ctx)
                    time.sleep(0.5)
                    logging.info("From main: call to fetch 60 min candle data...")

                    # # NEW LOGIC
                    expectedhr_timestamp = now.replace(second=0, microsecond=0) # Calculate timestamp we expect e.g., if it's 12:42:04, we expect a candle for 12:42:00
                    max_candlehr_retries = 5 # Poll until the broker publishes the expected candle
                    candledf_hr = None
                    latest_candlehr_time = None
                    for attempt in range(1, max_candlehr_retries + 1):
                        candledf_hr = get_candles(ctx=ctx, instrument_token=256265, interval="60minute", days=30, name="60min")
                        if candledf_hr is not None and not candledf_hr.empty:
                            latest_candlehr_time = pd.to_datetime( candledf_hr.iloc[-1]['timestamp'] ).tz_localize(None) 
                            if latest_candlehr_time >= expectedhr_timestamp: # If the latest candle is newer than expected, break the loop
                                break 
                            logging.warning(f"Attempt {attempt}/{max_candlehr_retries}: Expected is {expectedhr_timestamp} while Last is {latest_candlehr_time}. Retrying in 2s...")
                        else:
                            logging.warning(f"Attempt {attempt}/{max_candlehr_retries}: DataFrame empty or None. Retrying in 2s...")
                        time.sleep(1) # Wait 2 seconds before requesting again
                    if latest_candlehr_time is None or latest_candlehr_time < expectedhr_timestamp:
                        logging.error(f"Expected candle {expectedhr_timestamp} not available after {max_candlehr_retries} retries. Skipping this cycle." )
                        candledf_hr = None
                    if candledf_hr is not None and len(candledf_hr) >= 2:
                        logging.info(f"NOW={datetime.now()} | 60M_LAST={candledf_hr.iloc[-1]['timestamp']} | 60M_PREV={candledf_hr.iloc[-2]['timestamp']}")
                    else:
                        logging.error("Failed to fetch valid candle data after all retries.")
                    
                    if candledf_hr is not None and len(candledf_hr) > 1:
                        ts_hr = pd.to_datetime(candledf_hr.iloc[-1]['timestamp'])
                        if ts_hr.minute == 15:
                            logging.info(f"Checking for NHF entries or exits")
                            ind_val_hr = get_indicator_values(candledf_hr)
                            # ----- NHF -----
                            if (ind_val_hr['latest_close'] > ind_val_hr['latest_st102']
                                    and ind_val_hr['latest_close'] > ind_val_hr['latest_st103'] 
                                    and ind_val_hr['latest_close'] > ind_val_hr['latest_sma']
                                    and nhf_open_positions is None):
                                logging.info("FROM MAIN: NHF indicator entry triggered")
                                if get_count("NHF","ENTRY") < ctx.nhf_entry_limit:
                                    increment_count("NHF","ENTRY")
                                    safe_execute(nhf_entry, ctx)
                            if (ind_val_hr['latest_close'] < ind_val_hr['latest_st102'] 
                                    and nhf_open_positions is not None):
                                logging.info("FROM MAIN: NHF indicator exit triggered")
                                if get_count("NHF","EXIT") < ctx.nhf_exit_limit:
                                    increment_count("NHF","EXIT")
                                    safe_execute(nhf_exit, ctx, nhf_open_positions)
            logging.info("NHF Clean-up complete")
            break  

    # ================= PHASE 2 - COMMODITIES =================
    while datetime.now() < phase2_exit_time:
        now = datetime.now()
        reload_config_if_changed(ctx, base_dir)
        
        # ================= GOLD (15-MIN SIGNAL) =================
        if ctx.strategy_gold == True :
            if now.minute % 15 == 0 and now >= market_open + timedelta(minutes=2):
                logging.info(f" 15m scheduler fired {now}")
                gold_open_positions = gold_build_positions(ctx)
                time.sleep(2)
                logging.info("From main: call to fetch 15 min candle data...")
                expectedgqr_timestamp = now.replace(second=0, microsecond=0) # Calculate timestamp we expect e.g., if it's 12:42:04, we expect a candle for 12:42:00
                max_candlegqr_retries = 10 # Poll until the broker publishes the expected candle
                candledf_gqr = None
                latest_candlegqr_time = None

                days_to_expiry = (ctx.nearest_kite_gold_expiry_date - today_date).days
                if days_to_expiry <= 7:
                    gold_token = ctx.next_kite_gold_token
                else:
                    gold_token = ctx.nearest_kite_gold_token

                for attempt in range(1, max_candlegqr_retries + 1):
                    candledf_gqr = get_candles(ctx=ctx, instrument_token=gold_token, interval="15minute", days=10, name="15min")
                    if candledf_gqr is not None and not candledf_gqr.empty:
                        latest_candlegqr_time = pd.to_datetime( candledf_gqr.iloc[-1]['timestamp'] ).tz_localize(None) 
                        if latest_candlegqr_time >= expectedgqr_timestamp: # If the latest candle is newer than expected, break the loop
                            break 
                        logging.warning(f"Attempt {attempt}/{max_candlegqr_retries}: Expected is {expectedgqr_timestamp} while Last is {latest_candlegqr_time}. Retrying in 2s...")
                    else:
                        logging.warning(f"Attempt {attempt}/{max_candlegqr_retries}: DataFrame empty or None. Retrying in 2s...")
                    time.sleep(1) # Wait 2 seconds before requesting again
                if latest_candlegqr_time is None or latest_candlegqr_time < expectedgqr_timestamp:
                    logging.error(f"Expected candle {expectedgqr_timestamp} not available after {max_candlegqr_retries} retries. Skipping this cycle." )
                    candledf_gqr = None
                if candledf_gqr is not None and len(candledf_gqr) >= 2:
                    logging.info(f"NOW={datetime.now()} | 15M_LAST={candledf_gqr.iloc[-1]['timestamp']} | 15M_PREV={candledf_gqr.iloc[-2]['timestamp']}")
                else:
                    logging.error("Failed to fetch valid candle data after all retries.")

                if candledf_gqr is not None and len(candledf_gqr) > 1:
                    tsg15 = pd.to_datetime(candledf_gqr.iloc[-1]['timestamp'])
                    if tsg15.minute % 15 == 0:
                        logging.info("Checking for GOLD entries or exits")
                        ind_val_gqr = get_indicator_values(candledf_gqr)
                        # ---- SAME STYLE AS NCS (you can tweak later) ----
                        if ctx.strategy_gold == True:
                            if (ind_val_gqr['latest_close'] > ind_val_gqr['latest_st103']
                                    and ind_val_gqr['latest_close'] > ind_val_gqr['latest_sma']
                                    and gold_open_positions is None):
                                logging.info("FROM MAIN: GOLD indicator entry triggered")
                                if get_count("GOLD","ENTRY") < ctx.gold_entry_limit:
                                    increment_count("GOLD","ENTRY")
                                    safe_execute(gold_entry, ctx)
                            if (ind_val_gqr['latest_close'] < ind_val_gqr['latest_st103']
                                    and gold_open_positions is not None):
                                logging.info("FROM MAIN: GOLD indicator exit triggered")
                                if get_count("GOLD","EXIT") < ctx.gold_exit_limit:
                                    increment_count("GOLD","EXIT")
                                    safe_execute(gold_exit, ctx, gold_open_positions) 
        
        # ================= SILVER (15-MIN SIGNAL) =================
        if ctx.strategy_silver == True :
            if now.minute % 15 == 0 and now >= market_open + timedelta(minutes=2):
                logging.info(f" 15m scheduler fired {now}")
                silver_open_positions = silver_build_positions(ctx)
                time.sleep(2)
                logging.info("From main: call to fetch 15 min candle data...")
                expectedsqr_timestamp = now.replace(second=0, microsecond=0) # Calculate timestamp we expect e.g., if it's 12:42:04, we expect a candle for 12:42:00
                max_candlesqr_retries = 10 # Poll until the broker publishes the expected candle
                candledf_sqr = None
                latest_candlesqr_time = None

                days_to_expiry = (ctx.nearest_kite_silver_expiry_date - today_date).days
                if days_to_expiry <= 7:
                    silver_token = ctx.next_kite_silver_token
                else:
                    silver_token = ctx.nearest_kite_silver_token

                for attempt in range(1, max_candlesqr_retries + 1):
                    candledf_sqr = get_candles(ctx=ctx, instrument_token=silver_token, interval="15minute", days=10, name="15min")
                    if candledf_sqr is not None and not candledf_sqr.empty:
                        latest_candlesqr_time = pd.to_datetime( candledf_sqr.iloc[-1]['timestamp'] ).tz_localize(None) 
                        if latest_candlesqr_time >= expectedsqr_timestamp: # If the latest candle is newer than expected, break the loop
                            break 
                        logging.warning(f"Attempt {attempt}/{max_candlesqr_retries}: Expected is {expectedsqr_timestamp} while Last is {latest_candlesqr_time}. Retrying in 2s...")
                    else:
                        logging.warning(f"Attempt {attempt}/{max_candlesqr_retries}: DataFrame empty or None. Retrying in 2s...")
                    time.sleep(1) # Wait 2 seconds before requesting again
                if latest_candlesqr_time is None or latest_candlesqr_time < expectedsqr_timestamp:
                    logging.error(f"Expected candle {expectedsqr_timestamp} not available after {max_candlesqr_retries} retries. Skipping this cycle." )
                    candledf_sqr = None
                if candledf_sqr is not None and len(candledf_sqr) >= 2:
                    logging.info(f"NOW={datetime.now()} | 15M_LAST={candledf_sqr.iloc[-1]['timestamp']} | 15M_PREV={candledf_sqr.iloc[-2]['timestamp']}")
                else:
                    logging.error("Failed to fetch valid candle data after all retries.")

                if candledf_sqr is not None and len(candledf_sqr) > 1:
                    tss15 = pd.to_datetime(candledf_sqr.iloc[-1]['timestamp'])
                    if tss15.minute % 15 == 0:
                        logging.info("Checking for SILVER entries or exits")
                        ind_val_sqr = get_indicator_values(candledf_sqr)
                        # ---- SAME STYLE AS NCS (you can tweak later) ----
                        if ctx.strategy_silver == True:
                            if (ind_val_sqr['latest_close'] > ind_val_sqr['latest_st103']
                                    and ind_val_sqr['latest_close'] > ind_val_sqr['latest_sma']
                                    and silver_open_positions is None):
                                logging.info("FROM MAIN: SILVER indicator entry triggered")
                                if get_count("SILVER","ENTRY") < ctx.silver_entry_limit:
                                    increment_count("SILVER","ENTRY")
                                    safe_execute(silver_entry, ctx)
                            if (ind_val_sqr['latest_close'] < ind_val_sqr['latest_st103']
                                    and silver_open_positions is not None):
                                logging.info("FROM MAIN: SILVER indicator exit triggered")
                                if get_count("SILVER","EXIT") < ctx.silver_exit_limit:
                                    increment_count("SILVER","EXIT")
                                    safe_execute(silver_exit, ctx, silver_open_positions)
        
        # ================= CRUDE (15-MIN SIGNAL) =================
        if ctx.strategy_crude == True :
            if now.minute % 15 == 0 and now >= market_open + timedelta(minutes=2):
                logging.info(f" 15m scheduler fired {now}")
                crude_open_positions = crude_build_positions(ctx)
                time.sleep(2)
                logging.info("From main: call to fetch 15 min candle data...")
                expectedcqr_timestamp = now.replace(second=0, microsecond=0) # Calculate timestamp we expect e.g., if it's 12:42:04, we expect a candle for 12:42:00
                max_candlecqr_retries = 10 # Poll until the broker publishes the expected candle
                candledf_cqr = None
                latest_candlecqr_time = None

                days_to_expiry = (ctx.nearest_kite_crude_expiry_date - today_date).days
                if days_to_expiry <= 7:
                    crude_token = ctx.next_kite_crude_token
                else:
                    crude_token = ctx.nearest_kite_crude_token

                for attempt in range(1, max_candlecqr_retries + 1):
                    candledf_cqr = get_candles(ctx=ctx, instrument_token=crude_token, interval="15minute", days=10, name="15min")
                    if candledf_cqr is not None and not candledf_cqr.empty:
                        latest_candlecqr_time = pd.to_datetime( candledf_cqr.iloc[-1]['timestamp'] ).tz_localize(None) 
                        if latest_candlecqr_time >= expectedcqr_timestamp: # If the latest candle is newer than expected, break the loop
                            break 
                        logging.warning(f"Attempt {attempt}/{max_candlecqr_retries}: Expected is {expectedcqr_timestamp} while Last is {latest_candlecqr_time}. Retrying in 2s...")
                    else:
                        logging.warning(f"Attempt {attempt}/{max_candlecqr_retries}: DataFrame empty or None. Retrying in 2s...")
                    time.sleep(1) # Wait 2 seconds before requesting again
                if latest_candlecqr_time is None or latest_candlecqr_time < expectedcqr_timestamp:
                    logging.error(f"Expected candle {expectedcqr_timestamp} not available after {max_candlecqr_retries} retries. Skipping this cycle." )
                    candledf_cqr = None
                if candledf_cqr is not None and len(candledf_cqr) >= 2:
                    logging.info(f"NOW={datetime.now()} | 15M_LAST={candledf_cqr.iloc[-1]['timestamp']} | 15M_PREV={candledf_cqr.iloc[-2]['timestamp']}")
                else:
                    logging.error("Failed to fetch valid candle data after all retries.")

                if candledf_cqr is not None and len(candledf_cqr) > 1:
                    tss15 = pd.to_datetime(candledf_cqr.iloc[-1]['timestamp'])
                    if tss15.minute % 15 == 0:
                        logging.info("Checking for CRUDE entries or exits")
                        ind_val_cqr = get_indicator_values(candledf_cqr)
                        # ---- SAME STYLE AS NCS (you can tweak later) ----
                        if ctx.strategy_crude == True:
                            if (ind_val_cqr['latest_close'] > ind_val_cqr['latest_st103']
                                    and ind_val_cqr['latest_close'] > ind_val_cqr['latest_sma']
                                    and crude_open_positions is None):
                                logging.info("FROM MAIN: CRUDE indicator entry triggered")
                                if get_count("CRUDE","ENTRY") < ctx.crude_entry_limit:
                                    increment_count("CRUDE","ENTRY")
                                    safe_execute(crude_entry, ctx)
                            if (ind_val_cqr['latest_close'] < ind_val_cqr['latest_st103']
                                    and crude_open_positions is not None):
                                logging.info("FROM MAIN: CRUDE indicator exit triggered")
                                if get_count("CRUDE","EXIT") < ctx.crude_exit_limit:
                                    increment_count("CRUDE","EXIT")
                                    safe_execute(crude_exit, ctx, crude_open_positions)

        # 1-min heartbeat
        now = datetime.now()
        sleep_seconds = 60 - (now.second + now.microsecond / 1e6)
        logging.info(f"1 min cycle ends.. in {sleep_seconds} seconds \n")
        time.sleep(max(0.5, sleep_seconds))

    # ✅ shutdown AFTER loop ends
    logging.info("Final shutdown after GOLD evening session")
    os._exit(0)

else:
    print("Subscription expired")