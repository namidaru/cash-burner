#!/usr/bin/env python3
"""갭 reversion 백테스트 CLI.

사용법:
  python run_gap_bt.py --start 20200101 --end 20260401 --hold intraday
  python run_gap_bt.py --hold next_day --gap -0.05 --topn 10
  python run_gap_bt.py --all-variants
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE / "src"))

from swing.data_loader import list_cached_symbols, load_universe, load_calendar
from swing.gap_backtest import run_gap_backtest, report_gap


def run_one(prices, cal, args, hold, gap_th, topn, label):
    r = run_gap_backtest(
        prices=prices, calendar=cal,
        start=args.start, end=args.end,
        gap_threshold=gap_th, topn=topn, hold=hold,
        min_value=args.min_value * 1e8,
        initial_cash=args.cash,
        slippage_pct=args.slippage,
        fee_pct=args.fee,
        sell_tax_pct=args.sell_tax,
        exclude_surge_5d=args.exclude_surge,
    )
    report_gap(r, label=label)
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20200101")
    ap.add_argument("--end", default="20260401")
    ap.add_argument("--hold", default="intraday", choices=["intraday", "overnight", "next_day"])
    ap.add_argument("--gap", type=float, default=-0.03)
    ap.add_argument("--topn", type=int, default=20)
    ap.add_argument("--min-value", type=float, default=30.0)
    ap.add_argument("--cash", type=float, default=1e8)
    ap.add_argument("--slippage", type=float, default=0.003)
    ap.add_argument("--fee", type=float, default=0.00015)
    ap.add_argument("--sell-tax", type=float, default=0.0023)
    ap.add_argument("--exclude-surge", type=float, default=0.30)
    ap.add_argument("--all-variants", action="store_true")
    args = ap.parse_args()

    syms = list_cached_symbols()
    print(f"유니버스: {len(syms)}종목 캐시")
    prices = load_universe(syms, min_value=args.min_value * 1e8)
    print(f"  거래대금 {args.min_value}억+ 필터: {len(prices)}종목")

    cal = load_calendar()
    print(f"  캘린더: {len(cal)}거래일\n")

    if args.all_variants:
        # 갭 임계 × 홀딩 모드 × topn 변형
        configs = [
            ("intraday_gap3_n20", "intraday", -0.03, 20),
            ("intraday_gap5_n10", "intraday", -0.05, 10),
            ("overnight_gap3_n20", "overnight", -0.03, 20),
            ("nextday_gap3_n20", "next_day", -0.03, 20),
            ("nextday_gap5_n10", "next_day", -0.05, 10),
        ]
        results = {}
        for label, hold, gap_th, topn in configs:
            results[label] = run_one(prices, cal, args, hold, gap_th, topn, label)

        # 비교표
        print("\n=== 변형 비교 ===")
        print(f"  {'config':<22} {'CAGR':>8} {'Sharpe':>8} {'MDD':>8} {'n_trades':>9}")
        for label, r in results.items():
            import numpy as np
            eq = r.equity
            n_y = len(r.daily_returns) / 252
            cagr = (eq.iloc[-1] / r.initial_cash) ** (1/max(n_y, 1e-6)) - 1
            sh = r.daily_returns.mean() / (r.daily_returns.std() + 1e-12) * np.sqrt(252)
            mdd = (eq / eq.cummax() - 1).min()
            print(f"  {label:<22} {cagr*100:>7.2f}% {sh:>+8.2f} {mdd*100:>+7.2f}% {r.n_trades:>9}")
    else:
        run_one(prices, cal, args, args.hold, args.gap, args.topn, "single")


if __name__ == "__main__":
    main()
