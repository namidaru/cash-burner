"""daily_compact.py — 장 마감 후 일일 로그 압축 + 요약 생성.

사용법:
    python src/daily_compact.py                    # 오늘 날짜
    python src/daily_compact.py 20260320           # 특정 날짜
    python src/daily_compact.py 20260316 20260320  # 여러 날짜

동작:
  1. ws_dump에서 H0STASP0 다운샘플링(1/5 유지) → _compact.log (백테스트용, ~72% 크기)
  2. 원본 ws_dump gzip 압축
  3. 일일 요약 생성 (summary.txt)

기존 파일 구조:
  data/logs/{date}/ws_dump.log         (원본)
  data/logs/{date}/ws_dump_compact.log (H0STASP0 제거, 백테스트용)
  data/logs/{date}/ws_dump.log.gz      (원본 gzip)
  data/logs/{date}/summary.txt         (일일 요약)

레거시 경로(data/ws_dump_{date}.log)도 지원.
"""
from __future__ import annotations

import gzip
import glob
import os
import sys
import time
from collections import defaultdict

os.chdir(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, "src")

from parser import extract_raw, parse_ws_raw_multi


def find_dump(date_str: str) -> str | None:
    """날짜에 해당하는 ws_dump 파일 탐색."""
    candidates = [
        os.path.join("data", "logs", date_str, "ws_dump.log"),
        os.path.join("data", f"ws_dump_{date_str}.log"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


def find_signal_diag(date_str: str) -> str | None:
    candidates = [
        os.path.join("data", "logs", date_str, "signal_diag.log"),
        os.path.join("data", "signal_diag.log"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


def find_ledger() -> str:
    return os.path.join("data", "ledger_real.csv")


def _out_dir(date_str: str) -> str:
    """날짜별 출력 폴더. 없으면 생성."""
    d = os.path.join("data", "logs", date_str)
    os.makedirs(d, exist_ok=True)
    return d


def compact_dump(src: str, date_str: str, asp_keep_every: int = 5) -> tuple[str, dict]:
    """H0STASP0 다운샘플링 compact 파일 생성 + 통계 수집.

    asp_keep_every: 같은 종목의 H0STASP0을 N번째마다 1개만 유지.
      5 = 호가 데이터 80% 제거 (데이터 유실 최소화 + 용량 절감).
      0 = H0STASP0 전량 제거 (기존 동작).
    """
    dst_dir = _out_dir(date_str)
    dst = os.path.join(dst_dir, "ws_dump_compact.log")

    stats: dict = {
        "total_lines": 0,
        "kept_lines": 0,
        "h0stcnt0": 0,
        "h0stasp0": 0,
        "h0stasp0_kept": 0,
        "h0stanc0": 0,
        "other": 0,
        "symbols": set(),
        "first_ts": "",
        "last_ts": "",
        "trades_by_sym": defaultdict(int),
    }
    asp_counter: dict[str, int] = {}  # 종목별 H0STASP0 카운터

    with open(src, "r", encoding="utf-8") as fin, \
         open(dst, "w", encoding="utf-8") as fout:
        for line in fin:
            stats["total_lines"] += 1
            if line.startswith("#"):
                fout.write(line)
                stats["kept_lines"] += 1
                continue

            # 타임스탬프 추출
            ts_part = line[:19] if len(line) >= 19 else ""
            if not stats["first_ts"] and ts_part:
                stats["first_ts"] = ts_part
            if ts_part:
                stats["last_ts"] = ts_part

            if "|H0STASP0|" in line:
                stats["h0stasp0"] += 1
                if asp_keep_every <= 0:
                    continue  # 전량 제거
                # 종목코드 추출 → 다운샘플링
                parts = line.split("|")
                sym = ""
                if len(parts) >= 4:
                    sym = parts[3].split("^")[0] if "^" in parts[3] else ""
                cnt = asp_counter.get(sym, 0) + 1
                asp_counter[sym] = cnt
                if cnt % asp_keep_every != 1:
                    continue  # 1, 6, 11, 16, ... 번째만 유지
                stats["h0stasp0_kept"] += 1
                fout.write(line)
                stats["kept_lines"] += 1
                continue

            fout.write(line)
            stats["kept_lines"] += 1

            if "|H0STCNT0|" in line:
                stats["h0stcnt0"] += 1
                # 종목코드 추출 (간이)
                parts = line.split("|")
                if len(parts) >= 4:
                    sym = parts[3].split("^")[0] if "^" in parts[3] else ""
                    if sym and len(sym) == 6:
                        stats["symbols"].add(sym)
                        stats["trades_by_sym"][sym] += 1
            elif "|H0STANC0|" in line:
                stats["h0stanc0"] += 1
            else:
                stats["other"] += 1

    return dst, stats


def gzip_file(src: str, date_str: str) -> str:
    """원본 파일 gzip 압축 → 날짜 폴더에 저장."""
    dst = os.path.join(_out_dir(date_str), "ws_dump.log.gz")
    if os.path.exists(dst):
        return dst
    with open(src, "rb") as fin, gzip.open(dst, "wb") as fout:
        while True:
            chunk = fin.read(1024 * 1024)
            if not chunk:
                break
            fout.write(chunk)
    return dst


def parse_ledger_for_date(date_str: str) -> list[dict]:
    """ledger_real.csv에서 해당 날짜 거래만 추출."""
    ledger_path = find_ledger()
    if not os.path.isfile(ledger_path):
        return []

    date_disp = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
    trades = []
    with open(ledger_path, "r", encoding="utf-8") as f:
        header = f.readline().strip().split(",")
        for line in f:
            parts = line.strip().split(",", 7)
            if len(parts) < 7:
                continue
            ts_val = float(parts[0])
            ts_date = time.strftime("%Y%m%d", time.localtime(ts_val))
            if ts_date != date_str:
                continue
            trades.append({
                "ts": ts_val,
                "time": time.strftime("%H:%M:%S", time.localtime(ts_val)),
                "action": parts[1],
                "sym": parts[2],
                "qty": int(parts[3]),
                "price": float(parts[4]),
                "reason": parts[5].strip('"'),
                "rt_cd": parts[6],
            })
    return trades


def parse_signal_diag_for_date(diag_path: str, date_str: str) -> dict:
    """signal_diag.log에서 해당 날짜 진입 시도/차단 통계 추출."""
    date_disp = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
    result = {
        "fast_path_attempts": 0,
        "fast_path_entries": 0,
        "gate_blocks": defaultdict(int),
        "scores": [],
    }

    if not os.path.isfile(diag_path):
        return result

    with open(diag_path, "r", encoding="utf-8") as f:
        for line in f:
            if date_disp not in line[:19]:
                continue
            if "FAST_PATH" in line or "fast_path" in line:
                result["fast_path_attempts"] += 1
                if "BUY" in line or "ENTER" in line:
                    result["fast_path_entries"] += 1
            if "GATE_BLOCK" in line or "gate_block" in line:
                # 차단 사유 추출
                for part in line.split():
                    if "=" in part:
                        key = part.split("=")[0]
                        if "gate" in key.lower() or "block" in key.lower():
                            result["gate_blocks"][key] += 1
            if "score=" in line:
                try:
                    score_str = line.split("score=")[1].split()[0].split(",")[0]
                    result["scores"].append(float(score_str))
                except Exception:
                    pass

    return result


def generate_summary(date_str: str, dump_stats: dict, trades: list[dict], diag: dict) -> str:
    """일일 요약 텍스트 생성."""
    lines = []
    date_disp = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
    lines.append(f"=== Daily Summary: {date_disp} ===\n")

    # 데이터 범위
    lines.append(f"[Data]")
    lines.append(f"  Range: {dump_stats['first_ts']} ~ {dump_stats['last_ts']}")
    lines.append(f"  Total lines: {dump_stats['total_lines']:,}")
    lines.append(f"  H0STCNT0 (체결): {dump_stats['h0stcnt0']:,}")
    asp_kept = dump_stats.get('h0stasp0_kept', 0)
    asp_total = dump_stats['h0stasp0']
    lines.append(f"  H0STASP0 (호가): {asp_total:,} → compact {asp_kept:,} ({asp_kept*100//max(1,asp_total)}% 유지)")
    lines.append(f"  H0STANC0 (동시호가): {dump_stats['h0stanc0']:,}")
    lines.append(f"  Symbols tracked: {len(dump_stats['symbols'])}")
    lines.append("")

    # 거래 요약
    buys = [t for t in trades if t["action"] == "BUY"]
    sells = [t for t in trades if t["action"] == "SELL"]
    lines.append(f"[Trades]")
    lines.append(f"  Buys: {len(buys)}, Sells: {len(sells)}")

    # 종목별 P&L
    buy_map = {}
    pairs = []
    for t in trades:
        if t["action"] == "BUY":
            buy_map[t["sym"]] = t
        elif t["action"] == "SELL" and t["sym"] in buy_map:
            b = buy_map.pop(t["sym"])
            pnl_pct = (t["price"] - b["price"]) / b["price"] * 100
            pnl_won = (t["price"] - b["price"]) * b["qty"]
            pairs.append({
                "sym": t["sym"],
                "buy_time": b["time"],
                "sell_time": t["time"],
                "buy_price": b["price"],
                "sell_price": t["price"],
                "qty": b["qty"],
                "pnl_pct": pnl_pct,
                "pnl_won": pnl_won,
                "reason": t["reason"],
                "hold_sec": t["ts"] - b["ts"],
            })

    if pairs:
        lines.append("")
        lines.append(f"  {'Sym':>8s}  {'Buy':>8s}  {'Sell':>8s}  {'P&L%':>7s}  {'P&L₩':>10s}  {'Hold':>6s}  Reason")
        lines.append(f"  {'─'*8}  {'─'*8}  {'─'*8}  {'─'*7}  {'─'*10}  {'─'*6}  {'─'*12}")
        total_pnl = 0
        for p in pairs:
            hold_m = int(p["hold_sec"]) // 60
            hold_s = int(p["hold_sec"]) % 60
            lines.append(
                f"  {p['sym']:>8s}  {p['buy_time']:>8s}  {p['sell_time']:>8s}  "
                f"{p['pnl_pct']:>+6.2f}%  {p['pnl_won']:>+10,.0f}  "
                f"{hold_m:>2d}m{hold_s:02d}s  {p['reason']}"
            )
            total_pnl += p["pnl_won"]

        wins = sum(1 for p in pairs if p["pnl_pct"] > 0)
        losses = sum(1 for p in pairs if p["pnl_pct"] <= 0)
        lines.append("")
        lines.append(f"  Total P&L: {total_pnl:+,.0f}₩  ({wins}W/{losses}L)")
        if pairs:
            avg_pnl = sum(p["pnl_pct"] for p in pairs) / len(pairs)
            lines.append(f"  Avg P&L: {avg_pnl:+.2f}%")
            best = max(pairs, key=lambda p: p["pnl_pct"])
            worst = min(pairs, key=lambda p: p["pnl_pct"])
            lines.append(f"  Best:  {best['sym']} {best['pnl_pct']:+.2f}%")
            lines.append(f"  Worst: {worst['sym']} {worst['pnl_pct']:+.2f}%")

    # 미청산
    if buy_map:
        lines.append("")
        lines.append(f"  Open positions: {', '.join(buy_map.keys())}")

    lines.append("")

    # 활성 종목별 틱 수
    lines.append(f"[Tick Volume by Symbol (top 15)]")
    sorted_syms = sorted(dump_stats["trades_by_sym"].items(), key=lambda x: -x[1])[:15]
    for sym, cnt in sorted_syms:
        lines.append(f"  {sym}: {cnt:,} ticks")

    lines.append("")

    # 파일 크기
    lines.append(f"[File Sizes]")
    lines.append(f"  Compact kept: {dump_stats['kept_lines']:,} / {dump_stats['total_lines']:,} "
                 f"({dump_stats['kept_lines']/max(dump_stats['total_lines'],1)*100:.0f}%)")

    lines.append("")
    return "\n".join(lines)


def process_date(date_str: str):
    dump_path = find_dump(date_str)
    if not dump_path:
        print(f"[{date_str}] No dump file found, skip.")
        return

    print(f"[{date_str}] Processing {dump_path} ...", end=" ", flush=True)

    # 1. compact (H0STASP0 제거)
    compact_path, stats = compact_dump(dump_path, date_str)
    orig_size = os.path.getsize(dump_path)
    compact_size = os.path.getsize(compact_path)
    print(f"compact {orig_size//1024//1024}MB→{compact_size//1024//1024}MB", end=" ", flush=True)

    # 2. gzip 원본
    gz_path = gzip_file(dump_path, date_str)
    gz_size = os.path.getsize(gz_path)
    print(f"gz {gz_size//1024//1024}MB", end=" ", flush=True)

    # 3. 원본 삭제 (gz 존재 확인 후)
    if os.path.exists(gz_path) and gz_size > 0:
        os.remove(dump_path)
        print("rm_orig", end=" ", flush=True)

    # 4. 일일 요약
    trades = parse_ledger_for_date(date_str)
    diag_path = find_signal_diag(date_str)
    diag = parse_signal_diag_for_date(diag_path, date_str) if diag_path else {}

    summary = generate_summary(date_str, stats, trades, diag)
    summary_path = os.path.join(os.path.dirname(compact_path), "summary.txt")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(summary)

    print(f"→ summary.txt  done.")

    # 5. 테마 스캐너 전일 캐시 갱신 (다음날 장전 스코어링용)
    try:
        from scanner_theme import refresh_prev_day_cache
        print(f"[{date_str}] Refreshing theme prev-day cache ...", end=" ", flush=True)
        refresh_prev_day_cache()
        print("done.")
    except Exception as e:
        print(f"skip ({e})")

    # 6. 오래된 로그 정리
    cleanup_old_logs()


def cleanup_old_logs(keep_dump_days: int = 5, keep_small_days: int = 30):
    """오래된 로그 정리.

    보관 정책:
      ws_dump.log / ws_dump_compact.log / ws_dump.log.gz : keep_dump_days (기본 5일)
      ws_control.log                                     : 3일
      signal_diag.log / fastpath_debug.log / theme_debug.log / watchlist_debug.log : keep_small_days (기본 30일)
      daily_summary.json / summary.txt / theme_snapshot.json / selection_snapshot.json : 영구
    """
    import re

    logs_dir = os.path.join("data", "logs")
    if not os.path.isdir(logs_dir):
        return

    today_int = int(time.strftime("%Y%m%d"))
    removed_bytes = 0
    removed_count = 0

    # 영구 보관 파일
    keep_forever = {"summary.txt", "daily_summary.json", "theme_snapshot.json", "selection_snapshot.json"}
    # 단기 보관 (ws_dump 계열)
    dump_files = {"ws_dump.log", "ws_dump_compact.log", "ws_dump.log.gz"}
    # 3일 보관
    very_short = {"ws_control.log"}

    for entry in os.listdir(logs_dir):
        date_dir = os.path.join(logs_dir, entry)
        if not os.path.isdir(date_dir):
            continue
        m = re.match(r"(\d{8})$", entry)
        if not m:
            continue
        date_int = int(m.group(1))
        age_days = (today_int - date_int)  # 근사값 (월 경계 무시해도 충분)
        # 날짜 차이를 정확히 계산하면 좋지만, 로그 정리 목적으로는 근사 충분
        # 만약 월 경계(예: 0331→0401=70)가 걸리면 실제보다 크게 나옴 → 보수적(더 빨리 삭제)
        # → datetime으로 정확히 계산
        try:
            from datetime import datetime
            d1 = datetime.strptime(str(date_int), "%Y%m%d")
            d2 = datetime.strptime(str(today_int), "%Y%m%d")
            age_days = (d2 - d1).days
        except Exception:
            pass

        if age_days < 0:
            continue  # 미래 날짜 무시

        for fname in os.listdir(date_dir):
            fpath = os.path.join(date_dir, fname)
            if not os.path.isfile(fpath):
                continue
            if fname in keep_forever:
                continue

            should_delete = False
            if fname in dump_files and age_days > keep_dump_days:
                should_delete = True
            elif fname in very_short and age_days > 3:
                should_delete = True
            elif fname not in dump_files and fname not in very_short and age_days > keep_small_days:
                should_delete = True

            if should_delete:
                try:
                    sz = os.path.getsize(fpath)
                    os.remove(fpath)
                    removed_bytes += sz
                    removed_count += 1
                except Exception:
                    pass

        # 빈 폴더 정리
        try:
            if not os.listdir(date_dir):
                os.rmdir(date_dir)
        except Exception:
            pass

    if removed_count > 0:
        print(f"[Cleanup] Removed {removed_count} files, freed {removed_bytes / 1024 / 1024:.1f}MB")
    else:
        print(f"[Cleanup] Nothing to remove")


def split_accumulated_logs():
    """누적 로그 파일들을 날짜별 폴더로 분할.

    대상:
      data/ws_control.log      → YYYY-MM-DD 접두사
      data/signal_diag.log     → unix timestamp 접두사
      data/watchlist_debug.log → YYYY-MM-DD 접두사
      data/fastpath_debug_*.log → 파일명에 날짜 포함
    """
    import re

    # 1. YYYY-MM-DD 접두사 로그 분할
    for fname, out_name in [
        ("data/ws_control.log", "ws_control.log"),
        ("data/watchlist_debug.log", "watchlist_debug.log"),
    ]:
        if not os.path.isfile(fname):
            continue
        print(f"  Splitting {fname} ...", end=" ", flush=True)
        writers: dict[str, object] = {}
        count = 0
        with open(fname, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r"(\d{4})-(\d{2})-(\d{2})", line)
                if not m:
                    continue
                date_str = m.group(1) + m.group(2) + m.group(3)
                if date_str not in writers:
                    d = _out_dir(date_str)
                    writers[date_str] = open(os.path.join(d, out_name), "a", encoding="utf-8")
                writers[date_str].write(line)
                count += 1
        for w in writers.values():
            w.close()
        print(f"{count} lines → {len(writers)} dates")

    # 2. signal_diag.log (unix timestamp 접두사)
    fname = "data/signal_diag.log"
    if os.path.isfile(fname):
        print(f"  Splitting {fname} ...", end=" ", flush=True)
        writers: dict[str, object] = {}
        count = 0
        with open(fname, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r"(\d{10,})", line)
                if not m:
                    continue
                try:
                    ts = float(m.group(1))
                    date_str = time.strftime("%Y%m%d", time.localtime(ts))
                except Exception:
                    continue
                if date_str not in writers:
                    d = _out_dir(date_str)
                    writers[date_str] = open(os.path.join(d, "signal_diag.log"), "a", encoding="utf-8")
                writers[date_str].write(line)
                count += 1
        for w in writers.values():
            w.close()
        print(f"{count} lines → {len(writers)} dates")

    # 3. fastpath_debug 파일 (이미 날짜별)
    for fp in glob.glob("data/fastpath_debug_*.log"):
        m = re.search(r"(\d{8})", fp)
        if not m:
            continue
        date_str = m.group(1)
        dst_dir = _out_dir(date_str)
        dst = os.path.join(dst_dir, "fastpath_debug.log")
        if not os.path.exists(dst):
            import shutil
            shutil.move(fp, dst)
            print(f"  Moved {fp} → {dst}")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--split":
        print("[Split] Splitting accumulated logs into date folders...")
        split_accumulated_logs()
        print("[Split] Done. You can now delete the original accumulated files.")
        return

    if len(sys.argv) > 1:
        dates = sys.argv[1:]
    else:
        dates = [time.strftime("%Y%m%d")]

    for d in dates:
        process_date(d)


if __name__ == "__main__":
    main()
