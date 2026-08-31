import os
import logging
import pandas as pd
from user.credentials import *

def load_user_selection(base_dir):
    path = os.path.join(base_dir, "user_selection.csv")

    if not os.path.exists(path):
        raise FileNotFoundError(f"user_selection.csv not found at {path}")

    df = pd.read_csv(path)

    # 🔥 Normalize headers (this fixes BOM and whitespace problems)
    df.columns = [c.strip().lower().replace("\ufeff", "") for c in df.columns]

    config = {}

    for _, row in df.iterrows():
        if pd.isna(row['key']) or pd.isna(row['value']):
            continue

        key = str(row['key']).strip()
        val = str(row['value']).strip()

        if not key:
            continue
        # ---------- FORCE STRING FOR LOGIN IDS ----------
        if key in ("APPROVAL_IDA", "SUBSCRIPTION_IDK"):
            config[key] = val
            continue

        # Auto type-casting
        if val.lower() in ('true', '1', 'yes'):
            config[key] = True
        elif val.lower() in ('false', '0', 'no'):
            config[key] = False
        else:
            try:
                if '.' in val:
                    config[key] = float(val)
                else:
                    config[key] = int(val)
            except:
                config[key] = val

    return config

def apply_config(ctx, user_cfg):

    ctx.re_entry = user_cfg.get("RE_ENTRY", True)
    ctx.limitorder =  user_cfg.get("LIMITORDER", 1)/100
    ctx.gold_limitorder =  user_cfg.get("GOLD_LIMITORDER", 1)/100
    ctx.silver_limitorder =  user_cfg.get("SILVER_LIMITORDER", 1)/100
    ctx.crude_limitorder =  user_cfg.get("CRUDE_LIMITORDER", 1)/100
    ctx.productType = str(user_cfg.get("PRODUCTTYPE", "NRML")).upper()
    # ===== STRATEGY SWITCHES =====
    ctx.strategy_ceb = user_cfg.get("STRATEGY_CEB", True)
    ctx.strategy_c2eb = user_cfg.get("STRATEGY_C2EB", True)
    ctx.strategy_peb = user_cfg.get("STRATEGY_PEB", True)
    ctx.strategy_spe = user_cfg.get("STRATEGY_SPE", True)
    ctx.strategy_rce = user_cfg.get("STRATEGY_RCE", True)
    ctx.strategy_exd = user_cfg.get("STRATEGY_EXD", True)
    ctx.strategy_ncl = user_cfg.get("STRATEGY_NCL", True)
    ctx.strategy_ncs = user_cfg.get("STRATEGY_NCS", True)
    ctx.strategy_n2cs = user_cfg.get("STRATEGY_N2CS", True)


    ctx.strategy_nhf = user_cfg.get("STRATEGY_NHF", True)
    ctx.strategy_gold = user_cfg.get("STRATEGY_GOLD", True)
    ctx.strategy_silver = user_cfg.get("STRATEGY_SILVER", True)
    ctx.strategy_crude = user_cfg.get("STRATEGY_CRUDE", True)

    # ===== CEB =====
    ctx.ceb_lot = ctx.nifty_near_lot * user_cfg.get("CEB_LOT", 1)
    ctx.ceb_psl = user_cfg.get("CEB_PSL", 80)/100
    ctx.ceb_itm = user_cfg.get("CEB_ITM", 800)
    ctx.ceb_cp  = user_cfg.get("CEB_CP", 250)
    ctx.ceb_entry_limit = user_cfg.get("CEB_ENTRY_LIMIT", 5)
    ctx.ceb_exit_limit = user_cfg.get("CEB_EXIT_LIMIT", 5)

    # ===== C2EB =====
    ctx.c2eb_lot = ctx.nifty_near_lot * user_cfg.get("C2EB_LOT", 1)
    ctx.c2eb_psl = user_cfg.get("C2EB_PSL", 80)/100
    ctx.c2eb_itm = user_cfg.get("C2EB_ITM", 800)
    ctx.c2eb_cp  = user_cfg.get("C2EB_CP", 250)
    ctx.c2eb_entry_limit = user_cfg.get("C2EB_ENTRY_LIMIT", 5)
    ctx.c2eb_exit_limit = user_cfg.get("C2EB_EXIT_LIMIT", 5)

    # ===== PEB =====
    ctx.peb_lot = ctx.nifty_near_lot * user_cfg.get("PEB_LOT", 1)
    ctx.peb_psl = user_cfg.get("PEB_PSL", 30)/100
    ctx.peb_itm = user_cfg.get("PEB_ITM", 200)
    ctx.peb_entry_limit = user_cfg.get("PEB_ENTRY_LIMIT", 5)
    ctx.peb_exit_limit = user_cfg.get("PEB_EXIT_LIMIT", 5)

    # ===== PEB-SCE =====
    ctx.spe_lot = ctx.nifty_near_lot * user_cfg.get("SPE_LOT",1)
    ctx.spe_psl = user_cfg.get("SPE_PSL", 30)/100
    ctx.spe_hdg_dist = user_cfg.get("SPE_HDG_DIST", 500)
    ctx.spe_itm = user_cfg.get("SPE_ITM", -100)
    ctx.spe_entry_limit = user_cfg.get("SPE_ENTRY_LIMIT", 5)
    ctx.spe_exit_limit = user_cfg.get("SPE_EXIT_LIMIT", 5)

    # ===== RCE =====
    ctx.rce_lot = ctx.nifty_near_lot * user_cfg.get("RCE_LOT",1)
    ctx.rce_psl = user_cfg.get("RCE_PSL", 30)/100
    ctx.rce_hdg_dist = user_cfg.get("RCE_HDG_DIST", 500)
    ctx.rce_itm = user_cfg.get("RCE_ITM",-100)
    ctx.rce_entry_limit = user_cfg.get("RCE_ENTRY_LIMIT", 5)
    ctx.rce_exit_limit = user_cfg.get("RCE_EXIT_LIMIT", 5)

    # ===== EXD-NIFTY =====
    ctx.exd_nifty_lot = ctx.nifty_near_lot * user_cfg.get("EXD_NIFTY_LOT",1)
    ctx.exd_nifty_cp = user_cfg.get("EXD_NIFTY_CP",50)
    ctx.exd_nifty_psl = user_cfg.get("EXD_NIFTY_PSL", 40)/100
    ctx.exd_nifty_hdg_dist = user_cfg.get("EXD_NIFTY_HDG_DIST",500)

    # ===== EXD-SENSEX =====
    ctx.exd_sensex_lot = ctx.sensex_lot * user_cfg.get("EXD_SENSEX_LOT",1)
    ctx.exd_sensex_cp = user_cfg.get("EXD_SENSEX_CP", 200)
    ctx.exd_sensex_psl = user_cfg.get("EXD_SENSEX_PSL", 40)/100
    ctx.exd_sensex_hdg_dist = user_cfg.get("EXD_SENSEX_HDG_DIST", 2000)

    # ===== NHF =====
    ctx.nhf_near_lot = ctx.nifty_near_lot * user_cfg.get("NHF_LOT",1)
    ctx.nhf_next_lot = ctx.nifty_next_lot * user_cfg.get("NHF_LOT",1)
    ctx.nhf_hdg_dist = user_cfg.get("NHF_HDG_DIST", 800)
    ctx.nhf_entry_limit = user_cfg.get("NHF_ENTRY_LIMIT", 5)
    ctx.nhf_exit_limit = user_cfg.get("NHF_EXIT_LIMIT", 5)

    # ===== NCL =====
    ctx.ncl_near_lot = ctx.nifty_near_lot * user_cfg.get("NCL_LOT",1)
    ctx.ncl_next_lot = ctx.nifty_next_lot * user_cfg.get("NCL_LOT",1)
    ctx.ncl_hdg_dist = user_cfg.get("NCL_HDG_DIST", 800)
    ctx.ncl_entry_limit = user_cfg.get("NCL_ENTRY_LIMIT", 5)
    ctx.ncl_exit_limit = user_cfg.get("NCL_EXIT_LIMIT", 5)

    # ===== NCS =====
    ctx.ncs_near_lot = ctx.nifty_near_lot * user_cfg.get("NCS_LOT",1)
    ctx.ncs_next_lot = ctx.nifty_next_lot * user_cfg.get("NCS_LOT",1)
    ctx.ncs_monthend_lot = ctx.nifty_monthend_lot * user_cfg.get("NCS_LOT",1)
    ctx.ncs_next_monthend_lot = ctx.nifty_next_monthend_lot * user_cfg.get("NCS_LOT",1)
    ctx.ncs_hdg_dist = user_cfg.get("NCS_HDG_DIST", 800)
    ctx.ncs_itm = user_cfg.get("NCS_ITM", -300)
    ctx.ncs_cp  = user_cfg.get("NCS_CP", 300)
    ctx.ncs_entry_limit = user_cfg.get("NCS_ENTRY_LIMIT", 5)
    ctx.ncs_exit_limit = user_cfg.get("NCS_EXIT_LIMIT", 5)

    # ===== N2CS =====
    ctx.n2cs_near_lot = ctx.nifty_near_lot * user_cfg.get("N2CS_LOT",1)
    ctx.n2cs_next_lot = ctx.nifty_next_lot * user_cfg.get("N2CS_LOT",1)
    ctx.n2cs_monthend_lot = ctx.nifty_monthend_lot * user_cfg.get("N2CS_LOT",1)
    ctx.n2cs_next_monthend_lot = ctx.nifty_next_monthend_lot * user_cfg.get("N2CS_LOT",1)
    ctx.n2cs_hdg_dist = user_cfg.get("N2CS_HDG_DIST", 800)
    ctx.n2cs_itm = user_cfg.get("N2CS_ITM", -300)
    ctx.n2cs_cp  = user_cfg.get("N2CS_CP", 300)
    ctx.n2cs_entry_limit = user_cfg.get("N2CS_ENTRY_LIMIT", 5)
    ctx.n2cs_exit_limit = user_cfg.get("N2CS_EXIT_LIMIT", 5)

    # ===== CMDTY =====
    ctx.gold_near_lot = ctx.goldscripmaster_near_lot * user_cfg.get("GOLD_LOT",1)
    ctx.gold_next_lot = ctx.goldscripmaster_next_lot * user_cfg.get("GOLD_LOT",1)
    ctx.gold_entry_limit = user_cfg.get("GOLD_ENTRY_LIMIT", 5)
    ctx.gold_exit_limit = user_cfg.get("GOLD_EXIT_LIMIT", 5)
    ctx.silver_near_lot = ctx.silverscripmaster_near_lot * user_cfg.get("SILVER_LOT",1)
    ctx.silver_next_lot = ctx.silverscripmaster_next_lot * user_cfg.get("SILVER_LOT",1)
    ctx.silver_entry_limit = user_cfg.get("SILVER_ENTRY_LIMIT", 5)
    ctx.silver_exit_limit = user_cfg.get("SILVER_EXIT_LIMIT", 5)
    ctx.crude_near_lot = ctx.crudescripmaster_near_lot * user_cfg.get("CRUDE_LOT",1)
    ctx.crude_next_lot = ctx.crudescripmaster_next_lot * user_cfg.get("CRUDE_LOT",1)
    ctx.crude_entry_limit = user_cfg.get("CRUDE_ENTRY_LIMIT", 5)
    ctx.crude_exit_limit = user_cfg.get("CRUDE_EXIT_LIMIT", 5)
    
