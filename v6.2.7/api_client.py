#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
한국투자증권 API 클라이언트
"""

import requests
import json
import time
import traceback
from datetime import datetime, timedelta
from typing import Optional, Dict, Tuple, List
from pathlib import Path

from config import (
    APP_KEY, APP_SECRET, ACC_NO, URL_BASE, API_TIMEOUT, TOKEN_CACHE_FILE,
    VI_COOLDOWN_MINUTES, MIN_PRICE, MAX_PRICE, MIN_PRICE_CHANGE, MAX_PRICE_CHANGE,
    EXCLUDE_KEYWORDS
)
from utils import logger, APIRateLimiter, parse_account_no

# ============================================================================
# 전역 변수
# ============================================================================
token = None
token_expire_time = None
rate_limiter = APIRateLimiter()
vi_triggered_codes = set()
vi_cooldown_time = {}

# ============================================================================
# 토큰 관리
# ============================================================================
def save_token_cache(token_data: dict):
    """토큰을 파일에 저장"""
    try:
        cache = {
            "access_token": token_data.get("access_token"),
            "expires_at": time.time() + token_data.get("expires_in", 86400),
            "token_type": token_data.get("token_type", "Bearer")
        }
        
        with open(TOKEN_CACHE_FILE, 'w') as f:
            json.dump(cache, f, indent=2)
        
        logger.debug("💾 토큰 캐시 저장 완료")
    except Exception as e:
        logger.error(f"토큰 캐시 저장 오류: {e}")

def load_token_cache() -> Optional[str]:
    """캐시된 토큰 로드 (유효한 경우에만)"""
    try:
        if not Path(TOKEN_CACHE_FILE).exists():
            logger.debug("💾 캐시 파일 없음")
            return None
        
        with open(TOKEN_CACHE_FILE, 'r') as f:
            cache = json.load(f)
        
        expires_at = cache.get("expires_at", 0)
        remaining = expires_at - time.time()
        
        if remaining > 600:  # 10분 이상 남음
            token = cache.get("access_token")
            logger.info(f"✅ 캐시된 토큰 사용 (유효: {int(remaining/3600)}h {int((remaining%3600)/60)}m)")
            return token
        else:
            logger.info("⚠️  캐시된 토큰 만료 또는 여유 시간 부족")
            return None
    
    except Exception as e:
        logger.error(f"토큰 캐시 로드 오류: {e}")
        return None

def get_token() -> Optional[str]:
    """OAuth 토큰 발급 (캐시 우선)"""
    global token, token_expire_time
    
    # 캐시 확인
    cached_token = load_token_cache()
    if cached_token:
        token = cached_token
        try:
            with open(TOKEN_CACHE_FILE, 'r') as f:
                cache = json.load(f)
                expires_at = cache.get("expires_at", 0)
                token_expire_time = datetime.fromtimestamp(expires_at)
        except:
            pass
        return token
    
    # 새로 발급
    logger.info("🔑 새 토큰 발급 중...")
    
    for attempt in range(3):
        try:
            logger.info(f"   시도 ({attempt + 1}/3)...")
            
            url = f"{URL_BASE}/oauth2/tokenP"
            headers = {"content-type": "application/json"}
            data = {
                "grant_type": "client_credentials",
                "appkey": APP_KEY,
                "appsecret": APP_SECRET
            }
            
            response = requests.post(url, headers=headers, json=data, timeout=API_TIMEOUT)
            
            if response.status_code == 200:
                result = response.json()
                token = result.get('access_token')
                expires_in = result.get('expires_in', 86400)
                
                if token:
                    token_expire_time = datetime.now() + timedelta(seconds=expires_in - 300)
                    save_token_cache(result)
                    logger.info(f"✅ 토큰 발급 성공 (만료: {token_expire_time.strftime('%H:%M:%S')})")
                    return token
            
            if attempt < 2:
                time.sleep((attempt + 1) * 5)
        
        except Exception as e:
            logger.error(f"⚠️  토큰 발급 오류: {e}")
            if attempt < 2:
                time.sleep((attempt + 1) * 5)
    
    return None

def get_approval_key() -> Optional[str]:
    """웹소켓 접속키 발급"""
    max_attempts = 10
    
    for attempt in range(max_attempts):
        try:
            logger.info(f"🔐 접속키 발급 시도 ({attempt + 1}/{max_attempts})...")
            
            url = f"{URL_BASE}/oauth2/Approval"
            headers = {"content-type": "application/json"}
            data = {
                "grant_type": "client_credentials",
                "appkey": APP_KEY,
                "secretkey": APP_SECRET
            }
            
            response = requests.post(url, headers=headers, json=data, timeout=30)
            
            if response.status_code == 200:
                result = response.json()
                approval_key = result.get('approval_key')
                
                if approval_key:
                    logger.info(f"✅ 접속키 발급 성공: {approval_key[:20]}...")
                    return approval_key
                else:
                    logger.error(f"❌ 응답에 approval_key 없음!")
            
            elif response.status_code in [400, 401, 404, 405]:
                logger.error(f"❌ HTTP {response.status_code}")
                return None  # 재시도 불필요
            
            elif response.status_code == 429:
                logger.error("❌ HTTP 429 TOO_MANY_REQUESTS")
                time.sleep(60)
            
            if attempt < max_attempts - 1:
                wait_time = min(3 * (2 ** attempt), 60)
                logger.info(f"⏳ {wait_time}초 후 재시도...")
                time.sleep(wait_time)
        
        except requests.exceptions.Timeout:
            logger.error(f"❌ 접속키 발급 타임아웃")
            if attempt < max_attempts - 1:
                time.sleep(min(5 * (2 ** attempt), 60))
        
        except Exception as e:
            logger.error(f"⚠️  접속키 발급 오류: {e}")
            if attempt < max_attempts - 1:
                time.sleep(min(5 * (2 ** attempt), 60))
    
    logger.critical("❌ 접속키 발급 최종 실패")
    return None

def check_token_expiry():
    """토큰 만료 체크 및 갱신"""
    global token
    
    if token_expire_time and datetime.now() >= token_expire_time:
        logger.warning("⚠️  토큰 만료 임박, 재발급 중...")
        token = get_token()
        if not token:
            logger.error("❌ 토큰 재발급 실패")
            return False
    
    return True

# ============================================================================
# API 호출
# ============================================================================
def api_call(tr_id: str, path: str, params: Optional[Dict] = None, 
             method: str = "GET") -> Optional[Dict]:
    """통합 API 호출"""
    if not check_token_expiry():
        return None
    
    rate_limiter.wait_if_needed()
    
    try:
        url = f"{URL_BASE}{path}"
        headers = {
            "Content-Type": "application/json",
            "authorization": f"Bearer {token}",
            "appkey": APP_KEY,
            "appsecret": APP_SECRET,
            "tr_id": tr_id
        }
        
        if method == "GET":
            response = requests.get(url, headers=headers, params=params, timeout=API_TIMEOUT)
        else:
            response = requests.post(url, headers=headers, json=params, timeout=API_TIMEOUT)
        
        if response.status_code == 200:
            result = response.json()
            
            if result.get('rt_cd') != '0':
                logger.warning(f"⚠️  API 경고 ({tr_id}): {result.get('msg1', 'Unknown')}")
            
            return result
        else:
            logger.warning(f"⚠️  API 실패 ({tr_id}): HTTP {response.status_code}")
            return None
    
    except requests.Timeout:
        logger.error(f"❌ API 타임아웃 ({tr_id})")
        return None
    except Exception as e:
        logger.error(f"❌ API 오류 ({tr_id}): {e}")
        return None

# ============================================================================
# VI 감지
# ============================================================================
def check_vi_status(code: str) -> Tuple[bool, str]:
    """
    VI 발동 여부 확인
    Returns: (VI 발동 여부, VI 타입)
    """
    try:
        # 호가 데이터 조회
        data = api_call("FHKST01010200", "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
        
        if not data or 'output1' not in data:
            return False, ""
        
        output = data['output1']
        
        # 현재가 조회
        price_data = api_call("FHKST01010100", "/uapi/domestic-stock/v1/quotations/inquire-price",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
        
        if not price_data or 'output' not in price_data:
            return False, ""
        
        current_price = int(price_data['output'].get('stck_prpr', 0))
        
        # VI 감지
        askp1 = int(output.get('askp1', 0))
        bidp1 = int(output.get('bidp1', 0))
        
        # 호가 소멸
        if askp1 == 0 and bidp1 == 0:
            return True, "STATIC"
        
        # 호가 스프레드 이상 확대
        if askp1 > 0 and bidp1 > 0:
            spread_rate = (askp1 - bidp1) / current_price
            if spread_rate > 0.10:
                return True, "DYNAMIC"
        
        # 거래량 급증 + 가격 급등
        change_rate = float(price_data['output'].get('prdy_ctrt', 0))
        if abs(change_rate) > 15:
            return True, "SUSPECTED"
        
        return False, ""
    
    except Exception as e:
        logger.debug(f"VI 체크 오류: {e}")
        return False, ""

def check_vi_cooldown(code: str) -> bool:
    """VI 쿨다운 확인"""
    if code in vi_cooldown_time:
        elapsed = (datetime.now() - vi_cooldown_time[code]).total_seconds() / 60
        if elapsed < VI_COOLDOWN_MINUTES:
            return True
    return False

# ============================================================================
# 계좌 정보 조회
# ============================================================================
holdings_data = {}  # 전역 변수

def get_balance() -> int:
    """현금 잔고 조회"""
    try:
        cano, acnt_prdt_cd = parse_account_no(ACC_NO)
        if not cano or not acnt_prdt_cd:
            return 0
        
        data = api_call("TTTC8434R", "/uapi/domestic-stock/v1/trading/inquire-psbl-order",
            {
                "CANO": cano,
                "ACNT_PRDT_CD": acnt_prdt_cd,
                "PDNO": "005930",  # 삼성전자 (더미)
                "ORD_UNPR": "0",
                "ORD_DVSN": "01",
                "CMA_EVLU_AMT_ICLD_YN": "Y",
                "OVRS_ICLD_YN": "N"
            })
        
        if data and 'output' in data:
            return int(data['output'].get('ord_psbl_cash', 0))
        return 0
    
    except Exception as e:
        logger.error(f"❌ 잔고 조회 오류: {e}")
        return 0

def get_holdings() -> List[Dict]:
    """보유 종목 조회"""
    global holdings_data
    
    try:
        cano, acnt_prdt_cd = parse_account_no(ACC_NO)
        if not cano or not acnt_prdt_cd:
            return []
        
        data = api_call("TTTC8434R", "/uapi/domestic-stock/v1/trading/inquire-balance",
            {
                "CANO": cano,
                "ACNT_PRDT_CD": acnt_prdt_cd,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "01",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": ""
            })
        
        if not data or 'output1' not in data:
            return []
        
        holdings = []
        current_codes = set()
        
        for item in data['output1']:
            qty = int(item.get('hldg_qty', 0))
            if qty > 0:
                code = item['pdno']
                current_codes.add(code)
                
                holding = {
                    'code': code,
                    'name': item.get('prdt_name', ''),
                    'qty': qty,
                    'avg_price': int(float(item.get('pchs_avg_pric', 0))),
                    'current_price': int(item.get('prpr', 0)),
                    'eval_profit': int(item.get('evlu_pfls_amt', 0)),
                    'profit_rate': float(item.get('evlu_pfls_rt', 0))
                }
                
                holdings.append(holding)
                holdings_data[code] = holding
        
        # 실제 계좌에 없는 종목 제거
        if holdings_data:
            memory_codes = set(holdings_data.keys())
            removed_codes = memory_codes - current_codes
            
            for code in removed_codes:
                logger.warning(f"⚠️  실제 계좌에 없는 종목 제거: {holdings_data[code]['name']} ({code})")
                del holdings_data[code]
        
        return holdings
    
    except Exception as e:
        logger.error(f"❌ 보유 종목 조회 오류: {e}")
        return []

def get_price(code: str) -> int:
    """현재가 조회"""
    try:
        data = api_call("FHKST01010100", "/uapi/domestic-stock/v1/quotations/inquire-price",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
        
        if data and 'output' in data:
            return int(data['output'].get('stck_prpr', 0))
        return 0
    
    except Exception as e:
        logger.error(f"❌ 현재가 조회 오류: {e}")
        return 0

def get_stock_info(code: str) -> Optional[Dict]:
    """종목 정보 조회"""
    try:
        data = api_call("FHKST01010100", "/uapi/domestic-stock/v1/quotations/inquire-price",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
        
        if not data or 'output' not in data:
            return None
        
        output = data['output']
        
        return {
            'code': code,
            'name': output.get('prdt_abrv_name', ''),
            'price': int(output.get('stck_prpr', 0)),
            'open': int(output.get('stck_oprc', 0)),
            'high': int(output.get('stck_hgpr', 0)),
            'low': int(output.get('stck_lwpr', 0)),
            'volume': int(output.get('acml_vol', 0)),
            'change_rate': float(output.get('prdy_ctrt', 0)),
            'prev_close': int(output.get('stck_sdpr', 0))
        }
    
    except Exception as e:
        logger.error(f"❌ 종목 정보 오류: {e}")
        return None

def get_volume_rank_codes(limit: int = 50) -> List[str]:
    """거래량 상위 종목 조회"""
    try:
        data = api_call("FHPST01710000", "/uapi/domestic-stock/v1/quotations/volume-rank",
            {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_COND_SCR_DIV_CODE": "20171",
                "FID_INPUT_ISCD": "0000",
                "FID_DIV_CLS_CODE": "0",
                "FID_BLNG_CLS_CODE": "0",
                "FID_TRGT_CLS_CODE": "111111111",
                "FID_TRGT_EXLS_CLS_CODE": "0000000000",
                "FID_INPUT_PRICE_1": "",
                "FID_INPUT_PRICE_2": "",
                "FID_VOL_CNT": "",
                "FID_INPUT_DATE_1": ""
            })
        
        if not data or 'output' not in data:
            return []
        
        codes = []
        for item in data['output'][:limit]:
            code = item['mksc_shrn_iscd']
            name = item.get('hts_kor_isnm', '')
            
            # 제외 키워드 확인
            if any(kw in name for kw in EXCLUDE_KEYWORDS):
                continue
            
            codes.append(code)
        
        logger.info(f"✅ 거래량 상위 {len(codes)}개 종목 조회")
        return codes
    
    except Exception as e:
        logger.error(f"❌ 거래량 상위 조회 오류: {e}")
        return []

# ============================================================================
# 주문 체결 확인 및 취소
# ============================================================================
def check_order_status(order_no: str) -> Tuple[bool, int]:
    """주문 체결 확인"""
    try:
        from datetime import datetime
        cano, acnt_prdt_cd = parse_account_no(ACC_NO)
        if not cano or not acnt_prdt_cd:
            return False, 0
        
        data = api_call("TTTC8001R", "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
            {
                "CANO": cano,
                "ACNT_PRDT_CD": acnt_prdt_cd,
                "INQR_STRT_DT": datetime.now().strftime("%Y%m%d"),
                "INQR_END_DT": datetime.now().strftime("%Y%m%d"),
                "SLL_BUY_DVSN_CD": "00",
                "INQR_DVSN": "00",
                "PDNO": "",
                "CCLD_DVSN": "00",
                "ORD_GNO_BRNO": "",
                "ODNO": order_no,
                "INQR_DVSN_3": "00",
                "INQR_DVSN_1": "",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": ""
            })
        
        if data and 'output1' in data:
            for item in data['output1']:
                if item.get('odno') == order_no:
                    tot_ccld_qty = int(item.get('tot_ccld_qty', 0))
                    ord_qty = int(item.get('ord_qty', 0))
                    
                    if tot_ccld_qty >= ord_qty:
                        return True, tot_ccld_qty
                    else:
                        return False, tot_ccld_qty
        
        return False, 0
    
    except Exception as e:
        logger.error(f"❌ 체결 확인 오류: {e}")
        return False, 0

def cancel_order(code: str, order_no: str, qty: int) -> bool:
    """주문 취소"""
    try:
        cano, acnt_prdt_cd = parse_account_no(ACC_NO)
        if not cano or not acnt_prdt_cd:
            return False
        
        data = api_call("TTTC0803U", "/uapi/domestic-stock/v1/trading/order-rvsecncl",
            {
                "CANO": cano,
                "ACNT_PRDT_CD": acnt_prdt_cd,
                "KRX_FWDG_ORD_ORGNO": "",
                "ORGN_ODNO": order_no,
                "ORD_DVSN": "00",
                "RVSE_CNCL_DVSN_CD": "02",
                "ORD_QTY": "0",
                "ORD_UNPR": "0",
                "QTY_ALL_ORD_YN": "Y"
            }, "POST")
        
        if data and data.get('rt_cd') == '0':
            logger.info(f"✅ 주문 취소 성공: {code} ({order_no})")
            return True
        else:
            msg = data.get('msg1', 'Unknown') if data else 'Unknown'
            logger.warning(f"⚠️  주문 취소 실패: {msg}")
            return False
    
    except Exception as e:
        logger.error(f"❌ 주문 취소 오류: {e}")
        return False

def place_order(code: str, qty: int, side: str, limit_price: Optional[int] = None) -> Dict:
    """주문 실행"""
    from utils import calculate_order_price
    
    if not check_token_expiry():
        return {'success': False, 'msg': '토큰 만료'}
    
    try:
        cano, acnt_prdt_cd = parse_account_no(ACC_NO)
        if not cano or not acnt_prdt_cd:
            return {'success': False, 'msg': '계좌번호 오류'}
        
        current_price = get_price(code)
        if current_price == 0:
            return {'success': False, 'msg': '가격 조회 실패'}
        
        if limit_price is None:
            order_price = calculate_order_price(current_price, side)
        else:
            order_price = limit_price
        
        tr_id = "TTTC0802U" if side == "buy" else "TTTC0801U"
        
        params = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt_prdt_cd,
            "PDNO": code,
            "ORD_DVSN": "00",
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(order_price)
        }
        
        logger.info(f"📤 주문: {side.upper()} {code} {qty}주 @ {order_price:,}원 (현재가: {current_price:,}원)")
        
        data = api_call(tr_id, "/uapi/domestic-stock/v1/trading/order-cash", params, "POST")
        
        if data and data.get('rt_cd') == '0':
            order_no = data.get('output', {}).get('KRX_FWDG_ORD_ORGNO', 'N/A')
            logger.info(f"✅ 주문 접수: {code} (주문번호: {order_no})")
            return {
                'success': True, 
                'data': data,
                'order_price': order_price,
                'order_no': order_no
            }
        else:
            msg = data.get('msg1', 'API 호출 실패') if data else 'API 호출 실패'
            logger.error(f"❌ 주문 실패: {msg}")
            return {'success': False, 'msg': msg}
    
    except Exception as e:
        logger.error(f"❌ 주문 오류: {e}")
        return {'success': False, 'msg': str(e)}
