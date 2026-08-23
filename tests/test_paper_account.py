import json

from src.paper_account import PaperAccount


def _acct(tmp_path, **kw):
    return PaperAccount(
        cash={"KR": 1_000_000, "US": 10_000},
        fee_rate=kw.get("fee_rate", {"KR": 0.0}),
        slippage_bps=kw.get("slippage_bps", {"KR": 0.0}),
        state_path=tmp_path / "acct.json",
    )


def test_buy_reduces_cash_and_opens_position(tmp_path):
    acct = _acct(tmp_path)
    acct.fill("005930", "KR", "BUY", 10, 1000, "test")
    assert acct.cash["KR"] == 1_000_000 - 10_000
    assert acct.position("005930").qty == 10
    assert acct.position("005930").avg_price == 1000


def test_sell_realizes_pnl(tmp_path):
    acct = _acct(tmp_path)
    acct.fill("005930", "KR", "BUY", 10, 1000, "buy")
    acct.fill("005930", "KR", "SELL", 10, 1200, "sell")
    assert acct.position("005930").qty == 0
    assert acct.realized_pnl["KR"] == (1200 - 1000) * 10
    assert acct.cash["KR"] == 1_000_000 + (1200 - 1000) * 10


def test_sell_tax_only_on_sell(tmp_path):
    # 매도 거래세는 매도에만 부과되고 매수엔 안 붙는다(비대칭).
    acct = PaperAccount(cash={"KR": 1_000_000}, fee_rate={"KR": 0.0},
                        sell_tax_rate={"KR": 0.0015}, state_path=tmp_path / "acct.json")
    b = acct.fill("005930", "KR", "BUY", 10, 1000, "buy")
    assert b.fee == 0.0                                   # 매수엔 세금 없음
    s = acct.fill("005930", "KR", "SELL", 10, 1200, "sell")
    assert s.fee == 1200 * 10 * 0.0015                    # 매도 거래대금 × 세율
    # 실현손익은 세금만큼 차감된다.
    assert acct.realized_pnl["KR"] == (1200 - 1000) * 10 - s.fee


def test_slippage_makes_buy_more_expensive(tmp_path):
    acct = _acct(tmp_path, slippage_bps={"KR": 100})  # 1%
    f = acct.fill("005930", "KR", "BUY", 1, 1000, "buy")
    assert f.price == 1010.0


def test_state_persists(tmp_path):
    acct = _acct(tmp_path)
    acct.fill("AAPL", "US", "BUY", 2, 100, "buy")
    reloaded = PaperAccount(cash={"KR": 1_000_000, "US": 10_000},
                            state_path=tmp_path / "acct.json")
    assert reloaded.position("AAPL").qty == 2


# ── 일 손실 한도용 '오늘' 실현손익 (날짜 경계 리셋) ─────────────────
def test_daily_realized_pnl_accumulates_today(tmp_path):
    acct = _acct(tmp_path)
    acct.fill("005930", "KR", "BUY", 10, 1000, "buy")
    acct.fill("005930", "KR", "SELL", 10, 900, "sell")     # -1000
    assert acct.daily_realized_pnl("KR") == -1000
    assert acct.realized_pnl["KR"] == -1000                # 누적도 같이


def test_daily_realized_pnl_resets_next_day(tmp_path):
    acct = _acct(tmp_path)
    acct.realized_pnl["KR"] = -500_000                     # 과거 누적 손실
    acct.realized_pnl_today["KR"] = -500_000
    acct._pnl_day["KR"] = "2020-01-01"                     # 어제(과거) 날짜
    assert acct.daily_realized_pnl("KR") == 0.0            # 오늘은 0부터
    assert acct.realized_pnl["KR"] == -500_000             # 누적은 유지(리포팅)


def test_daily_pnl_survives_reload(tmp_path):
    acct = _acct(tmp_path)
    acct.fill("005930", "KR", "BUY", 10, 1000, "buy")
    acct.fill("005930", "KR", "SELL", 10, 900, "sell")
    acct2 = _acct(tmp_path)                                # 같은 state_path 재로드
    assert acct2.daily_realized_pnl("KR") == -1000


# ── 실시간 마크 → 미실현 손익 ──────────────────────────────────────
def test_unrealized_pnl_from_marks(tmp_path):
    acct = _acct(tmp_path)
    acct.fill("005930", "KR", "BUY", 10, 1000, "buy")
    acct.set_marks({"005930": 900})
    assert acct.unrealized_pnl("KR") == (900 - 1000) * 10


def test_unrealized_pnl_zero_without_marks(tmp_path):
    acct = _acct(tmp_path)
    acct.fill("005930", "KR", "BUY", 10, 1000, "buy")
    assert acct.unrealized_pnl("KR") == 0.0                # 마크 없으면 0(배치 경로 호환)


def test_set_marks_ignores_bad_prices(tmp_path):
    acct = _acct(tmp_path)
    acct.set_marks({"005930": None, "000660": 0, "035720": 100})
    assert acct.marks == {"035720": 100.0}


def test_원장_손상시_죽지않고_기본값으로_시작(tmp_path):
    """0바이트 원장이 데몬을 크래시 루프에 빠뜨리면 안 된다.

    파싱 실패는 '저장된 상태 없음'으로 강등해야 한다 — 라이브면 실계좌 동기화가
    현금·보유를 다시 채우므로 데몬이 사는 쪽이 언제나 낫다.
    """
    p = tmp_path / "paper_account.json"
    p.write_text("", encoding="utf-8")                 # 쓰기 도중 잘린 상태
    acc = PaperAccount(cash={"KR": 1_000_000}, state_path=p)
    assert acc.cash["KR"] == 1_000_000                 # 기본값으로 기동
    assert acc.open_count == 0
    # 손상 파일은 사후 분석용으로 보존되고, 원래 경로는 비워진다
    assert list(tmp_path.glob("paper_account.corrupt-*"))


def test_원장_깨진JSON도_동일(tmp_path):
    p = tmp_path / "paper_account.json"
    p.write_text('{"cash": {"KR": 1', encoding="utf-8")  # 중간에 끊긴 JSON
    acc = PaperAccount(cash={"KR": 500_000}, state_path=p)
    assert acc.cash["KR"] == 500_000


def test_저장은_원자적이라_tmp를_남기지_않는다(tmp_path):
    """직접 쓰기는 크래시 시 원장을 0바이트로 만든다 → tmp+replace 로 교체."""
    p = tmp_path / "paper_account.json"
    acc = PaperAccount(cash={"KR": 1_000_000}, state_path=p)
    acc.fill("005930", "KR", "BUY", 1, 70_000, "t")
    assert p.exists() and json.loads(p.read_text(encoding="utf-8"))["cash"]["KR"] < 1_000_000
    assert not list(tmp_path.glob("*.tmp"))            # 교체 후 tmp 잔재 없음

    # 저장→재로드 왕복이 정상(회귀 방지)
    acc2 = PaperAccount(cash={"KR": 1_000_000}, state_path=p)
    assert acc2.position("005930").qty == 1
