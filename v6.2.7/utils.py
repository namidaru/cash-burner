#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
유틸리티 함수 모음
"""

import logging
import requests
import time
import threading
from logging.handlers import RotatingFileHandler
from collections import deque
from typing import Optional, Tuple
from config import API_LIMIT_PER_SECOND, API_LIMIT_PER_MINUTE, DISCORD_WEBHOOK_URL

# ============================================================================
# 로깅 시스템
# ============================================================================
def setup_logging():
    """로깅 시스템 설정"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            RotatingFileHandler(
                'trading.log', 
                maxBytes=10*1024*1024,
                backupCount=5,
                encoding='utf-8'
            ),
            logging.StreamHandler()
        ]
    )
    return logging.getLogger(__name__)

logger = setup_logging()

# ============================================================================
# API 호출 제한 관리자
# ============================================================================
class APIRateLimiter:
    """API 호출 제한 관리"""
    def __init__(self):
        self.second_calls = deque(maxlen=API_LIMIT_PER_SECOND)
        self.minute_calls = deque(maxlen=API_LIMIT_PER_MINUTE)
        self.lock = threading.Lock()
    
    def wait_if_needed(self):
        """필요시 대기"""
        with self.lock:
            now = time.time()
            self.second_calls = deque([t for t in self.second_calls if now - t < 1], 
                                     maxlen=API_LIMIT_PER_SECOND)
            self.minute_calls = deque([t for t in self.minute_calls if now - t < 60], 
                                     maxlen=API_LIMIT_PER_MINUTE)
            
            if len(self.second_calls) >= API_LIMIT_PER_SECOND:
                sleep_time = 1 - (now - self.second_calls[0])
                if sleep_time > 0:
                    time.sleep(sleep_time)
            
            if len(self.minute_calls) >= API_LIMIT_PER_MINUTE:
                sleep_time = 60 - (now - self.minute_calls[0])
                if sleep_time > 0:
                    time.sleep(sleep_time)
            
            self.second_calls.append(time.time())
            self.minute_calls.append(time.time())

# ============================================================================
# 계좌번호 파싱
# ============================================================================
def parse_account_no(acc_no: str) -> Tuple[Optional[str], Optional[str]]:
    """
    계좌번호를 CANO와 ACNT_PRDT_CD로 분리
    
    Args:
        acc_no: 계좌번호 (예: "12345678-01")
    
    Returns:
        (CANO, ACNT_PRDT_CD)
    """
    try:
        if '-' in acc_no:
            parts = acc_no.split('-')
            return parts[0], parts[1]
        elif len(acc_no) >= 10:
            return acc_no[:8], acc_no[8:]
        else:
            logger.error(f"⚠️  잘못된 계좌번호 형식: {acc_no}")
            return None, None
    except Exception as e:
        logger.error(f"❌ 계좌번호 파싱 오류: {e}")
        return None, None

# ============================================================================
# Discord 알림
# ============================================================================
def send_discord_message(message: str):
    """Discord 웹훅 메시지 전송"""
    if not DISCORD_WEBHOOK_URL:
        return
    
    try:
        requests.post(
            DISCORD_WEBHOOK_URL, 
            json={"content": message}, 
            timeout=5
        )
    except Exception as e:
        logger.debug(f"Discord 알림 실패: {e}")

# ============================================================================
# 수수료 계산
# ============================================================================
from config import COMMISSION_RATE, TAX_RATE

def calculate_total_cost(price: int, qty: int, is_buy: bool) -> int:
    """
    실제 필요 금액 계산 (수수료 포함)
    
    Args:
        price: 주문 가격
        qty: 수량
        is_buy: 매수 여부
    
    Returns:
        총 필요 금액
    """
    base_amount = price * qty
    commission = int(base_amount * COMMISSION_RATE)
    
    if is_buy:
        # 매수: 위탁수수료만
        return base_amount + commission
    else:
        # 매도: 위탁수수료 + 증권거래세
        tax = int(base_amount * TAX_RATE)
        return base_amount - commission - tax  # 실수령액

def calculate_net_profit(buy_price: int, sell_price: int, qty: int) -> Tuple[int, float]:
    """
    순수익 계산 (수수료 포함)
    
    Returns:
        (순수익 금액, 수익률)
    """
    buy_cost = calculate_total_cost(buy_price, qty, True)
    sell_proceeds = calculate_total_cost(sell_price, qty, False)
    
    net_profit = sell_proceeds - buy_cost
    profit_rate = net_profit / buy_cost
    
    return net_profit, profit_rate

# ============================================================================
# 호가 단위 계산
# ============================================================================
def get_tick_size(price: int) -> int:
    """호가 단위 계산"""
    if price < 1000:
        return 1
    elif price < 5000:
        return 5
    elif price < 10000:
        return 10
    elif price < 50000:
        return 50
    elif price < 100000:
        return 100
    elif price < 500000:
        return 500
    else:
        return 1000

def calculate_order_price(current_price: int, side: str, slippage_ticks: int = 2) -> int:
    """
    주문 가격 계산 (지정가 + 슬리피지)
    
    Args:
        current_price: 현재가
        side: 'buy' or 'sell'
        slippage_ticks: 슬리피지 틱 수
    
    Returns:
        주문 가격
    """
    tick_size = get_tick_size(current_price)
    
    if side == "buy":
        return current_price + (tick_size * slippage_ticks)
    else:
        return current_price - (tick_size * slippage_ticks)
