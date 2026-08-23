"""M1 골격: stateful continuous 엔진.

상시 가동 단일 프로세스의 토대 모듈.
  ratelimit — 그룹별 토큰버킷 (토스 BASIC tier TPS 준수)
  store     — SQLite 저장소 (positions/events/snapshots/decisions)
  gateway   — 모든 토스 호출의 단일 통로 (rate limit + 폴링 헬퍼)
  triggers  — 각성 트리거 순수 함수 (손절/익절/변동성/관심가)
  loop      — Argus-Watch 상시 감시 루프 (폴링→트리거→로깅, 2층 폴링예산)
"""
