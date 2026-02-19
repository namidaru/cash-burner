# fake_stream_test.py
import time
import random
from collections import defaultdict, deque

# ====== 너가 준 스키마(필요한 최소만) ======
COLUMNS_H0STCNT0 = [
    "MKSC_SHRN_ISCD","STCK_CNTG_HOUR","STCK_PRPR","PRDY_VRSS_SIGN",
    "PRDY_VRSS","PRDY_CTRT","WGHN_AVRG_STCK_PRC","STCK_OPRC",
    "STCK_HGPR","STCK_LWPR","ASKP1","BIDP1","CNTG_VOL","ACML_VOL",
    "ACML_TR_PBMN","SELN_CNTG_CSNU","SHNU_CNTG_CSNU","NTBY_CNTG_CSNU",
    "CTTR","SELN_CNTG_SMTN","SHNU_CNTG_SMTN","CCLD_DVSN","SHNU_RATE",
    "PRDY_VOL_VRSS_ACML_VOL_RATE","OPRC_HOUR","OPRC_VRSS_PRPR_SIGN",
    "OPRC_VRSS_PRPR","HGPR_HOUR","HGPR_VRSS_PRPR_SIGN","HGPR_VRSS_PRPR",
    "LWPR_HOUR","LWPR_VRSS_PRPR_SIGN","LWPR_VRSS_PRPR","BSOP_DATE",
    "NEW_MKOP_CLS_CODE","TRHT_YN","ASKP_RSQN1","BIDP_RSQN1",
    "TOTAL_ASKP_RSQN","TOTAL_BIDP_RSQN","VOL_TNRT",
    "PRDY_SMNS_HOUR_ACML_VOL","PRDY_SMNS_HOUR_ACML_VOL_RATE",
    "HOUR_CLS_CODE","MRKT_TRTM_CLS_CODE","VI_STND_PRC"
]

COLUMNS_H0STASP0 = [
    "MKSC_SHRN_ISCD","BSOP_HOUR","HOUR_CLS_CODE",
    "ASKP1","ASKP2","ASKP3","ASKP4","ASKP5","ASKP6","ASKP7","ASKP8","ASKP9","ASKP10",
    "BIDP1","BIDP2","BIDP3","BIDP4","BIDP5","BIDP6","BIDP7","BIDP8","BIDP9","BIDP10",
    "ASKP_RSQN1","ASKP_RSQN2","ASKP_RSQN3","ASKP_RSQN4","ASKP_RSQN5","ASKP_RSQN6","ASKP_RSQN7","ASKP_RSQN8","ASKP_RSQN9","ASKP_RSQN10",
    "BIDP_RSQN1","BIDP_RSQN2","BIDP_RSQN3","BIDP_RSQN4","BIDP_RSQN5","BIDP_RSQN6","BIDP_RSQN7","BIDP_RSQN8","BIDP_RSQN9","BIDP_RSQN10",
    "TOTAL_ASKP_RSQN","TOTAL_BIDP_RSQN","OVTM_TOTAL_ASKP_RSQN","OVTM_TOTAL_BIDP_RSQN",
    "ANTC_CNPR","ANTC_CNQN","ANTC_VOL","ANTC_CNTG_VRSS","ANTC_CNTG_VRSS_SIGN",
    "ANTC_CNTG_PRDY_CTRT","ACML_VOL","TOTAL_ASKP_RSQN_ICDC","TOTAL_BIDP_RSQN_ICDC",
    "OVTM_TOTAL_ASKP_ICDC","OVTM_TOTAL_BIDP_ICDC","STCK_DEAL_CLS_CODE"
]

def make_ws_line(trid: str, values: list[str]) -> str:
    data = "^".join(values)
    return f"0|{trid}|0|{data}"

def row_to_values(cols, row: dict) -> list[str]:
    return [str(row.get(c, "")) for c in cols]

def parse_line(line: str):
    if not line.startswith("0|"):
        return None
    parts = line.split("|", 3)
    if len(parts) < 4:
        return None
    tr_id = parts[1]
    data = parts[3]
    cols = COLUMNS_H0STCNT0 if tr_id == "H0STCNT0" else COLUMNS_H0STASP0
    values = data.split("^")
    return tr_id, dict(zip(cols, values))

