from __future__ import annotations

import json
import math
import os
import time
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


class EngineSimple:
    """실전용 단순 모멘텀 엔진: 단일 매수 경로 / 단일 청산 경로."""

    def __init__(self):
        # files
        self.ledger_file = os.getenv("LEDGER_FILE", os.path.join("data", "ledger_real.csv"))
        self.state_file = os.getenv("POSITION_STATE_FILE", os.path.join("data", "positions_simple.json"))
        self.watchlist_file = os.getenv("WATCHLIST_FILE", os.path.join("data", "watchlist.txt"))
        self.signal_diag_file = os.getenv("SIGNAL_DIAG_FILE", os.path.join("data", "signal_diag_simple.log"))
        self.runtime_status_file = os.getenv("RUNTIME_STATUS_FILE", os.path.join("data", "runtime_status.json"))

        # buy / score
        self.position_pct = float(os.getenv("POSITION_PCT", "0.30"))
        self.max_positions = max(1, int(os.getenv("MAX_POSITIONS", "3")))
        self.entry_score_threshold = float(os.getenv("ENTRY_SCORE_THRESHOLD", "85"))
        self.entry_score_strong = float(os.getenv("ENTRY_SCORE_STRONG", "120"))
        self.entry_block_dayrise_pct = float(os.getenv("ENTRY_BLOCK_DAYRISE_PCT", "7.0"))
        self.entry_hard_dayrise_block_pct = float(os.getenv("ENTRY_HARD_DAYRISE_BLOCK_PCT", "18.0"))

        self.buy_ret10_min = float(os.getenv("BUY_RET10_MIN", "0.30"))
        self.buy_ret5_min = float(os.getenv("BUY_RET5_MIN", "0.15"))
        self.buy_trv10_min = float(os.getenv("BUY_TRV10_MIN", "30000000"))
        self.buy_ofi_min = float(os.getenv("BUY_OFI_MIN", "1.4"))
        self.buy_imb_min = float(os.getenv("BUY_IMB_MIN", "0.60"))
        self.pullback_pct = float(os.getenv("PULLBACK_PCT", "0.65"))
        self.pullback_rebound_pct = float(os.getenv("PULLBACK_REBOUND_PCT", "0.18"))
        self.vi_guard_pct = float(os.getenv("VI_GUARD_PCT", "0.25"))

        # sell (4 rules only)
        self.stop_loss_pct = float(os.getenv("STOP_LOSS_PCT", "2.5"))
        self.take_profit_pct = float(os.getenv("TAKE_PROFIT_PCT", "3.5"))
        self.trail_arm_pct = float(os.getenv("TRAIL_ARM_PCT", "3.0"))
        self.trail_drop_pct = float(os.getenv("TRAIL_DROP_PCT", "2.2"))
        self.max_hold_sec = float(os.getenv("MAX_HOLD_SEC", "240"))
        self.exit_grace_sec = float(os.getenv("EXIT_GRACE_SEC", "3.0"))
        self.cooldown_sec = float(os.getenv("COOLDOWN_SEC", "90"))

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
        self._skip_reason_counts: Dict[str, int] = defaultdict(int)
        self._score_eval_total = 0
        self._score_pass_total = 0
        self._gate_block_counts: Dict[str, int] = defaultdict(int)
        self._last_buy_time = 0.0
        self._last_sell_time = 0.0
        self._last_buy_symbol = ""
        self._last_sell_symbol = ""
        self._recent_events: Deque[Dict[str, Any]] = deque(maxlen=10)
        self._last_runtime_snapshot_ts = 0.0

        self.prev_close_cache = load_cache()
        self.notifier = DiscordNotifier()
        self._init_files()
        self._load_state()

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
                    entry_ts=float(item.get("entry_ts", time.time())),
                    max_price=float(item.get("max_price", item.get("entry_price", 0.0))),
                    max_pnl_pct=float(item.get("max_pnl_pct", 0.0)),
                    min_pnl_pct=float(item.get("min_pnl_pct", 0.0)),
                    score=float(item.get("score", 0.0)),
                    reasons=list(item.get("reasons") or []),
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
            with open(self.watchlist_file, "r", encoding="utf-8") as f:
                self.watch = {ln.strip() for ln in f if ln.strip()}
        except Exception:
            self.watch = set()

    def _session_weight(self, ts_epoch: float) -> float:
        hhmm = int(time.strftime("%H%M", time.localtime(ts_epoch)))
        if 900 <= hhmm < 930:
            return 1.08
        if 930 <= hhmm < 1430:
            return 1.0
        if 1430 <= hhmm < 1530:
            return 0.95
        return 0.9

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
        for t, px, vol in dq:
            if t < st:
                continue
            if cnt == 0:
                base = px
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
        bid = _f(ob.get("TOTAL_BIDP_RSQN")) + _f(ob.get("total_bidp_rsqn"))
        ask = _f(ob.get("TOTAL_ASKP_RSQN")) + _f(ob.get("total_askp_rsqn"))
        den = bid + ask
        return (bid / den) if den > 0 else 0.5

    def _depth_ratio(self, sym: str) -> float:
        ob = self.book.get(sym) or {}
        bid = _f(ob.get("BIDP_RSQN1")) + _f(ob.get("BIDP_RSQN2")) + _f(ob.get("BIDP_RSQN3"))
        ask = _f(ob.get("ASKP_RSQN1")) + _f(ob.get("ASKP_RSQN2")) + _f(ob.get("ASKP_RSQN3"))
        if ask <= 0:
            return 1.0
        return bid / ask

    def _pullback_rebound(self, dq: Deque[Tuple[float, float, float]], now: float, price: float) -> float:
        high30 = max((px for t, px, _ in dq if (now - t) <= 30.0), default=price)
        low10 = min((px for t, px, _ in dq if (now - t) <= 10.0), default=price)
        pulled = low10 <= high30 * (1.0 - max(0.0, self.pullback_pct) / 100.0)
        rebound = price >= low10 * (1.0 + max(0.0, self.pullback_rebound_pct) / 100.0)
        return 1.0 if (pulled and rebound and price >= high30 * 0.998) else 0.0

    def _safe_order(self, side: str, sym: str, qty: int, ts_epoch: float, price: float, reason: str, ord_dvsn: str = "01", ord_unpr: str = "0") -> Dict[str, Any]:
        try:
            j = order_cash(side, sym, qty, ord_dvsn=ord_dvsn, ord_unpr=ord_unpr)
            self._log_ledger(ts_epoch, side, sym, qty, price, reason, j.get("rt_cd", ""), j.get("msg1", ""))
            return j
        except Exception as e:
            self._log_ledger(ts_epoch, side, sym, qty, price, reason, "EX", f"{type(e).__name__}:{e}")
            return {"rt_cd": "EX", "msg1": str(e)}

    def _effective_buying_power(self) -> float:
        try:
            return float(account_buying_power(symbol=self.health_cash_symbol, ord_dvsn="01", price="0"))
        except Exception:
            return float(buyable_cash(self.health_cash_symbol, ord_dvsn="01", price="0"))

    # ---------- core strategy ----------
    def score_symbol(self, sym: str, price: float, ts_epoch: float) -> tuple[float, list[str], Dict[str, float]]:
        dq = self.ticks[sym]
        ret10, trv10, _ = self._window_stats(dq, ts_epoch, 10.0)
        ret5, _, _ = self._window_stats(dq, ts_epoch, 5.0)
        _, trv30, _ = self._window_stats(dq, ts_epoch, 30.0)
        ofi = self._compute_ofi_window(dq, ts_epoch, 10.0)
        imb = self._imbalance(sym)
        depth = self._depth_ratio(sym)
        dayrise = self._day_rise_pct(sym, price)
        accel = trv10 / max(1.0, trv30 / 3.0)
        pull_rebound = self._pullback_rebound(dq, ts_epoch, price)

        vi_std = _f((self.book.get(sym) or {}).get("VI_STND_PRC"))
        vi_gap = abs(price - vi_std) / vi_std * 100.0 if vi_std > 0 else 999.0

        # ---- positive groups ----
        momentum_score = (ret10 * 45.0) + (ret5 * 25.0)
        liquidity_score = (math.log1p(max(0.0, trv10)) * 4.0) + (max(0.0, accel - 1.0) * 12.0)
        ofi_boost = min(35.0, max(0.0, ofi - 1.0) * 18.0)
        imbalance_boost = max(-22.0, min(22.0, (imb - 0.5) * 90.0))
        depth_boost = max(-16.0, min(16.0, (depth - 1.0) * 16.0))
        orderflow_score = ofi_boost + imbalance_boost + depth_boost
        structure_score = pull_rebound * 10.0

        positive_total = momentum_score + liquidity_score + orderflow_score + structure_score
        score_pos = positive_total * self._session_weight(ts_epoch)

        # ---- penalties ----
        penalty_dayrise = 0.0
        if dayrise > self.entry_block_dayrise_pct:
            # soft penalty: 과열 구간 진입을 억제하되 완전 무력화는 방지
            penalty_dayrise = min(22.0, (dayrise - self.entry_block_dayrise_pct) * 3.0)

        penalty_vi = 0.0
        if vi_gap <= self.vi_guard_pct:
            penalty_vi = min(12.0, (self.vi_guard_pct - vi_gap + 0.02) * 90.0)

        high20 = max((px for t, px, _ in dq if (ts_epoch - t) <= 20.0), default=price)
        penalty_chase = 0.0
        if high20 > 0 and price >= high20 * 0.999 and pull_rebound <= 0.0:
            penalty_chase = 6.0

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
            "dayrise": dayrise,
            "pull_rebound": pull_rebound,
            "vi_gap": vi_gap,
            "score_pos": score_pos,
            "score_pen": penalty_score,
            "contrib_pos": contrib_pos,
            "contrib_neg": contrib_neg,
        }
        return score, reasons, metrics

    def should_buy(self, sym: str, score: float, metrics: Dict[str, float], ts_epoch: float) -> tuple[bool, str]:
        self._score_eval_total += 1
        if sym not in self.watch:
            return False, "watchlist_out"
        if sym in self.pos:
            return False, "already_held"
        if len(self.pos) >= self.max_positions:
            return False, f"max_positions={len(self.pos)}"
        if ts_epoch < self.cooldown_until.get(sym, 0.0):
            return False, f"cooldown<{self.cooldown_until[sym]-ts_epoch:.0f}s"
        if metrics.get("dayrise", 0.0) >= self.entry_hard_dayrise_block_pct:
            return False, f"dayrise_hard>{self.entry_hard_dayrise_block_pct:.1f}"
        if metrics.get("trv10", 0.0) < max(1.0, self.buy_trv10_min * 0.5):
            return False, "trv10_too_low"
        if score < self.entry_score_threshold:
            return False, f"score<{self.entry_score_threshold:.1f}"
        self._score_pass_total += 1
        return True, "pass"

    def _top_factor_strings(self, metrics: Dict[str, float]) -> tuple[str, str]:
        pos = metrics.get("contrib_pos", {}) or {}
        neg = metrics.get("contrib_neg", {}) or {}
        top_pos = sorted(((k, float(v)) for k, v in pos.items()), key=lambda kv: kv[1], reverse=True)[:3]
        top_neg = sorted(((k, abs(float(v))) for k, v in neg.items() if float(v) < 0), key=lambda kv: kv[1], reverse=True)[:2]
        pos_s = "[" + ",".join(f"{k}+{v:.1f}" for k, v in top_pos) + "]"
        neg_s = "[" + ",".join(f"{k}-{v:.1f}" for k, v in top_neg) + "]"
        return pos_s, neg_s

    def _position_pct_by_score(self, score: float) -> float:
        if score >= self.entry_score_strong:
            return min(self.position_pct, 0.30)
        if score >= self.entry_score_threshold + 20.0:
            return min(self.position_pct, 0.20)
        return min(self.position_pct, 0.12)

    def enter_position(self, sym: str, price: float, score: float, reasons: list[str], metrics: Dict[str, float], ts_epoch: float):
        cash = self._effective_buying_power()
        pct = self._position_pct_by_score(score)
        qty = int((cash * pct) // max(1.0, price))
        if qty <= 0:
            self._skip_reason_counts["cash_short"] += 1
            self._log_diag(ts_epoch, sym, "SKIP", f"cash_short cash={cash:.0f} price={price:.0f}")
            return

        reason = f"score={score:.1f} pct={pct:.2f} reasons={'|'.join(reasons[:5])}"
        j = self._safe_order("BUY", sym, qty, ts_epoch, price, reason)
        if j.get("rt_cd") != "0":
            self._log_diag(ts_epoch, sym, "BUY_FAIL", str(j.get("msg1", ""))[:160])
            return

        self.pos[sym] = Position(qty=qty, entry_price=price, entry_ts=ts_epoch, max_price=price, score=score, reasons=reasons[:5])
        self._last_buy_time = ts_epoch
        self._last_buy_symbol = sym
        self._record_event(ts_epoch, "BUY", sym, f"score={score:.1f} qty={qty}")
        self._save_state()
        self._log_diag(
            ts_epoch,
            sym,
            "BUY",
            f"score={score:.1f} pos={self._top_factor_strings(metrics)[0]} neg={self._top_factor_strings(metrics)[1]} price={price:.0f} qty={qty} metrics=ret10={metrics.get('ret10',0.0):.2f},ret5={metrics.get('ret5',0.0):.2f},trv10={metrics.get('trv10',0.0):.0f},ofi={metrics.get('ofi',0.0):.2f},imb={metrics.get('imb',0.0):.2f},depth={metrics.get('depth_ratio',0.0):.2f},dayrise={metrics.get('dayrise',0.0):.2f}",
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
        if price > p.max_price:
            p.max_price = price
        pnl_pct = (price / p.entry_price - 1.0) * 100.0 if p.entry_price > 0 else 0.0
        p.max_pnl_pct = max(p.max_pnl_pct, pnl_pct)
        p.min_pnl_pct = min(p.min_pnl_pct, pnl_pct)

        hold_sec = max(0.0, ts_epoch - p.entry_ts)
        reason = ""

        # stop / take always active
        if pnl_pct <= -abs(self.stop_loss_pct):
            reason = "STOP_LOSS"
        elif pnl_pct >= abs(self.take_profit_pct):
            reason = "TAKE_PROFIT"
        elif hold_sec >= self.exit_grace_sec:
            # trailing / max_hold after grace
            trail_stop = p.max_price * (1.0 - abs(self.trail_drop_pct) / 100.0)
            if p.max_pnl_pct >= abs(self.trail_arm_pct) and price <= trail_stop:
                reason = "TRAILING"
            elif hold_sec >= self.max_hold_sec:
                reason = "MAX_HOLD"

        if not reason:
            self._save_state()
            return

        qty_sell = max(0, int(sellable_qty(sym)))
        qty = min(qty_sell, p.qty)
        if qty <= 0:
            self.pos.pop(sym, None)
            self.cooldown_until[sym] = ts_epoch + self.cooldown_sec
            self._save_state()
            self._log_diag(ts_epoch, sym, "STATE_CLEAN", "sellable_qty_zero")
            return

        j = self._safe_order("SELL", sym, qty, ts_epoch, price, reason)
        if j.get("rt_cd") != "0":
            self._log_diag(ts_epoch, sym, "SELL_FAIL", str(j.get("msg1", ""))[:160])
            return

        self.cooldown_until[sym] = ts_epoch + self.cooldown_sec
        self.pos.pop(sym, None)
        self._last_sell_time = ts_epoch
        self._last_sell_symbol = sym
        self._record_event(ts_epoch, "SELL", sym, reason)
        self._save_state()

        self._log_diag(
            ts_epoch,
            sym,
            "SELL",
            f"reason={reason} pnl={pnl_pct:.2f}% hold={hold_sec:.1f}s max={p.max_pnl_pct:.2f}% min={p.min_pnl_pct:.2f}%",
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

        self._reload_watchlist(ts_epoch)

        dq = self.ticks[sym]
        dq.append((ts_epoch, price, max(0.0, vol)))
        while dq and (ts_epoch - dq[0][0]) > 360.0:
            dq.popleft()

        if sym in self.pos:
            self.manage_position(sym, price, ts_epoch)
            return

        score, reasons, metrics = self.score_symbol(sym, price, ts_epoch)
        ok, why = self.should_buy(sym, score, metrics, ts_epoch)

        last = self._last_candidate_log_ts.get(sym, 0.0)
        if (ts_epoch - last) >= 1.0:
            self._last_candidate_log_ts[sym] = ts_epoch
            status = "PASS" if ok else "DROP"
            pos_s, neg_s = self._top_factor_strings(metrics)
            self._log_diag(
                ts_epoch,
                sym,
                "CAND",
                f"score={score:.1f} pass={1 if ok else 0} why={why} pos={pos_s} neg={neg_s} metrics=ret10={metrics.get('ret10',0.0):.2f},ret5={metrics.get('ret5',0.0):.2f},trv10={metrics.get('trv10',0.0):.0f},ofi={metrics.get('ofi',0.0):.2f},imb={metrics.get('imb',0.0):.2f},depth={metrics.get('depth_ratio',0.0):.2f},dayrise={metrics.get('dayrise',0.0):.2f}",
            )

        if not ok:
            self._skip_reason_counts[why] += 1
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
        hard_block = sum(v for k, v in self._gate_block_counts.items() if (not k.startswith("watch")) and (not k.startswith("score<")) and k != "pass")
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
            "top_gate_blockers": [{"reason": k, "count": int(v)} for k, v in top_gate],
            "score_pass_rate": round(score_pass_rate, 4),
            "last_buy_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._last_buy_time)) if self._last_buy_time > 0 else "",
            "last_sell_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._last_sell_time)) if self._last_sell_time > 0 else "",
            "last_buy_symbol": self._last_buy_symbol,
            "last_sell_symbol": self._last_sell_symbol,
            "recent_events": list(self._recent_events),
            "operator_summary": self._operator_summary_runtime(len(self.watch), score_pass_rate),
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
        try:
            snap = account_cash_snapshot()
        except Exception:
            snap = {}
        lines = [
            f"watch={len(self.watch)} held={len(self.pos)} cooldown={len(self.cooldown_until)}",
            f"예수금={_fmt_won(snap.get('deposit'))} 출금가능={_fmt_won(snap.get('withdrawable'))}",
            f"주문가능={_fmt_won(snap.get('orderable'))} D+2={_fmt_won(snap.get('d2_deposit'))}",
        ]
        if self._skip_reason_counts:
            top = sorted(self._skip_reason_counts.items(), key=lambda kv: kv[1], reverse=True)[:3]
            lines.append("skip=" + ", ".join(f"{k}:{v}" for k, v in top))
        self.notifier.send(title="🩺 Simple Engine Health", color=0x5865F2, lines=lines)

    def on_timer(self, ts_epoch: float):
        self._reload_watchlist(ts_epoch)
        self._send_health(ts_epoch)
        if (ts_epoch - self._last_runtime_snapshot_ts) >= 1.0:
            self._last_runtime_snapshot_ts = ts_epoch
            self._write_runtime_status(ts_epoch)
