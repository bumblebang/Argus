"""SQLite 저장소 — 엔진의 단일 진실 소스(상태 + 기록).

거래뿐 아니라 모든 관측·판단·트리거를 시계열로 남긴다.
상주 프로세스가 죽어도 여기서 포지션 상태를 복원할 수 있어야 한다.

테이블:
  positions — 보유 포지션 + 상태기계(state) + thesis + 목표/손절가. 24h 트래킹의 심장.
  events    — 모든 트리거/판단/주문/검증 이벤트(타임스탬프). 사후 추적·디버깅.
  snapshots — 가격·지표·시황 시계열. 학습·백테스트 재료.
  decisions — LLM 판단 저널(action/conviction/thesis/verdict).

ts 는 전부 unix epoch(REAL) — 정렬·범위쿼리가 쉽다.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from .. import paths as _paths

log = get_logger("engine.store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    market      TEXT NOT NULL,
    strategy    TEXT,                       -- 배정된 전략 템플릿명 (장투/스윙/데이트)
    state       TEXT NOT NULL DEFAULT 'open', -- armed / open / closing / closed
    qty         REAL NOT NULL DEFAULT 0,
    avg_price   REAL NOT NULL DEFAULT 0,
    thesis      TEXT,                       -- 진입 사유(왜 샀나). 깨지면 청산 신호.
    target_price REAL,
    stop_price  REAL,
    opened_at   REAL NOT NULL,
    updated_at  REAL NOT NULL,
    closed_at   REAL,
    exit_price  REAL,                       -- 청산 체결가(성과귀속용)
    pnl         REAL,                       -- 실현손익 (exit-avg)×qty, 수수료 제외
    exit_reason TEXT,                       -- stop_hit/target_hit/strategy:x/session_end/brain...
    meta        TEXT                        -- 전략 파라미터 등 (JSON)
);
CREATE INDEX IF NOT EXISTS idx_positions_open ON positions(state, symbol);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    kind    TEXT NOT NULL,                  -- trigger / order / veto / fill / error ...
    symbol  TEXT,
    payload TEXT                            -- JSON
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, ts);

CREATE TABLE IF NOT EXISTS snapshots (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    symbol  TEXT NOT NULL,
    price   REAL,
    payload TEXT                            -- 지표/호가/시황 등 (JSON)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_symbol_ts ON snapshots(symbol, ts);

CREATE TABLE IF NOT EXISTS dossiers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    market      TEXT NOT NULL,
    created_at  REAL NOT NULL,
    expires_at  REAL,                        -- 이후엔 stale(뇌가 근거로 못 씀)
    thesis      TEXT,                        -- 딥리서치 결론(왜 사/팔/관망인가)
    entry_low   REAL,                        -- 진입 존 하단
    entry_high  REAL,                        -- 진입 존 상단
    invalidation REAL,                       -- 무효화 가격(손절 근거)
    target      REAL,                        -- 목표가
    rr          REAL,                        -- 기대 손익비
    conviction  REAL,                        -- 0~1
    evidence    TEXT,                        -- 증거 목록 JSON(재무/수급/기술/베이스레이트/공시)
    source      TEXT DEFAULT 'athena'
);
CREATE INDEX IF NOT EXISTS idx_dossiers_symbol ON dossiers(symbol, created_at);

CREATE TABLE IF NOT EXISTS decisions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    symbol     TEXT,
    action     TEXT,                        -- BUY / SELL / HOLD ...
    conviction REAL,
    thesis     TEXT,
    verdict    TEXT,                        -- approved / vetoed / ...
    payload    TEXT                         -- 원본 제안/검증 (JSON)
);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);

CREATE TABLE IF NOT EXISTS shadow_positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_ts        REAL NOT NULL,
    cycle_ts_iso    TEXT,
    sleeve          TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    market          TEXT NOT NULL,
    block_status    TEXT NOT NULL,
    block_bucket    TEXT,
    block_reason    TEXT,
    verifier_reason TEXT,
    concerns        TEXT,
    conviction      REAL,
    horizon         TEXT,
    target_weight   REAL,
    thesis          TEXT,
    strategy        TEXT,
    proposal_json   TEXT,
    entry_price     REAL NOT NULL,
    entry_ts        REAL NOT NULL,
    state           TEXT NOT NULL DEFAULT 'open',
    exit_price      REAL,
    exit_ts         REAL,
    exit_reason     TEXT,
    ret_pct         REAL,
    scored_at       REAL,
    meta            TEXT,
    UNIQUE(cycle_ts, symbol, sleeve)
);
CREATE INDEX IF NOT EXISTS idx_shadow_state ON shadow_positions(state);
CREATE INDEX IF NOT EXISTS idx_shadow_bucket ON shadow_positions(block_bucket);

-- 접수됐지만 종결되지 않은 실주문. 토스 API 에 '미체결 주문 목록' 조회가 없어
-- (order_get 단건뿐) 프로세스가 죽으면 고아 주문을 발견할 방법이 이 표뿐이다.
-- settled_at 이 찍힌 행은 종결됐지만 아직 원장 귀속(J3)이 안 된 체결분이다 —
-- 재대사가 실체결가 출처로 소비한 뒤 삭제한다.
CREATE TABLE IF NOT EXISTS working_orders (
    order_id        TEXT PRIMARY KEY,
    symbol          TEXT NOT NULL,
    market          TEXT NOT NULL,
    side            TEXT NOT NULL,           -- BUY / SELL
    qty             REAL NOT NULL,
    price           REAL NOT NULL,
    filled_qty      REAL NOT NULL DEFAULT 0, -- 마지막 조회의 누적 체결수량
    filled_avg      REAL,                    -- 누적 평균체결가
    fee             REAL,                    -- 누적 수수료+세금
    applied_qty     REAL NOT NULL DEFAULT 0, -- 이미 원장에 반영된 체결수량
    applied_notional REAL NOT NULL DEFAULT 0,
    applied_fee     REAL NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,           -- PENDING / PARTIAL_FILLED / ...
    placed_at       REAL NOT NULL,
    last_checked    REAL,
    settled_at      REAL,                    -- 종결 확인 시각(귀속 대기)
    reason          TEXT,
    meta            TEXT                     -- JSON
);
CREATE INDEX IF NOT EXISTS idx_working_symbol ON working_orders(symbol);
"""


