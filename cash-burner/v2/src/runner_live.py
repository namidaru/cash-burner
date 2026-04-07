# src/runner_live.py
from __future__ import annotations

import json
import os
import sys
import time
from parser import extract_raw, parse_ws_raw_multi
from engine_simple import EngineSimple

# Windows 10+ ANSI VT100 활성화
if os.name == "nt":
    os.system("")

# ── 전광판 렌더러 (v2: 고정높이, 잔상 제거) ──────────────────────────────────

_MAX_POS_ROWS = 5    # 대시보드 포지션 표시 슬롯 (5개 이상이면 스크롤 없이 최근 5개만)
_DASH_HEIGHT  = _MAX_POS_ROWS + 3   # 상단줄 + 포지션 N행 + 하단줄 + 상태행
_DASH_DRAWN   = [False]  # 대시보드가 현재 화면에 존재하는지
_SEP  = "\u2500" * 62
_DIM  = "\033[2m"
_RED  = "\033[91m"
_BLU  = "\033[94m"
_GRN  = "\033[92m"
_Y    = "\033[93m"
_RST  = "\033[0m"


def _erase_dash():
    """고정 높이만큼 커서 올려서 지우기"""
    if not _DASH_DRAWN[0]:
        return
    sys.stdout.write(f"\033[{_DASH_HEIGHT}A")
    for _ in range(_DASH_HEIGHT):
        sys.stdout.write("\033[2K\n")
    sys.stdout.write(f"\033[{_DASH_HEIGHT}A")
    _DASH_DRAWN[0] = False


def _draw_dash(s: dict):
    """고정 _DASH_HEIGHT 줄을 항상 출력 (빈 슬롯은 공백행)"""
    lines: list[str] = [_SEP]

    positions = s.get("positions", [])
    for i in range(_MAX_POS_ROWS):
        if i < len(positions):
            p = positions[i]
            pnl   = p.get("pnl_pct", 0.0)
            peak  = p.get("max_pnl_pct", 0.0)
            col   = _RED if pnl > 0 else (_BLU if pnl < 0 else _RST)
            sym   = p.get("sym", "")
            hold  = p.get("hold_sec", 0)
            entry = p.get("entry_price", 0.0)
            px    = p.get("last_price", 0.0)
            sc    = int(p.get("score", 0))
            lines.append(
                f"  {sym}  "
                f"{col}{pnl:>+6.2f}%{_RST}  "
                f"{_DIM}{entry:>8,.0f}{_RST}→{px:>8,.0f}원  "
                f"고:{peak:>+5.2f}%  {hold:>3}s  [{sc:>3}]"
            )
        else:
            lines.append("")  # 빈 슬롯

    halt     = s.get("trading_halted", False)
    pnl_d    = s.get("daily_realized_pnl", 0.0)
    pnl_col  = _RED if pnl_d > 0 else (_BLU if pnl_d < 0 else _RST)
    halt_tag = f"{_Y}HALT{_RST} " if halt else ""
    ts_short = s.get("ts", "")[-8:]
    lines.append(_SEP)
    lines.append(
        f"  {halt_tag}"
        f"감시 {s.get('watch_count',0):>2}  "
        f"현금 {s.get('orderable_cash_text','-')}  "
        f"일손익 {pnl_col}{pnl_d:>+,.0f}원{_RST}  "
        f"{ts_short}"
    )

    # 정확히 _DASH_HEIGHT 줄 출력 (넘치면 자르기)
    for i in range(_DASH_HEIGHT):
        line = lines[i] if i < len(lines) else ""
        sys.stdout.write(f"\033[2K{line}\n")

    _DASH_DRAWN[0] = True
    sys.stdout.flush()


def _draw_closed_dash():
    """장 마감 / 세션 시작 전 최소 대시보드"""
    lines = [_SEP]
    for _ in range(_MAX_POS_ROWS):
        lines.append("")
    lines.append(_SEP)
    now_s = time.strftime("%H:%M:%S")
    lines.append(f"  {_DIM}장외 시간{_RST}  {now_s}")
    for i in range(_DASH_HEIGHT):
        line = lines[i] if i < len(lines) else ""
        sys.stdout.write(f"\033[2K{line}\n")
    _DASH_DRAWN[0] = True
    sys.stdout.flush()


def _log(msg: str):
    """영구 스크롤 로그 — 대시보드를 지우고, 로그 한 줄 출력, 대시보드는 다음 사이클에서 그려짐"""
    _erase_dash()
    sys.stdout.write(f"\033[2K{msg}\n")
    sys.stdout.flush()


