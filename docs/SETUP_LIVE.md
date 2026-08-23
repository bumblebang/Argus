# 라이브 (실주문)

토스 Open API에는 모의투자가 없다. 아래를 모두 만족하기 전에는 `broker.mode` 를 live 로 두지 않는다.

## 체크리스트

1. 페이퍼로 watch 가 안정적으로 돈 뒤에만.
2. 토스 콘솔에 **이 머신 공인 IP** 등록 (`doctor` 가 출력). DHCP면 바뀔 수 있다.
3. 토스 앱에서 Open API 계좌·위임이 이 봇이 쓸 계좌인지 확인.
4. `config.yaml`: `broker.mode: live`, `live_markets: ["KR"]`, `max_order_notional` 소액.
5. `.env`: `DRY_RUN=false`, `TOSS_ACCOUNT_NO` 고정.
6. `python scripts/doctor.py` 가 `LIVE 가능` 을 출력.
7. 그다음 `python scripts/watch.py` (또는 OS 상주).

코드도 3중이다: `mode==live` AND dry 아님 AND watch 가 live_client 주입.
배치 스크립트(`athena`, `agent_cycle`)는 live_client 가 없어 페이퍼로 남는다.

## 안전핀

- `data/HALT` — 신규 주문 즉시 차단
- US 실주문 기본 끔
- 킬스위치·일손실·드로다운·섹터 한도는 `config.yaml` `risk`

뇌(Claude 구독)는 운영자 본인 CLI/크레딧을 쓴다. 한도가 나면 판단이 멈춘다. 상주 전에 `NTFY_TOPIC` 을 넣는 것이 안전하다(무음 실패). 실주문 권고 범위는 KR. US 는 시세·리서치.
