import time
import random
import math
from collections import defaultdict, deque
from dataclasses import dataclass

# =========================
# 30% 진입 + 최대 3종목 엔진
# =========================
def f(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default

@dataclass(frozen=True)
class BuyOrder:
    symbol: str
    qty: int
    reason: str = ""

class PaperBroker:
    def __init__(self, starting_cash: float = 10_000_000):
        self.cash = float(starting_cash)
        self.pos = defaultdict(int)   # symbol -> qty
        self.last_price = {}

    def update_price(self, symbol: str, price: float):
        if price > 0:
            self.last_price[symbol] = float(price)

    def position(self, symbol: str) -> int:
        return int(self.pos[symbol])

    def positions_count(self) -> int:
        return sum(1 for _, q in self.pos.items() if q > 0)

    def equity(self) -> float:
        eq = self.cash
        for sym, qty in self.pos.items():
            if qty <= 0:
                continue
            px = self.last_price.get(sym)
            if px:
                eq += qty * px
        return float(eq)

    def available_cash(self) -> float:
        return float(self.cash)

    def buy(self, order: BuyOrder, price_for_fill: float):
        cost = order.qty * price_for_fill
        if cost > self.cash:
            print(ts(), f"[REJECT] {order.symbol} need={cost:.0f} cash={self.cash:.0f}")
            return False
        self.cash -= cost
        self.pos[order.symbol] += order.qty
        print(ts(), f"[BUY] {order.symbol} qty={order.qty} fill={price_for_fill:.0f} cash={self.cash:.0f} "
                    f"pos_cnt={self.positions_count()} reason={order.reason}")
        return True

class SurgeDetector:
    def __init__(self, window_sec=10, min_ret_pct=0.6, min_tr_value=1_000_000,
                 min_imb=0.62, max_spread_pct=0.15):
        self.window_sec = window_sec
        self.min_ret_pct = min_ret_pct
        self.min_tr_value = min_tr_value
        self.min_imb = min_imb
        self.max_spread_pct = max_spread_pct
        self.ticks = defaultdict(lambda: deque(maxlen=2000))
        self.book = {}
        self.seen = set()

    def on_orderbook(self, sym: str, ask1: float, bid1: float, ask_tot: float, bid_tot: float):
        self.book[sym] = {"ASKP1": ask1, "BIDP1": bid1, "TOTAL_ASKP_RSQN": ask_tot, "TOTAL_BIDP_RSQN": bid_tot}

    def on_trade(self, sym: str, price: float, vol: float):
        if price <= 0:
            return None
        now = time.time()
        dq = self.ticks[sym]
        dq.append((now, price, vol))
        while dq and now - dq[0][0] > self.window_sec:
            dq.popleft()
        if len(dq) < 2:
            return None

        base = dq[0][1]
        ret = (price - base) / base * 100.0
        trv = sum(p * v for _, p, v in dq)

        if ret < self.min_ret_pct or trv < self.min_tr_value:
            return None

        ob = self.book.get(sym)
        if not ob:
            return None

        bid_tot = f(ob.get("TOTAL_BIDP_RSQN"))
        ask_tot = f(ob.get("TOTAL_ASKP_RSQN"))
        denom = bid_tot + ask_tot
        imb = (bid_tot / denom) if denom > 0 else 0.5

        ask1 = f(ob.get("ASKP1"))
        bid1 = f(ob.get("BIDP1"))
        mid = (ask1 + bid1) / 2 if (ask1 > 0 and bid1 > 0) else price
        spread = ((ask1 - bid1) / mid * 100.0) if mid > 0 else 999

        if imb < self.min_imb or spread > self.max_spread_pct:
            return None

        bucket = int(now // self.window_sec)
        sid = f"SURGE|{sym}|{bucket}"
        if sid in self.seen:
            return None
        self.seen.add(sid)

        return {"symbol": sym, "price": price, "ret_pct": ret, "tr_value": trv, "imb": imb, "spread_pct": spread, "signal_id": sid}

class BuyEngine:
    def __init__(self, broker: PaperBroker, position_pct=0.30, max_symbols=3, cooldown_sec=60):
        self.broker = broker
        self.det = SurgeDetector(window_sec=10, min_ret_pct=0.6, min_tr_value=1_000_000)  # 테스트는 거래대금 낮춤
        self.position_pct = position_pct
        self.max_symbols = max_symbols
        self.cooldown_sec = cooldown_sec
        self.last_buy_ts = {}

    def on_orderbook(self, sym: str):
        # 테스트용으로 "매수우위 호가"를 항상 유지시켜둠
        self.det.on_orderbook(sym, ask1=70100, bid1=70000, ask_tot=50_000, bid_tot=120_000)

    def on_trade(self, sym: str, price: float, vol: float):
        self.broker.update_price(sym, price)
        sig = self.det.on_trade(sym, price, vol)
        if not sig:
            return

        now = time.time()
        if now - self.last_buy_ts.get(sym, 0) < self.cooldown_sec:
            return
        if self.broker.position(sym) > 0:
            return
        if self.broker.positions_count() >= self.max_symbols:
            print(ts(), f"[BLOCK] max_symbols reached ({self.max_symbols}) -> skip {sym}")
            return

        eq = self.broker.equity()
        cash = self.broker.available_cash()
        target = min(eq * self.position_pct, cash)
        qty = int(math.floor(target / price))
        if qty <= 0:
            return

        reason = f"30% eq={eq:.0f} target={target:.0f} ret={sig['ret_pct']:.2f}%/10s"
        ok = self.broker.buy(BuyOrder(sym, qty, reason), price_for_fill=price)
        if ok:
            self.last_buy_ts[sym] = now

def ts():
    return time.strftime("%Y-%m-%d %H:%M:%S")

def run_scenario():
    broker = PaperBroker(starting_cash=10_000_000)
    eng = BuyEngine(broker, position_pct=0.30, max_symbols=3, cooldown_sec=1)

    symbols = ["005930", "000660", "035420", "051910"]  # 4번째는 막히는지 확인용

    # 각 종목에 대해 12초짜리 스트림을 만들어서 "급등"을 강제로 발생
    for sym in symbols:
        eng.on_orderbook(sym)

        price = 70000.0 + random.uniform(-500, 500)
        base = price

        start = time.time()
        bought = False
        while time.time() - start < 12:
            t = time.time() - start
            if t < 6:
                price *= (1 + random.uniform(-0.0002, 0.0002))
            else:
                price *= (1 + random.uniform(0.0009, 0.0020))  # 급등 구간
            vol = random.randint(1, 50)
            eng.on_trade(sym, price, vol)
            if broker.position(sym) > 0 and not bought:
                bought = True
            time.sleep(0.05)

        print(ts(), f"[SUMMARY] {sym} bought={broker.position(sym)>0} qty={broker.position(sym)} cash={broker.cash:.0f} eq={broker.equity():.0f}")
        print("-" * 80)

    print(ts(), "[FINAL_POS]")
    for s in symbols:
        if broker.position(s) > 0:
            print(" ", s, broker.position(s))
    print(ts(), f"cash={broker.cash:.0f} equity={broker.equity():.0f} positions_count={broker.positions_count()}")

if __name__ == "__main__":
    run_scenario()