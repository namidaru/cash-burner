from __future__ import annotations

import json
import math
import os
import time
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, asdict
from typing import Any, Deque, Dict, Tuple

from kis_orders import buyable_cash, sellable_qty, order_cash, account_buying_power, account_cash_snapshot
from notifier import DiscordNotifier
from quote_basic import load_cache


def _f(x: Any, d: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return d


def _fmt_won(v: Any) -> str:
    if v is None:
        return "-"
    try:
        n = float(v)
    except Exception:
        return "-"
    if not math.isfinite(n):
        return "-"
    return f"{int(round(n)):,}원"


@dataclass
class Position:
    qty: int
    entry_price: float
    entry_ts: float
    max_price: float
    max_pnl_pct: float = 0.0
    min_pnl_pct: float = 0.0
    score: float = 0.0
    reasons: list[str] | None = None
    sell_fail_count: int = 0
    atr_pct: float = 0.0       # PROMPT 1: 진입 시 30초 변동성
    partial_taken: bool = False  # PROMPT 1: 부분익실 완료 여부
    last_price: float = 0.0    # PROMPT 7: 시장상태 추적용
    fill_confirmed: bool = True


CASH_REFRESH_INTERVAL = float(os.getenv("CASH_REFRESH_INTERVAL_SEC", "30"))

class EngineSimple:
    """실전용 단순 모멘텀 엔진: 단일 매수 경로 / 단일 청산 경로."""

    def __init__(self):
        # files
        self.ledger_file = os.getenv("LEDGER_FILE", os.path.join("data", "ledger_real.csv"))
        self.state_file = os.getenv("POSITION_STATE_FILE", os.path.join("data", "positions_simple.json"))
        self.watchlist_file = os.getenv("WATCHLIST_FILE", os.path.join("data", "watchlist.txt"))
        self.radar_inject_file = os.getenv("WATCH_RADAR_INJECT_FILE", os.path.join("data", "radar_inject.txt"))
        self.signal_diag_file = os.getenv("SIGNAL_DIAG_FILE", os.path.join("data", "signal_diag.log"))
        self.runtime_status_file = os.getenv("RUNTIME_STATUS_FILE", os.path.join("data", "runtime_status.json"))

        # buy / score
        self.position_pct = float(os.getenv("POSITION_PCT", "0.30"))
        self.max_positions = max(1, int(os.getenv("MAX_POSITIONS", "3")))
        self.entry_score_threshold = float(os.getenv("ENTRY_SCORE_THRESHOLD", "80"))  # 85→80: score_pass_rate 개선
        self.entry_score_strong = float(os.getenv("ENTRY_SCORE_STRONG", "120"))
        self.entry_block_dayrise_pct = float(os.getenv("ENTRY_BLOCK_DAYRISE_PCT", "12.0"))  # 7.0→12.0: 급등주 타겟(+3~15%) 맞게 상향
        self.entry_hard_dayrise_block_pct = float(os.getenv("ENTRY_HARD_DAYRISE_BLOCK_PCT", "20.0"))  # 18.0→20.0

        self.buy_ret10_min = float(os.getenv("BUY_RET10_MIN", "0.20"))  # 0.30→0.20: 저가 종목 gate_ret10 완화
        self.buy_ret5_min = float(os.getenv("BUY_RET5_MIN", "0.15"))
        self.buy_trv10_min = float(os.getenv("BUY_TRV10_MIN", "20000000"))  # 30M→20M: gate_trv10 완화 (장초반 50%는 별도 적용중)
        self.buy_ofi_min = float(os.getenv("BUY_OFI_MIN", "1.4"))
        self.buy_imb_min = float(os.getenv("BUY_IMB_MIN", "0.60"))
        self.buy_spread_max_bps = float(os.getenv("BUY_SPREAD_MAX_BPS", "35"))
        self.pullback_pct = float(os.getenv("PULLBACK_PCT", "1.2"))        # 0.65→1.2: 급등주 눌림은 1~2% 현실적
        self.pullback_rebound_pct = float(os.getenv("PULLBACK_REBOUND_PCT", "0.30"))  # 0.18→0.30: 반등 확인 강화
        self.vi_guard_pct = float(os.getenv("VI_GUARD_PCT", "0.25"))

        # sell (4 rules only)
        self.stop_loss_pct = float(os.getenv("STOP_LOSS_PCT", "2.5"))
        self.take_profit_pct = float(os.getenv("TAKE_PROFIT_PCT", "3.5"))
        self.trail_arm_pct = float(os.getenv("TRAIL_ARM_PCT", "2.5"))  # 3.0→2.5: 이익보호 조기 발동
        self.trail_drop_pct = float(os.getenv("TRAIL_DROP_PCT", "1.8"))  # 2.2→1.8: 이익 반납 축소
        self.max_hold_sec = float(os.getenv("MAX_HOLD_SEC", "600"))  # 240→600: 눌림목 반등은 10분 정도 필요
        self.exit_grace_sec = float(os.getenv("EXIT_GRACE_SEC", "5.0"))
        self.take_profit_grace_sec = float(os.getenv("TAKE_PROFIT_GRACE_SEC", "5.0"))
        self.stop_loss_early_grace_sec = float(os.getenv("STOP_LOSS_EARLY_GRACE_SEC", "3.0"))
        self.stop_loss_early_relax_mult = float(os.getenv("STOP_LOSS_EARLY_RELAX_MULT", "1.6"))
        self.stop_loss_emergency_pct = float(os.getenv("STOP_LOSS_EMERGENCY_PCT", "4.5"))
        self.cooldown_sec = float(os.getenv("COOLDOWN_SEC", "90"))
        # PROMPT 3: 청산 이유별 쿨다운
        self.cooldown_stop_sec = float(os.getenv("COOLDOWN_STOP_SEC", "180"))
        self.cooldown_panic_sec = float(os.getenv("COOLDOWN_PANIC_SEC", "300"))
        self.cooldown_take_sec = float(os.getenv("COOLDOWN_TAKE_SEC", "30"))  # 45→30: 익절 후 연속 급등 재진입 기회 확보
        self.cooldown_trail_sec = float(os.getenv("COOLDOWN_TRAIL_SEC", "60"))
        self.cooldown_maxhold_sec = float(os.getenv("COOLDOWN_MAXHOLD_SEC", "30"))
        self.loss_streak_block_enabled = os.getenv("LOSS_STREAK_BLOCK", "1") == "1"
        # PROMPT 1: 부분익실
        self.partial_take_enabled = os.getenv("PARTIAL_TAKE_ENABLED", "1") == "1"  # 0→1: 부분익실 기본 활성화
        # PROMPT 7: 장초반 거래대금 완화
        self.morning_trv_relax = os.getenv("MORNING_TRV_RELAX", "1") == "1"
        self.daily_loss_limit_pct = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "3.0"))
        self._daily_loss_base_cash: float | None = None
        self._daily_realized_pnl: float = 0.0
        self._trading_halted: bool = False
        self._last_trading_day: str = ""

        self.entry_chase_penalty_dayrise_pct = float(os.getenv("ENTRY_CHASE_PENALTY_DAYRISE_PCT", "15.0"))  # entry_block(12%)보다 높아야 함

        self.health_check_sec = float(os.getenv("HEALTH_CHECK_SEC", "1800"))
        self.health_cash_symbol = os.getenv("HEALTH_CASH_SYMBOL", "005930").strip() or "005930"

        # runtime state
        self.ticks: Dict[str, Deque[Tuple[float, float, float]]] = defaultdict(lambda: deque(maxlen=4096))
        self.book: Dict[str, Dict[str, str]] = {}
        self.book_ts: Dict[str, float] = {}
        self.pos: Dict[str, Position] = {}
        self.cooldown_until: Dict[str, float] = {}
        self.watch: set[str] = set()
        self._last_watch_reload_ts = 0.0
        self._last_candidate_log_ts: Dict[str, float] = {}
        self._last_health_ts = 0.0
        self._score_eval_total = 0
        self._score_pass_total = 0
        self._gate_block_counts: Dict[str, int] = defaultdict(int)
        self._last_buy_time = 0.0
        self._last_sell_time = 0.0
        self._last_buy_symbol = ""
        self._last_sell_symbol = ""
        self._recent_events: Deque[Dict[str, Any]] = deque(maxlen=10)
        self._last_runtime_snapshot_ts = 0.0
        self._state_lock = threading.Lock()
        self._last_orderable_cash: float | None = None
        self._last_orderable_cash_ts = 0.0
        # PROMPT 3: 연속손절 추적
        self._loss_streak: Dict[str, int] = defaultdict(int)
        self._loss_streak_blocked: set[str] = set()
        # PROMPT 2: VI 해제 추적
        self._vi_clear_ts: Dict[str, float] = {}
        self._vi_prev_std: Dict[str, float] = {}
        # PROMPT 7: 시장 하락추세 감지
        self._market_declining: bool = False
        self._market_declining_until: float = 0.0

        self.prev_close_cache = load_cache()
        self.notifier = DiscordNotifier()
        self._init_files()
        self._load_state()
        self._prime_cash_status()

    # ---------- infra ----------
    def _init_files(self):
        for p in (self.ledger_file, self.signal_diag_file, self.state_file, self.runtime_status_file):
            d = os.path.dirname(p)
            if d:
                os.makedirs(d, exist_ok=True)
        if not os.path.exists(self.ledger_file):
            with open(self.ledger_file, "w", encoding="utf-8") as f:
                f.write("ts,action,symbol,qty,price,reason,rt_cd,msg\n")

    def _log_ledger(self, ts_epoch: float, action: str, sym: str, qty: int, price: float, reason: str, rt_cd: str, msg: str):
        safe = (msg or "").replace('"', "")[:240]
        with open(self.ledger_file, "a", encoding="utf-8") as f:
            f.write(f"{ts_epoch:.3f},{action},{sym},{qty},{price:.4f},\"{reason}\",{rt_cd},\"{safe}\"\n")

    def _log_diag(self, ts_epoch: float, sym: str, status: str, detail: str):
        with open(self.signal_diag_file, "a", encoding="utf-8") as f:
            f.write(f"{ts_epoch:.3f},{sym},{status},{detail}\n")

    def _save_state(self):
        rows = {
            "positions": {sym: asdict(p) for sym, p in self.pos.items()},
            "cooldown_until": self.cooldown_until,
            "loss_streak": dict(self._loss_streak),
            "loss_streak_blocked": list(self._loss_streak_blocked),
        }
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)

    def _load_state(self):
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                j = json.load(f)
        except Exception:
            return
        try:
            self.cooldown_until = {str(k): float(v) for k, v in (j.get("cooldown_until") or {}).items()}
        except Exception:
            self.cooldown_until = {}
        try:
            self._loss_streak = defaultdict(int, {k: int(v) for k, v in (j.get("loss_streak") or {}).items()})
        except Exception:
            pass
        try:
            self._loss_streak_blocked = set(j.get("loss_streak_blocked") or [])
        except Exception:
            pass
        dirty = False
        for sym, item in (j.get("positions") or {}).items():
            try:
                qty_state = int(item.get("qty", 0))
                qty_live = max(0, int(sellable_qty(sym)))
                qty_use = min(qty_state, qty_live)
                if qty_use <= 0:
                    self._log_diag(time.time(), sym, "STATE_CLEAN", "ghost_position_removed")
                    dirty = True
                    continue
                self.pos[sym] = Position(
                    qty=qty_use,
                    entry_price=float(item.get("entry_price", 0.0)),
                    entry_ts=max(float(item.get("entry_ts", time.time())), time.time() - self.max_hold_sec),
                    max_price=float(item.get("max_price", item.get("entry_price", 0.0))),
                    max_pnl_pct=float(item.get("max_pnl_pct", 0.0)),
                    min_pnl_pct=float(item.get("min_pnl_pct", 0.0)),
                    score=float(item.get("score", 0.0)),
                    reasons=list(item.get("reasons") or []),
                    atr_pct=float(item.get("atr_pct", 0.0)),
                    partial_taken=bool(item.get("partial_taken", False)),
                    last_price=float(item.get("last_price", 0.0)),
                    fill_confirmed=bool(item.get("fill_confirmed", True)),
                )
                if qty_use != qty_state:
                    dirty = True
            except Exception:
                continue
        if dirty:
            self._save_state()

    def _reload_watchlist(self, ts_epoch: float):
        if (ts_epoch - self._last_watch_reload_ts) < 1.0:
            return
        self._last_watch_reload_ts = ts_epoch
        try:
            new_watch: set[str] = set()
            try:
                with open(self.watchlist_file, "r", encoding="utf-8") as f:
                    new_watch = {ln.strip() for ln in f if ln.strip()}
            except Exception:
                pass
            try:
                with open(self.radar_inject_file, "r", encoding="utf-8") as f:
                    new_watch |= {ln.strip() for ln in f if ln.strip()}
            except Exception:
                pass
            if new_watch:
                with self._state_lock:
                    if new_watch != self.watch:
                        self.prev_close_cache = load_cache()
                    self.watch = new_watch
        except Exception:
            pass

    def _session_weight(self, ts_epoch: float) -> float:
        hhmm = int(time.strftime("%H%M", time.localtime(ts_epoch)))
        if 900 <= hhmm < 920:
            return 1.15   # 장 개시 20분: 급등 포착 골든타임
        if 920 <= hhmm < 930:
            return 1.08
        if 930 <= hhmm < 1100:
            return 1.0
        if 1100 <= hhmm < 1300:
            return 0.95   # 점심 시간대 약화
        if 1300 <= hhmm < 1430:
            return 1.0    # 오후 반등
        if 1430 <= hhmm < 1510:
            return 0.88   # 동시호가 준비
        return 0.85

    def _prev_close(self, sym: str) -> float:
        return float(self.prev_close_cache.get(sym, 0.0) or 0.0)

    def _day_rise_pct(self, sym: str, price: float) -> float:
        pc = self._prev_close(sym)
        if pc <= 0:
            return 0.0
        return (price / pc - 1.0) * 100.0

    def _window_stats(self, dq: Deque[Tuple[float, float, float]], now: float, sec: float) -> tuple[float, float, int]:
        st = now - sec
        base = 0.0
        last = 0.0
        trv = 0.0
        cnt = 0
        first_valid = True
        for t, px, vol in dq:
            if t < st:
                continue
            if first_valid:
                base = px
                first_valid = False
            last = px
            trv += px * vol
            cnt += 1
        if cnt < 2 or base <= 0:
            return 0.0, trv, cnt
        return ((last - base) / base * 100.0), trv, cnt

    def _compute_ofi_window(self, dq: Deque[Tuple[float, float, float]], now: float, sec: float = 10.0) -> float:
        st = now - sec
        buy = 0.0
        sell = 0.0
        prev = None
        for t, px, vol in dq:
            if t < st:
                if px > 0:
                    prev = px
                continue
            if px <= 0 or vol <= 0:
                continue
            if prev is None:
                prev = px
                continue
            if px > prev:
                buy += vol
            elif px < prev:
                sell += vol
            prev = px
        return buy / max(1.0, sell)

    def _imbalance(self, sym: str) -> float:
        ob = self.book.get(sym) or {}
        bid = _f(ob.get("TOTAL_BIDP_RSQN") or ob.get("total_bidp_rsqn"))
        ask = _f(ob.get("TOTAL_ASKP_RSQN") or ob.get("total_askp_rsqn"))
        den = bid + ask
        return (bid / den) if den > 0 else 0.5

    def _depth_ratio(self, sym: str) -> float:
        ob = self.book.get(sym) or {}
        bid = _f(ob.get("BIDP_RSQN1")) + _f(ob.get("BIDP_RSQN2")) + _f(ob.get("BIDP_RSQN3"))
        ask = _f(ob.get("ASKP_RSQN1")) + _f(ob.get("ASKP_RSQN2")) + _f(ob.get("ASKP_RSQN3"))
        if ask <= 0:
            return 1.0
        return bid / ask

    def _spread_bps(self, sym: str, price: float) -> float | None:
        ob = self.book.get(sym) or {}
        ask1 = _f(ob.get("ASKP1") or ob.get("askp1"))
        bid1 = _f(ob.get("BIDP1") or ob.get("bidp1"))
        if ask1 <= 0 or bid1 <= 0:
            return None
        mid = (ask1 + bid1) / 2.0
        if mid <= 0:
            return None
        return (ask1 - bid1) / mid * 10000.0

    def _pullback_rebound(self, dq: Deque[Tuple[float, float, float]], now: float, price: float) -> float:
        # 급등주는 고점이 더 오래 전에 형성됨 — 60초 고점 기준으로 확장
        high60 = max((px for t, px, _ in dq if (now - t) <= 60.0), default=price)
        low10 = min((px for t, px, _ in dq if (now - t) <= 10.0), default=price)
        pulled = low10 <= high60 * (1.0 - max(0.0, self.pullback_pct) / 100.0)
        rebound = price >= low10 * (1.0 + max(0.0, self.pullback_rebound_pct) / 100.0)
        recovery_floor = low10 + (high60 - low10) * 0.5
        return 1.0 if (pulled and rebound and price >= recovery_floor) else 0.0

    def _burst_ratio(self, dq: Deque[Tuple[float, float, float]], now: float) -> float:
        """최근 5초 거래대금 / 직전 5~10초 거래대금 비율."""
        trv5 = sum(px * vol for t, px, vol in dq if (now - t) <= 5.0)
        trv5_prev = sum(px * vol for t, px, vol in dq if 5.0 < (now - t) <= 10.0)
        if trv5_prev <= 0:
            return 1.0
        return trv5 / trv5_prev

    def _safe_order(self, side: str, sym: str, qty: int, ts_epoch: float, price: float, reason: str, ord_dvsn: str = "01", ord_unpr: str = "0") -> Dict[str, Any]:
        try:
            j = order_cash(side, sym, qty, ord_dvsn=ord_dvsn, ord_unpr=ord_unpr)
            self._log_ledger(ts_epoch, side, sym, qty, price, reason, j.get("rt_cd", ""), j.get("msg1", ""))
            return j
        except Exception as e:
            self._log_ledger(ts_epoch, side, sym, qty, price, reason, "EX", f"{type(e).__name__}:{e}")
            return {"rt_cd": "EX", "msg1": str(e)}

    def _is_afterhours_window(self, ts_epoch: float) -> bool:
        hhmm = int(time.strftime("%H%M", time.localtime(ts_epoch)))
        return not (900 <= hhmm < 1530)

    def _confirmed_fill_qty(self, j: Dict[str, Any]) -> int:
        cands = []
        if isinstance(j, dict):
            cands.append(j)
            for rk in ("output", "output1", "output2"):
                out = j.get(rk)
                if isinstance(out, dict):
                    cands.append(out)
                elif isinstance(out, list):
                    cands.extend([it for it in out if isinstance(it, dict)])
        for k in ("tot_ccld_qty", "TOT_CCLD_QTY", "ccld_qty", "CCLD_QTY", "exec_qty", "EXEC_QTY", "filled_qty", "FILLED_QTY"):
            for d in cands:
                v = _f(d.get(k), -1.0)
                if v > 0:
                    return int(v)
        return 0

    def _exit_cooldown(self, reason: str) -> float:
        """청산 이유별 쿨다운 시간 반환."""
        if "panic" in reason:
            return self.cooldown_panic_sec
        if "stop_loss" in reason:
            return self.cooldown_stop_sec
        if "take_profit" in reason or "partial_take" in reason:
            return self.cooldown_take_sec
        if "trail_stop" in reason:
            return self.cooldown_trail_sec
        if "max_hold" in reason:
            return self.cooldown_maxhold_sec
        return self.cooldown_sec

    def _eod_ts(self, ts_epoch: float) -> float:
        """거래일 캘린더 없이 손절 쿨다운 기준으로 대체."""
        return ts_epoch + self.cooldown_stop_sec

    def _update_market_state(self, ts_epoch: float):
        """보유 포지션 평균 pnl 기반 시장 하락추세 감지 (PROMPT 7)."""
        if not self.pos:
            if self._market_declining and ts_epoch > self._market_declining_until:
                self._market_declining = False
            return
        pnl_vals = [
            (p.last_price / p.entry_price - 1.0) * 100.0
            for p in self.pos.values()
            if p.last_price > 0 and p.entry_price > 0
        ]
        if not pnl_vals:
            return
        avg_pnl = sum(pnl_vals) / len(pnl_vals)
        if avg_pnl <= -1.0:
            self._market_declining = True
            self._market_declining_until = ts_epoch + 300.0
        elif ts_epoch > self._market_declining_until:
            self._market_declining = False

    def _prime_cash_status(self):
        now = time.time()
        self._refresh_orderable_cash(now, use_fallback=False)
        self._refresh_orderable_cash(now, use_fallback=True)
        if self._daily_loss_base_cash is None and self._last_orderable_cash is not None:
            self._daily_loss_base_cash = self._last_orderable_cash
            self._log_diag(
                time.time(), "ENGINE", "DAILY_BASE",
                f"base_cash={self._daily_loss_base_cash:.0f}"
            )

    def _refresh_orderable_cash(self, ts_epoch: float, use_fallback: bool = True) -> float | None:
        if (ts_epoch - self._last_orderable_cash_ts) < CASH_REFRESH_INTERVAL and self._last_orderable_cash is not None:
            return self._last_orderable_cash
        orderable = None
        try:
            snap = account_cash_snapshot()
        except Exception:
            snap = {}

        if isinstance(snap, dict):
            for k in ("orderable", "ord_psbl_cash"):
                v = _f(snap.get(k), -1.0)
                if v > 0:
                    orderable = v
                    break

        if orderable is None and use_fallback:
            try:
                bp = float(account_buying_power(symbol=self.health_cash_symbol, ord_dvsn="01", price="0"))
                if bp > 0:
                    orderable = bp
            except Exception:
                orderable = None

        # 초기 비정상 표기 방지: 매우 작은 fallback 값은 버리고 마지막 정상값 유지
        if orderable is not None and orderable >= 10000:
            self._last_orderable_cash = orderable
            self._last_orderable_cash_ts = ts_epoch
        elif orderable is not None and self._last_orderable_cash is None and not use_fallback:
            # account_cash_snapshot이 실제로 작은 값을 주는 경우는 그대로 허용
            self._last_orderable_cash = orderable
            self._last_orderable_cash_ts = ts_epoch

        return self._last_orderable_cash

    # ---------- core strategy ----------
    def score_symbol(self, sym: str, price: float, ts_epoch: float) -> tuple[float, list[str], Dict[str, float]]:
        dq = self.ticks[sym]
        ret10, trv10, _ = self._window_stats(dq, ts_epoch, 10.0)
        ret5, _, _ = self._window_stats(dq, ts_epoch, 5.0)
        _, trv30, _ = self._window_stats(dq, ts_epoch, 30.0)
        ofi = self._compute_ofi_window(dq, ts_epoch, 10.0)
        imb = self._imbalance(sym)
        depth = self._depth_ratio(sym)
        spread_bps_raw = self._spread_bps(sym, price)
        spread_bps = -1.0 if spread_bps_raw is None else spread_bps_raw
        dayrise = self._day_rise_pct(sym, price)
        accel = trv10 / max(1.0, trv30 / 3.0)
        pull_rebound = self._pullback_rebound(dq, ts_epoch, price)

        vi_std = self._vi_prev_std.get(sym, 0.0)
        vi_gap = abs(price - vi_std) / vi_std * 100.0 if vi_std > 0 else 999.0

        # ---- positive groups ----
        # 눌림목 반등 시 ret10 마이너스로 점수 크게 깎이는 것 방지
        ret10_for_score = max(ret10, 0.0) if pull_rebound > 0.0 else ret10
        momentum_score = (ret10_for_score * 45.0) + (ret5 * 25.0)
        liquidity_score = (math.log1p(max(0.0, trv10)) * 4.0) + (max(0.0, accel - 1.0) * 12.0)
        ofi_boost = min(35.0, max(0.0, ofi - 1.0) * 18.0)
        imbalance_boost = max(-22.0, min(22.0, (imb - 0.5) * 90.0))
        depth_boost = max(-16.0, min(16.0, (depth - 1.0) * 16.0))
        orderflow_score = ofi_boost + imbalance_boost + depth_boost
        structure_score = pull_rebound * 10.0

        # PROMPT 2: 거래량 폭발 감지
        burst_ratio = self._burst_ratio(dq, ts_epoch)
        burst_score = min(20.0, max(0.0, burst_ratio - 2.0) * 10.0)

        # PROMPT 2: VI 해제 직후 부스트 (3~15초)
        vi_clear_boost = 0.0
        vi_clear_ts = self._vi_clear_ts.get(sym, 0.0)
        if vi_clear_ts > 0 and 3.0 <= (ts_epoch - vi_clear_ts) <= 15.0:
            vi_clear_boost = 15.0

        positive_total = momentum_score + liquidity_score + orderflow_score + structure_score + burst_score + vi_clear_boost
        score_pos = positive_total * self._session_weight(ts_epoch)

        # ---- penalties ----
        penalty_dayrise = 0.0
        if dayrise > self.entry_block_dayrise_pct:
            # soft penalty: 과열 구간 진입을 억제하되 완전 무력화는 방지
            penalty_dayrise = min(34.0, (dayrise - self.entry_block_dayrise_pct) * 4.2)

        penalty_vi = 0.0
        if vi_gap <= self.vi_guard_pct:
            penalty_vi = min(12.0, (self.vi_guard_pct - vi_gap + 0.02) * 90.0)

        high20 = max((px for t, px, _ in dq if (ts_epoch - t) <= 20.0), default=price)
        penalty_chase = 0.0
        near_high = (high20 > 0 and price >= high20 * 0.999)
        if near_high and pull_rebound <= 0.0:
            penalty_chase = 5.0 + (7.0 if dayrise >= self.entry_chase_penalty_dayrise_pct else 0.0)
            if ret5 < self.buy_ret5_min:
                penalty_chase += 4.0
            if spread_bps_raw is not None and spread_bps_raw > (self.buy_spread_max_bps * 0.8):
                penalty_chase += min(5.0, (spread_bps_raw - self.buy_spread_max_bps * 0.8) * 0.25)

        penalty_score = penalty_dayrise + penalty_vi + penalty_chase
        score = score_pos - penalty_score

        contrib_pos = {
            "momentum": momentum_score,
            "liquidity": liquidity_score,
            "orderflow": orderflow_score,
            "rebound": structure_score,
        }
        contrib_neg = {
            "dayrise": -penalty_dayrise,
            "vi": -penalty_vi,
            "chase": -penalty_chase,
        }

        reasons = [
            f"mom={momentum_score:.1f}",
            f"liq={liquidity_score:.1f}",
            f"flow={orderflow_score:.1f}",
            f"reb={structure_score:.1f}",
            f"burst={burst_score:.1f}",
            f"pen={penalty_score:.1f}",
        ]
        metrics = {
            "ret10": ret10,
            "ret5": ret5,
            "trv10": trv10,
            "trv30": trv30,
            "trv_accel": accel,
            "ofi": ofi,
            "imb": imb,
            "depth_ratio": depth,
            "spread_bps": spread_bps,
            "dayrise": dayrise,
            "pull_rebound": pull_rebound,
            "vi_gap": vi_gap,
            "burst_ratio": burst_ratio,
            "vi_clear_boost": vi_clear_boost,
            "recent_high": high20,
            "recent_high_gap_pct": ((high20 - price) / high20 * 100.0) if high20 > 0 else 0.0,
            "near_recent_high": 1.0 if near_high else 0.0,
            "score_pos": score_pos,
            "score_pen": penalty_score,
            "contrib_pos": contrib_pos,
            "contrib_neg": contrib_neg,
        }
        return score, reasons, metrics

    def should_buy(self, sym: str, score: float, metrics: Dict[str, float], ts_epoch: float) -> tuple[bool, str]:
        if self._trading_halted:
            return False, "trading_halted"
        if sym not in self.watch:
            return False, "watchlist_out"
        if len(self.pos) >= self.max_positions:
            return False, f"max_positions={len(self.pos)}"
        if ts_epoch < self.cooldown_until.get(sym, 0.0):
            return False, f"cooldown<{self.cooldown_until[sym]-ts_epoch:.0f}s"
        if sym in self._loss_streak_blocked:
            return False, "loss_streak_block"
        if metrics.get("dayrise", 0.0) >= self.entry_hard_dayrise_block_pct:
            return False, "gate_dayrise_hard"
        # PROMPT 7: 장 초반 900~920 거래대금 기준 50% 완화
        hhmm = int(time.strftime("%H%M", time.localtime(ts_epoch)))
        effective_trv_min = self.buy_trv10_min * (0.5 if (self.morning_trv_relax and 900 <= hhmm < 920) else 1.0)
        if metrics.get("trv10", 0.0) < max(1.0, effective_trv_min):
            return False, "gate_trv10"
        if metrics.get("ret10", 0.0) < self.buy_ret10_min:
            # 눌림목 반등 감지 시 ret10 게이트 면제 — 눌림목은 정의상 ret10이 마이너스
            if metrics.get("pull_rebound", 0.0) <= 0.0:
                return False, "gate_ret10"
        spread_bps = metrics.get("spread_bps", -1.0)
        if spread_bps < 0:
            # 장 초반(9:00~9:10) 호가 미도착은 면제 — 급등 포착 골든타임 보호
            hhmm = int(time.strftime("%H%M", time.localtime(ts_epoch)))
            if not (900 <= hhmm < 910):
                return False, "spread_missing"
        if spread_bps > self.buy_spread_max_bps:
            return False, "gate_spread"
        ofi_ok = metrics.get("ofi", 0.0) >= self.buy_ofi_min
        imb_ok = metrics.get("imb", 0.0) >= self.buy_imb_min
        if not (ofi_ok or imb_ok):
            # 장 초반 9:00~9:10 호가/ofi 미도착 시 면제
            if not (900 <= hhmm < 910):
                return False, "gate_ofi_imb"
        # PROMPT 7: 시장 하락추세 시 진입 기준 +15점 강화
        effective_threshold = self.entry_score_threshold + (15.0 if self._market_declining else 0.0)
        if score < effective_threshold:
            if abs(metrics.get("contrib_neg", {}).get("chase", 0.0)) >= 10.0:
                return False, "chase_penalty_dominated"
            return False, "score_too_low"
        return True, "pass"

    def _top_factor_strings(self, metrics: Dict[str, float]) -> tuple[str, str]:
        pos = metrics.get("contrib_pos", {}) or {}
        neg = metrics.get("contrib_neg", {}) or {}
        top_pos = sorted(((k, float(v)) for k, v in pos.items()), key=lambda kv: kv[1], reverse=True)[:3]
        top_neg = sorted(((k, abs(float(v))) for k, v in neg.items() if float(v) < 0), key=lambda kv: kv[1], reverse=True)[:2]
        pos_s = "[" + ",".join(f"{k}+{v:.1f}" for k, v in top_pos) + "]"
        neg_s = "[" + ",".join(f"{k}-{v:.1f}" for k, v in top_neg) + "]"
        return pos_s, neg_s

    def _position_pct_by_score(self, score: float, metrics: Dict[str, float] | None = None) -> tuple[float, float]:
        if score >= self.entry_score_strong:
            base_pct = min(self.position_pct, 0.30)
        elif score >= self.entry_score_threshold + 20.0:
            base_pct = min(self.position_pct, 0.20)
        else:
            base_pct = min(self.position_pct, 0.12)

        if not metrics:
            return base_pct, 1.0

        spread_bps = metrics.get("spread_bps", 99.0)
        imb = metrics.get("imb", 0.5)
        burst = metrics.get("burst_ratio", 1.0)

        spread_mult = 1.0 if spread_bps < 10 else (0.85 if spread_bps < 20 else 0.70)
        imb_mult = 0.85 + min(0.30, (imb - 0.5) * 1.0)
        burst_mult = min(1.25, 1.0 + max(0.0, burst - 1.5) * 0.15)
        quality_mult = spread_mult * imb_mult * burst_mult

        final_pct = max(0.05, min(self.position_pct, base_pct * quality_mult))
        return final_pct, quality_mult

    def enter_position(self, sym: str, price: float, score: float, reasons: list[str], metrics: Dict[str, float], ts_epoch: float):
        if sym in self.pos:  # 중복 진입 방지
            return
        self._last_orderable_cash_ts = 0.0
        orderable_cash = self._refresh_orderable_cash(ts_epoch, use_fallback=True)
        cash = float(orderable_cash or 0.0)
        available_cash_source = "snapshot" if cash > 0 else "none"
        pct, quality_mult = self._position_pct_by_score(score, metrics)
        target_budget = cash * pct
        qty = int(target_budget // max(1.0, price))
        capped_qty_reason = ""
        if qty <= 0 and target_budget > 0:
            capped_qty_reason = "budget_below_lot"
        self._log_diag(
            ts_epoch,
            sym,
            "BUY_CASH",
            f"orderable_cash={orderable_cash if orderable_cash is not None else -1:.0f} effective_cash={cash:.0f} source={available_cash_source} target_budget={target_budget:.0f} qty={qty} cap={capped_qty_reason or '-'}",
        )
        if qty <= 0:
            self._log_diag(ts_epoch, sym, "SKIP", f"cash_short cash={cash:.0f} price={price:.0f} budget={target_budget:.0f} source={available_cash_source}")
            return

        reason = f"score={score:.1f} pct={pct:.2f} qmult={quality_mult:.3f} reasons={'|'.join(reasons[:5])}"
        j = self._safe_order("BUY", sym, qty, ts_epoch, price, reason)
        if j.get("rt_cd") != "0":
            self._log_diag(ts_epoch, sym, "BUY_FAIL", str(j.get("msg1", ""))[:160])
            return

        filled = self._confirmed_fill_qty(j)
        fill_confirmed = True
        if filled <= 0:
            self._log_diag(ts_epoch, sym, "BUY_FILL_UNKNOWN", f"rt_cd={j.get('rt_cd')} msg={j.get('msg1','')}")
            filled = qty
            fill_confirmed = False
        # PROMPT 1: 진입 시 30초 변동성(ATR) 계산
        prices_30s = [px for t, px, _ in self.ticks[sym] if (ts_epoch - t) <= 30.0]
        if len(prices_30s) >= 2:
            mid30 = (max(prices_30s) + min(prices_30s)) / 2.0
            atr_pct = (max(prices_30s) - min(prices_30s)) / max(1.0, mid30) * 100.0
        else:
            atr_pct = 0.0
        self.pos[sym] = Position(qty=filled, entry_price=price, entry_ts=ts_epoch, max_price=price, score=score, reasons=reasons[:5], atr_pct=atr_pct, fill_confirmed=fill_confirmed)
        if self._last_orderable_cash is not None:
            self._last_orderable_cash = max(0.0, self._last_orderable_cash - price * filled)
        self._last_buy_time = ts_epoch
        self._last_buy_symbol = sym
        self._record_event(ts_epoch, "BUY", sym, f"score={score:.1f} qty={qty}")
        self._save_state()
        pos_s, neg_s = self._top_factor_strings(metrics)
        self._log_diag(
            ts_epoch,
            sym,
            "BUY",
            f"score={score:.1f} pos={pos_s} neg={neg_s} gate=trv10={metrics.get('trv10',0.0):.0f}/{self.buy_trv10_min:.0f},ret10={metrics.get('ret10',0.0):.2f}/{self.buy_ret10_min:.2f},ofi={metrics.get('ofi',0.0):.2f}/{self.buy_ofi_min:.2f},imb={metrics.get('imb',0.0):.2f}/{self.buy_imb_min:.2f},spread={metrics.get('spread_bps',0.0):.2f}/{self.buy_spread_max_bps:.2f},pass=1 price={price:.0f} qty={qty} est_notional={price*qty:.0f} cash={cash:.0f} pct={pct:.3f} quality_mult={quality_mult:.3f} metrics=ret5={metrics.get('ret5',0.0):.2f},spread={metrics.get('spread_bps',0.0):.2f},dayrise={metrics.get('dayrise',0.0):.2f},recent_high={metrics.get('recent_high',0.0):.0f},near_high={metrics.get('near_recent_high',0.0):.0f},pull_rebound={metrics.get('pull_rebound',0.0):.0f}",
        )
        self.notifier.send(
            title=f"✅ 단순모멘텀 매수 {sym}",
            color=0x2ECC71,
            lines=[
                f"price={price:,.0f} qty={qty} score={score:.1f}",
                f"dayrise={metrics.get('dayrise',0.0):.2f}% accel={metrics.get('trv_accel',0.0):.2f}",
                f"ofi={metrics.get('ofi',0.0):.2f} imb={metrics.get('imb',0.0):.2f}",
                f"reason: {', '.join(reasons[:5])}",
            ],
        )

    def manage_position(self, sym: str, price: float, ts_epoch: float):
        p = self.pos.get(sym)
        if not p:
            return
        p.last_price = price
        if price > p.max_price:
            p.max_price = price
        pnl_pct = (price / p.entry_price - 1.0) * 100.0 if p.entry_price > 0 else 0.0
        p.max_pnl_pct = max(p.max_pnl_pct, pnl_pct)
        p.min_pnl_pct = min(p.min_pnl_pct, pnl_pct)

        hold_sec = max(0.0, ts_epoch - p.entry_ts)
        reason = ""

        # PROMPT 1: 동적 TP/SL (atr_pct 기반, atr_pct=0이면 고정값 fallback)
        if p.atr_pct > 0:
            dynamic_sl = max(self.stop_loss_pct, min(4.0, p.atr_pct * 1.2))
            dynamic_tp = max(self.take_profit_pct, min(5.0, p.atr_pct * 1.8))  # 6.0/2.0→5.0/1.8: 고변동 종목 TP 상한 완화
            dynamic_trail_drop = max(self.trail_drop_pct, min(3.5, p.atr_pct * 0.9))
        else:
            dynamic_sl = self.stop_loss_pct
            dynamic_tp = self.take_profit_pct
            dynamic_trail_drop = self.trail_drop_pct

        early_stop_pct = abs(dynamic_sl) * max(1.0, self.stop_loss_early_relax_mult)
        if hold_sec < self.stop_loss_early_grace_sec:
            if pnl_pct <= -abs(self.stop_loss_emergency_pct):
                reason = "stop_loss_panic"
            elif pnl_pct <= -early_stop_pct:
                reason = "stop_loss_early"
        elif pnl_pct <= -abs(dynamic_sl):
            reason = "stop_loss_after_grace"

        if not reason and hold_sec >= self.take_profit_grace_sec and pnl_pct >= abs(dynamic_tp):
            reason = "take_profit_after_grace"
        if not reason and hold_sec >= self.exit_grace_sec:
            trail_stop = p.max_price * (1.0 - abs(dynamic_trail_drop) / 100.0)
            if p.max_pnl_pct >= abs(self.trail_arm_pct) and price <= trail_stop:
                reason = "trail_stop"
            elif hold_sec >= self.max_hold_sec:
                reason = "max_hold"

        # PROMPT 1: 부분익실 — 풀청산 조건 없을 때만
        if not reason and self.partial_take_enabled and not p.partial_taken and p.qty >= 2:
            partial_tp_threshold = dynamic_tp * 0.6
            if hold_sec >= self.take_profit_grace_sec and pnl_pct >= partial_tp_threshold and pnl_pct >= p.max_pnl_pct - 0.30:
                partial_qty = p.qty // 2
                if partial_qty > 0:
                    j_partial = self._safe_order("SELL", sym, partial_qty, ts_epoch, price, "partial_take")
                    if j_partial.get("rt_cd") == "0":
                        pnl_partial = (price - p.entry_price) * partial_qty
                        self._daily_realized_pnl += pnl_partial
                        p.qty -= partial_qty
                        p.partial_taken = True
                        if self._last_orderable_cash is not None:
                            self._last_orderable_cash += price * partial_qty
                        self._record_event(ts_epoch, "PARTIAL_SELL", sym, f"qty={partial_qty} pnl={pnl_pct:.2f}%")
                        self._save_state()
                        self._log_diag(ts_epoch, sym, "PARTIAL_SELL", f"qty={partial_qty} remain={p.qty} pnl={pnl_pct:.2f}% threshold={partial_tp_threshold:.2f}% atr={p.atr_pct:.2f}")
                        return

        if not reason:
            return

        qty_sell = max(0, int(sellable_qty(sym)))
        qty = min(qty_sell, p.qty)
        if qty <= 0:
            pnl = (price - p.entry_price) * p.qty
            self._daily_realized_pnl += pnl
            self.pos.pop(sym, None)
            self.cooldown_until[sym] = ts_epoch + self._exit_cooldown(reason)
            self._save_state()
            self._log_diag(ts_epoch, sym, "STATE_CLEAN", f"sellable_qty_zero pnl_est={pnl:.0f}")
            return

        j = self._safe_order("SELL", sym, qty, ts_epoch, price, reason)
        if j.get("rt_cd") != "0":
            p.sell_fail_count += 1
            self._log_diag(ts_epoch, sym, "SELL_FAIL", f"attempt={p.sell_fail_count} {str(j.get('msg1', ''))[:140]}")
            if p.sell_fail_count >= 3:
                pnl = (price - p.entry_price) * p.qty
                self._daily_realized_pnl += pnl
                self._log_ledger(ts_epoch, "SELL", sym, p.qty, price,
                                 f"EVICT_sell_fail_{p.sell_fail_count}", "EVICT", "force_evicted")
                if self._daily_loss_base_cash and self._daily_loss_base_cash > 0:
                    loss_pct = max(0.0, -self._daily_realized_pnl) / self._daily_loss_base_cash * 100.0
                    if not self._trading_halted and loss_pct >= self.daily_loss_limit_pct:
                        self._trading_halted = True
                        self._log_diag(
                            ts_epoch, "ENGINE", "HALT",
                            f"daily_loss_pct={loss_pct:.2f} limit={self.daily_loss_limit_pct:.2f} pnl={self._daily_realized_pnl:.0f}"
                        )
                if self.loss_streak_block_enabled:
                    if "stop_loss" in reason:
                        self._loss_streak[sym] += 1
                    else:
                        self._loss_streak[sym] = 0
                evict_entry_price = p.entry_price
                evict_entry_ts = p.entry_ts
                evict_qty = p.qty
                evict_fail_count = p.sell_fail_count
                self.pos.pop(sym, None)
                if self.loss_streak_block_enabled and self._loss_streak.get(sym, 0) >= 2:
                    self._loss_streak_blocked.add(sym)
                    self.cooldown_until[sym] = self._eod_ts(ts_epoch)
                else:
                    self.cooldown_until[sym] = ts_epoch + self._exit_cooldown(reason)
                self._last_sell_time = ts_epoch
                self._last_sell_symbol = sym
                self._record_event(ts_epoch, "SELL", sym, f"EVICT_{reason}")
                self._save_state()
                pnl_pct_evict = (price / evict_entry_price - 1.0) * 100.0 if evict_entry_price > 0 else 0.0
                hold_sec_evict = ts_epoch - evict_entry_ts
                self._log_diag(
                    ts_epoch, sym, "SELL",
                    f"reason=EVICT_{reason} hold={hold_sec_evict:.1f}s pnl={pnl_pct_evict:.2f}% "
                    f"entry={evict_entry_price:.0f} price={price:.0f} qty={evict_qty}"
                )
                self._log_diag(ts_epoch, sym, "SELL_EVICT", f"evicted sell_fail_count={evict_fail_count}")
            return

        pnl = (price - p.entry_price) * qty
        self._daily_realized_pnl += pnl
        if self._daily_loss_base_cash and self._daily_loss_base_cash > 0:
            loss_pct = max(0.0, -self._daily_realized_pnl) / self._daily_loss_base_cash * 100.0
            if not self._trading_halted and loss_pct >= self.daily_loss_limit_pct:
                self._trading_halted = True
                self._log_diag(
                    ts_epoch, "ENGINE", "HALT",
                    f"daily_loss_pct={loss_pct:.2f} limit={self.daily_loss_limit_pct:.2f} pnl={self._daily_realized_pnl:.0f}"
                )
                self.notifier.send(
                    title="🚨 일일 손실 한도 도달 — 거래 중지",
                    color=0xE74C3C,
                    lines=[
                        f"누적손실={loss_pct:.2f}%  한도={self.daily_loss_limit_pct:.2f}%",
                        f"실현PnL={self._daily_realized_pnl:,.0f}원",
                    ],
                )

        # PROMPT 3: 연속손절 추적
        if self.loss_streak_block_enabled:
            if "stop_loss" in reason:
                self._loss_streak[sym] += 1
            else:
                self._loss_streak[sym] = 0

        # PROMPT 3: 쿨다운 차별화 + 연손절 2회 시 당일 블락
        if self.loss_streak_block_enabled and self._loss_streak.get(sym, 0) >= 2:
            self._loss_streak_blocked.add(sym)
            self.cooldown_until[sym] = self._eod_ts(ts_epoch)
            self._log_diag(ts_epoch, sym, "LOSS_STREAK_BLOCK", f"streak={self._loss_streak[sym]} reason={reason}")
        else:
            self.cooldown_until[sym] = ts_epoch + self._exit_cooldown(reason)

        self.pos.pop(sym, None)
        self._last_sell_time = ts_epoch
        self._last_sell_symbol = sym
        self._record_event(ts_epoch, "SELL", sym, reason)
        self._save_state()

        self._log_diag(
            ts_epoch,
            sym,
            "SELL",
            f"reason={reason} hold={hold_sec:.1f}s pnl={pnl_pct:.2f}% entry={p.entry_price:.0f} price={price:.0f} qty={qty} peak={p.max_pnl_pct:.2f}% min={p.min_pnl_pct:.2f}% atr={p.atr_pct:.2f} dyn_sl={dynamic_sl:.2f} dyn_tp={dynamic_tp:.2f} grace_stop={1 if hold_sec < self.stop_loss_early_grace_sec else 0} grace_take={1 if hold_sec < self.take_profit_grace_sec else 0}",
        )
        self.notifier.send(
            title=f"📉 단순모멘텀 매도 {sym}",
            color=0xE67E22,
            lines=[
                f"reason={reason}",
                f"pnl={pnl_pct:+.2f}% hold={hold_sec:.1f}s",
                f"max={p.max_pnl_pct:+.2f}% min={p.min_pnl_pct:+.2f}%",
            ],
        )

    # ---------- event handlers ----------
    def on_orderbook(self, row: Dict[str, str], ts_epoch: float):
        sym = row.get("MKSC_SHRN_ISCD", "")
        if not sym:
            return
        self.book[sym] = row
        self.book_ts[sym] = ts_epoch

    def on_trade(self, row: Dict[str, str], ts_epoch: float):
        sym = row.get("MKSC_SHRN_ISCD", "")
        price = _f(row.get("STCK_PRPR"))
        vol = _f(row.get("CNTG_VOL"))
        if (not sym) or price <= 0:
            return

        # VI 해제 감지 — VI_STND_PRC는 H0STCNT0(체결) 스키마에만 존재
        _vi_raw = row.get("VI_STND_PRC", "")
        if _vi_raw != "" and _vi_raw is not None:
            vi_std_new = _f(_vi_raw)
            vi_std_prev = self._vi_prev_std.get(sym, -1.0)
            if vi_std_prev > 0 and vi_std_new == 0.0:
                self._vi_clear_ts[sym] = ts_epoch
            self._vi_prev_std[sym] = vi_std_new

        self._reload_watchlist(ts_epoch)

        with self._state_lock:
            in_watch = sym in self.watch
            in_pos = sym in self.pos
        if not in_watch and not in_pos:
            return

        dq = self.ticks[sym]
        dq.append((ts_epoch, price, max(0.0, vol)))
        while dq and (ts_epoch - dq[0][0]) > 360.0:
            dq.popleft()

        if in_pos:
            self.manage_position(sym, price, ts_epoch)
            return

        score, reasons, metrics = self.score_symbol(sym, price, ts_epoch)
        ok, why = self.should_buy(sym, score, metrics, ts_epoch)

        last = self._last_candidate_log_ts.get(sym, 0.0)
        if (ts_epoch - last) >= 1.0:
            self._score_eval_total += 1
            if ok:
                self._score_pass_total += 1
            self._last_candidate_log_ts[sym] = ts_epoch
            status = "PASS" if ok else "DROP"
            pos_s, neg_s = self._top_factor_strings(metrics)
            self._log_diag(
                ts_epoch,
                sym,
                "CAND",
                f"score={score:.1f} pass={1 if ok else 0} why={why} gate=trv10={metrics.get('trv10',0.0):.0f}/{self.buy_trv10_min:.0f},ret10={metrics.get('ret10',0.0):.2f}/{self.buy_ret10_min:.2f},ofi={metrics.get('ofi',0.0):.2f}/{self.buy_ofi_min:.2f},imb={metrics.get('imb',0.0):.2f}/{self.buy_imb_min:.2f},spread={metrics.get('spread_bps',0.0):.2f}/{self.buy_spread_max_bps:.2f} pos={pos_s} neg={neg_s} metrics=ret5={metrics.get('ret5',0.0):.2f},depth={metrics.get('depth_ratio',0.0):.2f},spread={metrics.get('spread_bps',0.0):.2f},dayrise={metrics.get('dayrise',0.0):.2f},recent_high={metrics.get('recent_high',0.0):.0f},pull_rebound={metrics.get('pull_rebound',0.0):.0f}",
            )

        if not ok:
            self._gate_block_counts[why] += 1
            self._record_event(ts_epoch, "DROP", sym, why)
            return

        self.enter_position(sym, price, score, reasons, metrics, ts_epoch)

    def _record_event(self, ts_epoch: float, event: str, sym: str, detail: str = ""):
        self._recent_events.append({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts_epoch)),
            "event": event,
            "symbol": sym,
            "detail": (detail or "")[:120],
        })

    def _operator_summary_runtime(self, watch_n: int, score_pass_rate: float) -> str:
        hard_block = sum(v for k, v in self._gate_block_counts.items() if (not k.startswith("watch")) and k not in {"pass", "score_too_low", "chase_penalty_dominated"})
        watch_ok = watch_n >= max(3, self.max_positions * 2)
        if watch_n < max(3, self.max_positions):
            return "watch_small and scanner_overfiltered"
        if watch_ok and score_pass_rate < 0.10:
            return "watch_ok but score_blocked"
        if score_pass_rate >= 0.10 and hard_block > self._score_pass_total:
            return "scores_ok but hard_gates_blocking"
        if self._last_buy_time > 0 and (time.time() - self._last_buy_time) < 900:
            return "buying_normally"
        return "watch_ok monitoring"

    def _write_runtime_status(self, ts_epoch: float):
        top_gate = sorted(self._gate_block_counts.items(), key=lambda kv: kv[1], reverse=True)[:5]
        score_pass_rate = (self._score_pass_total / max(1, self._score_eval_total))
        payload = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts_epoch)),
            "watch_count": len(self.watch),
            "position_count": len(self.pos),
            "orderable_cash": self._last_orderable_cash,
            "orderable_cash_text": _fmt_won(self._last_orderable_cash),
            "top_gate_blockers": [{"reason": k, "count": int(v)} for k, v in top_gate],
            "score_pass_rate": round(score_pass_rate, 4),
            "last_buy_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._last_buy_time)) if self._last_buy_time > 0 else "",
            "last_sell_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._last_sell_time)) if self._last_sell_time > 0 else "",
            "last_buy_symbol": self._last_buy_symbol,
            "last_sell_symbol": self._last_sell_symbol,
            "recent_events": list(self._recent_events),
            "operator_summary": self._operator_summary_runtime(len(self.watch), score_pass_rate),
            "trading_halted": self._trading_halted,
            "daily_realized_pnl": round(self._daily_realized_pnl, 0),
        }
        try:
            d = os.path.dirname(self.runtime_status_file)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(self.runtime_status_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _send_health(self, ts_epoch: float):
        if self.health_check_sec <= 0:
            return
        if (ts_epoch - self._last_health_ts) < self.health_check_sec:
            return
        self._last_health_ts = ts_epoch

        # 상태는 갱신하되, 장외에는 외부 health 알림을 보내지 않음
        self._refresh_orderable_cash(ts_epoch, use_fallback=True)
        if self._is_afterhours_window(ts_epoch):
            return

        orderable_s = _fmt_won(self._last_orderable_cash)
        lines = [
            f"감시 {len(self.watch)} | 보유 {len(self.pos)}",
            f"주문가능 {orderable_s}",
        ]
        self.notifier.send(title="🩺 Simple Engine Health", color=0x5865F2, lines=lines)

    def _reset_daily_state(self, ts_epoch: float):
        today = time.strftime("%Y%m%d", time.localtime(ts_epoch))
        last = self._last_trading_day
        if last == today:
            return
        self._last_trading_day = today
        if self._trading_halted or self._daily_realized_pnl != 0.0:
            self._log_diag(
                ts_epoch, "ENGINE", "DAILY_RESET",
                f"prev_day={last} pnl={self._daily_realized_pnl:.0f} halted={self._trading_halted}"
            )
        self._trading_halted = False
        self._daily_realized_pnl = 0.0
        self._daily_loss_base_cash = None
        self._loss_streak.clear()
        self._loss_streak_blocked.clear()

    def on_timer(self, ts_epoch: float):
        self._reset_daily_state(ts_epoch)
        self._reload_watchlist(ts_epoch)
        self._refresh_orderable_cash(ts_epoch, use_fallback=True)
        if self._daily_loss_base_cash is None and self._last_orderable_cash is not None:
            self._daily_loss_base_cash = self._last_orderable_cash
            self._log_diag(
                ts_epoch, "ENGINE", "DAILY_BASE",
                f"base_cash={self._daily_loss_base_cash:.0f}"
            )
        self._update_market_state(ts_epoch)
        with self._state_lock:
            pos_items = list(self.pos.items())
        for sym, p in pos_items:
            last_px = p.last_price if p.last_price > 0 else p.entry_price
            if last_px > 0:
                self.manage_position(sym, last_px, ts_epoch)
        self._send_health(ts_epoch)
        if (ts_epoch - self._last_runtime_snapshot_ts) >= 1.0:
            self._last_runtime_snapshot_ts = ts_epoch
            self._write_runtime_status(ts_epoch)
