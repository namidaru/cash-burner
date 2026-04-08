"""
Short-term mean reversion on 069500 / 122630.

Tests:
  - RSI(2) < threshold → buy, exit when close > MA5
  - N-day down streak → buy
  - Z-score(N) < -2 → buy
  - Combine with regime filter (buy only above MA200)

Compare alpha vs buy&hold.
"""
import os, json, math
from statistics import mean

os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")

with open("data/daily_ohlc_cache.json", "r", encoding="utf-8") as f:
    cache = json.load(f)


def load(sym):
    days = sorted(cache[sym].keys())
    closes = [cache[sym][d]["close"] for d in days]
    rets = [0.0] + [(closes[i]/closes[i-1]-1)*100 for i in range(1, len(closes))]
    return days, closes, rets


def rsi(closes, n):
    """Wilder's RSI."""
    out = [None]*len(closes)
    if len(closes) <= n:
        return out
    gains = []
    losses = []
    for i in range(1, n+1):
        ch = closes[i] - closes[i-1]
        gains.append(max(ch, 0))
        losses.append(max(-ch, 0))
    avg_g = sum(gains)/n
    avg_l = sum(losses)/n
    out[n] = 100 - 100/(1 + avg_g/avg_l) if avg_l > 0 else 100
    for i in range(n+1, len(closes)):
        ch = closes[i] - closes[i-1]
        g = max(ch, 0)
        l = max(-ch, 0)
        avg_g = (avg_g*(n-1) + g) / n
        avg_l = (avg_l*(n-1) + l) / n
        out[i] = 100 - 100/(1 + avg_g/avg_l) if avg_l > 0 else 100
    return out


def equity_curve(daily_rets):
    eq = 100.0
    peak = 100.0
    max_dd = 0.0
    for r in daily_rets:
        eq *= (1 + r/100)
        if eq > peak:
            peak = eq
        dd = (eq/peak - 1)*100
        if dd < max_dd:
            max_dd = dd
    return eq, max_dd


def sharpe(rs):
    if not rs: return 0
    m = mean(rs)
    var = sum((r-m)**2 for r in rs)/max(len(rs)-1,1)
    sd = math.sqrt(var)
    return m/sd*math.sqrt(252) if sd > 0 else 0


def cagr(eq, n):
    if n <= 0 or eq <= 0: return 0
    return (eq/100)**(252/n)*100 - 100


def report_strategy(label, daily_rets, total_days):
    eq, mdd = equity_curve(daily_rets)
    in_market_days = sum(1 for r in daily_rets if r != 0)
    print(f"  {label:<35s}  final={eq:>7.1f}  cagr={cagr(eq,total_days):>+6.1f}%  "
          f"mdd={mdd:>+7.2f}%  sharpe={sharpe(daily_rets):>+5.2f}  "
          f"days_in={in_market_days}/{total_days}")


def test_symbol(sym, name):
    days, closes, rets = load(sym)
    n = len(closes)
    print(f"\n{'='*100}")
    print(f"{name} ({sym}) — {n} days  {days[0]}~{days[-1]}")
    print(f"{'='*100}")

    # baseline
    report_strategy("buy & hold", rets[1:], n-1)

    # MA200 filter (regime)
    ma200 = [None]*n
    for i in range(199, n):
        ma200[i] = mean(closes[i-199:i+1])

    # RSI(2)
    r2 = rsi(closes, 2)

    # ----- 1. RSI(2) < X → buy at close, exit when close > MA5 -----
    ma5 = [None]*n
    for i in range(4, n):
        ma5[i] = mean(closes[i-4:i+1])

    for thresh in [5, 10, 15, 20, 25, 30]:
        # signal at end of day i, hold from i+1 close to first day where close > MA5
        in_pos = False
        entry_close = 0
        daily = []
        for i in range(200, n-1):
            if not in_pos:
                # entry signal at end of day i
                if r2[i] is not None and r2[i] < thresh:
                    # enter at close of day i, get day i+1 return as full first day
                    in_pos = True
                    entry_close = closes[i]
                    daily.append(rets[i+1])
                else:
                    daily.append(0)
            else:
                # check exit at end of day i
                if ma5[i] is not None and closes[i] > ma5[i]:
                    in_pos = False
                    daily.append(0)
                else:
                    daily.append(rets[i+1])
        report_strategy(f"RSI2<{thresh} exit>MA5", daily, len(daily))

    # ----- 2. RSI(2) < X + MA200 filter (only in uptrend) -----
    print()
    for thresh in [10, 15, 20, 25]:
        in_pos = False
        daily = []
        for i in range(200, n-1):
            if not in_pos:
                if (r2[i] is not None and r2[i] < thresh
                    and ma200[i] is not None and closes[i] > ma200[i]):
                    in_pos = True
                    daily.append(rets[i+1])
                else:
                    daily.append(0)
            else:
                if ma5[i] is not None and closes[i] > ma5[i]:
                    in_pos = False
                    daily.append(0)
                else:
                    daily.append(rets[i+1])
        report_strategy(f"RSI2<{thresh} +MA200uptrend", daily, len(daily))

    # ----- 3. N-day consecutive down -----
    print()
    for n_down in [2, 3, 4, 5]:
        # entry: N consecutive down days. exit: 1 up day
        in_pos = False
        daily = []
        for i in range(200, n-1):
            if not in_pos:
                downs = all(rets[j] < 0 for j in range(i-n_down+1, i+1))
                if downs and closes[i] > ma200[i]:
                    in_pos = True
                    daily.append(rets[i+1])
                else:
                    daily.append(0)
            else:
                if rets[i] > 0:  # any up day → exit at end
                    in_pos = False
                    daily.append(0)
                else:
                    daily.append(rets[i+1])
        report_strategy(f"{n_down}일연속하락+MA200", daily, len(daily))

    # ----- 4. Z-score reversal -----
    print()
    for win in [10, 20]:
        for z_thresh in [-1.5, -2.0, -2.5]:
            in_pos = False
            daily = []
            for i in range(200, n-1):
                if i < win:
                    daily.append(0); continue
                w = closes[i-win+1:i+1]
                m = mean(w)
                sd = math.sqrt(sum((x-m)**2 for x in w)/(win-1))
                if sd == 0:
                    daily.append(0); continue
                z = (closes[i] - m) / sd
                if not in_pos:
                    if z < z_thresh and closes[i] > ma200[i]:
                        in_pos = True
                        daily.append(rets[i+1])
                    else:
                        daily.append(0)
                else:
                    if z > 0:
                        in_pos = False
                        daily.append(0)
                    else:
                        daily.append(rets[i+1])
            report_strategy(f"Z{win}<{z_thresh} +MA200", daily, len(daily))


for sym, name in [("069500", "KODEX200"), ("122630", "KODEX레버리지")]:
    test_symbol(sym, name)
