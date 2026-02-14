#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
급등 검증기 v6.3.0 (개선)
"""

import time
import threading
from collections import defaultdict, deque
from typing import Dict

from config import MIN_STRENGTH, MAX_STRENGTH, MIN_PRICE_STABILITY, get_time_based_thresholds
from utils import logger

# ============================================================================
# SurgeValidator
# ============================================================================
class SurgeValidator:
    """급등 검증 시스템 (개선 버전)"""
    
    def __init__(self):
        self.surge_data = defaultdict(lambda: deque(maxlen=30))  # ⬆️ 20 → 30
        self.price_history = defaultdict(lambda: deque(maxlen=60))  # ⬆️ 30 → 60
        self.lock = threading.Lock()
    
    def add_detection(self, code: str, strength: float, price: int):
        """급등 감지 데이터 추가"""
        with self.lock:
            self.surge_data[code].append({
                'time': time.time(),
                'strength': strength,
                'price': price
            })
            self.price_history[code].append({
                'time': time.time(),
                'price': price
            })
    
    def is_consistent_surge(self, code: str) -> bool:
        """
        급등이 일관되고 지속적인지 확인 (개선 버전)
        
        🔥 주요 개선:
        - 시간대별 동적 기준 적용
        - 최소 데이터 개수 완화 (3 → 2)
        - 안정성 기준 완화 (0.98 → 0.95)
        """
        with self.lock:
            surge_records = self.surge_data.get(code, [])
            if len(surge_records) < 2:  # ⬇️ 3 → 2
                return False
            
            # 🔥 시간대별 설정 적용
            thresholds = get_time_based_thresholds()
            
            # 최근 N초간 데이터만 사용
            now = time.time()
            recent = [d for d in surge_records 
                     if now - d['time'] < thresholds['min_consistent_seconds']]
            
            if len(recent) < 2:  # ⬇️ 3 → 2
                return False
            
            # 평균 체결강도 확인 (동적 기준)
            avg_strength = sum(d['strength'] for d in recent) / len(recent)
            if not (thresholds['min_strength'] <= avg_strength <= MAX_STRENGTH):
                return False
            
            # 가격 안정성 확인 (완화)
            prices = [d['price'] for d in recent]
            max_price = max(prices)
            current_price = prices[-1]
            
            stability = current_price / max_price
            if stability < MIN_PRICE_STABILITY:  # 0.95
                logger.debug(f"가격 불안정: {code} ({stability:.2%})")
                return False
            
            return True
    
    def calculate_momentum_score(self, code: str, strength: float, vol_ratio: float, 
                                 change_rate: float, price: int) -> int:
        """
        🆕 모멘텀 점수 계산 (0-100점)
        
        Args:
            code: 종목코드
            strength: 체결강도
            vol_ratio: 거래량 비율
            change_rate: 가격 변동률
            price: 현재가
        
        Returns:
            점수 (높을수록 좋음)
        """
        score = 0
        
        # 1. 체결강도 (30점)
        if strength >= 150:
            score += 30
        elif strength >= 120:
            score += 25
        elif strength >= 100:
            score += 20
        
        # 2. 거래량 (25점)
        if vol_ratio >= 500:
            score += 25
        elif vol_ratio >= 350:
            score += 20
        elif vol_ratio >= 250:
            score += 15
        elif vol_ratio >= 200:
            score += 10
        
        # 3. 가격 변동 (25점)
        if 2.5 <= change_rate <= 10.0:  # 스윗 스팟
            score += 25
        elif 1.5 <= change_rate <= 15.0:
            score += 18
        elif change_rate >= 1.0:
            score += 10
        
        # 4. 가격대 보너스 (10점)
        if 10000 <= price <= 100000:  # 중형주
            score += 10
        elif 5000 <= price <= 200000:
            score += 7
        elif 3000 <= price <= 300000:
            score += 5
        
        # 5. 지속성 (10점)
        with self.lock:
            surge_records = self.surge_data.get(code, [])
            if len(surge_records) >= 7:
                score += 10
            elif len(surge_records) >= 5:
                score += 7
            elif len(surge_records) >= 3:
                score += 5
        
        return score
    
    def meets_threshold(self, score: int) -> bool:
        """
        🆕 점수가 시간대별 기준을 충족하는지 확인
        
        Args:
            score: 모멘텀 점수
        
        Returns:
            기준 충족 여부
        """
        thresholds = get_time_based_thresholds()
        return score >= thresholds['score_threshold']
    
    def clear_old_data(self):
        """오래된 데이터 정리"""
        with self.lock:
            now = time.time()
            for code in list(self.surge_data.keys()):
                self.surge_data[code] = deque(
                    [d for d in self.surge_data[code] if now - d['time'] < 60],
                    maxlen=30  # ⬆️ 20 → 30
                )
                if not self.surge_data[code]:
                    del self.surge_data[code]
            
            for code in list(self.price_history.keys()):
                self.price_history[code] = deque(
                    [d for d in self.price_history[code] if now - d['time'] < 60],
                    maxlen=60  # ⬆️ 30 → 60
                )
                if not self.price_history[code]:
                    del self.price_history[code]
