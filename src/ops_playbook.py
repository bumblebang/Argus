"""Ops 경보 → 다음에 할 일 (ntfy / 대시보드 공유).

감지 로직은 alert_check.evaluate 가 담당하고, 여기는 사유 문구 → 액션만 매핑한다.
"""
from __future__ import annotations


def actions_for(reasons: list[str], *, brain_mode: str = "ok") -> list[str]:
    """경보 사유 → 짧은 조치 목록(중복 제거, 최대 4줄)."""
    actions: list[str] = []
    joined = " ".join(reasons or [])
    mode = (brain_mode or "ok").strip()

    def add(line: str) -> None:
        if line and line not in actions:
            actions.append(line)

    if any("하트비트" in r for r in reasons):
        add("데몬 확인: heartbeat age·pythonw·http://127.0.0.1:8787")
        add("죽었으면 포그라운드: python scripts/watch.py --ticks 1 로 stderr 확인")

    if any("장 상태 불일치" in r or "세션 캐시" in r for r in reasons):
        add("watch 재기동 또는 gateway.refresh_market_sessions — data/market_sessions.json 확인")
        add("US: FINNHUB_API_KEY·Yahoo marketState vs 토스 calendar 대조")

    if any("인증" in r for r in reasons) or mode == "auth_needed":
        add("scripts\\claude_login.bat 로 재로그인 후 다음 사이클 대기")

    if any("미무장" in r for r in reasons):
        add("브릿지 재무장: argus bridge --serve 60 (또는 bridge_hb_window.cmd)")
        add("PokeTokenBarWin 트레이 「브릿지 judge 켜기」도 동일")

    if any("회로차단" in r for r in reasons) or mode == "circuit_open":
        add("브릿지 --serve 60 켠 뒤 리셋 시각까지 관망(강제 재시도 금지)")

    if any("브릿지 운용" in r for r in reasons) or (
            mode == "bridge" and not any("미무장" in r for r in reasons)):
        add("클코 한도 — 브릿지 유지·리셋 시각까지 무거운 개발 위임 자제")

    if not actions and (reasons or mode not in ("", "ok")):
        add(f"대시보드 시스템 탭·logs/watch.log 확인 (mode={mode})")

    if not reasons and mode == "ok":
        return []

    # reasons 비었는데 mode 이상인 경우 위에서 이미 채움
    if not reasons and not actions and joined:
        add("ALERT.json 사유 확인")

    return actions[:4]


def format_push_body(reasons: list[str], actions: list[str] | None = None,
                     *, budget_line: str | None = None) -> str:
    """ntfy 본문: 사유 | 예산 | 다음: …"""
    body = " | ".join(reasons or []) or "(사유 없음)"
    if budget_line:
        bl = budget_line if budget_line.startswith("예산:") else f"예산: {budget_line}"
        body = f"{body}\n{bl}"
    acts = list(actions) if actions is not None else actions_for(reasons)
    if acts:
        body = f"{body}\n다음: " + " / ".join(acts)
    return body
