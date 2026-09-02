"""기동 전 점검: 설정·키·뇌·타임존·공인 IP. 네트워크 호출은 토스/IP만(있을 때).

종료코드: 0=기동 가능(페이퍼 또는 라이브 준비), 1=막힘.
Phase 2: --migrate-data [--apply] 로 data/ 레이아웃 이동(기본 dry-run).
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _ok(label: str, msg: str) -> None:
    print(f"  OK   {label}: {msg}")


def _warn(label: str, msg: str) -> None:
    print(f"  WARN {label}: {msg}")


def _bad(label: str, msg: str) -> None:
    print(f"  FAIL {label}: {msg}")


def _filled(name: str) -> bool:
    return bool((os.getenv(name) or "").strip())


def mask_acct(no) -> str:
    s = str(no or "")
    if len(s) <= 4:
        return "****"
    return ("*" * (len(s) - 4)) + s[-4:]


def probe_toss(cfg) -> tuple[bool, str]:
    """토스 계좌 1회 조회. 실패해도 예외를 밖으로 안 던진다."""
    try:
        from src.engine.gateway import TossGateway
        accts = TossGateway.from_config(cfg).get_accounts()
    except Exception as e:
        return False, f"조회 실패: {type(e).__name__}"
    if not accts:
        return False, "계좌 0건 - 허용 IP·앱 위임 확인"
    bits = [f"seq={a.get('accountSeq')} no={mask_acct(a.get('accountNo'))}"
            for a in accts[:4]]
    return True, f"{len(accts)}건 ({', '.join(bits)})"


def public_ip() -> str | None:
    try:
        from urllib.request import urlopen
        with urlopen("https://api.ipify.org", timeout=5) as r:
            return r.read().decode("ascii", errors="replace").strip() or None
    except Exception:
        return None


def check_tz() -> bool:
    try:
        ZoneInfo("Asia/Seoul")
        _ok("tz", "Asia/Seoul")
        return True
    except ZoneInfoNotFoundError:
        _bad("tz", "Asia/Seoul 없음 - pip install tzdata")
        return False


def check_claude(cmd: str) -> None:
    if not cmd or cmd == "claude":
        path = shutil.which("claude")
        if path:
            _ok("claude", path)
        else:
            _warn("claude", "PATH 에 없음. CLI 뇌면 Claude Code 설치 후 `claude -p ping`")
        return
    p = Path(cmd)
    if p.is_file():
        _ok("claude", str(p))
    else:
        _warn("claude", f"설정 경로 없음: {cmd}")


def check_ops_paths() -> None:
    """Phase 2: 논리 키 resolve 상태 (레거시/신 중 무엇이 잡히는지)."""
    from src import paths as p
    for key in ("db", "paper", "halt", "watch_hb", "inbox", "decisions", "brain_mode"):
        got = p.resolve(key)
        legacy = (ROOT / p.rel(key)).resolve()
        layout = (ROOT / p.layout_rel(key)).resolve()
        if got == layout and layout != legacy:
            tag = "layout"
        elif got == legacy:
            tag = "legacy"
        else:
            tag = "other"
        exists = "exists" if got.exists() else "missing"
        _ok(f"path:{key}", f"{tag} {exists} → {got}")
    lock = p.resolve("watch_lock")
    _ok("path:watch_lock", f"fixed → {lock}")
    legacy_lock = (ROOT / p.rel("watch_lock")).resolve()
    if legacy_lock != lock and legacy_lock.exists():
        _warn("path:watch_lock", f"레거시 락 잔재 — 쓰이지 않음, 삭제 가능: {legacy_lock}")


def _migrate_blockers() -> list[str]:
    """--apply 를 막아야 하는 조건. 컷오버는 파일 경로를 갈아엎으므로,
    장중이거나 watch 가 살아 있으면 원장/DB 를 실행 중인 프로세스 밑에서 빼게 된다."""
    blockers: list[str] = []
    from src import market_hours, paths as p
    from src.session_policy import market_tradable, trading_sessions_from_raw
    from src.engine.singleton import AlreadyRunning, SingleInstance

    try:
        from src.config import load_config
        tsess = trading_sessions_from_raw(load_config().raw)
    except Exception:
        tsess = None
    open_now = [m for m in ("KR", "US") if market_tradable(m, tsess)]
    if open_now:
        blockers.append(f"장중({', '.join(open_now)}) — 장 마감 후 재시도")

    probe = SingleInstance(p.resolve("watch_pid"), lockfile=p.resolve("watch_lock"))
    try:
        probe.acquire()
    except AlreadyRunning as e:
        blockers.append(f"watch 실행 중(pid={e.pid}) — 먼저 중지")
    except OSError as e:
        blockers.append(f"락 확인 실패({e}) — 수동 확인")
    else:
        probe.release()
    return blockers


def run_migrate(*, apply: bool) -> int:
    from src.paths_migrate import apply_moves, format_plan
    if apply:
        blockers = _migrate_blockers()
        if blockers:
            for b in blockers:
                _bad("migrate", b)
            return 1
    rows = apply_moves(root=ROOT, dry_run=not apply)
    print(format_plan(rows))
    if any(r.get("action") == "conflict" or r.get("result") == "conflict"
           for r in rows):
        _bad("migrate", "충돌 — 수동 확인 후 재시도 (docs/OPS_CUTOVER.md)")
        return 1
    if not apply:
        _ok("migrate", "dry-run 만. 적용: argus doctor --migrate-data --apply")
    else:
        _ok("migrate", "적용 완료 — doctor + watch --dry --ticks 1 로 검증")
    return 0


def check_market_state_freshness(root: Path) -> None:
    """batch_asof — fast slice 가 asof 를 덮어 fast slice 시각으로 착각하지 않게 검사."""
    from src.market_state import MarketState
    from src.agents.context import build_freshness, SLOW_SLOT_STALE_HOURS

    ms_path = root / "data" / "market_state.json"
    if not ms_path.is_file():
        _warn("market_state", "data/market_state.json 없음 — argus market-state 실행")
        return
    try:
        ms = MarketState.load(ms_path).to_dict()
    except Exception as e:
        _warn("market_state", f"읽기 실패: {e}")
        return
    fr = build_freshness(ms)
    batch = fr.get("batch_asof")
    if batch is None:
        _warn(
            "market_state",
            "batch_asof 없음 — 업그레이드 직후이거나 전량 빌드 전. "
            "argus market-state 1회 후 watch 기동",
        )
        return
    age = fr.get("batch_asof_age_sec")
    if fr.get("batch_asof_stale"):
        hrs = SLOW_SLOT_STALE_HOURS
        _warn(
            "market_state",
            f"batch_asof {int(age or 0)}s 경과(>{hrs}h) — fundamentals 등 느린 슬롯 stale. "
            "argus market-state 재실행",
        )
    else:
        _ok("market_state", f"batch_asof fresh ({int(age or 0)}s)")


def check_risk_capital(cfg) -> None:
    from src.risk_gate import capital_coverage_gaps, _normalize_capital

    risk_cfg = cfg.raw.get("risk") or {}
    broker = cfg.raw.get("broker") or {}
    live_markets = broker.get("live_markets", ["KR"])
    universe_markets = [str(m).upper() for m in (cfg.universe or {}).keys()]
    trade_markets = sorted({str(m).upper() for m in live_markets} | set(universe_markets))
    norm = _normalize_capital(risk_cfg.get("capital", {}))
    for m in capital_coverage_gaps(risk_cfg.get("capital", {}), trade_markets):
        _warn(
            "risk.capital",
            f"{m} 없음 — 일손실·DD·비중·총노출·섹터 한도 비활성",
        )
    for m in trade_markets:
        if m in norm and norm[m] <= 0:
            _warn("risk.capital", f"{m}=0 — 한도 비활성")


def check_research_boundary() -> None:
    """Phase 4: data/quant_review 잔여·research README."""
    from src import research_boundary as rb
    readme = ROOT / "research" / "README.md"
    if readme.is_file():
        _ok("research", "README.md (lab only)")
    else:
        _warn("research", "research/README.md 없음 — Phase 4 인덱스 누락")
    st = rb.residue_status(root=ROOT)
    if not st["present"]:
        _ok("research-residue", "data/quant_review 없음")
        return
    _warn(
        "research-residue",
        f"data/quant_review unused n={st['files']} — "
        "런타임 미사용. argus doctor --migrate-research [--apply]",
    )


def run_migrate_research(*, apply: bool, dest: str = "lab") -> int:
    from src.research_boundary import apply_migrate, format_plan
    rows = apply_migrate(root=ROOT, dest=dest, dry_run=not apply)
    print(format_plan(rows))
    if any(r.get("action") == "conflict" or r.get("result") == "conflict"
           for r in rows):
        _bad("migrate-research", "충돌 — 대상에 다른 내용 파일이 있음")
        return 1
    if not apply:
        _ok("migrate-research",
            "dry-run 만. 적용: argus doctor --migrate-research --apply")
    else:
        _ok("migrate-research", f"적용 완료 → {dest}")
    return 0


def _run_helper_script(name: str) -> int:
    """scripts/check_*.py main() 위임 (doctor 플래그 통합)."""
    import runpy
    path = ROOT / "scripts" / name
    ns = runpy.run_path(str(path), run_name="__argus_doctor__")
    fn = ns.get("main")
    if not callable(fn):
        _bad("doctor", f"no main() in {name}")
        return 2
    return int(fn() or 0)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="argus doctor")
    ap.add_argument("--migrate-data", action="store_true",
                    help="data/ 레이아웃 이동 계획(기본 dry-run)")
    ap.add_argument("--migrate-research", action="store_true",
                    help="data/quant_review → research/quant_review/data (기본 dry-run)")
    ap.add_argument("--apply", action="store_true",
                    help="--migrate-data/--migrate-research 와 함께: 실제 이동")
    ap.add_argument("--check-auth", action="store_true",
                    help="Anthropic API/구독 1회 연결 확인 (구 check_auth.py)")
    ap.add_argument("--check-cli", action="store_true",
                    help="claude CLI 백엔드 1회 확인 (구 check_cli.py)")
    # argv=None + pytest 등 라이브러리 호출: pytest 플래그를 먹지 않음.
    # scripts/doctor.py 또는 argus CLI(runpy)로 진입 시에만 sys.argv 사용.
    if argv is None:
        prog = Path(sys.argv[0]).name if sys.argv else ""
        if prog == "doctor.py" or prog.endswith("doctor.py"):
            argv = sys.argv[1:]
        else:
            argv = []
    args = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if args.check_auth:
        return _run_helper_script("check_auth.py")
    if args.check_cli:
        return _run_helper_script("check_cli.py")
    if args.migrate_research:
        return run_migrate_research(apply=bool(args.apply))
    if args.migrate_data:
        return run_migrate(apply=bool(args.apply))

    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    print("Argus doctor")
    blocked = False

    cfg_path = ROOT / "config.yaml"
    if not cfg_path.exists():
        _bad("config", "config.yaml 없음 - python scripts/bootstrap.py")
        return 1

    from src.config import load_config
    cfg = load_config(cfg_path)
    raw = cfg.raw
    broker = raw.get("broker") or {}
    mode = str(broker.get("mode") or "paper")
    dry = bool(cfg.dry_run)
    pyv = sys.version.split()[0]
    if sys.version_info < (3, 11):
        _bad("python", f"{pyv} - 3.11+ 필요")
        blocked = True
    else:
        _ok("python", pyv)
    _ok("config", f"mode={mode} dry_run={dry}")

    if not check_tz():
        blocked = True

    cmd = str((raw.get("agents") or {}).get("claude_command") or "claude")
    check_claude(cmd)

    try:
        check_ops_paths()
    except Exception as e:
        _warn("paths", f"resolve 점검 실패: {e}")

    try:
        check_research_boundary()
    except Exception as e:
        _warn("research", f"경계 점검 실패: {e}")

    try:
        check_market_state_freshness(ROOT)
    except Exception as e:
        _warn("market_state", f"신선도 점검 실패: {e}")

    try:
        check_risk_capital(cfg)
    except Exception as e:
        _warn("risk.capital", f"점검 실패: {e}")

    if _filled("TOSS_CLIENT_ID") and _filled("TOSS_CLIENT_SECRET"):
        _ok("toss keys", "CLIENT_ID/SECRET 있음")
        ok, msg = probe_toss(cfg)
        if ok:
            _ok("toss api", msg)
        elif mode == "live" and not dry:
            _bad("toss api", msg)
            blocked = True
        else:
            _warn("toss api", msg)
    else:
        if mode == "live" and not dry:
            _bad("toss keys", "라이브인데 TOSS_CLIENT_ID/SECRET 없음")
            blocked = True
        else:
            _warn("toss keys", "없음 - 시세·감시는 토스 키가 필요")

    if _filled("TOSS_ACCOUNT_NO"):
        _ok("account", mask_acct(os.getenv("TOSS_ACCOUNT_NO")))
    else:
        _warn("account", "TOSS_ACCOUNT_NO 비움 - 라이브 시 첫 계좌 자동조회")

    if _filled("ANTHROPIC_API_KEY"):
        _ok("brain", "ANTHROPIC_API_KEY")
    elif shutil.which("claude") or Path(cmd).is_file():
        _ok("brain", "claude CLI")
    else:
        _warn("brain", "CLI/키 없음 - watch --dry 로만 배선 확인")

    optional = [
        ("DART_API_KEY", "국내 공시"),
        ("FRED_API_KEY", "미 매크로"),
        ("FINNHUB_API_KEY", "미 뉴스·실적"),
        ("ECOS_API_KEY", "한은 매크로"),
        ("NTFY_TOPIC", "폰 푸시"),
        ("KRX_API_KEY", "KRX Open API(VKOSPI·풋콜)"),
    ]
    for key, why in optional:
        if _filled(key):
            _ok(key, why)
        else:
            _warn(key, f"없음 - {why} 배선 비활성")

    if _filled("KRX_API_KEY"):
        try:
            from src.datasources.krx_open import probe_services
            pr = probe_services()
            bits = []
            for k, lab in (("kospi", "지수"), ("vkospi", "VKOSPI"), ("opt", "옵션")):
                bits.append(f"{lab}={'OK' if pr.get(k) else 'DENIED'}")
            _ok("KRX Open API", " · ".join(bits))
        except Exception as e:
            _warn("KRX Open API", f"probe 실패: {e}")
    elif _filled("KRX_USER"):
        _ok("KRX_USER", "웹 로그인(포지셔닝 등)")

    ip = public_ip()
    if ip:
        _ok("public ip", f"{ip}  (토스 Open API 허용 IP에 이 주소를 넣는다)")
    else:
        _warn("public ip", "조회 실패 - 토스 콘솔에 이 머신 공인 IP를 직접 등록")

    want_live = mode == "live" and not dry
    if want_live:
        if blocked:
            _bad("live", "라이브 준비 안 됨")
        else:
            print("  LIVE 가능 - docs/SETUP_LIVE.md 체크리스트를 통과한 뒤에만 watch 상주")
    else:
        print("  PAPER - python scripts/watch.py --dry --ticks 1 로 스모크")

    return 1 if blocked else 0


if __name__ == "__main__":
    from src.cli.legacy import warn_legacy_script
    warn_legacy_script("argus doctor")
    sys.exit(main())
