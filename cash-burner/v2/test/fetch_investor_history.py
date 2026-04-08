"""
종목별 외국인/기관 일별 매매 데이터 수집.
inquire-investor (FHKST01010900) — 보통 최근 ~30거래일 반환.

대상: daily_ohlc_cache.json 종목 + 코스피200 ETF
저장: data/investor_cache.json  {sym: {date: {frgn_qty, orgn_qty, frgn_amt, orgn_amt, close}}}
"""
import os, sys, json, time
os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")
sys.path.insert(0, "src")
from kis_http import request

with open("data/daily_ohlc_cache.json", "r", encoding="utf-8") as f:
    cache = json.load(f)

INVESTOR_CACHE = "data/investor_cache.json"
if os.path.exists(INVESTOR_CACHE):
    with open(INVESTOR_CACHE, "r", encoding="utf-8") as f:
        inv = json.load(f)
else:
    inv = {}


def fetch(sym):
    j = request("GET", "/uapi/domestic-stock/v1/quotations/inquire-investor",
                "FHKST01010900", params={
                    "FID_COND_MRKT_DIV_CODE": "J",
                    "FID_INPUT_ISCD": sym,
                })
    items = j.get("output", [])
    out = {}
    for r in items:
        d = r.get("stck_bsop_date", "")
        if not d:
            continue
        try:
            out[d] = {
                "frgn_qty": int(r.get("frgn_ntby_qty", 0) or 0),
                "orgn_qty": int(r.get("orgn_ntby_qty", 0) or 0),
                "frgn_tr": int(r.get("frgn_ntby_tr_pbmn", 0) or 0),
                "orgn_tr": int(r.get("orgn_ntby_tr_pbmn", 0) or 0),
                "prsn_qty": int(r.get("prsn_ntby_qty", 0) or 0),
                "close": int(r.get("stck_clpr", 0) or 0),
            }
        except Exception:
            pass
    return out


syms = sorted(cache.keys())
print(f"target syms: {len(syms)}")
print(f"existing inv: {len(inv)}")

# 기존에 데이터 있는 종목은 스킵 (이미 ~30일 들어있음)
to_fetch = [s for s in syms if s not in inv or len(inv.get(s, {})) < 5]
print(f"to fetch: {len(to_fetch)}")

t0 = time.time()
for i, sym in enumerate(to_fetch):
    try:
        data = fetch(sym)
        if data:
            inv[sym] = data
    except Exception as e:
        print(f"  {sym}: ERR {e}")
    time.sleep(0.08)
    if (i+1) % 30 == 0:
        elapsed = time.time() - t0
        eta = elapsed / (i+1) * (len(to_fetch) - i - 1)
        print(f"  {i+1}/{len(to_fetch)}  {sym} {len(inv.get(sym,{}))}d  elapsed={elapsed:.0f}s eta={eta:.0f}s", flush=True)
        with open(INVESTOR_CACHE, "w", encoding="utf-8") as f:
            json.dump(inv, f)

with open(INVESTOR_CACHE, "w", encoding="utf-8") as f:
    json.dump(inv, f)

# stats
days_per_sym = [len(d) for d in inv.values() if d]
if days_per_sym:
    days_per_sym.sort()
    print(f"\ndone in {time.time()-t0:.0f}s")
    print(f"days per sym: min={min(days_per_sym)} max={max(days_per_sym)} median={days_per_sym[len(days_per_sym)//2]}")
    # show one sample
    s = sorted(inv.keys())[0]
    print(f"\nsample {s}: {len(inv[s])}일")
    for d in sorted(inv[s].keys())[-3:]:
        print(f"  {d}: {inv[s][d]}")
