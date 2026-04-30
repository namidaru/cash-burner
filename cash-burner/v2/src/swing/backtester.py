"""백테스트 엔진. 동등가중 + 주기 리밸런싱.

interface:
  select_fn(date: str, prices: dict[sym, DataFrame], hist_idx: int) -> list[sym]
    date 시점에 보유할 종목 리스트 반환. hist_idx = calendar.index(date).

  run_backtest(prices, calendar, select_fn, **kwargs) -> BacktestResult

가정:
- 매 리밸런싱 일자의 종가에 진입/청산 (T 종가 기준).
- 슬리피지/수수료는 양방향 적용 (BUY+SELL 둘 다).
- 동등가중. 리밸런싱 사이엔 가중치 drift 허용.
- 종목별 prices에 해당 date가 없으면 (상폐/거래정지) 그 종목은 다음 가용 종가에 청산.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

SelectFn = Callable[[str, dict[str, pd.DataFrame], int], list[str]]


@dataclass
class BacktestResult:
    equity: pd.Series          # 일별 portfolio value
    weights: pd.DataFrame      # 일별 종목 가중치 (sparse)
    trades: pd.DataFrame       # date, sym, side, price, qty, value, cost
    rebalance_dates: list[str]
    initial_cash: float


def _rebalance_dates(calendar: list[str], freq: str) -> list[str]:
    """freq: 'M' (월말 거래일), 'W' (매주 마지막 거래일=금)."""
    df = pd.DataFrame({"date": calendar})
    df["dt"] = pd.to_datetime(df["date"])
    if freq.upper() == "M":
        df["key"] = df["dt"].dt.to_period("M")
    elif freq.upper() == "W":
        df["key"] = df["dt"].dt.to_period("W")
    else:
        raise ValueError(f"freq must be 'M' or 'W', got {freq}")
    last = df.groupby("key")["date"].max().tolist()
    return sorted(last)


def _close_at(prices: dict[str, pd.DataFrame], sym: str, date: str) -> float | None:
    df = prices.get(sym)
    if df is None or date not in df.index:
        return None
    v = df.at[date, "close"]
    return float(v) if v and v > 0 else None


def _next_available_close(prices: dict[str, pd.DataFrame], sym: str, date: str) -> tuple[str, float] | None:
    df = prices.get(sym)
    if df is None:
        return None
    idx = df.index[df.index >= date]
    if idx.empty:
        return None
    for d in idx:
        v = df.at[d, "close"]
        if v and v > 0:
            return d, float(v)
    return None


def run_backtest(
    prices: dict[str, pd.DataFrame],
    calendar: list[str],
    select_fn: SelectFn,
    start: str,
    end: str,
    rebalance: str = "M",
    initial_cash: float = 1e8,
    slippage_pct: float = 0.003,
    fee_pct: float = 0.00015,
    sell_tax_pct: float = 0.0023,  # 한국 매도세 (KOSPI 기준)
) -> BacktestResult:
    cal = [d for d in calendar if start <= d <= end]
    if not cal:
        raise ValueError(f"no trading days in [{start}, {end}]")
    rb_dates = [d for d in _rebalance_dates(cal, rebalance) if start <= d <= end]
    if cal[0] not in rb_dates:
        rb_dates = [cal[0]] + rb_dates

    cash = initial_cash
    holdings: dict[str, float] = {}  # sym -> qty
    avg_cost: dict[str, float] = {}

    equity_rows = []
    weights_rows = []
    trades = []

    for i, date in enumerate(cal):
        # 1) 리밸런싱 트리거?
        if date in rb_dates:
            target = select_fn(date, prices, i)
            target = [s for s in target if _close_at(prices, s, date) is not None]

            # 1a) 보유 중이지만 target에 없는 종목 청산
            for sym in list(holdings.keys()):
                if sym in target:
                    continue
                close = _close_at(prices, sym, date)
                if close is None:
                    # 거래정지 등 → 다음 가용일에 청산 (단순화: 평균가 기준 marked)
                    nxt = _next_available_close(prices, sym, date)
                    if nxt is None:
                        # 영영 못 팔면 0으로 상각
                        cash += 0.0
                        del holdings[sym]
                        avg_cost.pop(sym, None)
                        continue
                    nxt_date, close = nxt
                else:
                    nxt_date = date
                qty = holdings[sym]
                fill = close * (1 - slippage_pct)
                gross = fill * qty
                cost = gross * (fee_pct + sell_tax_pct)
                cash += gross - cost
                trades.append({"date": nxt_date, "sym": sym, "side": "SELL",
                                "price": fill, "qty": qty, "value": gross, "cost": cost})
                del holdings[sym]
                avg_cost.pop(sym, None)

            # 1b) target에 있고 미보유인 종목 매수 (동등가중)
            new_buys = [s for s in target if s not in holdings]
            if new_buys and target:
                portfolio_val = cash + sum(
                    holdings[s] * _close_at(prices, s, date) for s in holdings
                    if _close_at(prices, s, date) is not None
                )
                target_per_name = portfolio_val / len(target)
                # 이미 보유 중인 종목 가치 차감
                for s in new_buys:
                    close = _close_at(prices, s, date)
                    if close is None:
                        continue
                    fill = close * (1 + slippage_pct)
                    spend = min(target_per_name, cash * 0.99)
                    if spend <= 0:
                        continue
                    qty = int(spend / fill)
                    if qty <= 0:
                        continue
                    gross = fill * qty
                    cost = gross * fee_pct
                    if gross + cost > cash:
                        continue
                    cash -= gross + cost
                    holdings[s] = holdings.get(s, 0) + qty
                    avg_cost[s] = fill
                    trades.append({"date": date, "sym": s, "side": "BUY",
                                    "price": fill, "qty": qty, "value": gross, "cost": cost})

        # 2) MTM equity 계산
        mv = cash
        for s, q in holdings.items():
            close = _close_at(prices, s, date)
            if close is None:
                # forward fill: 최근 가용 종가
                df = prices.get(s)
                if df is not None:
                    past = df.index[df.index <= date]
                    if not past.empty:
                        close = float(df.at[past[-1], "close"])
            if close:
                mv += q * close
        equity_rows.append({"date": date, "equity": mv, "cash": cash, "n_holdings": len(holdings)})

        # weights snapshot (리밸런싱 일자만)
        if date in rb_dates and mv > 0:
            for s, q in holdings.items():
                close = _close_at(prices, s, date)
                if close:
                    weights_rows.append({"date": date, "sym": s, "weight": q * close / mv})

    eq = pd.DataFrame(equity_rows).set_index("date")
    eq.index = pd.to_datetime(eq.index)
    return BacktestResult(
        equity=eq["equity"],
        weights=pd.DataFrame(weights_rows),
        trades=pd.DataFrame(trades),
        rebalance_dates=rb_dates,
        initial_cash=initial_cash,
    )
