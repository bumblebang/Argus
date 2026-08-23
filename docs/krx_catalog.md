# KRX 정보데이터 카탈로그

기계용 레지스트리: [`data/krx_bld_catalog.json`](../data/krx_bld_catalog.json).

## 표면

- URL: `https://data.krx.co.kr`
- JSON: `POST /comm/bldAttendant/getJsonData.cmd` + `bld=dbms/MDC/STAT/...`
- 로그인: `KRX_USER` / `KRX_PASS` → `MDCCOMS001D1.cmd` (iframe `login.jsp`). 구 `loginProc.cmd` 폐기.
  중복 로그인(CD011) 시 `skipDup=Y` 로 기존 세션 끊고 재로그인.
- 유료 Data Marketplace(호가·체결 ms)는 **범위 밖**

## asof / 지연

| 구분 | 대략 시각(KST) |
|------|----------------|
| 정규장 투자자·공매도 중간 | ~15:40–15:45 |
| 당일 최종(시간외 포함) | ~18:00–18:10 |
| 공매도 순보유잔고 | 보고 T → **T+2** |
| 외국인 한도·소진 | 장개시 기준 **D-1/D-2** 확정 |

장후 배치(`build_market_state`)는 **18:15+** 가 본진. 장중 live_slice는 Naver/캐시만 — KRX 전종목 폴링 금지.

## 슬롯 → 소비처

| slot | 소비 |
|------|------|
| positioning / short_market | focus positioning 렌즈, athena |
| flows / flows_market | Naver 폴백 위 KRX 우선, flows_regime 렌즈 |
| program_flows | program_flows 렌즈 |
| foreign_exhaustion | 후보 첨부, 한도 임박 워치 |
| warnings | risk_gate BUY 하드스킵, market_state |
| sentiment (VKOSPI) | fear_kr vol 축 |
| regime (전종목) | 브레드스 보강 |
| index_constituents | 유니버스·슬리브 정합 |
| quant_shadow | 연구 캐시만 — **메인/슬리브 승격 금지** |

## 운영

- spacing ≥ 0.25s, 동일 일자 `data/krx_cache/{slot}/{date}.json` 재사용
- 필드명 변동 → 다중 키 파서(fail-open)
- KRX 신규 피처만으로 Book/flat 슬리브 채택 문구 금지 ([quant-thin-sample](../../.cursor/rules/quant-thin-sample.mdc))
