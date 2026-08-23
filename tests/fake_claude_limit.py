"""테스트용 가짜 claude CLI — 모든 모델에서 사용량 한도로 실패(rc=1).

Cursor bridge 3단 폴백(opus→sonnet→inbox) 경로 검증용. 모델 폴백 후에도
한도 메시지가 남아 bridge 로 넘어가는지 확인한다.
"""
import sys

print("Error: weekly limit resets 6pm. Please try again later.")
sys.exit(1)
