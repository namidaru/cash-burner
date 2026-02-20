# src/engine_real.py
from __future__ import annotations

import os, time, math
from dataclasses import dataclass
from collections import defaultdict, deque
from typing import Dict, Deque, Tuple, Any

from kis_orders import buyable_cash, sellable_qty, order_cash
from quote_basic import load_cache


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


class EngineReal:
    def __init__(self):
        self.window_sec = int(os.getenv("WINDOW_SEC", "10"))
        self.orderbook_max_age_sec = float(os.getenv("ORDERBOOK_MAX_AGE_SEC", "1.0"))
        self.min_ticks_for_calc = int(os.getenv("MIN_TICKS_FOR_CALC", "2"))

        # fallback/base
        self.min_ret_pct = float(os.getenv("MIN_RET_PCT", "0.6"))
        self.min_tick_count = int(os.getenv("MIN_TICK_COUNT", "10"))
        self.min_tr_value = float(os.getenv("MIN_TR_VALUE", "0"))
        self.min_imb = float(os.getenv("MIN_IMB", "0.60"))
        self.max_spread_pct = float(os.getenv("MAX_SPREAD_PCT", "0.30"))
        self.confirm_sec = float(os.getenv("CONFIRM_SEC", "1.0"))
        self.cooldown_sec = float(os.getenv("COOLDOWN_SEC", "120"))

        # session presets
        self.session_cfg = {
            "OPEN": {
                "min_ret_pct": float(os.getenv("OPEN_MIN_RET_PCT", "0.90")),
                "min_tr_value": float(os.getenv("OPEN_MIN_TR_VALUE", "120000000")),
                "min_tick_count": int(os.getenv("OPEN_MIN_TICK_COUNT", "16")),
                "min_imb": float(os.getenv("OPEN_MIN_IMB", "0.64")),
                "max_spread_pct": float(os.getenv("OPEN_MAX_SPREAD_PCT", "0.22")),
                "confirm_sec": float(os.getenv("OPEN_CONFIRM_SEC", "1.2")),
                "cooldown_sec": float(os.getenv("OPEN_COOLDOWN_SEC", "180")),
                "vi_like_ret_pct": float(os.getenv("VI_LIKE_RET_PCT_OPEN", "2.5")),
            },
            "MID": {
                "min_ret_pct": float(os.getenv("MID_MIN_RET_PCT", "0.70")),
                "min_tr_value": float(os.getenv("MID_MIN_TR_VALUE", "50000000")),
                "min_tick_count": int(os.getenv("MID_MIN_TICK_COUNT", "12")),
                "min_imb": float(os.getenv("MID_MIN_IMB", "0.62")),
                "max_spread_pct": float(os.getenv("MID_MAX_SPREAD_PCT", "0.25")),
                "confirm_sec": float(os.getenv("MID_CONFIRM_SEC", "1.0")),
                "cooldown_sec": float(os.getenv("MID_COOLDOWN_SEC", "120")),
                "vi_like_ret_pct": float(os.getenv("VI_LIKE_RET_PCT_MID", "2.0")),
            },
            "CLOSE": {
                "min_ret_pct": float(os.getenv("CLOSE_MIN_RET_PCT", "0.60")),
                "min_tr_value": float(os.getenv("CLOSE_MIN_TR_VALUE", "80000000")),
                "min_tick_count": int(os.getenv("CLOSE_MIN_TICK_COUNT", "10")),
                "min_imb": float(os.getenv("CLOSE_MIN_IMB", "0.60")),
                "max_spread_pct": float(os.getenv("CLOSE_MAX_SPREAD_PCT", "0.20")),
                "confirm_sec": float(os.getenv("CLOSE_CONFIRM_SEC", "1.2")),
                "cooldown_sec": float(os.getenv("CLOSE_COOLDOWN_SEC", "180")),
                "vi_like_ret_pct": float(os.getenv("VI_LIKE_RET_PCT_CLOSE", "1.6")),
            },
        }

        self.vi_guard_pct = float(os.getenv("VI_GUARD_PCT", "0.40"))
        self.vi_cooldown_sec = float(os.getenv("VI_COOLDOWN_SEC", "120"))

        self.position_pct = float(os.getenv("POSITION_PCT", "0.30"))

        self.hard_stop_pct = float(os.getenv("HARD_STOP_PCT", "3.5"))
        self.trail_arm_pct = float(os.getenv("TRAIL_ARM_PCT", "4.0"))
        self.trail_drop_pct = float(os.getenv("TRAIL_DROP_PCT", "3.5"))

        self.entry_block_dayrise_pct = float(os.getenv("ENTRY_BLOCK_DAYRISE_PCT", "12.0"))
        self.limitup_gap_take_pct = float(os.getenv("LIMITUP_GAP_TAKE_PCT", "0.85"))

        self.kill_switch_file = os.getenv("KILL_SWITCH_FILE", os.path.join("data", "kill.switch"))
        self.ledger_file = os.getenv("LEDGER_FILE", os.path.join("data", "ledger_real.csv"))

        self.signal_diag_file = os.getenv("SIGNAL_DIAG_FILE", os.path.join("data", "signal_diag.log"))
        self.signal_diag_sec = float(os.getenv("SIGNAL_DIAG_SEC", "20"))
        self._last_diag_ts: Dict[str, float] = {}

        self.prev_close_cache = load_cache()

        self.book: Dict[str, Dict[str, str]] = {}
        self.book_ts: Dict[str, float] = {}
        self.ticks: Dict[str, Deque[Tuple[float, float, float]]] = defaultdict(lambda: deque(maxlen=5000))
        self.pos: Dict[str, Position] = {}
        self.last_entry_ts: Dict[str, float] = {}
        self.candidate_since: Dict[str, float] = {}
        self.vi_last_ts: Dict[str, float] = {}

        self._init_ledger()
        self._init_diag()

    def _session_name(self, ts_epoch: float) -> str:
        hhmm = int(time.strftime("%H%M", time.localtime(ts_epoch)))
        if 900 <= hhmm < 930:
            return "OPEN"
        if 930 <= hhmm < 1430:
            return "MID"
        return "CLOSE"

    def _params(self, ts_epoch: float) -> Dict[str, Any]:
        s = self._session_name(ts_epoch)
        return self.session_cfg.get(s, {})

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

    def _safe_order(self, side: str, sym: str, qty: int, ts_epoch: float, price: float, reason: str):
        try:
            j = order_cash(side, sym, qty, ord_dvsn="01", ord_unpr="0")
            self._log(ts_epoch, side, sym, qty, price, reason, j.get("rt_cd", ""), j.get("msg1", ""))
            return j
        except Exception as e:
            self._log(ts_epoch, side, sym, qty, price, reason, "EX", f"order_err:{type(e).__name__}:{e}")
            return {"rt_cd": "EX", "msg1": str(e)}

    def _cleanup_symbol_state(self, sym: str):
        self.pos.pop(sym, None)
        self.candidate_since.pop(sym, None)
        self.last_entry_ts.pop(sym, None)
        self.vi_last_ts.pop(sym, None)
        self._last_diag_ts.pop(sym, None)
        self.book.pop(sym, None)
        self.book_ts.pop(sym, None)
        self.ticks.pop(sym, None)

    def on_orderbook(self, row: Dict[str, str], ts_epoch: float):
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

    def _maybe_exit(self, sym: str, price: float, ts_epoch: float):
        p = self.pos.get(sym)
        if not p:
            return

        if price > p.max_price:
            p.max_price = price
        if (not p.trail_armed) and price >= p.entry_price * (1.0 + self.trail_arm_pct / 100.0):
            p.trail_armed = True

        tgt = self._limitup_target(sym)
        if tgt > 0 and price >= tgt:
            qty_sell = sellable_qty(sym)
            if qty_sell <= 0:
                return
            qty_ord = min(qty_sell, p.qty)
            j = self._safe_order("SELL", sym, qty_ord, ts_epoch, price, f"limitup_gap_take {self.limitup_gap_take_pct}")
            if j.get("rt_cd") == "0":
                self._cleanup_symbol_state(sym)
            return

        if price <= p.entry_price * (1.0 - self.hard_stop_pct / 100.0):
            qty_sell = sellable_qty(sym)
            if qty_sell <= 0:
                return
            qty_ord = min(qty_sell, p.qty)
            j = self._safe_order("SELL", sym, qty_ord, ts_epoch, price, f"hard_stop {self.hard_stop_pct}")
            if j.get("rt_cd") == "0":
                self._cleanup_symbol_state(sym)
            return

        if p.trail_armed:
            stop = p.max_price * (1.0 - self.trail_drop_pct / 100.0)
            if price <= stop:
                qty_sell = sellable_qty(sym)
                if qty_sell <= 0:
                    return
                qty_ord = min(qty_sell, p.qty)
                j = self._safe_order("SELL", sym, qty_ord, ts_epoch, price, f"trail_stop drop={self.trail_drop_pct}")
                if j.get("rt_cd") == "0":
                    self._cleanup_symbol_state(sym)
                return

    def on_trade(self, row: Dict[str, str], ts_epoch: float):
        if self._kill_active():
            return

        sym = row.get("MKSC_SHRN_ISCD", "")
        if not sym:
            return
        price = _f(row.get("STCK_PRPR"))
        vol = _f(row.get("CNTG_VOL"))
        if price <= 0:
            return

        if sym in self.pos:
            self._maybe_exit(sym, price, ts_epoch)
        if sym in self.pos:
            return

        p = self._params(ts_epoch)
        min_ret_pct = float(p.get("min_ret_pct", self.min_ret_pct))
        min_tick_count = int(p.get("min_tick_count", self.min_tick_count))
        max_spread_pct = float(p.get("max_spread_pct", self.max_spread_pct))
        confirm_sec = float(p.get("confirm_sec", self.confirm_sec))
        cooldown_sec = float(p.get("cooldown_sec", self.cooldown_sec))
        vi_like_ret_pct = float(p.get("vi_like_ret_pct", 2.0))

        if ts_epoch - self.last_entry_ts.get(sym, 0.0) < cooldown_sec:
            self._log_signal_diag(ts_epoch, sym, price, 0, 0, 0, 0, 0, 0, "NO_BUY", f"cooldown<{cooldown_sec:.0f}s")
            return

        dayrise = self._day_rise_pct(sym, price)
        if self._prev_close(sym) > 0 and dayrise >= self.entry_block_dayrise_pct:
            self._log_signal_diag(ts_epoch, sym, price, 0.0, 0, 0.0, 0.0, 0.0, dayrise, "NO_BUY", f"dayrise_block need<{self.entry_block_dayrise_pct:.2f}")
            return

        dq = self.ticks[sym]
        dq.append((ts_epoch, price, vol))
        while dq and ts_epoch - dq[0][0] > self.window_sec:
            dq.popleft()
        if len(dq) < self.min_ticks_for_calc:
            return

        base = dq[0][1]
        ret = (price - base) / base * 100.0
        trv = sum(px * vv for _, px, vv in dq)
        tick_count = len(dq)

        if ret >= vi_like_ret_pct:
            self.vi_last_ts[sym] = ts_epoch
            self._log_signal_diag(ts_epoch, sym, price, ret, tick_count, trv, 0, 0, dayrise, "NO_BUY", f"vi_like_ret {ret:.2f}>={vi_like_ret_pct:.2f}")
            return
        if ts_epoch - self.vi_last_ts.get(sym, 0.0) < self.vi_cooldown_sec:
            self._log_signal_diag(ts_epoch, sym, price, ret, tick_count, trv, 0, 0, dayrise, "NO_BUY", f"vi_cooldown<{self.vi_cooldown_sec:.0f}s")
            return

        ob = self.book.get(sym)
        ob_age = ts_epoch - self.book_ts.get(sym, 0.0)
        ob_stale = (not ob) or (ob_age > self.orderbook_max_age_sec)

        bid_tot = _f(ob.get("TOTAL_BIDP_RSQN")) if ob and not ob_stale else 0.0
        ask_tot = _f(ob.get("TOTAL_ASKP_RSQN")) if ob and not ob_stale else 0.0
        denom = bid_tot + ask_tot
        imb = (bid_tot / denom) if denom > 0 else 0.5

        ask1 = _f(ob.get("ASKP1")) if ob and not ob_stale else 0.0
        bid1 = _f(ob.get("BIDP1")) if ob and not ob_stale else 0.0
        mid = (ask1 + bid1) / 2 if (ask1 > 0 and bid1 > 0) else price
        spread = ((ask1 - bid1) / mid * 100.0) if mid > 0 else 999.0

        vi_std = _f(row.get("VI_STND_PRC"))
        vi_gap = abs(price - vi_std) / vi_std * 100.0 if vi_std > 0 else 999.0

        # 1) Trigger gate: fast/entry signal only (ret + tick_count + spread)
        trigger_fail = []
        if ret < min_ret_pct:
            trigger_fail.append(f"ret {ret:.2f}/{min_ret_pct:.2f}")
        if tick_count < min_tick_count:
            trigger_fail.append(f"ticks {tick_count}/{min_tick_count}")
        if spread > max_spread_pct:
            trigger_fail.append(f"spread {spread:.2f}>{max_spread_pct:.2f}")

        if trigger_fail:
            self.candidate_since.pop(sym, None)
            self._log_signal_diag(
                ts_epoch, sym, price, ret, tick_count, trv, imb, spread, dayrise, "NO_BUY", " | ".join(trigger_fail)
            )
            return

        c0 = self.candidate_since.get(sym)
        if c0 is None:
            self.candidate_since[sym] = ts_epoch
            self._log_signal_diag(ts_epoch, sym, price, ret, tick_count, trv, imb, spread, dayrise, "NO_BUY", f"confirm_wait {confirm_sec:.1f}s")
            return
        if ts_epoch - c0 < confirm_sec:
            return

        # 2) Guard gate: order-right-before safety checks only
        guard_fail = []
        if ob_stale:
            guard_fail.append(f"orderbook stale>{self.orderbook_max_age_sec:.1f}s")
        if vi_std > 0 and vi_gap <= self.vi_guard_pct:
            guard_fail.append(f"vi_guard gap {vi_gap:.2f}<={self.vi_guard_pct:.2f}")
        if guard_fail:
            self._log_signal_diag(
                ts_epoch, sym, price, ret, tick_count, trv, imb, spread, dayrise, "NO_BUY", " | ".join(guard_fail)
            )
            return

        try:
            cash = buyable_cash(sym, ord_dvsn="01", price="0")
        except Exception as e:
            self._log(ts_epoch, "BUY", sym, 0, price, "buyable_cash_error", "EX", f"{type(e).__name__}:{e}")
            self._log_signal_diag(ts_epoch, sym, price, ret, tick_count, trv, imb, spread, dayrise, "NO_BUY", "buyable_cash_error")
            return

        target = cash * self.position_pct
        qty = int(math.floor(target / price))
        if qty <= 0:
            self._log_signal_diag(
                ts_epoch, sym, price, ret, tick_count, trv, imb, spread, dayrise, "NO_BUY", f"qty=0 cash={cash:.0f} target={target:.0f}"
            )
            return

        j = self._safe_order("BUY", sym, qty, ts_epoch, price, f"signal ret={ret:.2f} imb={imb:.2f} spr={spread:.2f} dayrise={dayrise:.2f}")
        if j.get("rt_cd") == "0":
            self.pos[sym] = Position(qty=qty, entry_price=price, entry_ts=ts_epoch, max_price=price, trail_armed=False)
            self.last_entry_ts[sym] = ts_epoch
            self.candidate_since.pop(sym, None)
            self._log_signal_diag(ts_epoch, sym, price, ret, tick_count, trv, imb, spread, dayrise, "BUY_TRY", f"qty={qty}")
        else:
            self._log_signal_diag(ts_epoch, sym, price, ret, tick_count, trv, imb, spread, dayrise, "BUY_FAIL", j.get("msg1", ""))
