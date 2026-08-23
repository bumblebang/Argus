"""기동 전 점검: 설정·키·뇌·타임존·공인 IP. 네트워크 호출은 토스/IP만(있을 때).

종료코드: 0=기동 가능(페이퍼 또는 라이브 준비), 1=막힘.
"""
from __future__ import annotations

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


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
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
    sys.exit(main())