def _dumps(obj: Any) -> str | None:
    if obj is None:
        return None
    return json.dumps(obj, ensure_ascii=False, default=str)


class Store:
    def __init__(self, path: str | Path = "data/bot.db") -> None:
        self.path = _paths.resolve("db", configured=path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False + 내부 Lock 으로 멀티스레드 호출 직렬화.
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")     # 상시 쓰기 동시성
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()
        self._lock = threading.Lock()

    def _migrate(self) -> None:
        """기존 DB 에 새 컬럼 추가(멱등). CREATE IF NOT EXISTS 는 기존 테이블을 못 바꾼다."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(positions)")}
        for name, ddl in (("exit_price", "exit_price REAL"), ("pnl", "pnl REAL"),
                          ("exit_reason", "exit_reason TEXT"),
                          ("parent_id", "parent_id INTEGER")):
            if name not in cols:
                self.conn.execute(f"ALTER TABLE positions ADD COLUMN {ddl}")
        wcols = {r["name"] for r in self.conn.execute("PRAGMA table_info(working_orders)")}
        for name, ddl in (("filled_avg", "filled_avg REAL"), ("fee", "fee REAL"),
                          ("applied_qty", "applied_qty REAL NOT NULL DEFAULT 0"),
                          ("applied_notional", "applied_notional REAL NOT NULL DEFAULT 0"),
                          ("applied_fee", "applied_fee REAL NOT NULL DEFAULT 0"),
                          ("settled_at", "settled_at REAL")):
            if name not in wcols:
                self.conn.execute(f"ALTER TABLE working_orders ADD COLUMN {ddl}")
        self._dedupe_open_positions()
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_positions_one_open_per_symbol "
            "ON positions(symbol) WHERE state='open'")

    def _dedupe_open_positions(self) -> None:
        """symbol 당 open 2행 이상이면 최신 id만 남기고 나머지는 closed 처리."""
        dupes = self.conn.execute(
            "SELECT symbol FROM positions WHERE state='open' "
            "GROUP BY symbol HAVING COUNT(*) > 1").fetchall()
        if not dupes:
            return
        now = time.time()
        for row in dupes:
            sym = row["symbol"]
            ids = [int(r["id"]) for r in self.conn.execute(
                "SELECT id FROM positions WHERE symbol=? AND state='open' ORDER BY id",
                (sym,)).fetchall()]
            keep = ids[-1]
            for rid in ids[:-1]:
                self.conn.execute(
                    "UPDATE positions SET state='closed', closed_at=?, updated_at=?,"
                    " exit_reason=? WHERE id=?",
                    (now, now, "dedupe:duplicate_open", rid))
        self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ── 미체결 주문 레지스트리 (J2) ────────────────────────
    def upsert_working_order(self, *, order_id: str, symbol: str, market: str,
                             side: str, qty: float, price: float,
                             status: str, filled_qty: float = 0.0,
                             filled_avg: float | None = None,
                             fee: float | None = None,
                             applied_qty: float = 0.0,
                             applied_notional: float = 0.0,
                             applied_fee: float = 0.0,
                             placed_at: float | None = None,
                             reason: str | None = None,
                             meta: dict | None = None) -> None:
        """미체결 주문 기록(멱등). 같은 order_id 재접수 시 상태만 갱신.

        applied_* 는 '이미 원장에 반영된' 체결분이다. 접수 시점에 한 번만 쓰고
        이후 갱신하지 않는다 — 재대사 귀속이 미반영분을 정확히 계산하는 기준선.
        """
        now = time.time()
        with self._lock:
            self.conn.execute(
                "INSERT INTO working_orders(order_id, symbol, market, side, qty,"
                " price, filled_qty, filled_avg, fee, applied_qty, applied_notional,"
                " applied_fee, status, placed_at, last_checked, reason, meta)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(order_id) DO UPDATE SET"
                " filled_qty=excluded.filled_qty, status=excluded.status,"
                " last_checked=excluded.last_checked",
                (order_id, symbol, market, side, float(qty), float(price),
                 float(filled_qty), filled_avg, fee, float(applied_qty),
                 float(applied_notional), float(applied_fee),
                 status, placed_at or now, now, reason, _dumps(meta)))
            self.conn.commit()

    def update_working_order(self, order_id: str, *, status: str | None = None,
                             filled_qty: float | None = None,
                             filled_avg: float | None = None,
                             fee: float | None = None,
                             settled_at: float | None = None,
                             applied_qty: float | None = None,
                             applied_notional: float | None = None,
                             applied_fee: float | None = None) -> None:
        sets, vals = ["last_checked=?"], [time.time()]
        for col, val in (("status", status), ("filled_qty", filled_qty),
                         ("filled_avg", filled_avg), ("fee", fee),
                         ("settled_at", settled_at), ("applied_qty", applied_qty),
                         ("applied_notional", applied_notional),
                         ("applied_fee", applied_fee)):
            if val is None:
                continue
            sets.append(f"{col}=?")
            vals.append(val if col == "status" else float(val))
        vals.append(order_id)
        with self._lock:
            self.conn.execute(
                f"UPDATE working_orders SET {', '.join(sets)} WHERE order_id=?", vals)
            self.conn.commit()

    def delete_working_order(self, order_id: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM working_orders WHERE order_id=?", (order_id,))
            self.conn.commit()

    def get_working_orders(self, symbol: str | None = None, *,
                           side: str | None = None,
                           settled: bool | None = None) -> list[dict]:
        """settled=None 전체 / False 진행 중만 / True 귀속 대기분만."""
        sql, args = "SELECT * FROM working_orders", []
        where = []
        if symbol:
            where.append("symbol=?")
            args.append(symbol)
        if side:
            where.append("side=?")
            args.append(side)
        if settled is True:
            where.append("settled_at IS NOT NULL")
        elif settled is False:
            where.append("settled_at IS NULL")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY placed_at"
        return [dict(r) for r in self.conn.execute(sql, tuple(args)).fetchall()]

    def has_working_order(self, symbol: str, side: str | None = None) -> bool:
        """진행 중인(종결 미확인) 주문이 있는지. 귀속 대기분은 재발주를 막지 않는다.

        side 를 주면 같은 방향만 본다 — 미체결 BUY 가 손절 SELL 을 막지 않게.
        """
        sql = ("SELECT 1 FROM working_orders WHERE symbol=? AND settled_at IS NULL")
        args: list[Any] = [symbol]
        if side:
            sql += " AND side=?"
            args.append(side)
        sql += " LIMIT 1"
        row = self.conn.execute(sql, tuple(args)).fetchone()
        return row is not None

    # ── 이벤트/스냅샷/판단 기록 ───────────────────────────
    def log_event(self, kind: str, symbol: str | None = None,
                  payload: dict | None = None, ts: float | None = None) -> int:
        return self._insert(
            "INSERT INTO events(ts, kind, symbol, payload) VALUES(?,?,?,?)",
            (ts or time.time(), kind, symbol, _dumps(payload)))

    def record_snapshot(self, symbol: str, price: float | None = None,
                        payload: dict | None = None, ts: float | None = None) -> int:
        return self._insert(
            "INSERT INTO snapshots(ts, symbol, price, payload) VALUES(?,?,?,?)",
            (ts or time.time(), symbol, price, _dumps(payload)))

    def record_snapshots(self, rows: list[dict], ts: float | None = None) -> int:
        """배치 기록: [{symbol, price, payload?}]. /prices 1콜 결과를 한 번에 저장."""
        ts = ts or time.time()
        params = [(ts, r["symbol"], r.get("price"), _dumps(r.get("payload"))) for r in rows]
        with self._lock:
            self.conn.executemany(
                "INSERT INTO snapshots(ts, symbol, price, payload) VALUES(?,?,?,?)", params)
            self.conn.commit()
        return len(params)

    def nearest_snapshot_price(self, symbol: str, ts: float, *,
                               window_sec: float = 3600) -> float | None:
        """ts 근처 snapshots 가격 — shadow_ledger 등 Store._lock 경유 조회용."""
        with self._lock:
            row = self.conn.execute(
                "SELECT price FROM snapshots WHERE symbol=? AND ts BETWEEN ? AND ? "
                "ORDER BY ABS(ts-?) LIMIT 1",
                (symbol, ts - window_sec, ts + window_sec, ts),
            ).fetchone()
            if row and row[0]:
                return float(row[0])
        return None

    def record_decision(self, symbol: str | None, action: str | None,
                        conviction: float | None = None, thesis: str | None = None,
                        verdict: str | None = None, payload: dict | None = None,
                        ts: float | None = None) -> int:
        return self._insert(
            "INSERT INTO decisions(ts, symbol, action, conviction, thesis, verdict, payload)"
            " VALUES(?,?,?,?,?,?,?)",
            (ts or time.time(), symbol, action, conviction, thesis, verdict, _dumps(payload)))

    # ── 포지션 상태기계 ───────────────────────────────────
    def open_position(self, symbol: str, market: str, qty: float, avg_price: float,
                      strategy: str | None = None, thesis: str | None = None,
                      target_price: float | None = None, stop_price: float | None = None,
                      meta: dict | None = None) -> int:
        """open 포지션 등록. 동일 symbol open 행이 있으면 qty/avg 갱신(중복 open 방지)."""
        now = time.time()
        with self._lock:
            dup = self.conn.execute(
                "SELECT id FROM positions WHERE symbol=? AND state='open'", (symbol,)
            ).fetchone()
            if dup:
                pid = int(dup["id"])
                self.conn.execute(
                    "UPDATE positions SET qty=?, avg_price=?, updated_at=? WHERE id=?",
                    (qty, avg_price, now, pid))
                self.conn.commit()
                return pid
            cur = self.conn.execute(
                "INSERT INTO positions(symbol, market, strategy, state, qty, avg_price, thesis,"
                " target_price, stop_price, opened_at, updated_at, meta)"
                " VALUES(?,?,?,'open',?,?,?,?,?,?,?,?)",
                (symbol, market, strategy, qty, avg_price, thesis,
                 target_price, stop_price, now, now, _dumps(meta)))
            self.conn.commit()
            return int(cur.lastrowid)

    def get_open_positions(self) -> list[sqlite3.Row]:
        """실보유 포지션만(armed 진입대기 제외). 감시 루프의 손절/익절·전략청산 대상."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM positions WHERE state NOT IN ('closed','armed')"
                " ORDER BY opened_at")
            return cur.fetchall()

    # ── 진입 대기(armed) — 코드 자율 진입 ─────────────────
    def arm_candidate(self, symbol: str, market: str, *, strategy: str | None = None,
                      thesis: str | None = None, target_price: float | None = None,
                      stop_price: float | None = None, meta: dict | None = None) -> int:
        """진입 대기 후보 등록(state='armed', qty=0). 뇌가 종목·전략·파라미터를 배정하면
        감시 루프가 그 종목 캔들에 전략 decide() 를 돌려 BUY 신호 시 실제 진입(promote)."""
        now = time.time()
        return self._insert(
            "INSERT INTO positions(symbol, market, strategy, state, qty, avg_price, thesis,"
            " target_price, stop_price, opened_at, updated_at, meta)"
            " VALUES(?,?,?,'armed',0,0,?,?,?,?,?,?)",
            (symbol, market, strategy, thesis, target_price, stop_price, now, now, _dumps(meta)))

    def get_armed(self) -> list[sqlite3.Row]:
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM positions WHERE state='armed' ORDER BY opened_at")
            return cur.fetchall()

    def promote_armed(self, pos_id: int, qty: float, avg_price: float, *,
                      target_price: float | None = None, stop_price: float | None = None,
                      meta: dict | None = None) -> None:
        """armed → open 전환(실제 진입 체결 시). 진입가 기준 목표/손절을 확정한다."""
        now = time.time()
        sets = ["state='open'", "qty=?", "avg_price=?", "opened_at=?", "updated_at=?"]
        vals: list[Any] = [qty, avg_price, now, now]
        if target_price is not None:
            sets.append("target_price=?"); vals.append(target_price)
        if stop_price is not None:
            sets.append("stop_price=?"); vals.append(stop_price)
        if meta is not None:
            sets.append("meta=?"); vals.append(_dumps(meta))
        with self._lock:
            sym_row = self.conn.execute(
                "SELECT symbol FROM positions WHERE id=?", (pos_id,)).fetchone()
            if sym_row:
                sym = sym_row["symbol"]
                for row in self.conn.execute(
                        "SELECT id FROM positions WHERE symbol=? AND state='open' AND id!=?",
                        (sym, pos_id)).fetchall():
                    self.conn.execute(
                        "UPDATE positions SET state='closed', closed_at=?, updated_at=?,"
                        " exit_reason=? WHERE id=?",
                        (now, now, "dedupe:promote_merge", int(row["id"])))
            self.conn.execute(f"UPDATE positions SET {', '.join(sets)} WHERE id=?",
                              (*vals, pos_id))
            self.conn.commit()

    def update_position(self, pos_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = [_dumps(v) if k == "meta" else v for k, v in fields.items()]
        with self._lock:
            self.conn.execute(f"UPDATE positions SET {cols} WHERE id=?", (*vals, pos_id))
            self.conn.commit()

    def disarm_symbol(self, symbol: str, *, exclude_id: int | None = None,
                      reason: str = "disarm:already_held") -> int:
        """보유 중인 종목의 stale armed 행을 해제(피라미딩 누수 방지). promote 대상은 exclude."""
        now = time.time()
        with self._lock:
            rows = self.conn.execute(
                "SELECT id FROM positions WHERE symbol=? AND state='armed'",
                (symbol,)).fetchall()
            n = 0
            for row in rows:
                rid = int(row["id"])
                if exclude_id is not None and rid == int(exclude_id):
                    continue
                self.conn.execute(
                    "UPDATE positions SET state='closed', closed_at=?, updated_at=?,"
                    " exit_reason=? WHERE id=?",
                    (now, now, reason, rid))
                n += 1
            if n:
                self.conn.commit()
            return n

    def record_partial_exit(self, pos_id: int, sell_qty: float, exit_price: float,
                            *, reason: str | None = None, fee: float = 0.0) -> None:
        """부분 매도 귀속 — 매도 slice 를 closed 행으로 기록하고 open qty 를 줄인다."""
        now = time.time()
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM positions WHERE id=?", (pos_id,)).fetchone()
            if not row or row["state"] != "open":
                return
            old_qty = float(row["qty"] or 0)
            avg = float(row["avg_price"] or 0)
            sell_qty = min(float(sell_qty), old_qty)
            if sell_qty <= 1e-9 or not avg:
                return
            pnl = (float(exit_price) - avg) * sell_qty - float(fee or 0)
            new_qty = old_qty - sell_qty
            self.conn.execute(
                "INSERT INTO positions(symbol, market, strategy, state, qty, avg_price, thesis,"
                " target_price, stop_price, opened_at, updated_at, closed_at, exit_price, pnl,"
                " exit_reason, meta, parent_id)"
                " VALUES(?,?,?,'closed',?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["symbol"], row["market"], row["strategy"], sell_qty, avg,
                 row["thesis"], row["target_price"], row["stop_price"],
                 row["opened_at"], now, now, exit_price, pnl,
                 reason or "partial_exit", row["meta"], pos_id))
            if new_qty <= 1e-9:
                self.conn.execute(
                    "UPDATE positions SET state='closed', qty=0, closed_at=?, updated_at=?,"
                    " exit_reason=?, parent_id=? WHERE id=?",
                    (now, now, reason or "partial_exit", pos_id, pos_id))
            else:
                self.conn.execute(
                    "UPDATE positions SET qty=?, updated_at=? WHERE id=?",
                    (new_qty, now, pos_id))
            self.conn.commit()

    def close_position(self, pos_id: int, *, exit_price: float | None = None,
                       reason: str | None = None, fee: float = 0.0) -> None:
        """청산 처리 + 성과귀속 기록. exit_price 가 있으면 pnl=(exit-avg)×qty-fee 를 확정.

        pnl 은 account.realized_pnl(수수료 포함)과 맞춘다. exit_price 없이 닫으면 pnl NULL.
        """
        now = time.time()
        with self._lock:
            pnl = None
            parent_id = pos_id
            if exit_price is not None:
                row = self.conn.execute(
                    "SELECT qty, avg_price FROM positions WHERE id=?", (pos_id,)).fetchone()
                if row and row["qty"] and row["avg_price"]:
                    pnl = ((float(exit_price) - float(row["avg_price"])) * float(row["qty"])
                           - float(fee or 0))
            self.conn.execute(
                "UPDATE positions SET state='closed', closed_at=?, updated_at=?,"
                " exit_price=?, pnl=?, exit_reason=?, parent_id=? WHERE id=?",
                (now, now, exit_price, pnl, reason, parent_id, pos_id))
            self.conn.commit()

    def get_closed_positions(self, since: float | None = None,
                             limit: int | None = None) -> list[sqlite3.Row]:
        """청산 완료(pnl 확정) 포지션 — 성과귀속(attribution)의 원천. 최신순."""
        sql = "SELECT * FROM positions WHERE state='closed' AND pnl IS NOT NULL"
        params: list[Any] = []
        if since is not None:
            sql += " AND closed_at >= ?"
            params.append(since)
        sql += " ORDER BY closed_at DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def closed_trades(self, symbols: list[str] | None = None) -> list[sqlite3.Row]:
        """실제 진입됐다 청산된 포지션(qty>0) — 종목별 회고(lessons)용. 최신순.

        get_closed_positions 와 달리 pnl 결손도 포함한다: 진입은 됐으나 청산 손익이
        기록 안 된 거래도 "과거 이 종목을 샀었다"는 회고 재료다. armed 만 됐다 해제된
        행(qty=0, 실제 매수 없음)은 거래가 아니므로 제외. symbols 주면 그 종목만.
        """
        sql = "SELECT * FROM positions WHERE state='closed' AND qty>0"
        params: list[Any] = []
        if symbols:
            marks = ",".join("?" for _ in symbols)
            sql += f" AND symbol IN ({marks})"
            params.extend(symbols)
        sql += " ORDER BY closed_at DESC"
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    # ── 도시에(Athena 딥리서치 결과) ──────────────────────
    def save_dossier(self, symbol: str, market: str, *, thesis: str | None = None,
                     entry_low: float | None = None, entry_high: float | None = None,
                     invalidation: float | None = None, target: float | None = None,
                     rr: float | None = None, conviction: float | None = None,
                     evidence: dict | list | None = None, source: str = "athena",
                     ttl_hours: float = 48.0) -> int:
        now = time.time()
        return self._insert(
            "INSERT INTO dossiers(symbol, market, created_at, expires_at, thesis,"
            " entry_low, entry_high, invalidation, target, rr, conviction, evidence, source)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (symbol, market, now, now + ttl_hours * 3600, thesis, entry_low, entry_high,
             invalidation, target, rr, conviction, _dumps(evidence), source))

    def get_fresh_dossier(self, symbol: str, now: float | None = None) -> sqlite3.Row | None:
        """유효기간 내 최신 도시에 1건. 없으면 None(뇌는 그 종목을 얕은 근거로 못 산다)."""
        now = now or time.time()
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM dossiers WHERE symbol=? AND expires_at > ?"
                " ORDER BY created_at DESC LIMIT 1", (symbol, now)).fetchone()

    def dossier_coverage(self, now: float | None = None) -> list[sqlite3.Row]:
        """종목별 최신 도시에 나이 — Athena 가 '오래된 것부터 갱신'할 때 쓰는 커버리지 맵."""
        now = now or time.time()
        with self._lock:
            return self.conn.execute(
                "SELECT symbol, market, MAX(created_at) AS created_at,"
                " MAX(expires_at) AS expires_at FROM dossiers GROUP BY symbol"
                " ORDER BY created_at ASC").fetchall()

    def recent_events(self, kind: str, since: float,
                      limit: int = 20) -> list[sqlite3.Row]:
        """특정 종류의 최근 이벤트(최신순) — 예: 뇌 컨텍스트에 실을 최근 공시."""
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM events WHERE kind=? AND ts>=? ORDER BY ts DESC LIMIT ?",
                (kind, since, limit)).fetchall()

    def decision_counts(self, since: float) -> list[sqlite3.Row]:
        """액션×판정별 결정 건수(since 이후). 성과귀속(attribution)용 — 락으로 직렬화."""
        with self._lock:
            return self.conn.execute(
                "SELECT action, verdict, COUNT(*) n FROM decisions WHERE ts >= ?"
                " GROUP BY action, verdict", (since,)).fetchall()

    # ── 그림자 장부 (반사실 페이퍼) ─────────────────────────
    def insert_shadow_position(self, **fields: Any) -> int | None:
        """차단된 BUY 제안을 그림자 페이퍼로 등록. (cycle_ts,symbol,sleeve) 중복 시 None."""
        cols = [k for k in fields if fields[k] is not None]
        if not cols:
            return None
        ph = ", ".join("?" * len(cols))
        names = ", ".join(cols)
        vals = [_dumps(fields[k]) if k in ("concerns", "proposal_json", "meta")
                else fields[k] for k in cols]
        try:
            with self._lock:
                cur = self.conn.execute(
                    f"INSERT INTO shadow_positions ({names}) VALUES ({ph})", vals)
                self.conn.commit()
                return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            return None

    def get_open_shadow_positions(self) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM shadow_positions WHERE state='open' ORDER BY entry_ts"
            ).fetchall()

    def get_pending_shadow_positions(self) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM shadow_positions WHERE state='pending' ORDER BY entry_ts"
            ).fetchall()

    def get_scorable_shadow_positions(self) -> list[sqlite3.Row]:
        """open + pending — 채점 대상."""
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM shadow_positions WHERE state IN ('open','pending')"
                " ORDER BY entry_ts"
            ).fetchall()

    def cancel_shadow_positions(self, symbol: str,
                                  after_ts: float | None = None) -> int:
        now = time.time()
        sql = ("UPDATE shadow_positions SET state='cancelled',"
               " exit_reason='filled_actual', scored_at=? WHERE symbol=?"
               " AND state IN ('pending','open')")
        params: list[Any] = [now, symbol]
        if after_ts is not None:
            sql += " AND entry_ts >= ?"
            params.append(after_ts)
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur.rowcount

    def is_symbol_armed(self, symbol: str) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM positions WHERE symbol=? AND state='armed' LIMIT 1",
                (symbol,)).fetchone()
            return row is not None

    def has_open_since(self, symbol: str, since_ts: float) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM positions WHERE symbol=? AND state='open'"
                " AND opened_at >= ? LIMIT 1",
                (symbol, since_ts)).fetchone()
            return row is not None

    def had_position_since(self, symbol: str, since_ts: float) -> bool:
        """since 이후 실제 진입(open 또는 이미 청산)이 있었는지.

        has_open_since 는 지금 들고 있는 것만 본다. 그림자 채점 시점에 이미
        청산됐으면 '막아서 손해'로 남고, 실제로는 그 종목을 샀었다.
        """
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM positions WHERE symbol=? AND state IN ('open','closed')"
                " AND IFNULL(qty,0) > 0 AND opened_at >= ? LIMIT 1",
                (symbol, since_ts)).fetchone()
            return row is not None

    def get_scored_shadow_positions(self, *, since: float | None = None,
                                    limit: int | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM shadow_positions WHERE state='scored'"
        params: list[Any] = []
        if since is not None:
            sql += " AND scored_at >= ?"
            params.append(since)
        sql += " ORDER BY scored_at DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def score_shadow_position(self, shadow_id: int, *, exit_price: float,
                              exit_ts: float, exit_reason: str,
                              ret_pct: float) -> None:
        now = time.time()
        with self._lock:
            self.conn.execute(
                "UPDATE shadow_positions SET state='scored', exit_price=?, exit_ts=?,"
                " exit_reason=?, ret_pct=?, scored_at=? WHERE id=?"
                " AND state IN ('open','pending')",
                (exit_price, exit_ts, exit_reason, ret_pct, now, shadow_id))
            self.conn.commit()

    def update_shadow_ret_pct(self, shadow_id: int, ret_pct: float) -> None:
        """이미 scored 된 행의 ret_pct 만 갱신(비용 재채점). scored_at 유지."""
        with self._lock:
            self.conn.execute(
                "UPDATE shadow_positions SET ret_pct=? WHERE id=? AND state='scored'",
                (float(ret_pct), shadow_id))
            self.conn.commit()

    def skip_shadow_position(self, shadow_id: int, reason: str) -> None:
        now = time.time()
        with self._lock:
            self.conn.execute(
                "UPDATE shadow_positions SET state='skipped', exit_reason=?, scored_at=?"
                " WHERE id=? AND state IN ('open','pending')",
                (reason, now, shadow_id))
            self.conn.commit()

    # ── 내부 ──────────────────────────────────────────────
    def _insert(self, sql: str, params: tuple) -> int:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return int(cur.lastrowid)
