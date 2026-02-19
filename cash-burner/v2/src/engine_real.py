# src/engine_real.py
from __future__ import annotations

import os, time, math, json
from dataclasses import dataclass
from collections import defaultdict, deque
from typing import Dict, Deque, Tuple

from kis_orders import buyable_cash, sellable_qty, order_cash
from quote_basic import load_cache

def _f(x, d=0.0) -> float:
    try: return float(x)
    except: return d

def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")

@dataclass
class Position:
    qty: int
    entry_price: float
    entry_ts: float
    max_price: float
    trail_armed: bool

class EngineReal:
    def __init__(self):
        self.window_sec = int(os.getenv("WINDOW_SEC","10"))
        self.min_ret_pct = float(os.getenv("MIN_RET_PCT","0.6"))
        self.min_tick_count = int(os.getenv("MIN_TICK_COUNT","10"))
        self.min_tr_value = float(os.getenv("MIN_TR_VALUE","0"))
        self.min_imb = float(os.getenv("MIN_IMB","0.60"))
        self.max_spread_pct = float(os.getenv("MAX_SPREAD_PCT","0.30"))

        self.position_pct = float(os.getenv("POSITION_PCT","0.30"))

        self.hard_stop_pct = float(os.getenv("HARD_STOP_PCT","3.5"))
        self.trail_arm_pct = float(os.getenv("TRAIL_ARM_PCT","4.0"))
        self.trail_drop_pct = float(os.getenv("TRAIL_DROP_PCT","3.5"))

        self.entry_block_dayrise_pct = float(os.getenv("ENTRY_BLOCK_DAYRISE_PCT","12.0"))
        self.limitup_gap_take_pct = float(os.getenv("LIMITUP_GAP_TAKE_PCT","0.85"))  # 85% of 30% gap

        self.kill_switch_file = os.getenv("KILL_SWITCH_FILE", r"data\kill.switch")
        self.ledger_file = os.getenv("LEDGER_FILE", r"data\ledger_real.csv")

        self.prev_close_cache = load_cache()  # {sym: prev_close}

        self.book: Dict[str, Dict[str,str]] = {}
        self.ticks: Dict[str, Deque[Tuple[float,float,float]]] = defaultdict(lambda: deque(maxlen=5000))
        self.pos: Dict[str, Position] = {}

        self._init_ledger()

    def _init_ledger(self):
        d = os.path.dirname(self.ledger_file)
        if d: os.makedirs(d, exist_ok=True)
        if not os.path.exists(self.ledger_file):
            with open(self.ledger_file,"w",encoding="utf-8") as f:
                f.write("ts,action,symbol,qty,price,reason,rt_cd,msg")

    def _kill_active(self) -> bool:
        try: return os.path.exists(self.kill_switch_file)
        except: return False

    def _log(self, ts_epoch: float, action: str, sym: str, qty: int, price: float, reason: str, rt_cd: str, msg: str):
        safe = (msg or "").replace('"','')[:200]
        with open(self.ledger_file,"a",encoding="utf-8") as f:
            f.write(f"{ts_epoch:.3f},{action},{sym},{qty},{price:.4f},\"{reason}\",{rt_cd},\"{safe}\"\n")

    def on_orderbook(self, row: Dict[str,str], ts_epoch: float):
        sym = row.get("MKSC_SHRN_ISCD","")
        if sym:
            self.book[sym] = row

    def _prev_close(self, sym: str) -> float:
        return float(self.prev_close_cache.get(sym, 0.0) or 0.0)

    def _day_rise_pct(self, sym: str, price: float) -> float:
        pc = self._prev_close(sym)
        if pc <= 0: 
            return 0.0
        return (price/pc - 1.0)*100.0

    def _limitup_target(self, sym: str) -> float:
        pc = self._prev_close(sym)
        if pc <= 0:
            return 0.0
        # target = prev_close + 0.85*(0.30*prev_close) = prev_close*(1 + 0.255)
        return pc * (1.0 + 0.30*self.limitup_gap_take_pct)

    def _maybe_exit(self, sym: str, price: float, ts_epoch: float):
        p = self.pos.get(sym)
        if not p:
            return

        # update max & arm trail
        if price > p.max_price:
            p.max_price = price
        if (not p.trail_armed) and price >= p.entry_price*(1.0 + self.trail_arm_pct/100.0):
            p.trail_armed = True

        # limitup gap take-profit
        tgt = self._limitup_target(sym)
        if tgt > 0 and price >= tgt:
            qty_sell = sellable_qty(sym)
            if qty_sell <= 0:
                return
            j = order_cash("SELL", sym, min(qty_sell, p.qty), ord_dvsn="01", ord_unpr="0")
            self._log(ts_epoch, "SELL", sym, min(qty_sell,p.qty), price, f"limitup_gap_take {self.limitup_gap_take_pct}", j.get("rt_cd",""), j.get("msg1",""))
            if j.get("rt_cd")=="0":
                self.pos.pop(sym, None)
            return

        # hard stop
        if price <= p.entry_price*(1.0 - self.hard_stop_pct/100.0):
            qty_sell = sellable_qty(sym)
            if qty_sell <= 0:
                return
            j = order_cash("SELL", sym, min(qty_sell, p.qty), ord_dvsn="01", ord_unpr="0")
            self._log(ts_epoch, "SELL", sym, min(qty_sell,p.qty), price, f"hard_stop {self.hard_stop_pct}", j.get("rt_cd",""), j.get("msg1",""))
            if j.get("rt_cd")=="0":
                self.pos.pop(sym, None)
            return

        # trailing
        if p.trail_armed:
            stop = p.max_price*(1.0 - self.trail_drop_pct/100.0)
            if price <= stop:
                qty_sell = sellable_qty(sym)
                if qty_sell <= 0:
                    return
                j = order_cash("SELL", sym, min(qty_sell, p.qty), ord_dvsn="01", ord_unpr="0")
                self._log(ts_epoch, "SELL", sym, min(qty_sell,p.qty), price, f"trail_stop drop={self.trail_drop_pct}", j.get("rt_cd",""), j.get("msg1",""))
                if j.get("rt_cd")=="0":
                    self.pos.pop(sym, None)
                return

    def on_trade(self, row: Dict[str,str], ts_epoch: float):
        if self._kill_active():
            return

        sym = row.get("MKSC_SHRN_ISCD","")
        if not sym:
            return
        price = _f(row.get("STCK_PRPR"))
        vol = _f(row.get("CNTG_VOL"))
        if price <= 0:
            return

        # exits first
        if sym in self.pos:
            self._maybe_exit(sym, price, ts_epoch)

        # entry block if already holding
        if sym in self.pos:
            return

        # overbought block: >= +12% day rise
        dayrise = self._day_rise_pct(sym, price)
        if self._prev_close(sym) > 0 and dayrise >= self.entry_block_dayrise_pct:
            return

        dq = self.ticks[sym]
        dq.append((ts_epoch, price, vol))
        while dq and ts_epoch - dq[0][0] > self.window_sec:
            dq.popleft()
        if len(dq) < 2:
            return

        base = dq[0][1]
        ret = (price-base)/base*100.0
        trv = sum(p*v for _,p,v in dq)
        tick_count = len(dq)

        if ret < self.min_ret_pct:
            return
        if trv < self.min_tr_value:
            return
        if tick_count < self.min_tick_count:
            return

        ob = self.book.get(sym)
        if not ob:
            return

        bid_tot = _f(ob.get("TOTAL_BIDP_RSQN"))
        ask_tot = _f(ob.get("TOTAL_ASKP_RSQN"))
        denom = bid_tot + ask_tot
        imb = (bid_tot/denom) if denom>0 else 0.5

        ask1 = _f(ob.get("ASKP1"))
        bid1 = _f(ob.get("BIDP1"))
        mid = (ask1+bid1)/2 if (ask1>0 and bid1>0) else price
        spread = ((ask1-bid1)/mid*100.0) if mid>0 else 999

        if imb < self.min_imb or spread > self.max_spread_pct:
            return

        cash = buyable_cash(sym, ord_dvsn="01", price="0")
        target = cash * self.position_pct
        qty = int(math.floor(target / price))
        if qty <= 0:
            return

        j = order_cash("BUY", sym, qty, ord_dvsn="01", ord_unpr="0")
        self._log(ts_epoch, "BUY", sym, qty, price, f"signal ret={ret:.2f} imb={imb:.2f} spr={spread:.2f} dayrise={dayrise:.2f}", j.get("rt_cd",""), j.get("msg1",""))
        if j.get("rt_cd")=="0":
            self.pos[sym] = Position(qty=qty, entry_price=price, entry_ts=ts_epoch, max_price=price, trail_armed=False)
