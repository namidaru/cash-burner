"""scanner_theme.py — 네이버 금융 테마 크롤링 기반 워치리스트 빌더.

독립 모듈. 기존 scanner_company_rank.py 와 코드 공유 없음.
출력: build_watchlist_theme() -> List[str]  (종목코드 리스트)

사용법:
  SCANNER_MODE=theme  in run_real_auto.cmd
"""
from __future__ import annotations

import os
import re
import time
import math
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# 로그
# ---------------------------------------------------------------------------
_LOG_FILE: str = ""  # main_real.py 가 설정

def _init_log():
    global _LOG_FILE
    if _LOG_FILE:
        return
    # signal_diag 와 같은 날짜 폴더 사용
    base = os.getenv("SIGNAL_DIAG_FILE", os.path.join("data", "signal_diag.log"))
    log_dir = os.path.dirname(base)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    _LOG_FILE = os.path.join(log_dir, "theme_debug.log") if log_dir else "data/theme_debug.log"


def _log(msg: str):
    _init_log()
    try:
        d = os.path.dirname(_LOG_FILE)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{msg}\n")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
_BASE = "https://finance.naver.com"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
_TIMEOUT = 8  # seconds

# 대형주 블랙리스트 (scanner_company_rank 와 동일 기본값)
def _largecap_bl() -> set:
    raw = os.getenv(
        "WATCH_LARGECAP_BLACKLIST",
        "005930,000660,035420,005380,005490,051910,006400,035720,068270,028260",
    )
    bl = set(s.strip() for s in raw.split(",") if s.strip())
    user = os.getenv("WATCH_BLACKLIST", "")
    bl |= set(s.strip() for s in user.split(",") if s.strip())
    return bl

# ---------------------------------------------------------------------------
# 크롤링: 테마 목록
# ---------------------------------------------------------------------------

def _fetch_html(url: str) -> Optional[BeautifulSoup]:
    """URL fetch → BeautifulSoup. 실패 시 None."""
    try:
        r = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        r.raise_for_status()
        return BeautifulSoup(r.content, "html.parser", from_encoding="euc-kr")
    except Exception as e:
        _log(f"FETCH_ERR url={url} err={type(e).__name__}: {e}")
        return None


def _parse_pct(text: str) -> float:
    """'+9.30%' 또는 '-1.20%' → float. 파싱 실패 시 0.0."""
    text = text.strip().replace(",", "").replace("%", "").replace("+", "")
    try:
        return float(text)
    except Exception:
        return 0.0


def _parse_int(text: str) -> int:
    text = text.strip().replace(",", "")
    try:
        return int(text)
    except Exception:
        return 0


def fetch_theme_list(max_pages: int = 3) -> List[Dict[str, Any]]:
    """네이버 테마 목록에서 상위 테마 추출.

    Returns:
        [{"no": "167", "name": "테마명", "chg_pct": 9.3, "chg_3d": 5.1,
          "total": 30, "up": 20, "down": 5, "lead1": "065530", "lead2": "012340"}, ...]
    """
    themes: List[Dict[str, Any]] = []
    seen_nos: set = set()

    for page in range(1, max_pages + 1):
        url = f"{_BASE}/sise/theme.naver?&page={page}"
        soup = _fetch_html(url)
        if soup is None:
            _log(f"THEME_LIST page={page} FAILED")
            continue

        table = soup.find("table", class_="type_1")
        if not table:
            _log(f"THEME_LIST page={page} no table")
            continue

        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 8:
                continue  # separator row

            # 테마명 + 링크
            link = cells[0].find("a", href=lambda h: h and "sise_group_detail" in h)
            if not link:
                continue
            href = link.get("href", "")
            m = re.search(r"no=(\d+)", href)
            if not m:
                continue
            theme_no = m.group(1)
            if theme_no in seen_nos:
                continue
            seen_nos.add(theme_no)

            name = link.get_text(strip=True)
            chg_pct = _parse_pct(cells[1].get_text())
            chg_3d = _parse_pct(cells[2].get_text())
            total = _parse_int(cells[3].get_text())
            up_cnt = _parse_int(cells[4].get_text())
            down_cnt = _parse_int(cells[5].get_text())

            # 주도주 코드 추출
            def _extract_code(cell) -> str:
                a = cell.find("a", href=lambda h: h and "/item/main.naver" in h)
                if a:
                    mc = re.search(r"code=(\d{6})", a.get("href", ""))
                    if mc:
                        return mc.group(1)
                return ""

            lead1 = _extract_code(cells[6])
            lead2 = _extract_code(cells[7])

            themes.append({
                "no": theme_no, "name": name,
                "chg_pct": chg_pct, "chg_3d": chg_3d,
                "total": total, "up": up_cnt, "down": down_cnt,
                "lead1": lead1, "lead2": lead2,
            })

    _log(f"THEME_LIST fetched={len(themes)} themes from {max_pages} pages")
    return themes


