"""판단 단위 측정 인프라 — 라이브 집행과 분리.

v1: 컨텍스트 아카이브 · 전방 수익 라벨 · 널 매니저 · 리플레이.
리플레이/널 Δ 로는 메인·슬리브 승격 금지.

서브모듈을 직접 import 한다 (from src.eval.labels import …).
여기서는 labels/archive 등을 eager 로드하지 않는다 —
패키지 __init__ 사이드이펙트가 shadow_ledger↔labels 순환을 숨기거나
만들 수 있기 때문이다.
"""
