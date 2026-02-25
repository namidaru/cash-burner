# src/kis_orders.py
from __future__ import annotations

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


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        s = str(v).strip().replace(",", "")
        if not s:
            return None
        return float(s)
    except Exception:
        return None


def _extract_buyable_cash_strict(payload: Dict[str, Any]) -> float:
    """주문가능금액은 단일 canonical 필드(ord_psbl_cash)로만 해석한다.

    파싱 실패시 fallback 하지 않고 예외를 발생시켜 상위에서 거래 중단 판단을 하게 한다.
    """
    out = payload.get("output")
    if not isinstance(out, dict):
        raise ValueError(
            f"buyable_cash_parse_error: missing output(dict), top_keys={sorted(payload.keys())[:12]}"
        )

    raw = out.get("ord_psbl_cash")
    if raw is None:
        raw = out.get("ORD_PSBL_CASH")

    val = _to_float(raw)
    if val is None:
        rt_cd = str(payload.get("rt_cd", ""))
        msg1 = str(payload.get("msg1", ""))
        raise ValueError(
            f"buyable_cash_parse_error: output.ord_psbl_cash missing/invalid, output_keys={sorted(out.keys())[:20]}, rt_cd={rt_cd}, msg1={msg1[:120]}"
        )
    return val


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
    return _extract_buyable_cash_strict(j)


def sellable_qty(symbol: str) -> int:
    cano, prdt = split_account(ACC_NO)
    params = {"CANO": cano, "ACNT_PRDT_CD": prdt, "PDNO": symbol}
    j = request("GET", PATH_SELLABLE, TRID_SELLABLE, params=params)
    out = j.get("output", {}) or j.get("output1", {}) or {}
    for k in ("ord_psbl_qty", "ORD_PSBL_QTY", "sell_psbl_qty"):
        n = _to_float(out.get(k))
        if n is not None:
            return int(n)
    return 0


def account_buying_power(symbol: str = "005930", ord_dvsn: str = "01", price: str = "0") -> float:
    return buyable_cash(symbol=symbol, ord_dvsn=ord_dvsn, price=price)


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