# ---------------------------------------------------------------------------
# 크롤링: 테마 구성종목
# ---------------------------------------------------------------------------

def fetch_theme_stocks(theme_no: str) -> List[Dict[str, Any]]:
    """테마 상세 페이지에서 구성종목 추출.

    Returns:
        [{"code": "065530", "name": "종목명", "price": 12345, "chg_pct": 5.0,
          "volume": 123456, "tr_value": 987654321}, ...]
    """
    url = f"{_BASE}/sise/sise_group_detail.naver?type=theme&no={theme_no}"
    soup = _fetch_html(url)
    if soup is None:
        return []

    table = soup.find("table", class_="type_5")
    if not table:
        return []

    stocks: List[Dict[str, Any]] = []
    rows = table.find_all("tr")
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 10:
            continue

        link = cells[0].find("a", href=lambda h: h and "/item/main.naver?code=" in h)
        if not link:
            continue
        m = re.search(r"code=(\d{6})", link.get("href", ""))
        if not m:
            continue

        code = m.group(1)
        name = link.get_text(strip=True)
        price = _parse_int(cells[2].get_text())
        chg_pct = _parse_pct(cells[4].get_text())
        volume = _parse_int(cells[7].get_text())
        tr_value_raw = cells[8].get_text(strip=True).replace(",", "")
        try:
            tr_value = int(tr_value_raw) * 1_000_000  # 네이버: 백만원 단위
        except Exception:
            tr_value = 0

        stocks.append({
            "code": code, "name": name, "price": price,
            "chg_pct": chg_pct, "volume": volume, "tr_value": tr_value,
        })

    return stocks


# ---------------------------------------------------------------------------
# 전일 데이터 캐시 (장전 크롤링 시 종목별 chg/vol/tr_value가 0인 문제 해결)
# ---------------------------------------------------------------------------
_PREV_CACHE_FILE = os.path.join("data", "theme_prev_cache.json")


