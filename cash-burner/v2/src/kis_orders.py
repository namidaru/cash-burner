# src/kis_orders.py
from __future__ import annotations

import os
from typing import Any, Dict, Tuple
from kis_http import request, split_account, ACC_NO

# 매수가능조회
TRID_BUYABLE = "TTTC8908R"
PATH_BUYABLE = "/uapi/domestic-stock/v1/trading/inquire-psbl-order"

# 매도가능수량조회
TRID_SELLABLE = "TTTC8408R"
PATH_SELLABLE = "/uapi/domestic-stock/v1/trading/inquire-psbl-sell"

# 현금주문 (문서 기준)
TRID_SELL = "TTTC0011U"
TRID_BUY  = "TTTC0012U"
PATH_ORDER = "/uapi/domestic-stock/v1/trading/order-cash"

def buyable_cash(symbol: str, ord_dvsn: str="01", price: str="0") -> float:
    cano, prdt = split_account(ACC_NO)
    params = {
        "CANO": cano,
        "ACNT_PRDT_CD": prdt,
        "PDNO": symbol,
        "ORD_DVSN": ord_dvsn,
        "ORD_UNPR": str(price),
    }
    j = request("GET", PATH_BUYABLE, TRID_BUYABLE, params=params)
    out = j.get("output", {}) or j.get("output1", {}) or {}
    for k in ("ord_psbl_cash","ORD_PSBL_CASH","ord_psbl_amt","max_buy_amt"):
        if k in out and out[k]:
            try: return float(out[k])
            except: pass
    # fallback
    return float(os.getenv("START_CASH","10000000"))

def sellable_qty(symbol: str) -> int:
    cano, prdt = split_account(ACC_NO)
    params = {"CANO": cano, "ACNT_PRDT_CD": prdt, "PDNO": symbol}
    j = request("GET", PATH_SELLABLE, TRID_SELLABLE, params=params)
    out = j.get("output", {}) or j.get("output1", {}) or {}
    for k in ("ord_psbl_qty","ORD_PSBL_QTY","sell_psbl_qty"):
        if k in out and out[k] is not None:
            try: return int(float(out[k]))
            except: pass
    return 0

def order_cash(side: str, symbol: str, qty: int, ord_dvsn: str="01", ord_unpr: str="0") -> Dict[str,Any]:
    cano, prdt = split_account(ACC_NO)
    tr_id = TRID_BUY if side.upper()=="BUY" else TRID_SELL
    body = {
        "CANO": cano,
        "ACNT_PRDT_CD": prdt,
        "PDNO": symbol,
        "ORD_DVSN": ord_dvsn,
        "ORD_QTY": str(int(qty)),
        "ORD_UNPR": str(ord_unpr),
    }
    return request("POST", PATH_ORDER, tr_id, body=body)