def f(x, d=0.0):
    try: return float(x)
    except: return d

class SurgeDetector:
    def __init__(self, window_sec=10, min_ret_pct=0.6, min_tr_value=30_000_000):
        self.window_sec = window_sec
        self.min_ret_pct = min_ret_pct
        self.min_tr_value = min_tr_value
        self.ticks = defaultdict(lambda: deque(maxlen=2000))
        self.book = {}
        self.seen = set()

    def on_orderbook(self, row):
        self.book[row["MKSC_SHRN_ISCD"]] = row

    def on_trade(self, row):
        sym = row["MKSC_SHRN_ISCD"]
        price = f(row.get("STCK_PRPR"))
        vol = f(row.get("CNTG_VOL"))
        if price <= 0: return None

        now = time.time()
        dq = self.ticks[sym]
        dq.append((now, price, vol))
        while dq and now - dq[0][0] > self.window_sec:
            dq.popleft()
        if len(dq) < 2: return None

        base = dq[0][1]
        ret = (price - base) / base * 100.0
        trv = sum(p*v for _,p,v in dq)

        if ret < self.min_ret_pct or trv < self.min_tr_value:
            return None

        ob = self.book.get(sym)
        if not ob:
            return {"type":"CANDIDATE","symbol":sym,"ret_pct":ret,"tr_value":trv}

        bid_tot = f(ob.get("TOTAL_BIDP_RSQN"))
        ask_tot = f(ob.get("TOTAL_ASKP_RSQN"))
        imb = bid_tot/(bid_tot+ask_tot) if (bid_tot+ask_tot)>0 else 0.5

        ask1 = f(ob.get("ASKP1"))
        bid1 = f(ob.get("BIDP1"))
        mid = (ask1+bid1)/2 if (ask1>0 and bid1>0) else price
        spread = ((ask1-bid1)/mid*100.0) if mid>0 else 999

        if imb < 0.62 or spread > 0.15:
            return {"type":"CANDIDATE","symbol":sym,"ret_pct":ret,"tr_value":trv,"imb":imb,"spread_pct":spread}

        bucket = int(now//self.window_sec)
        sid = f"SURGE|{sym}|{bucket}"
        if sid in self.seen: return None
        self.seen.add(sid)
        return {"type":"SURGE","symbol":sym,"ret_pct":ret,"tr_value":trv,"imb":imb,"spread_pct":spread,"signal_id":sid}

def demo_stream(symbol="005930"):
    """
    시나리오:
      0~6초: 횡보
      6~12초: 급등(1%+)
    """
    det = SurgeDetector(window_sec=10, min_ret_pct=0.6, min_tr_value=1_000_000)  # 테스트용으로 거래대금 기준 낮춤

    # 호가(매수우위) 먼저 넣어두기
    ob = {"MKSC_SHRN_ISCD": symbol, "ASKP1": "70100", "BIDP1": "70000", "TOTAL_ASKP_RSQN":"50000", "TOTAL_BIDP_RSQN":"120000"}
    ob_line = make_ws_line("H0STASP0", row_to_values(COLUMNS_H0STASP0, ob))
    tr_id, row = parse_line(ob_line)
    det.on_orderbook(row)

    price = 70000.0
    acml_vol = 0

    for i in range(120):  # 0.1초 * 120 = 12초
        t = i * 0.1
        # 횡보
        if t < 6:
            price *= (1 + random.uniform(-0.0002, 0.0002))
        else:
            # 급등 구간
            price *= (1 + random.uniform(0.0008, 0.0020))

        vol = random.randint(1, 50)
        acml_vol += vol

        trade = {
            "MKSC_SHRN_ISCD": symbol,
            "STCK_CNTG_HOUR": "090000",
            "STCK_PRPR": str(int(price)),
            "CNTG_VOL": str(vol),
            "ACML_VOL": str(acml_vol),
        }
        trade_line = make_ws_line("H0STCNT0", row_to_values(COLUMNS_H0STCNT0, trade))
        tr_id, row = parse_line(trade_line)

        sig = det.on_trade(row)
        if sig:
            print(sig)

        time.sleep(0.05)

if __name__ == "__main__":
    demo_stream()