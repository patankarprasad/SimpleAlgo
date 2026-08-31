class Context:
    def __init__(self):
        # Angel object
        self.obj = None
        self.angel_pin = None
        # Zerodha objet
        self.kiteobj = None
        # Kotak object
        self.client = None
        self.kotak_mpin = None
        # Client / identity
        self.clientname = None

        # Telegram bot
        self.bot_token = None
        self.chat_id = None

        # KOTAK SCRIPMASTER
        self.token_df = None
        self.nearest_nifty_expiry_date = None
        self.next_nifty_expiry_date = None
        self.far_nifty_expiry_date  = None
        self.nifty_monthend_expiry_date = None
        self.nifty_next_monthend_expiry_date = None
        self.nifty_near_lot = None
        self.nifty_next_lot = None
        self.nifty_monthend_lot = None
        self.nifty_next_monthend_lot = None

        self.sensextoken_df = None
        self.nearest_sensex_expiry_date = None
        self.sensex_lot = None

        self.cmdtytoken_df = None
        self.nearest_gold_expiry_date = None
        self.next_gold_expiry_date = None
        self.goldscripmaster_near_lot = None
        self.goldscripmaster_next_lot = None
        self.gold_near_lot = None
        self.gold_next_lot = None
        self.nearest_silver_expiry_date = None
        self.next_silver_expiry_date = None
        self.silverscripmaster_near_lot = None
        self.silverscripmaster_next_lot = None
        self.silver_near_lot = None
        self.silver_next_lot = None
        self.nearest_crude_expiry_date = None
        self.next_crude_expiry_date = None
        self.crudescripmaster_near_lot = None
        self.crudescripmaster_next_lot = None
        self.crude_near_lot = None
        self.crude_next_lot = None

        # ZERODHA SCRIPMASTER
        self.kite_df = None
        self.kite_cmdty_df = None
        
        self.nearest_kite_gold_expiry_date = None
        self.next_kite_gold_expiry_date = None
        self.nearest_kite_gold_token = None
        self.next_kite_gold_token = None
        self.nearest_kite_silver_expiry_date = None
        self.next_kite_silver_expiry_date = None
        self.nearest_kite_silver_token = None
        self.next_kite_silver_token = None
        self.nearest_kite_crude_expiry_date = None
        self.next_kite_crude_expiry_date = None
        self.nearest_kite_crude_token = None
        self.next_kite_crude_token = None

        # USER_SELECTION FILE
        self.re_entry = None 
        self.limitorder = None
        self.productType = None
        
        # STRATEGY SELECTOR
        self.strategy_ceb = None
        self.strategy_c2eb = None
        self.strategy_peb = None
        self.strategy_spe = None
        self.strategy_rce = None
        self.strategy_exd = None
        self.strategy_ncl = None
        self.strategy_ncs = None
        self.strategy_n2cs = None
        self.strategy_nhf = None
        self.strategy_orh = None
        self.strategy_orl = None
        self.strategy_gold = None
        self.strategy_silver = None
        self.strategy_crude = None    
        
        # STRATEGY PARAMETERS
        self.ceb_lot = None
        self.ceb_psl = None
        self.ceb_itm = None
        self.ceb_cp = None
        self.ceb_entry_limit = None
        self.ceb_exit_limit = None

        self.c2eb_lot = None
        self.c2eb_psl = None
        self.c2eb_itm = None
        self.c2eb_cp = None
        self.c2eb_entry_limit = None
        self.c2eb_exit_limit = None

        self.peb_lot = None
        self.peb_psl = None
        self.peb_itm = None
        self.peb_entry_limit = None
        self.peb_exit_limit = None

        self.spe_lot = None
        self.spe_psl = None
        self.spe_hdg_dist = None
        self.spe_itm = None
        self.spe_entry_limit = None
        self.spe_exit_limit = None

        self.rce_lot = None
        self.rce_psl = None
        self.rce_hdg_dist = None
        self.rce_itm = None
        self.rce_entry_limit = None
        self.rce_exit_limit = None
        
        self.exd_nifty_lot = None
        self.exd_nifty_cp = None
        self.exd_nifty_psl = None
        self.exd_nifty_hdg_dist = None

        self.exd_sensex_lot = None
        self.exd_sensex_cp = None
        self.exd_sensex_psl = None      
        self.exd_sensex_hdg_dist = None

        self.nhf_near_lot = None
        self.nhf_next_lot = None
        self.nhf_hdg_dist = None
        self.nhf_entry_limit = None
        self.nhf_exit_limit = None

        self.ncl_near_lot = None
        self.ncl_next_lot = None
        self.ncl_hdg_dist = None
        self.ncl_entry_limit = None
        self.ncl_exit_limit = None

        self.ncs_near_lot = None
        self.ncs_next_lot = None
        self.ncs_hdg_dist = None
        self.ncs_monthend_lot = None
        self.ncs_next_monthend_lot = None
        self.ncs_itm = None
        self.ncs_cp = None
        self.ncs_entry_limit = None
        self.ncs_exit_limit = None

        self.n2cs_near_lot = None
        self.n2cs_next_lot = None
        self.n2cs_hdg_dist = None
        self.n2cs_monthend_lot = None
        self.n2cs_next_monthend_lot = None
        self.n2cs_itm = None
        self.n2cs_cp = None
        self.n2cs_entry_limit = None
        self.n2cs_exit_limit = None

        self.orb_min_range = None
        self.orh_lot = None
        self.orh_itm = None
        self.orh_cp = None
        self.orh_tgt = None
        self.orh_sl = None

        self.orl_lot = None
        self.orl_itm = None
        self.orl_cp = None
        self.orl_tgt = None
        self.orl_sl = None
        
        self.gold_instrument = None
        self.gold_entry_limit = None
        self.gold_exit_limit = None
        self.silver_instrument = None
        self.silver_entry_limit = None
        self.silver_exit_limit = None
        self.crude_instrument = None
        self.cruce_entry_limit = None
        self.crude_exit_limit = None

        # Arbitrary timed strategy tracker
        self.executed_times = {} # this is helpful to run time based strategies in future.