def save_prev_day_cache(themes: List[Dict[str, Any]], stock_info: Dict[str, Dict[str, Any]]):
    """장 후 크롤링 데이터를 캐시 파일에 저장. 다음날 장전에 사용."""
    try:
        import json
        cache = {
            "date": time.strftime("%Y%m%d"),
            "themes": {t["no"]: {"up": t["up"], "down": t["down"], "total": t["total"]}
                       for t in themes},
            "stocks": {code: {"chg_pct": info["chg_pct"], "tr_value": info["tr_value"]}
                       for code, info in stock_info.items()},
        }
        with open(_PREV_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        _log(f"PREV_CACHE saved {len(cache['stocks'])} stocks {len(cache['themes'])} themes")
    except Exception as e:
        _log(f"PREV_CACHE save err: {e}")


def _load_prev_cache() -> Dict:
    """전일 캐시 로드. 없으면 빈 dict."""
    try:
        import json
        with open(_PREV_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def refresh_prev_day_cache():
    """장 후 호출용: 네이버 크롤링 → 전일 캐시 갱신. daily_compact 등에서 사용.

    모든 테마(3페이지)의 구성종목을 전부 크롤링해서 캐시에 저장.
    다음날 장전에는 chg_3d 기준으로 다른 테마가 뽑힐 수 있으므로 넓게 수집.
    """
    _log("REFRESH_CACHE start")
    theme_pages = int(os.getenv("THEME_PAGES", "3"))

    all_themes = fetch_theme_list(max_pages=theme_pages)
    if not all_themes:
        _log("REFRESH_CACHE no themes")
        return

    # 전체 테마 수집 — 다음날 chg_3d 기준으로 어떤 테마가 선택될지 모르므로
    targets = all_themes
    if not targets:
        _log("REFRESH_CACHE no themes to crawl")
        return

    stock_info: Dict[str, Dict[str, Any]] = {}
    for t in targets:
        stocks = fetch_theme_stocks(t["no"])
        for s in stocks:
            code = s["code"]
            if code not in stock_info:
                stock_info[code] = {"chg_pct": s["chg_pct"], "tr_value": s["tr_value"]}

    save_prev_day_cache(targets, stock_info)
    _log(f"REFRESH_CACHE done: {len(targets)} themes, {len(stock_info)} stocks")


# ---------------------------------------------------------------------------
# 핵심: 테마 교차 빈도 기반 종목 선별
# ---------------------------------------------------------------------------

# 모듈 레벨 상태 (main_real.py scanner_loop 에서 get_* 으로 조회)
_LAST_META: str = ""
_LAST_THEME_DETAIL: List[str] = []  # 로그용 상세
_LAST_SCORES: Dict[str, float] = {}  # {sym: score} — preopen에서 참조
_SNAPSHOT_SAVED_TODAY: str = ""


def get_last_build_meta() -> str:
    return _LAST_META


def get_last_scores() -> Dict[str, float]:
    """스캐너 스코어 맵 반환. preopen quality에 합산용."""
    return dict(_LAST_SCORES)


def get_last_theme_detail() -> List[str]:
    return list(_LAST_THEME_DETAIL)


def build_watchlist_theme() -> List[str]:
    """네이버 테마 크롤링 → 교차 빈도 스코어링 → 상위 종목 반환.

    선별 로직:
      1. 전일 상승률 상위 테마 추출 (chg_pct > 0)
      2. 상위 N개 테마의 구성종목 수집
      3. 종목별 "테마 빈도" = 상승 테마 몇 개에 동시 소속
      4. score = theme_freq * avg_theme_chg_pct
      5. 가격/블랙리스트 필터 → 상위 THEME_TOP_N 개 반환
    """
    global _LAST_META, _LAST_THEME_DETAIL
    _LAST_THEME_DETAIL = []

    want_n = int(os.getenv("THEME_TOP_N", "5"))
    min_price = float(os.getenv("WATCH_MIN_PRICE", "5000"))
    max_price = float(os.getenv("WATCH_MAX_PRICE", "150000"))
    top_themes_n = int(os.getenv("THEME_SCAN_TOP_N", "20"))  # 상세 크롤링할 테마 수
    theme_pages = int(os.getenv("THEME_PAGES", "3"))
    bl = _largecap_bl()

    # ── STEP 1: 테마 목록 크롤링 ──
    all_themes = fetch_theme_list(max_pages=theme_pages)
    if not all_themes:
        _LAST_META = "theme_list_empty"
        _log("BUILD empty theme list — returning []")
        return []

    # 테마 정렬: 장중이면 chg_pct, 장전(chg_pct 전부 0)이면 chg_3d 사용
    has_live = any(t["chg_pct"] != 0 for t in all_themes)
    sort_key = "chg_pct" if has_live else "chg_3d"

    rising = [t for t in all_themes if t[sort_key] > 0]
    rising.sort(key=lambda t: t[sort_key], reverse=True)
    top_themes = rising[:top_themes_n]

    if not top_themes:
        _LAST_META = f"no_rising_themes (total={len(all_themes)} sort={sort_key})"
        _log(f"BUILD no rising themes out of {len(all_themes)} (sort={sort_key})")
        return []

    _log(f"BUILD top_themes={len(top_themes)} / rising={len(rising)} / total={len(all_themes)} sort={sort_key}")
    for t in top_themes[:5]:
        _log(f"  THEME no={t['no']} name={t['name']} chg={t['chg_pct']:.2f}% 3d={t['chg_3d']:.2f}%")

    # ── STEP 2: 상위 테마별 구성종목 수집 ──
    # 종목별: {code: {"themes": [테마정보], "price": N, "name": str, ...}}
    stock_info: Dict[str, Dict[str, Any]] = {}
    fetch_ok = 0
    fetch_fail = 0

    for t in top_themes:
        stocks = fetch_theme_stocks(t["no"])
        if not stocks:
            fetch_fail += 1
            continue
        fetch_ok += 1
        # 테마 상승비율
        t_up_ratio = t["up"] / max(1, t["up"] + t["down"])
        for s in stocks:
            code = s["code"]
            if code not in stock_info:
                stock_info[code] = {
                    "name": s["name"], "price": s["price"],
                    "chg_pct": s["chg_pct"], "volume": s["volume"],
                    "tr_value": s["tr_value"],
                    "themes": [],
                    "theme_chg_sum": 0.0,
                    "theme_up_ratio_sum": 0.0,
                }
            stock_info[code]["themes"].append(t["name"])
            stock_info[code]["theme_chg_sum"] += t[sort_key]
            stock_info[code]["theme_up_ratio_sum"] += t_up_ratio

    _log(f"BUILD detail_fetch ok={fetch_ok} fail={fetch_fail} unique_stocks={len(stock_info)}")

    if not stock_info:
        _LAST_META = f"no_stocks_from_themes (themes={len(top_themes)})"
        _log("BUILD no stocks found from any theme")
        return []

    # ── STEP 2b: 전일 캐시 적용 (0인 필드만 전일 값으로 채움) ──
    prev_cache = _load_prev_cache()
    if not prev_cache:
        # 캐시 없으면 즉석 크롤링 후 저장
        try:
            _log("PREV_CACHE not found — fetching now")
            _all_themes = fetch_theme_list(max_pages=int(os.getenv("THEME_PAGES", "3")))
            _pc_stock_info: Dict[str, Dict[str, Any]] = {}
            for _t in (_all_themes or []):
                for _s in fetch_theme_stocks(_t["no"]):
                    _c = _s["code"]
                    if _c not in _pc_stock_info:
                        _pc_stock_info[_c] = {"chg_pct": _s["chg_pct"], "tr_value": _s["tr_value"]}
            if _pc_stock_info:
                save_prev_day_cache(_all_themes or [], _pc_stock_info)
                prev_cache = _load_prev_cache()
                _log(f"PREV_CACHE created: {len(_pc_stock_info)} stocks")
        except Exception as e:
            _log(f"PREV_CACHE fetch err: {e}")

    if prev_cache:
        cached_stocks = prev_cache.get("stocks", {})
        cached_themes = prev_cache.get("themes", {})
        cache_date = prev_cache.get("date", "?")

        # 종목별 chg_pct / tr_value 보정 (0인 필드만)
        if cached_stocks:
            patched = 0
            for code, info in stock_info.items():
                cs = cached_stocks.get(code)
                if cs:
                    if info["chg_pct"] == 0 and cs.get("chg_pct", 0) != 0:
                        info["chg_pct"] = cs["chg_pct"]
                    if info["tr_value"] == 0 and cs.get("tr_value", 0) > 0:
                        info["tr_value"] = cs["tr_value"]
                    patched += 1
            _log(f"PREV_CACHE applied stocks: {patched}/{len(stock_info)} from {cache_date}")

        # 테마 up/down 비율 보정 (0인 테마만)
        if cached_themes:
            patched_t = 0
            for t in top_themes:
                ct = cached_themes.get(t["no"])
                if ct and (t["up"] == 0 and t["down"] == 0):
                    t["up"] = ct.get("up", 0)
                    t["down"] = ct.get("down", 0)
                    patched_t += 1
            # theme_up_ratio_sum 재계산 (캐시 적용된 up/down 기준)
            if patched_t > 0:
                for code, info in stock_info.items():
                    info["theme_up_ratio_sum"] = 0.0
                for t in top_themes:
                    t_up_ratio = t["up"] / max(1, t["up"] + t["down"])
                    for code, info in stock_info.items():
                            if t["name"] in info["themes"]:
                                info["theme_up_ratio_sum"] += t_up_ratio
                _log(f"PREV_CACHE applied themes: {patched_t}/{len(top_themes)} from {cache_date}")
        else:
            _log("PREV_CACHE not found — scoring axes chg/liq/conviction may be 0")

    # ── STEP 2c: KIS 거래량 상위 교차 부스트용 데이터 수집 ──
    vol_boost_set: set = set()
    try:
        from scanner_company_rank import fetch_volume_rank
        _vol_boost_pt = float(os.getenv("THEME_VOL_BOOST", "15"))
        for _mkt in ("J", "NX"):
            _vrows, _ = fetch_volume_rank(_mkt)
            for _r in _vrows:
                for _k in ("mksc_shrn_iscd", "MKSC_SHRN_ISCD", "stnd_iscd", "pdno"):
                    _s = _r.get(_k, "")
                    if _s and len(_s) == 6 and _s.isdigit():
                        vol_boost_set.add(_s)
                        break
        _log(f"VOL_BOOST loaded {len(vol_boost_set)} syms from KIS volume rank")
    except Exception as e:
        _vol_boost_pt = 0.0
        _log(f"VOL_BOOST skip: {e}")

    # ── STEP 3: 스코어링 ──
    scored: List[Tuple[float, str]] = []
    dropped = {"price": 0, "blacklist": 0}

    for code, info in stock_info.items():
        # 블랙리스트
        if code in bl:
            dropped["blacklist"] += 1
            continue

        # 가격 필터
        px = info["price"]
        if px > 0 and (px < min_price or px > max_price):
            dropped["price"] += 1
            continue

        freq = len(info["themes"])
        avg_theme_chg = info["theme_chg_sum"] / freq if freq > 0 else 0.0

        # ── 1. 테마 교차 빈도 (0~30pt) ──
        # freq=1→5, freq=2→12, freq=3→21, freq=4→28, freq=5→30
        freq_score = min(30.0, freq * 8.0 - 3.0) if freq >= 1 else 0.0

        # ── 2. 테마 상승률 (0~20pt) ──
        avg_theme_chg = info["theme_chg_sum"] / freq if freq > 0 else 0.0
        theme_chg_score = min(20.0, max(0.0, avg_theme_chg * 3.0))

        # ── 3. 테마 컨빅션: 소속 테마 평균 상승비율 (0~25pt) ──
        # up/(up+down) = 0.5→0pt, 0.7→10pt, 0.85→17.5pt, 1.0→25pt
        avg_up_ratio = info["theme_up_ratio_sum"] / freq if freq > 0 else 0.5
        conviction_score = max(0.0, (avg_up_ratio - 0.5) * 50.0)

        # ── 4. 전일등락률 (-10~15pt) ──
        # 상승 모멘텀 가점, 하락 감점. 과열 감점 없음 (45종목 풀 단계, preopen이 최종 필터)
        stk_chg = info["chg_pct"]
        if stk_chg > 1.0:
            chg_score = min(15.0, stk_chg * 1.0)  # 모멘텀 (5%→5pt, 10%→10pt, 15+%→15pt)
        elif stk_chg > -1.0:
            chg_score = 0.0    # 보합
        else:
            chg_score = max(-10.0, stk_chg * 1.5)  # 전일 약세 감점

        # ── 5. 전일 거래대금 (0~10pt) ──
        # 10억→3pt, 50억→5pt, 200억→7pt, 1000억→10pt
        tr_v = info["tr_value"]
        if tr_v > 0:
            liq_score = min(10.0, math.log10(max(1, tr_v / 1e8)) * 3.3)
        else:
            liq_score = 0.0

        # ── 6. KIS 거래량 상위 교차 부스트 (0 or +15pt) ──
        vol_score = _vol_boost_pt if code in vol_boost_set else 0.0

        score = freq_score + theme_chg_score + conviction_score + chg_score + liq_score + vol_score
        scored.append((score, code))

    scored.sort(reverse=True)
    global _LAST_SCORES
    _LAST_SCORES = {code: sc for sc, code in scored}
    result = [code for _, code in scored[:want_n]]

    # ── STEP 4: 로그 ──
    _vol_hit = sum(1 for _, c in scored if c in vol_boost_set)
    _LAST_META = (
        f"theme pool={len(stock_info)} selected={len(result)} "
        f"drop_price={dropped['price']} drop_bl={dropped['blacklist']} "
        f"themes_scanned={fetch_ok}/{len(top_themes)} vol_hit={_vol_hit}"
    )

    for score, code in scored[:10]:
        info = stock_info[code]
        freq = len(info["themes"])
        avg_ur = info["theme_up_ratio_sum"] / freq if freq > 0 else 0.0
        vb = "V" if code in vol_boost_set else ""
        detail = (
            f"{code} {info['name']} score={score:.1f} freq={freq} "
            f"themes={info['themes'][:3]} price={info['price']} chg={info['chg_pct']:.1f}% "
            f"up_ratio={avg_ur:.2f} tr_val={info['tr_value']/1e8:.1f}억{' VOL★' if vb else ''}"
        )
        _LAST_THEME_DETAIL.append(detail)
        _log(f"  RANK {detail}")

    _log(f"BUILD RESULT: {_LAST_META}")
    _log(f"BUILD SELECTED: {result}")

    _save_theme_snapshot(stock_info, result, scored, dropped, top_themes, sort_key)

    save_prev_day_cache(top_themes, stock_info)

    return result


def _save_theme_snapshot(stock_info: Dict[str, Dict], selected: List[str],
                         scored: List[Tuple[float, str]], dropped: Dict[str, int],
                         top_themes: list, sort_key: str):
    """테마 스캐너 선별 스냅샷 저장 (하루 1회)."""
    global _SNAPSHOT_SAVED_TODAY
    today = time.strftime("%Y%m%d")
    if _SNAPSHOT_SAVED_TODAY == today:
        return
    hhmm = int(time.strftime("%H%M"))
    if not (845 <= hhmm <= 915):
        return
    try:
        import json
        snap_dir = os.path.join("data", "logs", today)
        os.makedirs(snap_dir, exist_ok=True)
        snap_path = os.path.join(snap_dir, "theme_snapshot.json")
        score_map = {sym: round(sc, 3) for sc, sym in scored}
        theme_map = {code: info["themes"] for code, info in stock_info.items()}
        payload = {
            "date": today,
            "saved_at": time.strftime("%H:%M:%S"),
            "mode": "theme",
            "sort_key": sort_key,
            "pool_count": len(stock_info),
            "selected_count": len(selected),
            "pool_symbols": list(stock_info.keys()),
            "selected": selected,
            "scores": score_map,
            "themes": theme_map,
            "dropped": dropped,
            "top_themes": [{"name": t["name"], "no": t["no"],
                            "chg_pct": t["chg_pct"], "chg_3d": t["chg_3d"]}
                           for t in top_themes[:20]],
        }
        with open(snap_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        _SNAPSHOT_SAVED_TODAY = today
        _log(f"SNAPSHOT saved {snap_path} pool={len(stock_info)} selected={len(selected)}")
    except Exception as e:
        _log(f"SNAPSHOT ERR {e}")
