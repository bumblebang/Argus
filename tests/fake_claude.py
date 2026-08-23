"""테스트용 가짜 claude CLI — stdin(프롬프트)을 읽고, 스키마 종류에 맞는 JSON을 출력.

ClaudeCLIClient 의 subprocess/stdin/stdout/파싱 경로를 실제로 검증하기 위함.
"""
import sys

prompt = sys.stdin.read()
# DecisionOutput 인지 ValidationOutput 인지 프롬프트의 스키마로 구분
if "verdicts" in prompt:
    print('여기 결과입니다: {"verdicts": []}')   # 머리말이 섞여도 추출되는지 확인
else:
    print('{"market_view": "fake-cli", "proposals": []}')
