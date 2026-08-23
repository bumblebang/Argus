# 뇌 인증

판단(LLM) 백엔드. 실주문 키(토스)와 별개다.

| 방법 | 비용 | 설정 |
|---|---|---|
| **claude CLI** (권장) | 구독 한도 | PATH의 `claude`, `watch.brain_backend: cli` |
| API 키 | Anthropic 종량제 | `.env` `ANTHROPIC_API_KEY` |
| `--dry` | 없음 | MockLLM. 배선 확인용 |

CLI는 운영자가 쓰는 Claude 구독과 **같은 한도**를 나눈다. 장중 무거운 개발을 같은 구독으로 돌리면 봇 판단이 멈출 수 있다.

확인:

```
claude -p "hello"
python scripts/check_cli.py
```

API 키 경로:

```
python scripts/check_auth.py
```

모델·타임아웃은 `config.yaml` 의 `agents` 블록.
하루 콜 감각·절약 노브·키 목록: [docs/USAGE.md](docs/USAGE.md).
