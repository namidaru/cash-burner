# src/engine_real.py
from __future__ import annotations

import os, time, math, sys, json
from dataclasses import dataclass
from collections import defaultdict, deque
from typing import Dict, Deque, Tuple, Any

from kis_orders import buyable_cash, sellable_qty, order_cash, account_buying_power
from quote_basic import load_cache
from notifier import DiscordNotifier


def _f(x, d=0.0) -> float:
    try:
        return float(x)
    except Exception:
        return d


@dataclass
class Position:
    qty: int
    entry_price: float
    entry_ts: float
    max_price: float
    trail_armed: bool
    hard_stop_since: float = 0.0
    partial_taken: bool = False




class EngineReal:
    def __init__(self):
        self.window_sec = int(os.getenv("WINDOW_SEC", "20"))
        self.orderbook_max_age_sec = float(os.getenv("ORDERBOOK_MAX_AGE_SEC", "2.5"))
        self.min_ticks_for_calc = int(os.getenv("MIN_TICKS_FOR_CALC", "2"))

        # fallback/base
        self.min_ret_pct = float(os.getenv("MIN_RET_PCT", "0.6"))
        self.min_tick_count = int(os.getenv("MIN_TICK_COUNT", "8"))
        self.min_tr_value = float(os.getenv("MIN_TR_VALUE", "0"))
        self.min_imb = float(os.getenv("MIN_IMB", "0.60"))
        self.max_spread_pct = float(os.getenv("MAX_SPREAD_PCT", "0.30"))
        self.confirm_sec = float(os.getenv("CONFIRM_SEC", "1.0"))
        self.cooldown_sec = float(os.getenv("COOLDOWN_SEC", "120"))

        # session presets
        self.session_cfg = {
            "OPEN": {
                "min_ret_pct": float(os.getenv("OPEN_MIN_RET_PCT", "0.12")),
                "min_tr_value": float(os.getenv("OPEN_MIN_TR_VALUE", "90000000")),
                "min_tick_count": int(os.getenv("OPEN_MIN_TICK_COUNT", "12")),
                "min_imb": float(os.getenv("OPEN_MIN_IMB", "0.64")),
                "max_spread_pct": float(os.getenv("OPEN_MAX_SPREAD_PCT", "0.28")),
                "confirm_sec": float(os.getenv("OPEN_CONFIRM_SEC", "0.9")),
                "cooldown_sec": float(os.getenv("OPEN_COOLDOWN_SEC", "180")),
                "vi_like_ret_pct": float(os.getenv("VI_LIKE_RET_PCT_OPEN", "2.5")),
                "spike_10s_min_pct": float(os.getenv("OPEN_SPIKE_10S_MIN_PCT", "0.18")),
                "orderbook_ratio_min": float(os.getenv("OPEN_ORDERBOOK_RATIO_MIN", "1.10")),
            },
            "MID": {
                "min_ret_pct": float(os.getenv("MID_MIN_RET_PCT", "0.05")),
                "min_tr_value": float(os.getenv("MID_MIN_TR_VALUE", "30000000")),
                "min_tick_count": int(os.getenv("MID_MIN_TICK_COUNT", "10")),
                "min_imb": float(os.getenv("MID_MIN_IMB", "0.59")),
                "max_spread_pct": float(os.getenv("MID_MAX_SPREAD_PCT", "0.25")),
                "confirm_sec": float(os.getenv("MID_CONFIRM_SEC", "0.9")),
                "cooldown_sec": float(os.getenv("MID_COOLDOWN_SEC", "120")),
                "vi_like_ret_pct": float(os.getenv("VI_LIKE_RET_PCT_MID", "2.0")),
                "spike_10s_min_pct": float(os.getenv("MID_SPIKE_10S_MIN_PCT", "0.24")),
                "orderbook_ratio_min": float(os.getenv("MID_ORDERBOOK_RATIO_MIN", "1.15")),
            },
            "CLOSE": {
                "min_ret_pct": float(os.getenv("CLOSE_MIN_RET_PCT", "0.12")),
                "min_tr_value": float(os.getenv("CLOSE_MIN_TR_VALUE", "40000000")),
                "min_tick_count": int(os.getenv("CLOSE_MIN_TICK_COUNT", "8")),
                "min_imb": float(os.getenv("CLOSE_MIN_IMB", "0.60")),
                "max_spread_pct": float(os.getenv("CLOSE_MAX_SPREAD_PCT", "0.24")),
                "confirm_sec": float(os.getenv("CLOSE_CONFIRM_SEC", "0.9")),
                "cooldown_sec": float(os.getenv("CLOSE_COOLDOWN_SEC", "180")),
                "vi_like_ret_pct": float(os.getenv("VI_LIKE_RET_PCT_CLOSE", "1.6")),
                "spike_10s_min_pct": float(os.getenv("CLOSE_SPIKE_10S_MIN_PCT", "0.25")),
                "orderbook_ratio_min": float(os.getenv("CLOSE_ORDERBOOK_RATIO_MIN", "1.05")),
            },
        }

        self.vi_guard_pct = float(os.getenv("VI_GUARD_PCT", "0.40"))
        self.vi_cooldown_sec = float(os.getenv("VI_COOLDOWN_SEC", "120"))

        self.position_pct = float(os.getenv("POSITION_PCT", "0.30"))
        self.entry_score_min = float(os.getenv("ENTRY_SCORE_MIN", "120"))
        self.open_entry_score_min = float(os.getenv("OPEN_ENTRY_SCORE_MIN", "150"))
        self.mid_entry_score_min = float(os.getenv("MID_ENTRY_SCORE_MIN", "135"))
        self.close_entry_score_min = float(os.getenv("CLOSE_ENTRY_SCORE_MIN", "125"))
        self.entry_pick_window_sec = float(os.getenv("ENTRY_PICK_WINDOW_SEC", "1.2"))
        self.open_entry_pick_window_sec = float(os.getenv("OPEN_ENTRY_PICK_WINDOW_SEC", "0.6"))
        self.mid_entry_pick_window_sec = float(os.getenv("MID_ENTRY_PICK_WINDOW_SEC", "0.9"))
        self.close_entry_pick_window_sec = float(os.getenv("CLOSE_ENTRY_PICK_WINDOW_SEC", "0.8"))
        spike_raw = float(os.getenv("SPIKE_10S_MIN_PCT", "0.30"))
        self.spike_10s_min_pct = (spike_raw / 100.0) if spike_raw >= 10.0 else spike_raw
        self.burst_ratio_min = float(os.getenv("BURST_RATIO_MIN", "1.12"))
        self.burst_baseline_sec = float(os.getenv("BURST_BASELINE_SEC", "120"))
        self.burst_min_ticks = int(os.getenv("BURST_MIN_TICKS", "10"))
        self.burst_require_baseline = os.getenv("BURST_REQUIRE_BASELINE", "1") == "1"
        self.baseline_ready_bin_ratio = float(os.getenv("BASELINE_READY_BIN_RATIO", "0.5"))
        # Legacy knob kept for backward compatibility; burst baseline now uses bucket history.
        self.tick_history_sec = float(os.getenv("TICK_HISTORY_SEC", str(max(self.window_sec, self.burst_baseline_sec + 20.0))))
        self.bucket_sec = float(os.getenv("BURST_BUCKET_SEC", "10.0"))
        self.bucket_history_sec = float(os.getenv("BURST_BUCKET_HISTORY_SEC", str(self.burst_baseline_sec + 30.0)))
        self.flow_bucket_maxlen = int(os.getenv("FLOW_BUCKET_MAXLEN", str(max(64, math.ceil(self.bucket_history_sec / max(0.1, self.bucket_sec)) + 8))))
        self.first_trade_reset_gap_sec = float(os.getenv("FIRST_TRADE_RESET_GAP_SEC", str(self.burst_baseline_sec + self.bucket_sec)))
        self.candidate_reset_grace_sec = float(os.getenv("CANDIDATE_RESET_GRACE_SEC", "0.6"))
        self.orderbook_ratio_min = float(os.getenv("ORDERBOOK_RATIO_MIN", "1.1"))
        self.depth_ratio_keep_sec = float(os.getenv("DEPTH_RATIO_KEEP_SEC", "1.2"))
        self.depth_ratio_entry_min = float(os.getenv("DEPTH_RATIO_ENTRY_MIN", "1.35"))
        self.ofi_threshold = float(os.getenv("OFI_THRESHOLD", "1.8"))
        self.ofi_min_trv10 = float(os.getenv("OFI_MIN_TRV10", "30000000"))
        self.ofi_min_ret10 = float(os.getenv("OFI_MIN_RET10", "0.2"))
        self.sweep_min_score = float(os.getenv("SWEEP_MIN_SCORE", "0.8"))
        self.sweep_threshold = float(os.getenv("SWEEP_THRESHOLD", "1.2"))
        self.sweep_window_sec = float(os.getenv("SWEEP_WINDOW_SEC", "10.0"))
        self.sweep_min_count = int(os.getenv("SWEEP_MIN_COUNT", "2"))
        self.breakout_hold_sec = float(os.getenv("BREAKOUT_HOLD_SEC", "2.0"))
        self.fake_trv2s_min = float(os.getenv("FAKE_TRV2S_MIN", "10000000"))
        self.fake_depth_min = float(os.getenv("FAKE_DEPTH_MIN", "1.3"))
        self.fake_ofi_min = float(os.getenv("FAKE_OFI_MIN", "1.5"))
        self.fake_pullback_pct = float(os.getenv("FAKE_PULLBACK_PCT", "0.3"))
        self.orderbook_stale_mode = os.getenv("ORDERBOOK_STALE_MODE", "guard").strip().lower()
        self.cum_vol_first_tick_mode = os.getenv("CUM_VOL_FIRST_TICK_MODE", "zero").strip().lower()
        self.max_first_cum_vol = float(os.getenv("MAX_FIRST_CUM_VOL", "0"))

        self.hard_stop_pct = float(os.getenv("HARD_STOP_PCT", "3.5"))
        self.hard_stop_grace_sec = float(os.getenv("HARD_STOP_GRACE_SEC", "12"))
        self.hard_stop_confirm_sec = float(os.getenv("HARD_STOP_CONFIRM_SEC", "1.2"))
        self.trail_arm_pct = float(os.getenv("TRAIL_ARM_PCT", "4.0"))
        self.trail_drop_pct = float(os.getenv("TRAIL_DROP_PCT", "3.5"))

        # Spike-momentum dedicated exits
        self.protect_stop_pct = float(os.getenv("PROTECT_STOP_PCT", "2.8"))
        self.protect_grace_sec = float(os.getenv("PROTECT_GRACE_SEC", "8"))
        self.momentum_exit_ret5 = float(os.getenv("MOMENTUM_EXIT_RET5", "-1.2"))
        self.spike_trail_arm_pct = float(os.getenv("SPIKE_TRAIL_ARM_PCT", "3.0"))
        self.spike_trail_drop_pct = float(os.getenv("SPIKE_TRAIL_DROP_PCT", "2.2"))
        self.partial_take_pct = float(os.getenv("PARTIAL_TAKE_PCT", "3.0"))
        self.partial_take_qty_ratio = float(os.getenv("PARTIAL_TAKE_QTY_RATIO", "0.5"))
        self.exhaustion_high_band = float(os.getenv("EXHAUSTION_HIGH_BAND", "0.998"))
        self.exhaustion_ofi_max = float(os.getenv("EXHAUSTION_OFI_MAX", "1.2"))
        self.liquidity_collapse_depth = float(os.getenv("LIQUIDITY_COLLAPSE_DEPTH", "0.7"))

        self.entry_block_dayrise_pct = float(os.getenv("ENTRY_BLOCK_DAYRISE_PCT", "5.0"))
        self.limitup_gap_take_pct = float(os.getenv("LIMITUP_GAP_TAKE_PCT", "0.85"))

        self.ret_dayrise_add_2 = float(os.getenv("RET_DAYRISE_ADD_2", "0.15"))
        self.ret_dayrise_add_4 = float(os.getenv("RET_DAYRISE_ADD_4", "0.30"))
        self.ret_dayrise_add_7 = float(os.getenv("RET_DAYRISE_ADD_7", "0.50"))
        self.ret10_relax_start = float(os.getenv("RET10_RELAX_START", "0.30"))
        self.ret10_relax_end = float(os.getenv("RET10_RELAX_END", "0.60"))
        self.ret10_relax_max = float(os.getenv("RET10_RELAX_MAX", "0.12"))

        # Entry anti-chase: buy after a small pullback + rebound instead of buying spike top.
        self.pullback_entry_enabled = os.getenv("PULLBACK_ENTRY_ENABLED", "1") == "1"
        self.pullback_pct = float(os.getenv("PULLBACK_PCT", "0.65"))
        self.pullback_rebound_pct = float(os.getenv("PULLBACK_REBOUND_PCT", "0.18"))
        self.pullback_wait_sec = float(os.getenv("PULLBACK_WAIT_SEC", "12"))
        self.entry_slip_cap_bps = float(os.getenv("ENTRY_SLIP_CAP_BPS", "12"))
        self.entry_use_limit_price = os.getenv("ENTRY_USE_LIMIT_PRICE", "0") == "1"

        # Early-score (상한가급 전조 점수): hard filter를 줄이고 품질을 점수화.
        self.early_score_enabled = os.getenv("EARLY_SCORE_ENABLED", "1") == "1"
        self.early_score_min = float(os.getenv("EARLY_SCORE_MIN", "118"))
        self.open_early_score_min = float(os.getenv("OPEN_EARLY_SCORE_MIN", "126"))
        self.mid_early_score_min = float(os.getenv("MID_EARLY_SCORE_MIN", "118"))
        self.close_early_score_min = float(os.getenv("CLOSE_EARLY_SCORE_MIN", "112"))
        self.early_trv_short_sec = float(os.getenv("EARLY_TRV_SHORT_SEC", "60"))
        self.early_trv_long_sec = float(os.getenv("EARLY_TRV_LONG_SEC", "300"))
        self.early_dd_ref_pct = float(os.getenv("EARLY_DD_REF_PCT", "1.2"))
        self.early_new_high_cooldown_sec = float(os.getenv("EARLY_NEW_HIGH_COOLDOWN_SEC", "10"))
        self.early_new_high_window_sec = float(os.getenv("EARLY_NEW_HIGH_WINDOW_SEC", "300"))
        self.early_imb_keep_sec = float(os.getenv("EARLY_IMB_KEEP_SEC", "2.0"))
        self.early_imb_keep_min = float(os.getenv("EARLY_IMB_KEEP_MIN", "0.65"))
        self.early_spread_ref_pct = float(os.getenv("EARLY_SPREAD_REF_PCT", "0.25"))

        self.kill_switch_file = os.getenv("KILL_SWITCH_FILE", os.path.join("data", "kill.switch"))
        self.ledger_file = os.getenv("LEDGER_FILE", os.path.join("data", "ledger_real.csv"))
        self.position_state_file = os.getenv("POSITION_STATE_FILE", os.path.join("data", "positions_real.json"))
        self.auto_position_log_file = os.getenv("AUTO_POSITION_LOG_FILE", os.path.join("data", "auto_positions_real.csv"))
        self.afterhours_enabled = os.getenv("AFTERHOURS_ENABLED", "1") == "1"

        self.signal_diag_file = os.getenv("SIGNAL_DIAG_FILE", os.path.join("data", "signal_diag.log"))
        self.signal_diag_sec = float(os.getenv("SIGNAL_DIAG_SEC", "20"))
        self._last_diag_ts: Dict[str, float] = {}

        self.prev_close_cache = load_cache()
        self.notifier = DiscordNotifier()

        self.health_check_sec = float(os.getenv("HEALTH_CHECK_SEC", "1800"))
        self.ws_stale_sec = float(os.getenv("WS_STALE_SEC", "20"))
        self._last_health_ts = 0.0
        self._health_signal_hits = 0
        self._health_order_tries = 0
        self._health_failures = 0
        self.buy_fail_cooldown_sec = float(os.getenv("BUY_FAIL_COOLDOWN_SEC", "30"))
        self.buy_fail_state_ttl_sec = float(os.getenv("BUY_FAIL_STATE_TTL_SEC", "1800"))
        self._buy_fail_by_symbol = {}
        self.health_cash_symbol = os.getenv("HEALTH_CASH_SYMBOL", "005930").strip() or "005930"
        self._last_buyable_cash = 0.0
        self._last_buyable_cash_ts = 0.0
        self.cash_check_retry_sec = float(os.getenv("CASH_CHECK_RETRY_SEC", "30"))
        self.trade_ready = False
        self.trade_block_reason = "startup_cash_unchecked"
        self._last_cash_check_ts = 0.0
        self._lat_sum = 0.0
        self._lat_cnt = 0
        self._lat_max = 0.0
        self._nobuy_reason_counts: Dict[str, int] = defaultdict(int)

        self.day_key = ""
        self.day_started = False
        self.day_closed = False
        self.day_buy_count = 0
        self.day_sell_count = 0
        self.day_win_count = 0
        self.day_loss_count = 0
        self.day_realized_pnl = 0.0
        self.day_cum_pnl = 0.0
        self.day_peak_pnl = 0.0
        self.day_mdd = 0.0
        self.day_best = None
        self.day_worst = None
        self.ws_last_event_ts = 0.0

        self.book: Dict[str, Dict[str, str]] = {}
        self.book_ts: Dict[str, float] = {}
        self.ticks: Dict[str, Deque[Tuple[float, float, float]]] = defaultdict(lambda: deque(maxlen=5000))
        self.flow_buckets: Dict[str, Deque[Tuple[float, float, int]]] = defaultdict(lambda: deque(maxlen=self.flow_bucket_maxlen))
        self.last_trade_vol: Dict[str, float] = {}
        self.symbol_first_trade_ts: Dict[str, float] = {}
        self.buy_vol: Dict[str, float] = defaultdict(float)
        self.sell_vol: Dict[str, float] = defaultdict(float)
        self.last_trade_price: Dict[str, float] = {}
        self.max_price: Dict[str, float] = {}
        self.partial_taken: set[str] = set()
        self.pos: Dict[str, Position] = {}
        self.loaded_carry_positions = 0
        self.last_entry_ts: Dict[str, float] = {}
        self.candidate_since: Dict[str, float] = {}
        self.candidate_peak_price: Dict[str, float] = {}
        self.candidate_pullback_seen: Dict[str, bool] = {}
        self.retest_peak_price: Dict[str, float] = {}
        self.retest_pullback_seen: Dict[str, bool] = {}
        self.retest_ready: Dict[str, bool] = {}
        self.ignition_ts: Dict[str, float] = {}
        self.ignition_price: Dict[str, float] = {}
        self.pb_seen: Dict[str, bool] = {}
        self.pb_low: Dict[str, float] = {}
        self.rebreak_ready: Dict[str, bool] = {}
        self.breakout_ts: Dict[str, float] = {}
        self.breakout_price: Dict[str, float] = {}
        self.imb_samples: Dict[str, Deque[Tuple[float, float]]] = defaultdict(lambda: deque(maxlen=1024))
        self.depth_ratio_samples: Dict[str, Deque[Tuple[float, float]]] = defaultdict(lambda: deque(maxlen=1024))
        self.sweep_samples: Dict[str, Deque[Tuple[float, float]]] = defaultdict(lambda: deque(maxlen=1024))
        self.new_high_events: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=256))
        self.new_high_last_ts: Dict[str, float] = {}
        self.vi_last_ts: Dict[str, float] = {}
        self._score_pick_bucket_start = 0.0
        self._score_pick_candidates: list[Dict[str, Any]] = []
        self.score_pick_top_n = max(1, int(os.getenv("SCORE_PICK_TOP_N", "3")))

        self._init_ledger()
        self._init_auto_position_log()
        self._init_diag()
        self._load_positions_state()
        self._verify_startup_cash_or_block()


    def _account_buying_power(self) -> float:
        return account_buying_power(symbol=self.health_cash_symbol, ord_dvsn="01", price="0")

    def _buyable_cash(self, sym: str) -> float:
        return buyable_cash(sym, ord_dvsn="01", price="0")

    def _sellable_qty(self, sym: str) -> int:
        return int(sellable_qty(sym))

    def _tick_size(self, price: float) -> float:
        if price < 2000:
            return 1.0
        if price < 5000:
            return 5.0
        if price < 20000:
            return 10.0
        if price < 50000:
            return 50.0
        if price < 200000:
            return 100.0
        if price < 500000:
            return 500.0
        return 1000.0

    def _verify_startup_cash_or_block(self):
        now = time.time()
        try:
            cash = self._account_buying_power()
            self.trade_ready = cash > 0
            self.trade_block_reason = "" if self.trade_ready else f"cash_non_positive:{cash:.0f}"
            self._last_buyable_cash = cash
            self._last_buyable_cash_ts = now
            self._last_cash_check_ts = now
        except Exception as e:
            self.trade_ready = False
            self.trade_block_reason = f"cash_parse_fail:{type(e).__name__}:{e}"
            self._last_cash_check_ts = now
            self.notifier.send(
                title="⛔ 거래 시작 차단",
                color=0xE74C3C,
                lines=[
                    "사유: 주문가능금액 파싱 실패",
                    f"detail: {type(e).__name__}: {e}",
                    "조치: API 응답/계좌설정 확인 후 자동 재시도",
                ],
            )

    def _refresh_trade_ready(self, ts_epoch: float):
        if self.trade_ready:
            return
        if (ts_epoch - self._last_cash_check_ts) < self.cash_check_retry_sec:
            return
        self._last_cash_check_ts = ts_epoch
        try:
            cash = self._account_buying_power()
            self._last_buyable_cash = cash
            self._last_buyable_cash_ts = ts_epoch
            if cash > 0:
                self.trade_ready = True
                self.trade_block_reason = ""
                self.notifier.send(
                    title="✅ 거래 시작 허용",
                    color=0x2ECC71,
                    lines=[f"주문가능금액 확인: {cash:,.0f}원"],
                )
            else:
                self.trade_block_reason = f"cash_non_positive:{cash:.0f}"
        except Exception as e:
            self.trade_block_reason = f"cash_parse_fail:{type(e).__name__}:{e}"

    def _session_name(self, ts_epoch: float) -> str:
        hhmm = int(time.strftime("%H%M", time.localtime(ts_epoch)))
        if 900 <= hhmm < 930:
            return "OPEN"
        if 930 <= hhmm < 1430:
            return "MID"
        if self._is_afterhours_window(ts_epoch):
            return "AFTER"
        return "CLOSE"

    def _params(self, ts_epoch: float) -> Dict[str, Any]:
        s = self._session_name(ts_epoch)
        return self.session_cfg.get(s, {})

    def _normalize_pct_input(self, v: float) -> float:
        return (v / 100.0) if v >= 10.0 else v

    def _init_ledger(self):
        d = os.path.dirname(self.ledger_file)
        if d:
            os.makedirs(d, exist_ok=True)
        if not os.path.exists(self.ledger_file):
            with open(self.ledger_file, "w", encoding="utf-8") as f:
                f.write("ts,action,symbol,qty,price,reason,rt_cd,msg\n")

    def _init_diag(self):
        d = os.path.dirname(self.signal_diag_file)
        if d:
            os.makedirs(d, exist_ok=True)
        if not os.path.exists(self.signal_diag_file):
            with open(self.signal_diag_file, "w", encoding="utf-8") as f:
                f.write("ts,symbol,session,price,ret,tick_count,trv,imb,spread,dayrise,status,detail\n")


    def _init_auto_position_log(self):
        d = os.path.dirname(self.auto_position_log_file)
        if d:
            os.makedirs(d, exist_ok=True)
        if not os.path.exists(self.auto_position_log_file):
            with open(self.auto_position_log_file, "w", encoding="utf-8") as f:
                f.write("ts,event,symbol,qty,entry_price,ref_price,pnl_pct,note\n")

    def _log_auto_position(self, ts_epoch: float, event: str, sym: str, p: Position, ref_price: float = 0.0, note: str = ""):
        safe_note = (note or "").replace('"', "")[:200]
        pnl_pct = ((ref_price / p.entry_price - 1.0) * 100.0) if (p.entry_price > 0 and ref_price > 0) else 0.0
        with open(self.auto_position_log_file, "a", encoding="utf-8") as f:
            f.write(
                f"{ts_epoch:.3f},{event},{sym},{int(p.qty)},{float(p.entry_price):.4f},{float(ref_price):.4f},{pnl_pct:.3f},\"{safe_note}\"\n"
            )

    def _kill_active(self) -> bool:
        try:
            return os.path.exists(self.kill_switch_file)
        except Exception:
            return False

    def _log(self, ts_epoch: float, action: str, sym: str, qty: int, price: float, reason: str, rt_cd: str, msg: str):
        safe = (msg or "").replace('"', "")[:200]
        with open(self.ledger_file, "a", encoding="utf-8") as f:
            f.write(f"{ts_epoch:.3f},{action},{sym},{qty},{price:.4f},\"{reason}\",{rt_cd},\"{safe}\"\n")

    def _log_signal_diag(
        self,
        ts_epoch: float,
        sym: str,
        price: float,
        ret: float,
        tick_count: int,
        trv: float,
        imb: float,
        spread: float,
        dayrise: float,
        status: str,
        detail: str,
    ):
        last = self._last_diag_ts.get(sym, 0.0)
        if (ts_epoch - last) < self.signal_diag_sec:
            return
        self._last_diag_ts[sym] = ts_epoch
        ses = self._session_name(ts_epoch)
        with open(self.signal_diag_file, "a", encoding="utf-8") as f:
            f.write(
                f"{ts_epoch:.3f},{sym},{ses},{price:.4f},{ret:.3f},{tick_count},{trv:.0f},{imb:.3f},{spread:.3f},{dayrise:.3f},{status},{detail}\n"
            )


    def _latest_trade_price(self, sym: str) -> float:
        dq = self.ticks.get(sym)
        if dq and len(dq) > 0:
            return float(dq[-1][1])
        return 0.0

    def _auto_holdings_lines(self, max_items: int = 4) -> list[str]:
        if not self.pos:
            return ["자동매매 보유: 없음"]
        total_qty = sum(p.qty for p in self.pos.values())
        lines = [f"자동매매 보유: {len(self.pos)}종목 / {total_qty:,}주"]
        ranked = []
        for sym, p in self.pos.items():
            now = self._latest_trade_price(sym)
            if now <= 0:
                now = p.entry_price
            pnl_pct = (now / p.entry_price - 1.0) * 100.0 if p.entry_price > 0 else 0.0
            ranked.append((sym, p, now, pnl_pct))
        ranked.sort(key=lambda x: abs(x[3]), reverse=True)
        for sym, p, now, pnl_pct in ranked[:max_items]:
            lines.append(f"- {sym} {p.qty}주 | {p.entry_price:,.0f}→{now:,.0f} ({pnl_pct:+.2f}%)")
        remain = len(ranked) - max_items
        if remain > 0:
            lines.append(f"- 외 {remain}종목")
        return lines

    def _note_no_buy(self, ts_epoch: float, sym: str, price: float, ret: float, tick_count: int, trv: float, imb: float, spread: float, dayrise: float, detail: str):
        key = (detail or "unknown").split(" | ", 1)[0]
        self._nobuy_reason_counts[key] += 1
        self._log_signal_diag(ts_epoch, sym, price, ret, tick_count, trv, imb, spread, dayrise, "NO_BUY", detail)

    def _safe_order(self, side: str, sym: str, qty: int, ts_epoch: float, price: float, reason: str, ord_dvsn: str = "01", ord_unpr: str = "0"):
        self._health_order_tries += 1
        try:
            j = order_cash(side, sym, qty, ord_dvsn=ord_dvsn, ord_unpr=ord_unpr)
            self._log(ts_epoch, side, sym, qty, price, reason, j.get("rt_cd", ""), j.get("msg1", ""))
            if j.get("rt_cd") != "0":
                self._health_failures += 1
                if side.upper() == "BUY":
                    self._mark_buy_fail(sym, ts_epoch, j.get("msg1", ""))
            else:
                if side.upper() == "BUY":
                    self._clear_buy_fail(sym)
            return j
        except Exception as e:
            self._health_failures += 1
            if side.upper() == "BUY":
                self._mark_buy_fail(sym, ts_epoch, f"order_err:{type(e).__name__}:{e}")
            self._log(ts_epoch, side, sym, qty, price, reason, "EX", f"order_err:{type(e).__name__}:{e}")
            return {"rt_cd": "EX", "msg1": str(e)}

    def _mark_buy_fail(self, sym: str, ts_epoch: float, msg: str):
        self._buy_fail_by_symbol[sym] = (ts_epoch, (msg or "").strip())

    def _clear_buy_fail(self, sym: str):
        self._buy_fail_by_symbol.pop(sym, None)

    def _prune_buy_fail_state(self, ts_epoch: float):
        if self.buy_fail_state_ttl_sec <= 0:
            return
        cutoff = ts_epoch - self.buy_fail_state_ttl_sec
        stale = [sym for sym, (fail_ts, _) in self._buy_fail_by_symbol.items() if fail_ts < cutoff]
        for sym in stale:
            self._buy_fail_by_symbol.pop(sym, None)

    def _is_buy_blocked_after_fail(self, sym: str, ts_epoch: float) -> Tuple[bool, str]:
        if self.buy_fail_cooldown_sec <= 0:
            return False, ""
        state = self._buy_fail_by_symbol.get(sym)
        if not state:
            return False, ""
        fail_ts, fail_msg = state
        remain = self.buy_fail_cooldown_sec - (ts_epoch - fail_ts)
        if remain <= 0:
            self._buy_fail_by_symbol.pop(sym, None)
            return False, ""
        msg = fail_msg or "order_fail"
        return True, f"buy_fail_cooldown<{remain:.1f}s msg={msg[:80]}"

    def _cleanup_symbol_state(self, sym: str):
        self.pos.pop(sym, None)
        self.candidate_since.pop(sym, None)
        self.candidate_peak_price.pop(sym, None)
        self.candidate_pullback_seen.pop(sym, None)
        self.retest_peak_price.pop(sym, None)
        self.retest_pullback_seen.pop(sym, None)
        self.retest_ready.pop(sym, None)
        self.ignition_ts.pop(sym, None)
        self.ignition_price.pop(sym, None)
        self.pb_seen.pop(sym, None)
        self.pb_low.pop(sym, None)
        self.rebreak_ready.pop(sym, None)
        self.breakout_ts.pop(sym, None)
        self.breakout_price.pop(sym, None)
        self.imb_samples.pop(sym, None)
        self.depth_ratio_samples.pop(sym, None)
        self.sweep_samples.pop(sym, None)
        self.new_high_events.pop(sym, None)
        self.new_high_last_ts.pop(sym, None)
        self.last_entry_ts.pop(sym, None)
        self.vi_last_ts.pop(sym, None)
        self._last_diag_ts.pop(sym, None)
        self.book.pop(sym, None)
        self.book_ts.pop(sym, None)
        self.ticks.pop(sym, None)
        self.flow_buckets.pop(sym, None)
        self.last_trade_vol.pop(sym, None)
        self.symbol_first_trade_ts.pop(sym, None)
        self.buy_vol.pop(sym, None)
        self.sell_vol.pop(sym, None)
        self.last_trade_price.pop(sym, None)
        self.max_price.pop(sym, None)
        self.partial_taken.discard(sym)
        self._save_positions_state()

    def _save_positions_state(self):
        d = os.path.dirname(self.position_state_file)
        if d:
            os.makedirs(d, exist_ok=True)
        payload = {
            "updated_at": time.time(),
            "positions": [
                {
                    "symbol": sym,
                    "qty": int(p.qty),
                    "entry_price": float(p.entry_price),
                    "entry_ts": float(p.entry_ts),
                    "max_price": float(p.max_price),
                    "trail_armed": bool(p.trail_armed),
                    "hard_stop_since": float(p.hard_stop_since),
                    "partial_taken": bool(p.partial_taken),
                }
                for sym, p in sorted(self.pos.items())
            ],
        }
        with open(self.position_state_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def _load_positions_state(self):
        if not os.path.exists(self.position_state_file):
            return
        try:
            with open(self.position_state_file, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            return
        raw_positions = payload.get("positions", []) if isinstance(payload, dict) else []
        if not isinstance(raw_positions, list):
            return

        loaded = 0
        dropped = 0
        for item in raw_positions:
            if not isinstance(item, dict):
                continue
            sym = str(item.get("symbol", "")).strip()
            qty = int(_f(item.get("qty"), 0))
            entry_price = _f(item.get("entry_price"), 0.0)
            entry_ts = _f(item.get("entry_ts"), time.time())
            max_price = _f(item.get("max_price"), entry_price)
            trail_armed = bool(item.get("trail_armed", False))
            hard_stop_since = _f(item.get("hard_stop_since"), 0.0)
            partial_taken = bool(item.get("partial_taken", False))
            if not sym or qty <= 0 or entry_price <= 0:
                dropped += 1
                continue

            try:
                qty_live = self._sellable_qty(sym)
            except Exception:
                qty_live = qty
            if qty_live <= 0:
                dropped += 1
                continue
            qty = min(qty, qty_live)
            self.pos[sym] = Position(
                qty=qty,
                entry_price=entry_price,
                entry_ts=entry_ts,
                max_price=max(max_price, entry_price),
                trail_armed=trail_armed,
                hard_stop_since=max(0.0, hard_stop_since),
                partial_taken=partial_taken,
            )
            self._log_auto_position(time.time(), "LOAD", sym, self.pos[sym], note="carry_state_restore")
            loaded += 1

        self.loaded_carry_positions = loaded
        if loaded > 0:
            self.notifier.send(
                title="📦 이월 보유 복구",
                color=0x3498DB,
                lines=[
                    f"복구 종목수: {loaded}개",
                    f"제외 종목수: {dropped}개",
                    f"상태파일: {self.position_state_file}",
                ],
            )
            self._save_positions_state()

    def on_orderbook(self, row: Dict[str, str], ts_epoch: float):
        self._ensure_day_roll(ts_epoch)
        self.ws_last_event_ts = ts_epoch
        self._event_latency_update(ts_epoch)
        sym = row.get("MKSC_SHRN_ISCD", "")
        if sym:
            self.book[sym] = row
            self.book_ts[sym] = ts_epoch

    def _prev_close(self, sym: str) -> float:
        return float(self.prev_close_cache.get(sym, 0.0) or 0.0)

    def _day_rise_pct(self, sym: str, price: float) -> float:
        pc = self._prev_close(sym)
        if pc <= 0:
            return 0.0
        return (price / pc - 1.0) * 100.0

    def _limitup_target(self, sym: str) -> float:
        pc = self._prev_close(sym)
        if pc <= 0:
            return 0.0
        return pc * (1.0 + 0.30 * self.limitup_gap_take_pct)


    def _window_stats_between(self, dq: Deque[Tuple[float, float, float]], start_ts: float, end_ts: float) -> tuple[float, float, int]:
        cnt = 0
        base = 0.0
        last = 0.0
        trv = 0.0
        for t, px, vv in dq:
            if t < start_ts or t >= end_ts:
                continue
            if cnt == 0:
                base = px
            last = px
            trv += px * vv
            cnt += 1
        if cnt < 2:
            return 0.0, trv, cnt
        ret = ((last - base) / base * 100.0) if base > 0 else 0.0
        return ret, trv, cnt

    def _window_stats(self, dq: Deque[Tuple[float, float, float]], ts_epoch: float, sec: float) -> tuple[float, float, int]:
        st = ts_epoch - sec
        return self._window_stats_between(dq, st, ts_epoch)

    def _update_flow_bucket(self, sym: str, ts_epoch: float, price: float, vol: float, is_cum_vol: bool):
        if self.bucket_sec <= 0:
            return
        use_vol = vol
        if is_cum_vol:
            prev = self.last_trade_vol.get(sym)
            if prev is None:
                use_vol = vol if self.cum_vol_first_tick_mode == "raw" else 0.0
                if self.max_first_cum_vol > 0:
                    use_vol = min(use_vol, self.max_first_cum_vol)
            else:
                use_vol = max(0.0, vol - prev)
            self.last_trade_vol[sym] = vol
        bts = math.floor(ts_epoch / self.bucket_sec) * self.bucket_sec
        dq = self.flow_buckets[sym]
        if dq and dq[-1][0] == bts:
            bt, trv, cnt = dq[-1]
            dq[-1] = (bt, trv + (price * use_vol), cnt + 1)
        else:
            dq.append((bts, price * use_vol, 1))
        keep_after = ts_epoch - max(self.bucket_history_sec, self.burst_baseline_sec + self.bucket_sec)
        while dq and dq[0][0] < keep_after:
            dq.popleft()

    def _bucket_flow_stats(self, sym: str, start_ts: float, end_ts: float) -> tuple[float, int, int]:
        trv = 0.0
        ticks = 0
        n = 0
        for bts, btrv, bcnt in reversed(self.flow_buckets.get(sym, ())):
            if bts >= end_ts:
                continue
            if bts < start_ts:
                break
            trv += btrv
            ticks += bcnt
            n += 1
        return trv, ticks, n

    def _depth3_ratio(self, ob: Dict[str, str] | None) -> float:
        if not ob:
            return 0.0
        bid = (
            _f(ob.get("BIDP_RSQN1")) + _f(ob.get("BIDP_RSQN2")) + _f(ob.get("BIDP_RSQN3"))
            + _f(ob.get("bidp_rsqn1")) + _f(ob.get("bidp_rsqn2")) + _f(ob.get("bidp_rsqn3"))
        )
        ask = (
            _f(ob.get("ASKP_RSQN1")) + _f(ob.get("ASKP_RSQN2")) + _f(ob.get("ASKP_RSQN3"))
            + _f(ob.get("askp_rsqn1")) + _f(ob.get("askp_rsqn2")) + _f(ob.get("askp_rsqn3"))
        )
        if ask <= 0:
            return 0.0
        return bid / ask

    def _update_depth_ratio_sample(self, sym: str, ts_epoch: float, depth_ratio: float):
        dq = self.depth_ratio_samples[sym]
        if depth_ratio > 0:
            dq.append((ts_epoch, depth_ratio))
        keep_sec = max(0.2, self.depth_ratio_keep_sec)
        while dq and (ts_epoch - dq[0][0]) > (keep_sec + 0.5):
            dq.popleft()

    def _avg_depth_ratio(self, sym: str, ts_epoch: float, fallback: float) -> float:
        dq = self.depth_ratio_samples[sym]
        keep_sec = max(0.2, self.depth_ratio_keep_sec)
        vals = [v for t, v in dq if (ts_epoch - t) <= keep_sec]
        if vals:
            return sum(vals) / max(1, len(vals))
        return fallback

    def _compute_ofi_window(self, dq: Deque[Tuple[float, float, float]], ts_epoch: float, sec: float = 10.0) -> tuple[float, float, float, float]:
        start_ts = ts_epoch - sec
        buy_vol = 0.0
        sell_vol = 0.0
        trv = 0.0
        prev_price: float | None = None

        for t, px, vol in dq:
            if t < start_ts:
                continue
            if px <= 0 or vol <= 0:
                prev_price = px if px > 0 else prev_price
                continue
            trv += (px * vol)
            if prev_price is None:
                prev_price = px
                continue
            if px > prev_price:
                buy_vol += vol
            elif px < prev_price:
                sell_vol += vol
            prev_price = px

        ofi = buy_vol / max(sell_vol, 1.0)
        return ofi, trv, buy_vol, sell_vol

    def _detect_liquidity_sweep(self, trade_price: float, trade_volume: float, ask1_price: float, ask1_volume: float) -> float:
        if ask1_volume <= 0:
            return 0.0
        if trade_price < ask1_price:
            return 0.0
        sweep_score = trade_volume / max(ask1_volume, 1.0)
        if sweep_score < self.sweep_min_score:
            return 0.0
        return sweep_score

    def _update_sweep_signal(self, sym: str, ts_epoch: float, sweep_score: float) -> tuple[int, float, bool]:
        dq = self.sweep_samples[sym]
        if sweep_score > 0:
            dq.append((ts_epoch, sweep_score))
        keep_sec = max(1.0, self.sweep_window_sec)
        while dq and (ts_epoch - dq[0][0]) > keep_sec:
            dq.popleft()
        count = len(dq)
        max_score = max((sc for _, sc in dq), default=0.0)
        signal = (count >= self.sweep_min_count) and (max_score > self.sweep_threshold)
        return count, max_score, signal

    def _fake_breakout_filter(self, price: float, breakout_price: float, trv_2s: float, depth_ratio: float, ofi: float) -> tuple[bool, int, str]:
        fake_score = 0
        reasons: list[str] = []

        if trv_2s < self.fake_trv2s_min:
            fake_score += 1
            reasons.append(f"trv2s<{self.fake_trv2s_min:.0f}")
        if depth_ratio < self.fake_depth_min:
            fake_score += 1
            reasons.append(f"depth<{self.fake_depth_min:.2f}")
        if ofi < self.fake_ofi_min:
            fake_score += 1
            reasons.append(f"ofi<{self.fake_ofi_min:.2f}")

        pullback_ratio = max(0.0, self.fake_pullback_pct) / 100.0
        if breakout_price > 0 and price < breakout_price * (1.0 - pullback_ratio):
            fake_score += 1
            reasons.append(f"retrace>{self.fake_pullback_pct:.2f}%")

        detail = ",".join(reasons) if reasons else "clean"
        return (fake_score < 2), fake_score, detail

    def _clamp01(self, v: float) -> float:
        return max(0.0, min(1.0, v))

    def _update_early_microstructure(self, sym: str, ts_epoch: float, price: float, imb: float):
        imb_q = self.imb_samples[sym]
        imb_q.append((ts_epoch, imb))
        while imb_q and (ts_epoch - imb_q[0][0]) > max(5.0, self.early_imb_keep_sec + 2.0):
            imb_q.popleft()

        if not self.ticks.get(sym):
            return
        h_5m = max(px for t, px, _ in self.ticks[sym] if (ts_epoch - t) <= self.early_new_high_window_sec) if self.ticks[sym] else price
        if h_5m <= 0:
            return
        if price >= h_5m * 0.9998:
            last_evt = self.new_high_last_ts.get(sym, 0.0)
            if (ts_epoch - last_evt) >= self.early_new_high_cooldown_sec:
                self.new_high_last_ts[sym] = ts_epoch
                self.new_high_events[sym].append(ts_epoch)

    def _calc_early_score(self, sym: str, ts_epoch: float, price: float, spread: float, imb: float, trv_prev: float) -> tuple[float, Dict[str, float]]:
        dq = self.ticks.get(sym, deque())
        _, trv_1m, _ = self._window_stats_between(dq, ts_epoch - self.early_trv_short_sec, ts_epoch)
        _, trv_5m, _ = self._window_stats_between(dq, ts_epoch - self.early_trv_long_sec, ts_epoch)
        trv_5m_avg = trv_5m / max(1.0, self.early_trv_long_sec / max(1.0, self.early_trv_short_sec))
        eps = max(1.0, trv_prev, trv_5m_avg * 0.25)
        trv_ratio = trv_1m / max(eps, trv_5m_avg)
        score_trv = 60.0 * self._clamp01((trv_ratio - 0.6) / 0.6)

        h_5m = price
        if dq:
            h_5m = max(px for t, px, _ in dq if (ts_epoch - t) <= self.early_new_high_window_sec)
        dd = ((h_5m - price) / h_5m) if h_5m > 0 else 1.0
        dd_ref = max(0.2, self.early_dd_ref_pct) / 100.0
        score_dd = 40.0 * self._clamp01((dd_ref - dd) / dd_ref)

        evq = self.new_high_events[sym]
        while evq and (ts_epoch - evq[0]) > self.early_new_high_window_sec:
            evq.popleft()
        new_high_cnt_5m = len(evq)
        score_hh = 30.0 * self._clamp01(new_high_cnt_5m / 3.0)

        imb_q = self.imb_samples[sym]
        keep_vals = [v for t, v in imb_q if (ts_epoch - t) <= self.early_imb_keep_sec]
        if not keep_vals:
            keep_vals = [imb]
        imb_keep_2s = sum(1.0 for v in keep_vals if v >= self.early_imb_keep_min) / max(1, len(keep_vals))
        score_imb = 40.0 * imb_keep_2s

        spr_ref = max(0.05, self.early_spread_ref_pct)
        score_spr = 30.0 * self._clamp01((spr_ref - spread) / spr_ref)

        early_score = score_trv + score_dd + score_hh + score_imb + score_spr
        return early_score, {
            "trv_ratio": trv_ratio,
            "dd": dd * 100.0,
            "new_high_cnt_5m": float(new_high_cnt_5m),
            "imb_keep_2s": imb_keep_2s,
            "score_trv": score_trv,
            "score_dd": score_dd,
            "score_hh": score_hh,
            "score_imb": score_imb,
            "score_spr": score_spr,
        }

    def _entry_score(self, ret: float, ret10: float, tick_count: int, trv: float, imb: float, depth_ratio: float, spread: float, max_spread_pct: float) -> float:
        # 모멘텀은 중요하지만 과대추격을 막기 위해 구간별 감쇄를 둔다.
        ret10_main = min(max(ret10, 0.0), 1.20) * 55.0
        ret10_tail = max(0.0, ret10 - 1.20) * 20.0
        ret_main = min(max(ret, 0.0), 1.00) * 20.0

        # 품질 지표는 가산/감산 모두 반영해 저품질(imb/depth/spread) 고점을 억제한다.
        imb_component = max(-35.0, min(35.0, (imb - 0.5) * 140.0))
        depth_component = max(-20.0, min(20.0, (depth_ratio - 1.0) * 30.0)) if depth_ratio > 0 else -8.0
        spread_component = max(-20.0, min(20.0, (max_spread_pct - spread) * 60.0))

        # 유동성(trv/ticks)은 보조지표로 제한한다.
        trv_component = min(math.log10(max(0.0, trv) / 10000000.0 + 1.0) * 12.0, 18.0)
        tick_component = min(tick_count, 20) * 0.5
        return ret10_main + ret10_tail + ret_main + imb_component + depth_component + spread_component + trv_component + tick_component

    def _notify_buy(
        self,
        sym: str,
        qty: int,
        price: float,
        ret: float,
        tick_count: int,
        trv: float,
        imb: float,
        spread: float,
        dayrise: float,
        score: float,
        ts_epoch: float,
        early_score: float = 0.0,
    ):
        kst = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts_epoch))
        self.notifier.send(
            title=f"✅ 매수 체결 {sym}",
            color=0x2ECC71,
            lines=[
                f"시간: {kst}",
                f"수량/단가: {qty}주 @ {price:,.0f}",
                f"진입 종합점수: {score:.1f} (early={early_score:.1f})",
                f"근거: ret={ret:.3f}% | ticks={tick_count} | trv={trv:,.0f}",
                f"호가: imb={imb:.3f} | spread={spread:.3f}%",
                f"당일등락: {dayrise:.3f}%",
            ] + self._auto_holdings_lines(),
        )

    def _notify_sell(self, sym: str, qty: int, price: float, reason: str, detail: str, p: Position, ts_epoch: float):
        pnl_pct = (price / p.entry_price - 1.0) * 100.0 if p.entry_price > 0 else 0.0
        pnl_amt = (price - p.entry_price) * qty
        self.day_sell_count += 1
        self.day_realized_pnl += pnl_amt
        self.day_cum_pnl += pnl_amt
        self.day_peak_pnl = max(self.day_peak_pnl, self.day_cum_pnl)
        self.day_mdd = max(self.day_mdd, self.day_peak_pnl - self.day_cum_pnl)
        if pnl_amt >= 0:
            self.day_win_count += 1
        else:
            self.day_loss_count += 1
        if (self.day_best is None) or (pnl_amt > self.day_best[1]):
            self.day_best = (sym, pnl_amt, pnl_pct)
        if (self.day_worst is None) or (pnl_amt < self.day_worst[1]):
            self.day_worst = (sym, pnl_amt, pnl_pct)
        hold_sec = max(0.0, ts_epoch - p.entry_ts)
        hold_min = hold_sec / 60.0
        icon = "💰" if pnl_amt >= 0 else "🩸"
        color = 0x3498DB if pnl_amt >= 0 else 0xE74C3C
        kst = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts_epoch))
        self.notifier.send(
            title=f"{icon} 매도 체결 {sym} ({reason})",
            color=color,
            lines=[
                f"시간: {kst}",
                f"수량/단가: {qty}주 @ {price:,.0f}",
                f"진입가: {p.entry_price:,.0f}",
                f"손익: {pnl_amt:,.0f}원 ({pnl_pct:+.3f}%)",
                f"보유시간: {hold_min:.1f}분",
                f"청산사유: {detail}",
            ] + self._auto_holdings_lines(),
        )
    def _ensure_day_roll(self, ts_epoch: float):
        dk = time.strftime("%Y%m%d", time.localtime(ts_epoch))
        if self.day_key == dk:
            return
        self.day_key = dk
        self.day_started = False
        self.day_closed = False
        self.day_buy_count = 0
        self.day_sell_count = 0
        self.day_win_count = 0
        self.day_loss_count = 0
        self.day_realized_pnl = 0.0
        self.day_cum_pnl = 0.0
        self.day_peak_pnl = 0.0
        self.day_mdd = 0.0
        self.day_best = None
        self.day_worst = None
        self._health_signal_hits = 0
        self._health_order_tries = 0
        self._health_failures = 0
        self._lat_sum = 0.0
        self._lat_cnt = 0
        self._lat_max = 0.0
        self._nobuy_reason_counts.clear()
        self._buy_fail_by_symbol.clear()
        self._last_buyable_cash = 0.0
        self._last_buyable_cash_ts = 0.0
        self.cash_check_retry_sec = float(os.getenv("CASH_CHECK_RETRY_SEC", "30"))
        self.trade_ready = False
        self.trade_block_reason = "startup_cash_unchecked"
        self._last_cash_check_ts = 0.0

    def _event_latency_update(self, ts_epoch: float):
        lag = max(0.0, time.time() - ts_epoch)
        self._lat_sum += lag
        self._lat_cnt += 1
        if lag > self._lat_max:
            self._lat_max = lag

    def _memory_mb(self) -> float:
        # 1) Try psutil first when available (cross-platform, current RSS)
        try:
            import psutil  # type: ignore

            return float(psutil.Process(os.getpid()).memory_info().rss) / (1024.0 * 1024.0)
        except Exception:
            pass

        # 2) Windows fallback via ctypes (no extra dependency)
        if os.name == "nt":
            try:
                import ctypes
                from ctypes import wintypes

                class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                    _fields_ = [
                        ("cb", wintypes.DWORD),
                        ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t),
                    ]

                GetCurrentProcess = ctypes.windll.kernel32.GetCurrentProcess
                GetProcessMemoryInfo = ctypes.windll.psapi.GetProcessMemoryInfo

                counters = PROCESS_MEMORY_COUNTERS()
                counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
                ok = GetProcessMemoryInfo(GetCurrentProcess(), ctypes.byref(counters), counters.cb)
                if ok:
                    return float(counters.WorkingSetSize) / (1024.0 * 1024.0)
            except Exception:
                pass

        # 3) Linux /proc fallback
        try:
            with open("/proc/self/status", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        kb = float(line.split()[1])
                        return kb / 1024.0
        except Exception:
            pass

        # 4) Generic POSIX fallback
        try:
            import resource

            rss = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
            if sys.platform == "darwin":
                # macOS reports bytes
                return rss / (1024.0 * 1024.0)
            # Linux and many Unix variants report KiB
            return rss / 1024.0
        except Exception:
            pass

        return 0.0

    def _day_summary_lines(self) -> list[str]:
        total = self.day_sell_count
        win_rate = (self.day_win_count / total * 100.0) if total > 0 else 0.0
        lines = [
            f"오늘 거래: 매수 {self.day_buy_count} / 매도 {self.day_sell_count}",
            f"승률: {win_rate:.1f}% ({self.day_win_count}승 {self.day_loss_count}패)",
            f"실현손익: {self.day_realized_pnl:,.0f}원",
            f"MDD(실현기준): -{self.day_mdd:,.0f}원",
            f"자동매매 보유: {len(self.pos)}종목 / {sum(p.qty for p in self.pos.values()):,}주",
        ]
        if self.day_best is not None:
            lines.append(f"최대 수익 1건: {self.day_best[0]} {self.day_best[1]:,.0f}원 ({self.day_best[2]:+.2f}%)")
        else:
            lines.append("최대 수익: 없음 (매도 0건)")
        if self.day_worst is not None:
            lines.append(f"최대 손실 1건: {self.day_worst[0]} {self.day_worst[1]:,.0f}원 ({self.day_worst[2]:+.2f}%)")
        else:
            lines.append("최대 손실: 없음 (매도 0건)")
        return lines

    def _send_day_start_summary(self, ts_epoch: float):
        if self.day_started:
            return
        self.day_started = True
        kst = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts_epoch))
        self.notifier.send(
            title="✅ 매매 시작 요약",
            color=0x2ECC71,
            lines=[f"시간: {kst}", f"이월 복구: {self.loaded_carry_positions}종목"] + self._day_summary_lines() + self._auto_holdings_lines(),
        )

    def _send_day_close_summary(self, ts_epoch: float):
        if self.day_closed:
            return
        self.day_closed = True
        kst = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts_epoch))
        self.notifier.send(
            title="📌 장 마감 요약",
            color=0xF1C40F,
            lines=[f"시간: {kst}"] + self._day_summary_lines() + self._auto_holdings_lines(),
        )

    def _send_health_check(self, ts_epoch: float):
        if self.health_check_sec <= 0:
            return
        if (ts_epoch - self._last_health_ts) < self.health_check_sec:
            return
        self._last_health_ts = ts_epoch
        if self.ws_last_event_ts > 0:
            ws_gap = max(0.0, ts_epoch - self.ws_last_event_ts)
            ws_state = "정상" if ws_gap <= self.ws_stale_sec else f"지연({ws_gap:.1f}s)"
        else:
            ws_gap = 0.0
            ws_state = "초기화중(이벤트 대기)"
        lat_avg = (self._lat_sum / self._lat_cnt) if self._lat_cnt else 0.0
        try:
            buyable_cash_now = self._account_buying_power()
            self._last_buyable_cash = buyable_cash_now
            self._last_buyable_cash_ts = ts_epoch
            cash_state = f"{buyable_cash_now:,.0f}원"
        except Exception as e:
            if self._last_buyable_cash_ts > 0:
                age = max(0.0, ts_epoch - self._last_buyable_cash_ts)
                cash_state = f"조회실패({type(e).__name__}) / 마지막 {self._last_buyable_cash:,.0f}원 {age:.0f}s전"
            else:
                cash_state = f"조회실패({type(e).__name__})"
        top_reason = "-"
        top_cnt = 0
        if self._nobuy_reason_counts:
            top_reason, top_cnt = max(self._nobuy_reason_counts.items(), key=lambda kv: kv[1])
        total_sell = self.day_sell_count
        win_rate = (self.day_win_count / total_sell * 100.0) if total_sell > 0 else 0.0
        lines = [
            f"WS 상태: {ws_state}",
            f"최근 이벤트: 신호 {self._health_signal_hits} / 주문 {self._health_order_tries} / 실패 {self._health_failures}",
            f"현재 주문가능금액: {cash_state}",
            f"거래가능 상태: {'ON' if self.trade_ready else 'BLOCKED'} {self.trade_block_reason[:80]}",
            f"미체결 주원인: {top_reason} ({top_cnt}회)",
            f"지연: avg {lat_avg:.3f}s / max {self._lat_max:.3f}s",
            f"당일 승률: {win_rate:.1f}% ({self.day_win_count}승 {self.day_loss_count}패)",
        ]
        self.notifier.send(
            title="🩺 정기 헬스체크",
            color=0x5865F2,
            lines=lines + self._auto_holdings_lines(),
        )
        self._health_signal_hits = 0
        self._health_order_tries = 0
        self._health_failures = 0
        self._lat_sum = 0.0
        self._lat_cnt = 0
        self._lat_max = 0.0
        self._nobuy_reason_counts.clear()

    def on_timer(self, ts_epoch: float):
        self._ensure_day_roll(ts_epoch)
        self._refresh_trade_ready(ts_epoch)
        hhmm = int(time.strftime("%H%M", time.localtime(ts_epoch)))
        if 900 <= hhmm <= 910:
            self._send_day_start_summary(ts_epoch)
        if hhmm >= 1530:
            self._send_day_close_summary(ts_epoch)
        self._send_health_check(ts_epoch)

    def _score_pick_update(self, ts_epoch: float, sym: str, score: float, price: float, ret: float, tick_count: int, trv: float, imb: float, spread: float, dayrise: float, ret10: float, baseline_ready: bool, early_score: float = 0.0):
        if self._score_pick_bucket_start <= 0:
            self._score_pick_bucket_start = ts_epoch

        cand = {
            "sym": sym,
            "score": score,
            "price": price,
            "ret": ret,
            "tick_count": tick_count,
            "trv": trv,
            "imb": imb,
            "spread": spread,
            "dayrise": dayrise,
            "ret10": ret10,
            "baseline_ready": baseline_ready,
            "early_score": early_score,
            "session": self._session_name(ts_epoch),
            "ts": ts_epoch,
        }

        self._score_pick_candidates = [c for c in self._score_pick_candidates if str(c.get("sym", "")) != sym]
        self._score_pick_candidates.append(cand)

    def _entry_pick_window_for_ts(self, ts_epoch: float) -> float:
        ses = self._session_name(ts_epoch)
        if ses == "OPEN":
            return self.open_entry_pick_window_sec
        if ses == "MID":
            return self.mid_entry_pick_window_sec
        return self.close_entry_pick_window_sec

    def _score_pick_ready(self, ts_epoch: float) -> bool:
        if self._score_pick_bucket_start <= 0:
            return False
        return (ts_epoch - self._score_pick_bucket_start) >= self._entry_pick_window_for_ts(ts_epoch)

    def _score_pick_take(self) -> list[Dict[str, Any]]:
        cands = sorted(self._score_pick_candidates, key=lambda c: float(c.get("score", -1e18)), reverse=True)
        out = cands[: self.score_pick_top_n]
        self._score_pick_candidates = []
        self._score_pick_bucket_start = 0.0
        return out

    def _try_buy_from_candidate(
        self,
        best: Dict[str, Any],
        ts_epoch: float,
        ofi: float,
        trv_10s: float,
        trv_2s: float,
        sweep_max: float,
        sweep_count: int,
        avg_depth_ratio: float,
        ob_stale: bool,
        ob_age: float,
    ) -> bool:
        sym = str(best.get("sym", ""))
        if not sym or sym in self.pos:
            return False
        price = float(best.get("price", 0.0))
        if price <= 0:
            return False
        ret = float(best.get("ret", 0.0))
        tick_count = int(best.get("tick_count", 0))
        trv = float(best.get("trv", 0.0))
        imb = float(best.get("imb", 0.0))
        spread = float(best.get("spread", 999.0))
        dayrise = float(best.get("dayrise", 0.0))
        score = float(best.get("score", 0.0))
        ret10 = float(best.get("ret10", 0.0))
        baseline_ready = bool(best.get("baseline_ready", False))
        early_score = float(best.get("early_score", 0.0))
        session = str(best.get("session", self._session_name(ts_epoch)))

        dq_best = self.ticks.get(sym, [])
        high_60 = max((px for t, px, _ in dq_best if (ts_epoch - t) < 60.0), default=price)
        if high_60 > 0 and price >= high_60 * 0.998:
            self._note_no_buy(ts_epoch, sym, price, ret, tick_count, trv, imb, spread, dayrise, f"near_60s_high cur={price:.2f} high60={high_60:.2f}")
            return False

        if not self.rebreak_ready.get(sym, False):
            self._note_no_buy(ts_epoch, sym, price, ret, tick_count, trv, imb, spread, dayrise, "rebreak_not_ready")
            return False

        try:
            cash = self._buyable_cash(sym)
        except Exception as e:
            self._log(ts_epoch, "BUY", sym, 0, price, "buyable_cash_error", "EX", f"{type(e).__name__}:{e}")
            self._note_no_buy(ts_epoch, sym, price, ret, tick_count, trv, imb, spread, dayrise, "buyable_cash_error")
            return False
        target = cash * self.position_pct
        qty = int(math.floor(target / price))
        if qty <= 0:
            self._note_no_buy(ts_epoch, sym, price, ret, tick_count, trv, imb, spread, dayrise, f"qty=0 cash={cash:.0f} target={target:.0f}")
            return False

        ord_dvsn, ord_unpr, order_detail = self._build_buy_order(sym, ts_epoch, price)
        if not ord_dvsn:
            self._note_no_buy(ts_epoch, sym, price, ret, tick_count, trv, imb, spread, dayrise, order_detail)
            return False
        buy_reason = (
            f"signal ses={session} ret={ret:.2f} ret10={ret10:.2f} ofi={ofi:.2f} trv10={trv_10s:.0f} trv2s={trv_2s:.0f} sweep_max={sweep_max:.2f} sweep_cnt={sweep_count} imb={imb:.2f} spr={spread:.2f} depth_avg={avg_depth_ratio:.2f} "
            f"dayrise={dayrise:.2f} score={score:.1f} early={early_score:.1f} base_ready={int(baseline_ready)} "
            f"ob_stale={int(ob_stale)} ob_age={ob_age:.2f}s {order_detail}"
        )
        j = self._safe_order("BUY", sym, qty, ts_epoch, price, buy_reason, ord_dvsn=ord_dvsn, ord_unpr=ord_unpr)
        if j.get("rt_cd") != "0":
            self._log_signal_diag(ts_epoch, sym, price, ret, tick_count, trv, imb, spread, dayrise, "BUY_FAIL", j.get("msg1", ""))
            return False

        self.pos[sym] = Position(qty=qty, entry_price=price, entry_ts=ts_epoch, max_price=price, trail_armed=False)
        self._log_auto_position(ts_epoch, "BUY", sym, self.pos[sym], ref_price=price, note=f"score={score:.1f}")
        self._save_positions_state()
        self.last_entry_ts[sym] = ts_epoch
        self.day_buy_count += 1
        self._send_day_start_summary(ts_epoch)
        self.candidate_since.pop(sym, None)
        self.candidate_peak_price.pop(sym, None)
        self.candidate_pullback_seen.pop(sym, None)
        self.retest_peak_price.pop(sym, None)
        self.retest_pullback_seen.pop(sym, None)
        self.retest_ready.pop(sym, None)
        self.ignition_ts.pop(sym, None)
        self.ignition_price.pop(sym, None)
        self.pb_seen.pop(sym, None)
        self.pb_low.pop(sym, None)
        self.rebreak_ready.pop(sym, None)
        self.breakout_ts.pop(sym, None)
        self.breakout_price.pop(sym, None)
        self._log_signal_diag(ts_epoch, sym, price, ret, tick_count, trv, imb, spread, dayrise, "BUY_TRY", f"qty={qty} score={score:.1f}")
        self._notify_buy(sym, qty, price, ret, tick_count, trv, imb, spread, dayrise, score, ts_epoch, early_score)
        return True

    def _orderbook_best_quote(self, sym: str, ts_epoch: float) -> tuple[float, float, float, bool, float]:
        ob = self.book.get(sym)
        ob_age = ts_epoch - self.book_ts.get(sym, 0.0)
        ob_stale = (not ob) or (ob_age > self.orderbook_max_age_sec)
        ask1 = _f(ob.get("ASKP1")) if ob and not ob_stale else 0.0
        bid1 = _f(ob.get("BIDP1")) if ob and not ob_stale else 0.0
        mid = (ask1 + bid1) / 2.0 if (ask1 > 0 and bid1 > 0) else 0.0
        return bid1, ask1, mid, ob_stale, ob_age

    def _build_buy_order(self, sym: str, ts_epoch: float, fallback_price: float) -> tuple[str, str, str]:
        bid1, ask1, mid, ob_stale, ob_age = self._orderbook_best_quote(sym, ts_epoch)
        ord_dvsn = "01"
        ord_unpr = "0"
        px_ref = fallback_price

        if (not ob_stale) and ask1 > 0:
            cap_px = mid * (1.0 + (self.entry_slip_cap_bps / 10000.0)) if mid > 0 else ask1
            if ask1 > cap_px:
                return "", "", f"ask_slip_guard ask1={ask1:.2f} cap={cap_px:.2f} mid={mid:.2f} bps={self.entry_slip_cap_bps:.1f}"
            px_ref = mid if mid > 0 else ask1
            if self.entry_use_limit_price:
                ord_dvsn = "00"
                ord_unpr = str(int(round(ask1)))

        detail = (
            f"order_ts={ts_epoch:.3f} px_ref={px_ref:.2f} best_bid={bid1:.2f} "
            f"best_ask={ask1:.2f} order_type={ord_dvsn} order_price={ord_unpr} ob_stale={int(ob_stale)} ob_age={ob_age:.2f}s"
        )
        return ord_dvsn, ord_unpr, detail

    def _pullback_gate(self, sym: str, price: float, ts_epoch: float) -> tuple[bool, str]:
        if not self.pullback_entry_enabled:
            return True, ""
        c0 = self.candidate_since.get(sym)
        if c0 is None:
            return True, ""
        peak = max(self.candidate_peak_price.get(sym, price), price)
        self.candidate_peak_price[sym] = peak
        age = ts_epoch - c0
        if age > self.pullback_wait_sec:
            return True, f"pullback_timeout age={age:.1f}s"
        pullback_thr = peak * (1.0 - self.pullback_pct / 100.0)
        rebound_thr = peak * (1.0 - self.pullback_rebound_pct / 100.0)
        pulled_before = self.candidate_pullback_seen.get(sym, False)
        pulled_now = price <= pullback_thr
        if pulled_now:
            self.candidate_pullback_seen[sym] = True
            pulled_before = True
        rebounded = pulled_before and (price >= rebound_thr)
        if rebounded:
            return True, f"pullback_ok peak={peak:.2f} pb={self.pullback_pct:.2f}% reb={self.pullback_rebound_pct:.2f}%"
        return False, f"pullback_wait cur={price:.2f} peak={peak:.2f} pb_thr={pullback_thr:.2f} reb_thr={rebound_thr:.2f} pulled={int(pulled_before)}"

    def _retest_gate(self, sym: str, price: float) -> tuple[bool, str]:
        prev_peak = self.retest_peak_price.get(sym, price)
        if price <= prev_peak * 0.995:
            self.retest_pullback_seen[sym] = True

        pulled = self.retest_pullback_seen.get(sym, False)
        ready = self.retest_ready.get(sym, False)
        if pulled and price > prev_peak:
            ready = True
            self.retest_ready[sym] = True

        if price > prev_peak:
            self.retest_peak_price[sym] = price

        if ready:
            return True, f"retest_ok peak={prev_peak:.2f}"
        return False, f"retest_wait cur={price:.2f} peak={prev_peak:.2f} pulled={int(pulled)}"

    def _ignition_rebreak_gate(self, sym: str, ts_epoch: float, price: float, ret10: float, trv10: float, trv_prev: float, dayrise: float, imb: float, spread: float, confirm_sec: float) -> tuple[bool, str]:
        if dayrise >= 6.0:
            return False, f"quality_dayrise cur={dayrise:.2f} max=6.00"
        if imb <= 0.60:
            return False, f"quality_imb cur={imb:.3f} min=0.600"
        if spread >= 0.30:
            return False, f"quality_spread cur={spread:.3f} max=0.300"

        ign_now = (ret10 > 0.22) and (trv10 > (trv_prev * 1.5))
        ign_ts = self.ignition_ts.get(sym)
        if ign_ts is None:
            if not ign_now:
                return False, f"ignition_wait ret10={ret10:.2f} trv10={trv10:.0f} prev={trv_prev:.0f}"
            self.ignition_ts[sym] = ts_epoch
            self.ignition_price[sym] = price
            self.pb_seen[sym] = False
            self.pb_low[sym] = price
            self.rebreak_ready[sym] = False
            return False, "ignition_detected wait_pullback"

        if (ts_epoch - ign_ts) < confirm_sec:
            return False, f"ignition_confirm_wait {confirm_sec:.1f}s"

        peak = max(self.ignition_price.get(sym, price), price)
        self.ignition_price[sym] = peak

        pb_thr = peak * (1.0 - 0.55 / 100.0)
        if price <= pb_thr:
            self.pb_seen[sym] = True
            self.pb_low[sym] = min(self.pb_low.get(sym, price), price)

        if not self.pb_seen.get(sym, False):
            return False, f"micro_pullback_wait cur={price:.2f} peak={peak:.2f} pb_thr={pb_thr:.2f}"

        low = self.pb_low.get(sym, price)
        rb_thr = low * (1.0 + 0.18 / 100.0)
        if price >= rb_thr:
            self.rebreak_ready[sym] = True

        if not self.rebreak_ready.get(sym, False):
            return False, f"rebound_wait cur={price:.2f} low={low:.2f} rb_thr={rb_thr:.2f}"

        if price <= peak:
            return False, f"rebreak_wait cur={price:.2f} peak={peak:.2f}"

        if sym not in self.breakout_ts:
            self.breakout_ts[sym] = ts_epoch
            self.breakout_price[sym] = price

        return True, f"rebreak_ok peak={peak:.2f} low={low:.2f}"

    def compute_ofi(self, sym: str) -> float:
        buy = float(self.buy_vol.get(sym, 0.0))
        sell = float(self.sell_vol.get(sym, 0.0))
        return buy / max(sell, 1.0)

    def compute_features(self, sym: str, dq: Deque[Tuple[float, float, float]], orderbook: Dict[str, str] | None, ts_epoch: float, price: float) -> Dict[str, float]:
        ret10, trv10, _ = self._window_stats(dq, ts_epoch, 10.0)
        ret5, trv5, _ = self._window_stats(dq, ts_epoch, 5.0)
        depth_ratio = self._depth3_ratio(orderbook) if orderbook else 0.0
        ofi = self.compute_ofi(sym)
        return {
            "ret10": ret10,
            "ret5": ret5,
            "trv10": trv10,
            "trv5": trv5,
            "ofi": ofi,
            "depth_ratio": depth_ratio,
            "price": price,
        }

    def position_size(self, price: float, cash: float, position_pct: float | None = None) -> int:
        if price <= 0:
            return 0
        pct = self.position_pct if position_pct is None else max(0.0, position_pct)
        return int((cash * pct) // price)

    def enter_position(
        self,
        sym: str,
        price: float,
        ts_epoch: float,
        feat: Dict[str, float],
        dayrise: float,
        spread: float,
        imb: float,
        position_pct: float | None = None,
        entry_score: float | None = None,
    ) -> bool:
        try:
            cash = self._buyable_cash(sym)
        except Exception as e:
            self._log(ts_epoch, "BUY", sym, 0, price, "buyable_cash_error", "EX", f"{type(e).__name__}:{e}")
            self._note_no_buy(ts_epoch, sym, price, 0, 0, 0, imb, spread, dayrise, "buyable_cash_error")
            return False
        qty = self.position_size(price, cash, position_pct=position_pct)
        if qty <= 0:
            self._note_no_buy(ts_epoch, sym, price, 0, 0, 0, imb, spread, dayrise, f"qty=0 cash={cash:.0f}")
            return False

        ord_dvsn, ord_unpr, order_detail = self._build_buy_order(sym, ts_epoch, price)
        if not ord_dvsn:
            self._note_no_buy(ts_epoch, sym, price, 0, 0, 0, imb, spread, dayrise, order_detail)
            return False

        pct_used = self.position_pct if position_pct is None else max(0.0, position_pct)
        reason = f"entry score ret10={feat['ret10']:.2f} trv10={feat['trv10']:.0f} ofi={feat['ofi']:.2f} depth={feat['depth_ratio']:.2f} pos={pct_used:.2f}"
        if entry_score is not None:
            reason += f" score={entry_score:.1f}"
        j = self._safe_order("BUY", sym, qty, ts_epoch, price, reason, ord_dvsn=ord_dvsn, ord_unpr=ord_unpr)
        if j.get("rt_cd") == "0":
            self.pos[sym] = Position(qty=qty, entry_price=price, entry_ts=ts_epoch, max_price=price, trail_armed=False)
            self.max_price[sym] = price
            self.partial_taken.discard(sym)
            self._save_positions_state()
            self.last_entry_ts[sym] = ts_epoch
            self.day_buy_count += 1
            self._log_signal_diag(ts_epoch, sym, price, feat['ret10'], 0, feat['trv10'], imb, spread, dayrise, "BUY_TRY", f"qty={qty}")
            self._notify_buy(sym, qty, price, feat['ret10'], 0, feat['trv10'], imb, spread, dayrise, 0.0, ts_epoch, 0.0)
            return True
        return False

    def try_entry(self, sym: str, price: float, ts_epoch: float, feat: Dict[str, float], dayrise: float, spread: float, imb: float, sweep_score: float = 0.0) -> bool:
        ret10 = float(feat.get("ret10", 0.0))
        trv10 = float(feat.get("trv10", 0.0))
        if ret10 <= 0.15 or trv10 <= 20_000_000:
            return False

        ofi = float(feat.get("ofi", 0.0))
        depth_ratio = float(feat.get("depth_ratio", 0.0))
        score = 0.0
        score += min(ret10, 1.2) * 50.0
        score += max(0.0, ofi - 1.0) * 20.0
        score += max(0.0, depth_ratio - 1.0) * 30.0
        score += max(0.0, sweep_score) * 25.0
        score += math.log1p(max(0.0, trv10)) * 2.0

        dq = self.ticks.get(sym, deque())
        _, trv2, _ = self._window_stats(dq, ts_epoch, 2.0)
        breakout_ref = max(self.ignition_price.get(sym, price), price)
        self.ignition_price[sym] = breakout_ref
        _fake_ok, fake_score, _fake_detail = self._fake_breakout_filter(price, breakout_ref, trv2, depth_ratio, ofi)
        if fake_score >= 2:
            score -= 40.0

        if score <= 120.0:
            return False

        if score > 170.0:
            position_pct = 0.30
        elif score > 140.0:
            position_pct = 0.20
        else:
            position_pct = 0.10

        return self.enter_position(
            sym,
            price,
            ts_epoch,
            feat,
            dayrise,
            spread,
            imb,
            position_pct=position_pct,
            entry_score=score,
        )

    def _execute_sell_action(self, sym: str, p: Position, price: float, ts_epoch: float, reason: str, detail: str, qty_target: int | None = None, cleanup_if_flat: bool = True) -> bool:
        qty_sell = self._sellable_qty(sym)
        if qty_sell <= 0:
            return False
        target_qty = p.qty if qty_target is None else max(1, qty_target)
        qty_ord = min(qty_sell, target_qty)
        if qty_ord <= 0:
            return False
        j = self._safe_order("SELL", sym, qty_ord, ts_epoch, price, detail)
        if j.get("rt_cd") != "0":
            return False
        self._notify_sell(sym, qty_ord, price, reason, detail, p, ts_epoch)
        self._log_auto_position(ts_epoch, "SELL", sym, p, ref_price=price, note=reason)
        p.qty = max(0, p.qty - qty_ord)
        if cleanup_if_flat and p.qty <= 0:
            self._cleanup_symbol_state(sym)
        else:
            self._save_positions_state()
        return True

    def manage_position(self, sym: str, price: float, ts_epoch: float, feat: Dict[str, float]):
        p = self.pos.get(sym)
        if not p:
            return
        if price > self.max_price.get(sym, p.max_price):
            self.max_price[sym] = price
            p.max_price = max(p.max_price, price)

        entry = p.entry_price
        if (not p.partial_taken) and price >= entry * (1.0 + self.partial_take_pct / 100.0):
            qty_part = int(max(1, math.floor(p.qty * self.partial_take_qty_ratio)))
            if self._execute_sell_action(sym, p, price, ts_epoch, "PARTIAL_TP", "partial_take", qty_target=qty_part, cleanup_if_flat=False):
                p.partial_taken = True
                self.partial_taken.add(sym)
                self._save_positions_state()
                return

        if (ts_epoch - p.entry_ts) > self.protect_grace_sec and price <= entry * (1.0 - self.protect_stop_pct / 100.0):
            self._execute_sell_action(sym, p, price, ts_epoch, "PROTECT_STOP", "protect_stop")
            return

        if feat.get("ret5", 0.0) <= self.momentum_exit_ret5:
            self._execute_sell_action(sym, p, price, ts_epoch, "MOMENTUM_EXIT", f"ret5={feat.get('ret5',0.0):.2f}")
            return

        if feat.get("depth_ratio", 0.0) > 0 and feat.get("depth_ratio", 0.0) < self.liquidity_collapse_depth:
            self._execute_sell_action(sym, p, price, ts_epoch, "LIQUIDITY_EXIT", f"depth={feat.get('depth_ratio',0.0):.2f}")
            return

        high_60 = max((px for t, px, _ in self.ticks.get(sym, []) if (ts_epoch - t) <= 60.0), default=price)
        if high_60 > 0 and price >= high_60 * self.exhaustion_high_band and feat.get("ofi", 0.0) < self.exhaustion_ofi_max:
            self._execute_sell_action(sym, p, price, ts_epoch, "FLOW_EXIT", f"ofi={feat.get('ofi',0.0):.2f}")
            return

        if (not p.trail_armed) and price >= entry * (1.0 + self.spike_trail_arm_pct / 100.0):
            p.trail_armed = True
            self._save_positions_state()
        if p.trail_armed:
            stop = self.max_price.get(sym, p.max_price) * (1.0 - self.spike_trail_drop_pct / 100.0)
            if price <= stop:
                self._execute_sell_action(sym, p, price, ts_epoch, "TRAIL_STOP", f"stop={stop:.2f}")
                return


    def on_trade(self, row: Dict[str, str], ts_epoch: float):
        self._ensure_day_roll(ts_epoch)
        self.ws_last_event_ts = ts_epoch
        self._event_latency_update(ts_epoch)
        self._send_health_check(ts_epoch)
        if self._kill_active():
            return

        sym = row.get("MKSC_SHRN_ISCD", "")
        if not sym:
            return
        self._prune_buy_fail_state(ts_epoch)
        price = _f(row.get("STCK_PRPR"))
        vol = _f(row.get("CNTG_VOL"))
        is_cum_vol = row.get("CNTG_VOL_CUM", "0") == "1"
        if price <= 0:
            return

        holding_position = sym in self.pos

        # manage position after feature computation to keep a single feature path

        if (not holding_position) and (not self.trade_ready):
            self._refresh_trade_ready(ts_epoch)
            if not self.trade_ready:
                self._note_no_buy(ts_epoch, sym, price, 0.0, 0, 0.0, 0.0, 0.0, self._day_rise_pct(sym, price), f"trade_blocked {self.trade_block_reason[:120]}")
                return

        if (not holding_position) and (not self._is_entry_window(ts_epoch)):
            self._note_no_buy(ts_epoch, sym, price, 0.0, 0, 0.0, 0.0, 0.0, self._day_rise_pct(sym, price), "outside_entry_window")
            return

        p = self._params(ts_epoch)
        min_ret_pct = float(p.get("min_ret_pct", self.min_ret_pct))
        min_tick_count = int(p.get("min_tick_count", self.min_tick_count))
        min_tr_value = float(p.get("min_tr_value", self.min_tr_value))
        min_imb = float(p.get("min_imb", self.min_imb))
        max_spread_pct = float(p.get("max_spread_pct", self.max_spread_pct))
        spike_10s_min_pct = self._normalize_pct_input(float(p.get("spike_10s_min_pct", self.spike_10s_min_pct)))
        orderbook_ratio_min = float(p.get("orderbook_ratio_min", self.orderbook_ratio_min))
        confirm_sec = float(p.get("confirm_sec", self.confirm_sec))
        cooldown_sec = float(p.get("cooldown_sec", self.cooldown_sec))
        dayrise = self._day_rise_pct(sym, price)
        if not holding_position:
            if ts_epoch - self.last_entry_ts.get(sym, 0.0) < cooldown_sec:
                self._note_no_buy(ts_epoch, sym, price, 0, 0, 0, 0, 0, 0, f"cooldown<{cooldown_sec:.0f}s")
                return
            if dayrise > 7.0:
                self._note_no_buy(ts_epoch, sym, price, 0, 0, 0, 0, 0, dayrise, "dayrise_hard_block>7")
                return

            blocked, block_detail = self._is_buy_blocked_after_fail(sym, ts_epoch)
            if blocked:
                self._note_no_buy(ts_epoch, sym, price, 0, 0, 0, 0, 0, dayrise, block_detail)
                return

        dq = self.ticks[sym]
        dq.append((ts_epoch, price, vol))
        prev_bucket_ts = self.flow_buckets[sym][-1][0] if self.flow_buckets[sym] else 0.0
        self._update_flow_bucket(sym, ts_epoch, price, vol, is_cum_vol)
        first_ts = self.symbol_first_trade_ts.get(sym)
        if (first_ts is None) or (prev_bucket_ts > 0 and (ts_epoch - prev_bucket_ts) > self.first_trade_reset_gap_sec):
            self.symbol_first_trade_ts[sym] = ts_epoch
        while dq and ts_epoch - dq[0][0] > self.window_sec:
            dq.popleft()

        ret, trv, tick_count = self._window_stats(dq, ts_epoch, float(self.window_sec))
        if tick_count < self.min_ticks_for_calc:
            return

        ret10, _, _ = self._window_stats(dq, ts_epoch, 10.0)
        _, trv_2s, _ = self._window_stats(dq, ts_epoch, 2.0)
        ofi, trv_10s, buy_vol_10s, sell_vol_10s = self._compute_ofi_window(dq, ts_epoch, 10.0)
        cur_start = ts_epoch - self.bucket_sec
        cur_end = ts_epoch
        trv10, ticks10, _cur_bins = self._bucket_flow_stats(sym, cur_start, cur_end)
        baseline_start = ts_epoch - (self.burst_baseline_sec + self.bucket_sec)
        baseline_end = ts_epoch - self.bucket_sec
        trv_hist, ticks_hist, hist_bins = self._bucket_flow_stats(sym, baseline_start, baseline_end)
        baseline_scale = (self.burst_baseline_sec / self.bucket_sec) if (self.burst_baseline_sec > 0 and self.bucket_sec > 0) else 0.0
        trv_prev = (trv_hist / baseline_scale) if baseline_scale > 0 else 0.0
        ticks_prev = (ticks_hist / baseline_scale) if baseline_scale > 0 else 0.0

        ob = self.book.get(sym)
        ob_age = ts_epoch - self.book_ts.get(sym, 0.0)
        ob_stale = (not ob) or (ob_age > self.orderbook_max_age_sec)

        bid_tot = _f(ob.get("TOTAL_BIDP_RSQN")) if ob and not ob_stale else 0.0
        ask_tot = _f(ob.get("TOTAL_ASKP_RSQN")) if ob and not ob_stale else 0.0
        denom = bid_tot + ask_tot
        imb = (bid_tot / denom) if denom > 0 else 0.5

        ask1 = _f(ob.get("ASKP1")) if ob and not ob_stale else 0.0
        bid1 = _f(ob.get("BIDP1")) if ob and not ob_stale else 0.0
        ask1_vol = _f(ob.get("ASKP_RSQN1")) if ob and not ob_stale else 0.0
        mid = (ask1 + bid1) / 2 if (ask1 > 0 and bid1 > 0) else price
        spread = ((ask1 - bid1) / mid * 100.0) if mid > 0 else 999.0
        depth_ratio = self._depth3_ratio(ob if (ob and not ob_stale) else None)
        sweep_score = self._detect_liquidity_sweep(price, vol, ask1, ask1_vol)
        sweep_count, sweep_max, sweep_signal = self._update_sweep_signal(sym, ts_epoch, sweep_score)
        self._update_depth_ratio_sample(sym, ts_epoch, depth_ratio)
        avg_depth_ratio = self._avg_depth_ratio(sym, ts_epoch, depth_ratio)

        vi_std = _f(row.get("VI_STND_PRC"))
        vi_gap = abs(price - vi_std) / vi_std * 100.0 if vi_std > 0 else 999.0

        prev_px = self.last_trade_price.get(sym)
        if prev_px is not None:
            if price > prev_px:
                self.buy_vol[sym] += max(0.0, vol)
            elif price < prev_px:
                self.sell_vol[sym] += max(0.0, vol)
        self.last_trade_price[sym] = price

        feat = self.compute_features(sym, dq, (ob if (ob and not ob_stale) else None), ts_epoch, price)
        if sym in self.pos:
            self.manage_position(sym, price, ts_epoch, feat)
            return

        if self.try_entry(sym, price, ts_epoch, feat, dayrise, spread, imb, sweep_score=sweep_score):
            return

        # Entry path is unified below; keep feature computation shared with position management.
        c0 = self.candidate_since.get(sym)
        session = self._session_name(ts_epoch)

        # 1) Trigger gate: ret/tick/spread + confirm
        trigger_fail = []
        dynamic_ret_min = min_ret_pct
        if dayrise >= 2.0:
            dynamic_ret_min += self.ret_dayrise_add_2
        if dayrise >= 4.0:
            dynamic_ret_min += self.ret_dayrise_add_4
        if dayrise >= 7.0:
            dynamic_ret_min += self.ret_dayrise_add_7

        ret10_relax = 0.0
        if self.ret10_relax_end > self.ret10_relax_start and ret10 > self.ret10_relax_start:
            ratio = min(1.0, (ret10 - self.ret10_relax_start) / (self.ret10_relax_end - self.ret10_relax_start))
            ret10_relax = self.ret10_relax_max * ratio
            dynamic_ret_min = max(min_ret_pct, dynamic_ret_min - ret10_relax)

        if ret < dynamic_ret_min:
            trigger_fail.append(f"ret cur={ret:.2f} thr={dynamic_ret_min:.2f} margin={ret-dynamic_ret_min:.2f} relax={ret10_relax:.2f}")
        if tick_count < min_tick_count:
            trigger_fail.append(f"ticks cur={tick_count} thr={min_tick_count} margin={tick_count-min_tick_count}")
        if ret10 < spike_10s_min_pct:
            trigger_fail.append(f"ret10 cur={ret10:.2f} thr={spike_10s_min_pct:.2f} margin={ret10-spike_10s_min_pct:.2f}")
        if ret10 <= 0:
            trigger_fail.append(f"ret10_non_positive cur={ret10:.2f}")
        if trv <= max(1.0, min_tr_value * 0.25):
            trigger_fail.append(f"trv_too_low cur={trv:.0f} floor={max(1.0, min_tr_value*0.25):.0f}")

        if ret10 <= self.ofi_min_ret10:
            trigger_fail.append(f"ofi_ret10_gate cur={ret10:.2f} min={self.ofi_min_ret10:.2f}")
        if trv_10s < self.ofi_min_trv10:
            trigger_fail.append(f"ofi_trv10_gate cur={trv_10s:.0f} min={self.ofi_min_trv10:.0f}")
        if ofi < self.ofi_threshold:
            trigger_fail.append(f"ofi_gate cur={ofi:.2f} min={self.ofi_threshold:.2f} buy={buy_vol_10s:.0f} sell={sell_vol_10s:.0f}")
        if not sweep_signal:
            trigger_fail.append(f"sweep_gate cnt={sweep_count} max={sweep_max:.2f} min_cnt={self.sweep_min_count} thr={self.sweep_threshold:.2f}")
        min_hist_bins = max(1, math.ceil(baseline_scale * max(0.1, self.baseline_ready_bin_ratio)))
        baseline_ready = (hist_bins >= min_hist_bins) and (ticks_hist >= self.burst_min_ticks)
        if trigger_fail:
            if c0 is None or (ts_epoch - c0) > self.candidate_reset_grace_sec:
                self.candidate_since.pop(sym, None)
            self.candidate_peak_price.pop(sym, None)
            self.candidate_pullback_seen.pop(sym, None)
            self.retest_peak_price.pop(sym, None)
            self.retest_pullback_seen.pop(sym, None)
            self.retest_ready.pop(sym, None)
            self.ignition_ts.pop(sym, None)
            self.ignition_price.pop(sym, None)
            self.pb_seen.pop(sym, None)
            self.pb_low.pop(sym, None)
            self.rebreak_ready.pop(sym, None)
            self.breakout_ts.pop(sym, None)
            self.breakout_price.pop(sym, None)
            primary = trigger_fail[0]
            detail = f"reject={primary} | all={'; '.join(trigger_fail)}"
            self._note_no_buy(ts_epoch, sym, price, ret, tick_count, trv, imb, spread, dayrise, detail)
            return

        if c0 is None:
            self.candidate_since[sym] = ts_epoch
            self.candidate_peak_price[sym] = price
            self.candidate_pullback_seen[sym] = False
            self.retest_peak_price[sym] = price
            self.retest_pullback_seen[sym] = False
            self.retest_ready[sym] = False

        ignition_ok, ignition_detail = self._ignition_rebreak_gate(
            sym, ts_epoch, price, ret10, trv10, trv_prev, dayrise, imb, spread, confirm_sec
        )
        if not ignition_ok:
            self._note_no_buy(ts_epoch, sym, price, ret, tick_count, trv, imb, spread, dayrise, ignition_detail)
            return

        bts = self.breakout_ts.get(sym)
        if bts is None or (ts_epoch - bts) < self.breakout_hold_sec:
            self._note_no_buy(ts_epoch, sym, price, ret, tick_count, trv, imb, spread, dayrise, f"breakout_hold<{self.breakout_hold_sec:.1f}s")
            return

        bpx = self.breakout_price.get(sym, price)
        fake_ok, fake_score, fake_detail = self._fake_breakout_filter(price, bpx, trv_2s, avg_depth_ratio, ofi)
        if not fake_ok:
            self._note_no_buy(ts_epoch, sym, price, ret, tick_count, trv, imb, spread, dayrise, f"fake_breakout score={fake_score} {fake_detail}")
            return

        self._update_early_microstructure(sym, ts_epoch, price, imb)
        early_score, early_meta = self._calc_early_score(sym, ts_epoch, price, spread, imb, trv_prev)
        early_floor = self.early_score_min
        if session == "OPEN":
            early_floor = max(early_floor, self.open_early_score_min)
        elif session == "MID":
            early_floor = max(early_floor, self.mid_early_score_min)
        else:
            early_floor = max(early_floor, self.close_early_score_min)
        if self.early_score_enabled and early_score < early_floor:
            self._note_no_buy(
                ts_epoch,
                sym,
                price,
                ret,
                tick_count,
                trv,
                imb,
                spread,
                dayrise,
                f"early_score {early_score:.1f}<{early_floor:.1f} trvR={early_meta['trv_ratio']:.2f} dd={early_meta['dd']:.2f}% hh={early_meta['new_high_cnt_5m']:.0f} imb2s={early_meta['imb_keep_2s']:.2f}",
            )
            return

        # 2) Guard gate: order-right-before safety checks only
        guard_fail = []
        if ob_stale and self.orderbook_stale_mode == "guard":
            guard_fail.append(f"orderbook_stale age={ob_age:.2f}s max={self.orderbook_max_age_sec:.2f}s")
        if vi_std > 0 and vi_gap <= self.vi_guard_pct:
            guard_fail.append(f"vi_guard cur={vi_gap:.2f} min={self.vi_guard_pct:.2f} margin={vi_gap-self.vi_guard_pct:.2f}")
        depth_gate_min = max(orderbook_ratio_min, self.depth_ratio_entry_min)
        if ret10 >= 0.25 and avg_depth_ratio > 0 and avg_depth_ratio < depth_gate_min:
            guard_fail.append(f"ret10_depth_mismatch ret10={ret10:.2f} depth_avg={avg_depth_ratio:.2f} min={depth_gate_min:.2f}")
        if (not ob_stale) and avg_depth_ratio > 0 and avg_depth_ratio < depth_gate_min:
            guard_fail.append(f"depth_ratio_avg cur={avg_depth_ratio:.2f} min={depth_gate_min:.2f} margin={avg_depth_ratio-depth_gate_min:.2f}")
        if guard_fail:
            primary = guard_fail[0]
            detail = f"reject={primary} | all={'; '.join(guard_fail)}"
            self._note_no_buy(ts_epoch, sym, price, ret, tick_count, trv, imb, spread, dayrise, detail)
            return

        try:
            cash = self._buyable_cash(sym)
        except Exception as e:
            self._log(ts_epoch, "BUY", sym, 0, price, "buyable_cash_error", "EX", f"{type(e).__name__}:{e}")
            self._note_no_buy(ts_epoch, sym, price, ret, tick_count, trv, imb, spread, dayrise, "buyable_cash_error")
            return

        target = cash * self.position_pct
        qty = int(math.floor(target / price))
        if qty <= 0:
            self._note_no_buy(
                ts_epoch, sym, price, ret, tick_count, trv, imb, spread, dayrise, f"qty=0 cash={cash:.0f} target={target:.0f}"
            )
            return

        score_spread = max_spread_pct if (ob_stale and self.orderbook_stale_mode == "guard") else spread
        score_raw = self._entry_score(ret, ret10, tick_count, trv, imb, avg_depth_ratio, score_spread, max_spread_pct)
        score = score_raw + (early_score * 0.35 if self.early_score_enabled else 0.0)
        score_floor = self.entry_score_min
        if session == "OPEN":
            score_floor = max(score_floor, self.open_entry_score_min)
        elif session == "MID":
            score_floor = max(score_floor, self.mid_entry_score_min)
        else:
            score_floor = max(score_floor, self.close_entry_score_min)
        if score < score_floor:
            self._note_no_buy(
                ts_epoch,
                sym,
                price,
                ret,
                tick_count,
                trv,
                imb,
                spread,
                dayrise,
                f"score {score:.1f}<{score_floor:.1f} raw={score_raw:.1f} early={early_score:.1f}",
            )
            return

        self._score_pick_update(ts_epoch, sym, score, price, ret, tick_count, trv, imb, spread, dayrise, ret10, baseline_ready, early_score)
        if not self._score_pick_ready(ts_epoch):
            return
        candidates = self._score_pick_take()
        if not candidates:
            return
        for cand in candidates:
            if self._try_buy_from_candidate(
                cand,
                ts_epoch,
                ofi=ofi,
                trv_10s=trv_10s,
                trv_2s=trv_2s,
                sweep_max=sweep_max,
                sweep_count=sweep_count,
                avg_depth_ratio=avg_depth_ratio,
                ob_stale=ob_stale,
                ob_age=ob_age,
            ):
                return
