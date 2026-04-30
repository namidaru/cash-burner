"""갭 reversion 백테스트 (단일홀딩 = 1일).

기존 backtester (월/주 리밸)와 별도. 매일 진입/청산하는 패턴이라 단순화.

가설:
  - gap = open / prev_close - 1
  - gap < -3% (갭다운) → 시가 매수, 당일 종가 또는 익일 종가 청산
  - 한국시장 단기 mean-reversion 알파 (오버나잇 패닉 → intraday/익일 회복)

검증 변형:
  hold='intraday'  : open → close (당일)
  hold='overnight' : close → next_open (오버나잇)
  hold='next_day'  : open → next_close (1.5일 홀딩)

필터:
  min_value: 일평균 거래대금
  exclude_surge_5d: 5일 누적 +30%↑ 종목 제외 (오버슛 reversion 따로)
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class GapResult:
    equity: pd.Series
    daily_returns: pd.Series
    n_trades: int
    avg_n_per_day: float
    initial_cash: float


def _avg_value(df: pd.DataFrame, end_idx: int, lookback: int = 60) -> float:
    if end_idx < lookback:
        return 0
    return float(df["value"].iloc[end_idx - lookback:end_idx].mean())


def run_gap_backtest(
    prices: dict[str, pd.DataFrame],
    calendar: list[str],
    start: str,
    end: str,
    gap_threshold: float = -0.03,
    topn: int = 20,
    hold: str = "intraday",
    min_value: float = 3e9,
    initial_cash: float = 1e8,
    slippage_pct: float = 0.003,
    fee_pct: float = 0.00015,
    sell_tax_pct: float = 0.0023,
    exclude_surge_5d: float = 0.30,
) -> GapResult:
    """매일 갭다운 종목 동등가중 매수 → hold 모드대로 청산.

    수익률 계산 (단순화 — 슬리피지/수수료 양방향 적용):
      raw = exit_price / entry_price - 1
      cost = (slip + fee) + (slip + fee + sell_tax)
      net = raw - cost - (실제로는 raw에 slip 포함)

    구현:
      entry = open * (1 + slip)
      exit  = exit_raw * (1 - slip)
      ret_per_name = exit / entry - 1 - (fee + fee + sell_tax)
    """
    cal = [d for d in calendar if start <= d <= end]
    if len(cal) < 2:
        raise ValueError("not enough calendar days")

    # 종목별 인덱스 캐시 (date string → row pos)
    sym_pos = {s: {d: i for i, d in enumerate(df.index)} for s, df in prices.items()}

    daily_rets = []
    n_trades_total = 0

    for di in range(len(cal) - 1):
        date = cal[di]
        next_date = cal[di + 1] if di + 1 < len(cal) else None

        candidates = []
        for sym, df in prices.items():
            pos = sym_pos[sym].get(date)
            if pos is None or pos < 1:
                continue
            prev_close = df["close"].iloc[pos - 1]
            o = df["open"].iloc[pos]
            c = df["close"].iloc[pos]
            if prev_close <= 0 or o <= 0 or c <= 0:
                continue
            gap = o / prev_close - 1
            if gap > gap_threshold:
                continue
            if _avg_value(df, pos, 60) < min_value:
                continue
            # 5일 surge 제외 (오버슛 reversion이면 다른 동학)
            if exclude_surge_5d and pos >= 5:
                p5 = df["close"].iloc[pos - 5]
                if p5 > 0 and prev_close / p5 - 1 > exclude_surge_5d:
                    continue

            # 청산가 결정
            if hold == "intraday":
                exit_p = c
            elif hold == "overnight":
                if next_date is None:
                    continue
                npos = sym_pos[sym].get(next_date)
                if npos is None:
                    continue
                exit_p = df["open"].iloc[npos]
            elif hold == "next_day":
                if next_date is None:
                    continue
                npos = sym_pos[sym].get(next_date)
                if npos is None:
                    continue
                exit_p = df["close"].iloc[npos]
            else:
                raise ValueError(f"unknown hold: {hold}")
            if exit_p <= 0:
                continue

            entry_p = o * (1 + slippage_pct)
            sell_p = exit_p * (1 - slippage_pct)
            costs = fee_pct * 2 + sell_tax_pct  # buy fee + sell fee + sell tax
            ret = sell_p / entry_p - 1 - costs

            candidates.append({"sym": sym, "gap": gap, "ret": ret})

        if not candidates:
            daily_rets.append(0.0)
            continue

        # 갭 깊은 순 (가장 reversion 강할 것으로 가정) topn
        df_c = pd.DataFrame(candidates).sort_values("gap").head(topn)
        port_ret = float(df_c["ret"].mean())  # 동등가중
        daily_rets.append(port_ret)
        n_trades_total += len(df_c)

    rets_s = pd.Series(daily_rets, index=pd.to_datetime(cal[:len(daily_rets)]))
    eq = (1 + rets_s).cumprod() * initial_cash
    avg_n = n_trades_total / max(len(daily_rets), 1)
    return GapResult(
        equity=eq,
        daily_returns=rets_s,
        n_trades=n_trades_total,
        avg_n_per_day=avg_n,
        initial_cash=initial_cash,
    )


def report_gap(result: GapResult, label: str = "") -> None:
    eq = result.equity
    rets = result.daily_returns
    if len(rets) < 2:
        print("not enough data")
        return

    n_days = len(rets)
    n_years = n_days / 252.0
    total = float(eq.iloc[-1] / result.initial_cash - 1)
    cagr = float((eq.iloc[-1] / result.initial_cash) ** (1 / max(n_years, 1e-6)) - 1)
    vol = float(rets.std() * np.sqrt(252))
    sharpe = float(rets.mean() / (rets.std() + 1e-12) * np.sqrt(252))
    cummax = eq.cummax()
    mdd = float((eq / cummax - 1).min())
    win = float((rets[rets != 0] > 0).mean()) if (rets != 0).sum() > 0 else 0
    active_days = int((rets != 0).sum())

    print(f"\n=== Gap Backtest {label} ===")
    print(f"기간: {eq.index[0].date()}~{eq.index[-1].date()} ({n_days}일, {n_years:.2f}년)")
    print(f"초기:{result.initial_cash:,.0f} → 최종:{eq.iloc[-1]:,.0f}")
    print(f"총수익: {total*100:>+7.2f}%   CAGR: {cagr*100:>+6.2f}%")
    print(f"변동성: {vol*100:>7.2f}%   Sharpe: {sharpe:>+5.2f}")
    print(f"MDD:    {mdd*100:>+7.2f}%   진입일수: {active_days}/{n_days}, 일평균종목수 {result.avg_n_per_day:.1f}")
    print(f"진입일 승률: {win*100:>5.1f}%   총거래: {result.n_trades}")

    # 연도별
    rets_year = rets.groupby(rets.index.year)
    print("연도별:")
    for y, r in rets_year:
        if len(r) < 5:
            continue
        ret_y = float((1 + r).prod() - 1)
        sh_y = float(r.mean() / (r.std() + 1e-12) * np.sqrt(252)) if r.std() > 0 else 0
        print(f"  {y}: {ret_y*100:>+7.2f}%  sharpe {sh_y:>+5.2f}  ({len(r)}일)")
