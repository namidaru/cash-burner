# 트레이딩 봇 v6.2.7 (모듈화 버전)

한국투자증권 API를 사용한 자동매매 봇 (모듈화된 버전)

## 📁 파일 구조

```
trading_bot/
├── config.py              # 설정 및 상수
├── utils.py               # 유틸리티 함수 (로깅, Discord, API Rate Limiter 등)
├── api_client.py          # 한투 API 클라이언트 (토큰, 주문, 계좌조회 등)
├── order_manager.py       # 주문 관리자 (이벤트 기반 체결 확인)
├── surge_validator.py     # 급등 검증기
├── data_store.py          # 실시간 데이터 저장소
├── websocket_client.py    # 웹소켓 클라이언트
├── trading_logic.py       # 매매 로직 (매수/매도 판단)
├── main.py                # 메인 실행 파일
├── requirements.txt       # 필요한 패키지
└── README.md              # 이 파일
```

## 🚀 사용법

### 1. 환경변수 설정

```bash
export KOREA_INVEST_APP_KEY="your_app_key"
export KOREA_INVEST_APP_SECRET="your_app_secret"
export KOREA_INVEST_ACC_NO="12345678-01"
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."  # 선택사항
```

### 2. 패키지 설치

```bash
pip install -r requirements.txt
```

### 3. 실행

```bash
python main.py
```

## ⚙️ 주요 설정 (config.py)

config.py 파일에서 다음 설정을 변경할 수 있습니다:

```python
# 거래 설정
MIN_PRICE = 5000          # 최소 가격
MAX_PRICE = 500000        # 최대 가격
MAX_STOCKS = 3            # 최대 보유 종목 수
INVESTMENT_RATIO = 0.30   # 투자 비율

# 급등 감지 설정
MIN_STRENGTH = 110        # 최소 체결강도
MAX_STRENGTH = 300        # 최대 체결강도

# 손익 관리
STOP_LOSS = 0.03          # 손절매 (-3%)
PROFIT_TARGET = 0.08      # 익절 (+8%)
TRAILING_STOP = 0.04      # 트레일링 스톱 (-4%)

# 일일 리스크 관리
DAILY_MAX_LOSS_RATE = 0.05    # 일일 최대 손실률 5%
DAILY_MAX_TRADES = 10         # 일일 최대 매매 횟수
```

## 📊 기능

### v6.2.7 주요 기능
- ✅ 이벤트 기반 체결 확인 (0.1초 실시간 체크, 기존 대비 5배 빠름)
- ✅ API 폴백 체결 확인 (웹소켓 미수신 대비)
- ✅ 부분 체결 최소 비율 검증 (50% 이상)
- ✅ 웹소켓 자동 재연결 (지수 백오프)
- ✅ VI 감지 및 회피
- ✅ 지정가 주문 + 슬리피지 제한
- ✅ 수수료 계산 포함
- ✅ 일일 손실 제한
- ✅ 긴급 정지 메커니즘

## 🔧 유지보수

### 각 모듈의 역할

- **config.py**: 모든 설정값 관리
- **utils.py**: 공통 유틸리티 함수
- **api_client.py**: 한투 API 호출 관련
- **order_manager.py**: 주문 체결 관리
- **surge_validator.py**: 급등 패턴 검증
- **data_store.py**: 실시간 데이터 관리
- **websocket_client.py**: 실시간 시세 수신
- **trading_logic.py**: 매수/매도 판단 로직
- **main.py**: 프로그램 시작점

### 수정 시 주의사항

1. **설정 변경**: config.py만 수정
2. **거래 로직 변경**: trading_logic.py 수정
3. **API 추가**: api_client.py에 함수 추가
4. **데이터 처리**: data_store.py 또는 websocket_client.py 수정

## ⚠️ 주의사항

- 이 코드는 모의투자용입니다
- 실전 투자 시 충분한 테스트가 필요합니다
- 본인의 투자 판단과 책임 하에 사용하세요

## 📝 로그

- `trading.log`: 모든 거래 로그 (자동 로테이션)

## 💬 Discord 알림

DISCORD_WEBHOOK_URL을 설정하면 주요 이벤트 알림을 받을 수 있습니다:
- 프로그램 시작/종료
- 매수/매도 체결
- 웹소켓 재연결
- 오류 발생