def _drain_engine_msgs(eng):
    """엔진의 _console_msgs 큐를 비우고 영구 로그로 출력"""
    if not eng._console_msgs:
        return
    _erase_dash()
    for msg in eng._console_msgs:
        sys.stdout.write(f"\033[2K{msg}\n")
    sys.stdout.flush()
    eng._console_msgs.clear()


def _is_market_hours() -> bool:
    """현재 시각이 장 시간대(08:50~15:40)인지"""
    hhmm = int(time.strftime("%H%M"))
    return 850 <= hhmm <= 1540


def _clear_stale_status(path: str):
    """시작 시 이전 세션의 runtime_status.json 정리"""
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "watch_count": 0, "position_count": 0,
                "orderable_cash": 0, "orderable_cash_text": "-",
                "top_gate_blockers": [], "score_pass_rate": 0,
                "last_buy_time": "", "last_sell_time": "",
                "last_buy_symbol": "", "last_sell_symbol": "",
                "recent_events": [], "operator_summary": "startup",
                "trading_halted": False, "daily_realized_pnl": 0,
                "top_scored_symbols": [], "positions": []
            }, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ── 유틸 ──────────────────────────────────────────────────────────────────────

def parse_prefix_ts(line: str) -> float:
    if "\t" not in line:
        return time.time()
    prefix = line.split("\t", 1)[0].strip()
    try:
        import calendar
        KST_OFFSET = 32400
        st = time.strptime(prefix, "%Y-%m-%d %H:%M:%S")
        return calendar.timegm(st) - KST_OFFSET
    except Exception:
        return time.time()


def _resolve_in_file(path: str) -> str:
    if "{date}" in path:
        return path.replace("{date}", time.strftime("%Y%m%d"))
    return path



# ── 메인 루프 ─────────────────────────────────────────────────────────────────

def run_live(in_file: str, poll: float = 0.2, cap=None, eng=None):
    _last_timer  = [0.0]
    if eng is None:
        eng = EngineSimple()

    # 이전 세션 잔상 제거
    _clear_stale_status(eng.runtime_status_file)

    last_live_path = ""
    _startup = True

    while True:
        try:
            live_path  = _resolve_in_file(in_file)
            is_new_file = (live_path != last_live_path)
            last_live_path = live_path
            with open(live_path, "r", encoding="utf-8") as f:
                if is_new_file and not _startup:
                    f.seek(0)
                else:
                    f.seek(0, 2)
                _startup = False
                while True:
                    line = f.readline()
                    if line:
                        ts_epoch = parse_prefix_ts(line.rstrip("\n"))
                        raw = extract_raw(line)
                        for tr_id, row in parse_ws_raw_multi(raw):
                            vlen = int(row.get("_values_len", "0"))
                            clen = int(row.get("_cols_len",   "0"))
                            if vlen != clen:
                                continue
                            try:
                                if tr_id == "H0STASP0":
                                    eng.on_orderbook(row, ts_epoch)
                                elif tr_id == "H0STANC0":
                                    eng.on_preopen_tick(row, ts_epoch)
                                else:
                                    eng.on_trade(row, ts_epoch)
                                _drain_engine_msgs(eng)
                            except Exception as e:
                                _log(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [LIVE][WARN] {tr_id} {type(e).__name__}: {e}")
                    else:
                        now = time.time()
                        if now - _last_timer[0] >= 1.0:
                            _last_timer[0] = now
                            try:
                                eng.on_timer(now)
                                _drain_engine_msgs(eng)

                                # 장외 시간: 최소 대시보드만
                                if not _is_market_hours():
                                    _erase_dash()
                                    _draw_closed_dash()
                                    time.sleep(poll)
                                    continue

                                with open(eng.runtime_status_file, "r", encoding="utf-8") as _f:
                                    _s = json.load(_f)

                                # ── 전광판 갱신 ────────────────────────────
                                _erase_dash()
                                _draw_dash(_s)

                            except Exception as e:
                                _log(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [LIVE][WARN] timer {type(e).__name__}: {e}")
                        time.sleep(poll)

        except FileNotFoundError:
            now = time.time()
            if now - _last_timer[0] >= 1.0:
                _last_timer[0] = now
                try:
                    eng.on_timer(now)
                    _drain_engine_msgs(eng)
                except Exception as e:
                    _log(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [LIVE][WARN] timer(no-file) {type(e).__name__}: {e}")
                # 파일 없어도 대시보드 표시
                if not _is_market_hours():
                    _erase_dash()
                    _draw_closed_dash()
            time.sleep(max(1.0, poll))
        except Exception as e:
            _log(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [LIVE][ERR] {type(e).__name__}: {e}")
            time.sleep(1)