def apply_login_config(ctx, user_cfg):
    
    raw_angel = str(user_cfg.get("APPROVAL_IDA", "")).strip()
    raw_kotak = str(user_cfg.get("SUBSCRIPTION_IDK", "")).strip()

    if len(raw_angel) < 4:
        raise ValueError("ANGEL_PIN must be at least 4 digits")

    if len(raw_kotak) < 6:
        raise ValueError("KOTAK_MPIN must be at least 6 digits")

    ctx.angel_pin = raw_angel[:4]
    ctx.kotak_mpin = raw_kotak[:6]

    #ctx.kotak_mpin = raw_kotak[2:8] # add 10 digit number with 2 to 8 digits are actually MPIN

    # ===== CMDTY =====
    ctx.gold_instrument = user_cfg.get("GOLD_INSTRUMENT", "GOLDTEN")
    ctx.silver_instrument = user_cfg.get("SILVER_INSTRUMENT", "SILVERMIC")
    ctx.crude_instrument = user_cfg.get("CRUDE_INSTRUMENT", "CRUDEOILM")
    
_last_config_mtime = 0.0

def reload_config_if_changed(ctx, base_dir):
    global _last_config_mtime
    path = os.path.join(base_dir, "user_selection.csv")
    try:
        mtime = os.path.getmtime(path)
        if mtime > _last_config_mtime:
            user_cfg = load_user_selection(base_dir)
            apply_config(ctx, user_cfg)
            _last_config_mtime = mtime
            logging.info("Config hot-reloaded from user_selection.csv")
    except Exception as e:
        logging.error(f"Config reload failed: {e}")