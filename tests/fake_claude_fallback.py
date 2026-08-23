"""테스트용 가짜 claude CLI — 주모델(opus)이면 실패(rc=1), 폴백 모델이면 정상 JSON.

ClaudeCLIClient 의 모델 폴백 경로(주모델 실패 → 가벼운 모델 재시도)를 실제 subprocess 로 검증.
"""
import sys

argv = sys.argv[1:]
model = None
if "--model" in argv:
    i = argv.index("--model")
    if i + 1 < len(argv):
        model = argv[i + 1]

prompt = sys.stdin.read()

if model == "opus":
    # 사용량 한도를 모사: 메시지는 stdout 으로, 종료코드는 1.
    print("Usage limit reached. Try again later.")
    sys.exit(1)

# 폴백 모델(sonnet 등): 스키마에 맞는 JSON 출력
if "verdicts" in prompt:
    print('{"verdicts": []}')
else:
    print('{"market_view": "fallback-sonnet", "proposals": []}')
