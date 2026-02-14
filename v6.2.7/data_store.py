#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
실시간 데이터 저장소
"""

import time
import threading
from collections import deque
from typing import Dict, List, Callable

from utils import logger

# ============================================================================
# RealtimeDataStore
# ============================================================================
class RealtimeDataStore:
    """실시간 데이터 저장소"""
    
    def __init__(self):
        self.prices = {}
        self.contracts = {}
        self.surge_callbacks = []
        self.lock = threading.Lock()
    
    def update_price(self, code: str, price: int):
        """가격 업데이트"""
        with self.lock:
            self.prices[code] = {'price': price, 'time': time.time()}
    
    def get_price(self, code: str) -> int:
        """현재가 조회 (10초 이내 데이터만)"""
        with self.lock:
            data = self.prices.get(code)
            if data and time.time() - data['time'] < 10:
                return data['price']
            return 0
    
    def update_contract(self, code: str, data: Dict):
        """체결 데이터 업데이트"""
        with self.lock:
            if code not in self.contracts:
                self.contracts[code] = deque(maxlen=100)
            self.contracts[code].append({'time': time.time(), 'data': data})
    
    def register_surge_callback(self, callback: Callable):
        """급등 콜백 등록"""
        self.surge_callbacks.append(callback)
    
    def trigger_surge_check(self, code: str):
        """급등 체크 트리거"""
        for callback in self.surge_callbacks:
            try:
                callback(code)
            except Exception as e:
                logger.error(f"❌ 콜백 오류: {e}")
