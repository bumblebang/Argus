# 라이브 (실주문)

토스 Open API에는 모의투자가 없다. 아래를 모두 만족하기 전에는 `broker.mode` 를 live 로 두지 않는다.

## 체크리스트

1. 페이퍼로 watch 가 안정적으로 돈 뒤에만.
2. 토스 콘솔에 **이 머신 공인 IP** 등록 (`doctor` 가 출력). DHCP면 바뀔 수 있다.
3. 토스 앱에서 Open API 계좌·위임이 이 봇이 쓸 계좌인지 확인.
4. `config.yaml`: `broker.mode: live`, `live_markets`에 열 시장(`KR` 단독 또는 `KR,US`),
   `risk.max_positions` 시장별, US면 소액 `capital.US`·필요 시 `max_order_notional.US`.
5. `.env`: `DRY_RUN=false`, `TOSS_ACCOUNT_NO` 고정.
6. `python scripts/doctor.py` 가 `LIVE 가능` 을 출력.
7. 그다음 `python scripts/watch.py` (또는 OS 상주).

코드도 3중이다: `mode==live` AND dry 아님 AND watch 가 live_client 주입.
배치 스크립트(`athena`, `agent_cycle`)는 live_client 가 없어 페이퍼로 남는다.

## 안전핀

- `data/state/HALT`(또는 `kill_switch_file`) — **전역** 킬스위치, BUY/SELL 전부 차단
- `data/state/HALT.KR` / `HALT.US` — **마켓 pause**, 해당 시장 BUY만 차단(청산 SELL 허용)
- `risk.max_positions: {KR: n, US: m}` — 슬롯은 시장별(스칼라 int면 양쪽에 동일 적용)
- 현금·자본·일손실·DD·capital_sync 는 원래부터 시장별 원장
- 대시 자산 관제: KR/US book + `market_state.fx.USDKRW` 로 ₩ 환산 총자산(참고용)

뇌(Claude 구독)는 운영자 본인 CLI/크레딧을 쓴다. 한도가 나면 판단이 멈춘다. 상주 전에 `NTFY_TOPIC` 을 넣는 것이 안전하다(무음 실패).
