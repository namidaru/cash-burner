"""
Extend daily_ohlc_cache.json from 65 days to 3 years (2023~2026).
Fetches in 100-day chunks via KIS API.
"""
import os, sys, json, time
os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")
sys.path.insert(0, "src")
from kis_http import request

CACHE = "data/daily_ohlc_cache.json"
with open(CACHE, "r", encoding="utf-8") as f:
    cache = json.load(f)
print(f"loaded {len(cache)} syms", flush=True)

WINDOWS = [
    ("20230101", "20230401"),
    ("20230401", "20230701"),
    ("20230701", "20231001"),
    ("20231001", "20240101"),
    ("20240101", "20240401"),
    ("20240401", "20240701"),
    ("20240701", "20241001"),
    ("20241001", "20260101"),
]


def fetch_window(sym, start, end):
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": sym,
        "FID_INPUT_DATE_1": start,
        "FID_INPUT_DATE_2": end,
        "FID_PERIOD_DIV_CODE": "D",
        "FID_ORG_ADJ_PRC": "0",
    }
    j = request("GET", "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                "FHKST03010100", params=params)
    out = {}
    for r in j.get("output2", []):
        d = r.get("stck_bsop_date", "")
        try:
            out[d] = {
                "open": int(r["stck_oprc"]),
                "high": int(r["stck_hgpr"]),
                "low": int(r["stck_lwpr"]),
                "close": int(r["stck_clpr"]),
                "vol": int(r.get("acml_vol", 0) or 0),
                "tr": int(r.get("acml_tr_pbmn", 0) or 0),
            }
        except Exception:
            pass
    return out


syms = sorted(cache.keys())
t0 = time.time()
for i, sym in enumerate(syms):
    existing = cache.get(sym, {}) or {}
    earliest = min(existing.keys()) if existing else "20260408"
    if earliest <= "20230102":
        continue  # already extended
    for start, end in WINDOWS:
        if start >= earliest:
            continue
        try:
            new = fetch_window(sym, start, end)
            existing.update(new)
        except Exception as e:
            print(f"  {sym} {start}~{end}: ERR {e}")
        time.sleep(0.08)
    cache[sym] = existing
    if (i + 1) % 30 == 0:
        elapsed = time.time() - t0
        eta = elapsed / (i + 1) * (len(syms) - i - 1)
        print(f"  {i+1}/{len(syms)}  {sym}  {len(existing)}d  elapsed={elapsed:.0f}s eta={eta:.0f}s", flush=True)
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f)

with open(CACHE, "w", encoding="utf-8") as f:
    json.dump(cache, f)
print(f"\ndone in {time.time()-t0:.0f}s")

# stats
days_per_sym = [len(d) for d in cache.values() if d]
print(f"days per sym: min={min(days_per_sym)} max={max(days_per_sym)} median={sorted(days_per_sym)[len(days_per_sym)//2]}")
