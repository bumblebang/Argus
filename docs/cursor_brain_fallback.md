# Cursor Auto 뇌 폴백 (cursor_bridge)

클코(Claude Code) 구독 한도가 바닥났을 때, 장중 **Decision + Validation** 만
Cursor 채팅(Auto)이 파일 inbox로 대신 채워 주는 비상 경로다.

Athena / 밸류 트랙은 범위 밖(클코 복구까지 스킵·별도 실패 가능).

## 폴백 체인

1. `claude_model` (opus 등)
2. `claude_fallback_model` (sonnet)
3. **한도성 실패일 때만** — `bridge.heartbeat` 가 신선(armed)이면 `FileInboxLLM`
4. 한도인데 **미무장** → `BrainQuotaError` 즉시 → 뇌 모드 `circuit_open` (240s 대기 없음)

타임아웃·경로 오류는 inbox로 넘기지 않는다.

## 켜기

기본 config.example 은 `cursor_bridge.enabled: false`. 비상 경로라 기본 기동에 넣지 않는다.

1. `config.yaml` `agents.cursor_bridge.enabled: true`
2. `require_armed: true` (기본) — heartbeat 게이트
3. watch 재기동 (Windows 작업 스케줄러 또는 macOS launchd — OS 문서)
4. 기동 로그에 `cursor_bridge ON` 확인
5. heartbeat 무장: `python scripts/bridge_tick.py --serve 60` (기본 **judge**)

`--serve 60 --auto` 는 Decision=관망·Validation=거부(토큰 ≈0, 비상용).
**기본 `--serve`** 는 judge — `request.json`의 **system+user 전체**로 response 작성.

헤드리스 judge: `CURSOR_API_KEY` + `pip install cursor-sdk` (+ optional `CURSOR_BRIDGE_MODEL`).
키 없으면 `judge=pending` → Cursor `/loop` judge(스킬 `argus-bridge`)가 inbox를 채운다.
직접 1회: `python scripts/bridge_tick.py --judge`.

워크스페이스에 Argus 브릿지 스킬이 있으면 그걸 써도 된다. 필수는 아니다.

아래 경로는 **이 저장소 루트** 기준이다. Cursor 워크스페이스가 상위 폴더이고
이 저장소가 `argus/` 하위면, 경로 앞에 `argus/` 를 붙인다.

## Cursor `/loop` 계약

대략 1분마다(또는 요청이 있을 때만):

### 1) 무장 신호 (필수)

`data/inbox/bridge.heartbeat` 를 갱신한다 (`data/llm_inbox` 는 같은 폴더 junction일 수 있음):

```json
{"ts": <unix_epoch_seconds>, "source": "cursor_loop"}
```

`armed_max_age_sec`(기본 90) 보다 오래되면 데몬은 브릿지를 **미무장**으로 보고
한도 시 회로차단한다. 루프가 살아 있으면 매 틱 heartbeat 만 써도 된다.

### 2) 요청 응답

1. `data/inbox/request.json` 이 있으면 읽는다.
2. 필드: `id`, `schema` (`DecisionOutput` | `ValidationOutput`), `system`, `user`.
3. 스키마에 맞는 JSON을 `result`에 넣어 같은 폴더에 `response.json` 작성:

```json
{
  "id": "<request.json 의 id 그대로>",
  "result": { }
}
```

4. **BUY thesis는 `[CURSOR_FALLBACK]`로 시작** (감사추적).
5. 요청 없으면 heartbeat 만 갱신하고 no-op.

`result` 예시 (관망):

```json
{
  "id": "...",
  "result": {
    "market_view": "[CURSOR_FALLBACK] 클코 한도 — 보수 관망",
    "proposals": []
  }
}
```

검증(`ValidationOutput`) 예:

```json
{
  "id": "...",
  "result": {
    "verdicts": [
      {"symbol": "005930", "approved": false, "reason": "[CURSOR_FALLBACK] 근거 부족"}
    ]
  }
}
```

한 사이클은 Decision 1회 + Validation 1회라 **요청이 두 번** 올 수 있다. 각각 응답.

타임아웃 기본 600초. armed 인데도 그 안에 응답 없으면 브릿지 실패로 쌓이고,
연속 N회(`watch.circuit_fail_threshold`, 기본 2)면 `circuit_open`.

## 뇌 모드 (`data/brain_mode.json`)

| mode | 의미 |
|---|---|
| `ok` | 클코 정상 |
| `bridge` | 한도 + 브릿지 운용 |
| `circuit_open` | 미무장/브릿지 실패 — wake 스킵, 리셋·재무장까지 정지 |
| `auth_needed` | 인증 만료 — 재로그인 후 다음 사이클이 복구 |

경보(`alert_check`)는 이 모드 **전이**에만 ntfy 한다. 휴장으로 끄지 않으며,
`ok` 복귀 시에만 "뇌 정상 재개" 푸시.

## 알림

- 첫 bridge 진입 / circuit 진입 / auth: ntfy 1회(모드 전이)
- 프로세스당 구 `cursor_bridge` 1회 알림도 유지(보조)

## 끄기

`enabled: false` + 데몬 재기동. Cursor 루프도 중지.
