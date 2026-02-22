# src/engine_real.py
from __future__ import annotations

import os, time, math
from dataclasses import dataclass
from collections import defaultdict, deque
from typing import Dict, Deque, Tuple, Any

from kis_orders import buyable_cash, sellable_qty, order_cash
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


class EngineReal:
    def __init__(self):
        self.window_sec = int(os.getenv("WINDOW_SEC", "20"))
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
        self.entry_score_min = float(os.getenv("ENTRY_SCORE_MIN", "80"))
        self.entry_pick_window_sec = float(os.getenv("ENTRY_PICK_WINDOW_SEC", "1.2"))
        self.spike_10s_min_pct = float(os.getenv("SPIKE_10S_MIN_PCT", "1.0"))
        self.burst_ratio_min = float(os.getenv("BURST_RATIO_MIN", "2.2"))
        self.orderbook_ratio_min = float(os.getenv("ORDERBOOK_RATIO_MIN", "1.2"))

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
        self.notifier = DiscordNotifier()

        self.health_check_sec = float(os.getenv("HEALTH_CHECK_SEC", "1800"))
        self.ws_stale_sec = float(os.getenv("WS_STALE_SEC", "20"))
        self._last_health_ts = 0.0
        self._health_signal_hits = 0
        self._health_order_tries = 0
        self._health_failures = 0
        self._lat_sum = 0.0
        self._lat_cnt = 0
        self._lat_max = 0.0

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
        self.pos: Dict[str, Position] = {}
        self.last_entry_ts: Dict[str, float] = {}
        self.candidate_since: Dict[str, float] = {}
        self.vi_last_ts: Dict[str, float] = {}
        self._score_pick_bucket_start = 0.0
        self._score_pick_best: Dict[str, Any] | None = None

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
        self._health_order_tries += 1
        try:
            j = order_cash(side, sym, qty, ord_dvsn="01", ord_unpr="0")
            self._log(ts_epoch, side, sym, qty, price, reason, j.get("rt_cd", ""), j.get("msg1", ""))
            if j.get("rt_cd") != "0":
                self._health_failures += 1
            return j
        except Exception as e:
            self._health_failures += 1
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


    def _window_stats(self, dq: Deque[Tuple[float, float, float]], ts_epoch: float, sec: float) -> tuple[float, float, int]:
        st = ts_epoch - sec
        arr = [(t, p, v) for (t, p, v) in dq if t >= st]
        if len(arr) < 2:
            return 0.0, 0.0, len(arr)
        base = arr[0][1]
        last = arr[-1][1]
        ret = ((last - base) / base * 100.0) if base > 0 else 0.0
        trv = sum(p * v for _, p, v in arr)
        return ret, trv, len(arr)

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

    def _entry_score(self, ret: float, tick_count: int, trv: float, imb: float, spread: float, max_spread_pct: float) -> float:
        spread_room = max(0.0, max_spread_pct - spread)
        spread_component = spread_room * 35.0
        tick_component = min(tick_count, 20) * 2.0
        trv_component = min(trv / 10000000.0, 60.0)
        imb_component = max(0.0, imb - 0.5) * 120.0
        ret_component = ret * 45.0
        return ret_component + tick_component + trv_component + imb_component + spread_component

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
    ):
        kst = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts_epoch))
        self.notifier.send(
            title=f"✅ 매수 체결 {sym}",
            color=0x2ECC71,
            lines=[
                f"시간: {kst}",
                f"수량/단가: {qty}주 @ {price:,.0f}",
                f"진입 종합점수: {score:.1f}",
                f"근거: ret={ret:.3f}% | ticks={tick_count} | trv={trv:,.0f}",
                f"호가: imb={imb:.3f} | spread={spread:.3f}%",
                f"당일등락: {dayrise:.3f}%",
            ],
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
            ],
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

    def _event_latency_update(self, ts_epoch: float):
        lag = max(0.0, time.time() - ts_epoch)
        self._lat_sum += lag
        self._lat_cnt += 1
        if lag > self._lat_max:
            self._lat_max = lag

    def _memory_mb(self) -> float:
        if os.name != "posix":
            return 0.0
        try:
            with open("/proc/self/status", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        kb = float(line.split()[1])
                        return kb / 1024.0
        except Exception:
            pass
        return 0.0

    def _day_summary_lines(self) -> list[str]:
        total = self.day_sell_count
        win_rate = (self.day_win_count / total * 100.0) if total > 0 else 0.0
        best = self.day_best or ("-", 0.0, 0.0)
        worst = self.day_worst or ("-", 0.0, 0.0)
        return [
            f"오늘 거래: 매수 {self.day_buy_count} / 매도 {self.day_sell_count}",
            f"승률: {win_rate:.1f}% ({self.day_win_count}승 {self.day_loss_count}패)",
            f"실현손익: {self.day_realized_pnl:,.0f}원",
            f"MDD(실현기준): -{self.day_mdd:,.0f}원",
            f"최대 수익 1건: {best[0]} {best[1]:,.0f}원 ({best[2]:+.2f}%)",
            f"최대 손실 1건: {worst[0]} {worst[1]:,.0f}원 ({worst[2]:+.2f}%)",
        ]

    def _send_day_start_summary(self, ts_epoch: float):
        if self.day_started:
            return
        self.day_started = True
        kst = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts_epoch))
        self.notifier.send(
            title="✅ 매매 시작 요약",
            color=0x2ECC71,
            lines=[f"시간: {kst}"] + self._day_summary_lines(),
        )

    def _send_day_close_summary(self, ts_epoch: float):
        if self.day_closed:
            return
        self.day_closed = True
        kst = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts_epoch))
        self.notifier.send(
            title="📌 장 마감 요약",
            color=0xF1C40F,
            lines=[f"시간: {kst}"] + self._day_summary_lines(),
        )

    def _send_health_check(self, ts_epoch: float):
        if self.health_check_sec <= 0:
            return
        if (ts_epoch - self._last_health_ts) < self.health_check_sec:
            return
        self._last_health_ts = ts_epoch
        ws_gap = ts_epoch - self.ws_last_event_ts if self.ws_last_event_ts > 0 else 999.0
        ws_state = "정상" if ws_gap <= self.ws_stale_sec else f"지연({ws_gap:.1f}s)"
        lat_avg = (self._lat_sum / self._lat_cnt) if self._lat_cnt else 0.0
        self.notifier.send(
            title="🩺 정기 헬스체크",
            color=0x5865F2,
            lines=[
                f"WS 상태: {ws_state}",
                f"최근 이벤트: 신호 {self._health_signal_hits} / 주문 {self._health_order_tries} / 실패 {self._health_failures}",
                f"지연: avg {lat_avg:.3f}s / max {self._lat_max:.3f}s",
                f"메모리(RSS): {self._memory_mb():.1f} MB",
            ],
        )
        self._health_signal_hits = 0
        self._health_order_tries = 0
        self._health_failures = 0
        self._lat_sum = 0.0
        self._lat_cnt = 0
        self._lat_max = 0.0

    def on_timer(self, ts_epoch: float):
        self._ensure_day_roll(ts_epoch)
        hhmm = int(time.strftime("%H%M", time.localtime(ts_epoch)))
        if 900 <= hhmm <= 910:
            self._send_day_start_summary(ts_epoch)
        if hhmm >= 1530:
            self._send_day_close_summary(ts_epoch)
        self._send_health_check(ts_epoch)

    def _score_pick_update(self, ts_epoch: float, sym: str, score: float, price: float, ret: float, tick_count: int, trv: float, imb: float, spread: float, dayrise: float):
        if self._score_pick_bucket_start <= 0:
            self._score_pick_bucket_start = ts_epoch
            self._score_pick_best = {
                "sym": sym,
                "score": score,
                "price": price,
                "ret": ret,
                "tick_count": tick_count,
                "trv": trv,
                "imb": imb,
                "spread": spread,
                "dayrise": dayrise,
                "ts": ts_epoch,
            }
            return

        best = self._score_pick_best
        if (best is None) or (score > float(best.get("score", -1e18))):
            self._score_pick_best = {
                "sym": sym,
                "score": score,
                "price": price,
                "ret": ret,
                "tick_count": tick_count,
                "trv": trv,
                "imb": imb,
                "spread": spread,
                "dayrise": dayrise,
                "ts": ts_epoch,
            }

    def _score_pick_ready(self, ts_epoch: float) -> bool:
        if self._score_pick_bucket_start <= 0:
            return False
        return (ts_epoch - self._score_pick_bucket_start) >= self.entry_pick_window_sec

    def _score_pick_take(self) -> Dict[str, Any] | None:
        best = self._score_pick_best
        self._score_pick_best = None
        self._score_pick_bucket_start = 0.0
        return best

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
                self._notify_sell(sym, qty_ord, price, "LIMITUP", f"limitup_gap_take={self.limitup_gap_take_pct}", p, ts_epoch)
                self._cleanup_symbol_state(sym)
            return

        if price <= p.entry_price * (1.0 - self.hard_stop_pct / 100.0):
            qty_sell = sellable_qty(sym)
            if qty_sell <= 0:
                return
            qty_ord = min(qty_sell, p.qty)
            j = self._safe_order("SELL", sym, qty_ord, ts_epoch, price, f"hard_stop {self.hard_stop_pct}")
            if j.get("rt_cd") == "0":
                self._notify_sell(sym, qty_ord, price, "HARD_STOP", f"hard_stop={self.hard_stop_pct}%", p, ts_epoch)
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
                    self._notify_sell(sym, qty_ord, price, "TRAIL_STOP", f"trail_drop={self.trail_drop_pct}% stop={stop:.2f}", p, ts_epoch)
                    self._cleanup_symbol_state(sym)
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
        if ts_epoch - self.last_entry_ts.get(sym, 0.0) < cooldown_sec:
            self._log_signal_diag(ts_epoch, sym, price, 0, 0, 0, 0, 0, 0, "NO_BUY", f"cooldown<{cooldown_sec:.0f}s")
            return

        dayrise = self._day_rise_pct(sym, price)

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

        ret10, trv10, ticks10 = self._window_stats(dq, ts_epoch, 10.0)
        _, trv_prev, ticks_prev = self._window_stats(dq, ts_epoch - 10.0, 10.0)

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

        # 1) Trigger gate: ret/tick/spread + confirm
        trigger_fail = []
        if ret < min_ret_pct:
            trigger_fail.append(f"ret {ret:.2f}/{min_ret_pct:.2f}")
        if tick_count < min_tick_count:
            trigger_fail.append(f"ticks {tick_count}/{min_tick_count}")
        if spread > max_spread_pct:
            trigger_fail.append(f"spread {spread:.2f}>{max_spread_pct:.2f}")
        if ret10 < self.spike_10s_min_pct:
            trigger_fail.append(f"ret10 {ret10:.2f}/{self.spike_10s_min_pct:.2f}")
        if trv_prev > 0 and trv10 < trv_prev * self.burst_ratio_min:
            trigger_fail.append(f"trv_burst {trv10:.0f}/{trv_prev*self.burst_ratio_min:.0f}")
        if ticks_prev > 0 and ticks10 < int(ticks_prev * self.burst_ratio_min):
            trigger_fail.append(f"tick_burst {ticks10}/{int(ticks_prev*self.burst_ratio_min)}")

        if trigger_fail:
            self.candidate_since.pop(sym, None)
            self._log_signal_diag(
                ts_epoch, sym, price, ret, tick_count, trv, imb, spread, dayrise, "NO_BUY", " | ".join(trigger_fail)
            )
            return

        c0 = self.candidate_since.get(sym)
        if c0 is None:
            self.candidate_since[sym] = ts_epoch
            self._health_signal_hits += 1
            self._log_signal_diag(ts_epoch, sym, price, ret, tick_count, trv, imb, spread, dayrise, "NO_BUY", f"confirm_wait {confirm_sec:.1f}s")
            return
        if ts_epoch - c0 < confirm_sec:
            return

        # 2) Guard gate: order-right-before safety checks only
        guard_fail = []
        if vi_std > 0 and vi_gap <= self.vi_guard_pct:
            guard_fail.append(f"vi_guard gap {vi_gap:.2f}<={self.vi_guard_pct:.2f}")
        depth_ratio = self._depth3_ratio(ob if (ob and not ob_stale) else None)
        if depth_ratio > 0 and depth_ratio < self.orderbook_ratio_min:
            guard_fail.append(f"depth_ratio {depth_ratio:.2f}<{self.orderbook_ratio_min:.2f}")
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

        score = self._entry_score(ret, tick_count, trv, imb, spread, max_spread_pct)
        if score < self.entry_score_min:
            self._log_signal_diag(
                ts_epoch,
                sym,
                price,
                ret,
                tick_count,
                trv,
                imb,
                spread,
                dayrise,
                "NO_BUY",
                f"score {score:.1f}<{self.entry_score_min:.1f}",
            )
            return

        self._score_pick_update(ts_epoch, sym, score, price, ret, tick_count, trv, imb, spread, dayrise)
        if not self._score_pick_ready(ts_epoch):
            return
        best = self._score_pick_take()
        if not best:
            return
        sym = str(best.get("sym", sym))
        if sym in self.pos:
            return
        price = float(best.get("price", price))
        ret = float(best.get("ret", ret))
        tick_count = int(best.get("tick_count", tick_count))
        trv = float(best.get("trv", trv))
        imb = float(best.get("imb", imb))
        spread = float(best.get("spread", spread))
        dayrise = float(best.get("dayrise", dayrise))
        score = float(best.get("score", score))
        try:
            cash = buyable_cash(sym, ord_dvsn="01", price="0")
        except Exception as e:
            self._log(ts_epoch, "BUY", sym, 0, price, "buyable_cash_error", "EX", f"{type(e).__name__}:{e}")
            self._log_signal_diag(ts_epoch, sym, price, ret, tick_count, trv, imb, spread, dayrise, "NO_BUY", "buyable_cash_error")
            return
        target = cash * self.position_pct
        qty = int(math.floor(target / price))
        if qty <= 0:
            self._log_signal_diag(ts_epoch, sym, price, ret, tick_count, trv, imb, spread, dayrise, "NO_BUY", f"qty=0 cash={cash:.0f} target={target:.0f}")
            return

        j = self._safe_order("BUY", sym, qty, ts_epoch, price, f"signal ret={ret:.2f} imb={imb:.2f} spr={spread:.2f} dayrise={dayrise:.2f} score={score:.1f}")
        if j.get("rt_cd") == "0":
            self.pos[sym] = Position(qty=qty, entry_price=price, entry_ts=ts_epoch, max_price=price, trail_armed=False)
            self.last_entry_ts[sym] = ts_epoch
            self.day_buy_count += 1
            self._send_day_start_summary(ts_epoch)
            self.candidate_since.pop(sym, None)
            self._log_signal_diag(ts_epoch, sym, price, ret, tick_count, trv, imb, spread, dayrise, "BUY_TRY", f"qty={qty} score={score:.1f}")
            self._notify_buy(sym, qty, price, ret, tick_count, trv, imb, spread, dayrise, score, ts_epoch)
        else:
            self._log_signal_diag(ts_epoch, sym, price, ret, tick_count, trv, imb, spread, dayrise, "BUY_FAIL", j.get("msg1", ""))
