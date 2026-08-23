"""공개 페이지 발행 — 생성 → 유출 가드 → 변경 있을 때만 GitHub 로 푸시.

흐름: `public_brief`(TTL 안이면 LLM 0콜) → `public_page`(항상 LLM 0콜) →
발행 저장소로 복사 → **내용 해시 비교** → 바뀌었을 때만 커밋·푸시.

■ 커밋 1개 정책(single_commit)
    `commit --amend` + `push --force` 로 저장소를 **항상 커밋 1개**로 유지한다.
    이유: ①공유용 스냅샷이라 버전 히스토리가 필요 없고 ②혹시 유출이 새어 나가도 다음
    푸시가 덮어쓴다(히스토리에 영구 박제되는 것보다 낫다 — 다만 GitHub 이 unreachable
    커밋을 한동안 SHA 로 접근 가능하게 두므로 **완전한 삭제는 아니다**) ③저장소가 안 커진다.
    대가: 롤백 불가. `single_commit: false` 면 일반 커밋으로 쌓는다.

■ force push 안전장치 (이 스크립트는 원격 히스토리를 지운다 — 실수하면 남의 저장소가 날아간다)
    ①`repo_dir` 이 git 저장소가 아니면 중단
    ②origin 원격이 config 의 `remote` 와 정확히 일치하지 않으면 중단
    ③저장소에 우리가 만드는 파일(_ALLOWED) 외의 추적 파일이 있으면 중단
      — 엉뚱한 저장소를 가리켰을 때 그 내용을 날려버리는 걸 막는 마지막 그물이다
    ④`public_page` 가 유출 가드에 걸려 exit≠0 이면 애초에 여기까지 오지 않는다
"""
from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.logging_setup import setup_logging, get_logger  # noqa: E402

log = get_logger("publish_public")

# 발행 저장소에 있어도 되는 파일. 이 밖의 추적 파일이 있으면 '엉뚱한 저장소'로 보고 중단한다.
_ALLOWED = {"index.html", ".nojekyll", "README.md", ".gitignore"}

# 내용 비교에서 제외할 것 — 생성 시각만 바뀐 페이지로 빈 커밋을 쌓지 않기 위해.
_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?")


def content_hash(html: str) -> str:
    """생성 시각을 지운 뒤의 내용 해시. 시각만 다른 페이지는 '안 바뀐 것'으로 본다."""
    return hashlib.sha256(_TS_RE.sub("", html or "").encode("utf-8")).hexdigest()


def publish_cfg() -> dict:
    raw: dict = {}
    try:
        from src.config import load_config
        raw = ((load_config().raw.get("public_page") or {}).get("publish") or {})
    except Exception as e:
        log.debug("publish 설정 로드 실패(기본값 사용): %s", e)
    return {
        "enabled": bool(raw.get("enabled", False)),
        "repo_dir": str(raw.get("repo_dir") or ""),
        "remote": str(raw.get("remote") or ""),
        "branch": str(raw.get("branch") or "main"),
        "single_commit": bool(raw.get("single_commit", True)),
    }


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", check=check)


def preflight(repo: Path, remote: str) -> list[str]:
    """force push 전 안전 점검. 위반 사유 목록을 반환(빈 목록 = 통과)."""
    bad: list[str] = []
    if not repo.exists() or not (repo / ".git").exists():
        return [f"git 저장소가 아님: {repo}"]
    try:
        got = _git(repo, "remote", "get-url", "origin").stdout.strip()
    except subprocess.CalledProcessError:
        return ["origin 원격이 없음"]
    if remote and got.rstrip("/") != remote.rstrip("/"):
        bad.append(f"origin 불일치: 설정={remote} 실제={got}")
    tracked = {p for p in _git(repo, "ls-files").stdout.split("\n") if p.strip()}
    extra = {p for p in tracked if p not in _ALLOWED}
    if extra:
        bad.append(f"우리가 만들지 않은 추적 파일 {sorted(extra)[:5]} — 엉뚱한 저장소일 수 있음")
    return bad


def publish(html_src: Path, cfg: dict, *, dry: bool = False,
            force: bool = False) -> dict:
    """생성된 HTML 을 발행 저장소로 옮기고 변경이 있을 때만 커밋·푸시."""
    repo = Path(cfg["repo_dir"]) if cfg["repo_dir"] else None
    if repo is None:
        return {"status": "skipped", "reason": "repo_dir 미설정"}
    bad = preflight(repo, cfg["remote"])
    if bad:
        log.error("발행 사전점검 실패 — 푸시하지 않습니다: %s", " | ".join(bad))
        return {"status": "blocked", "reasons": bad}

    new = html_src.read_text(encoding="utf-8")
    dst = repo / "index.html"
    old = dst.read_text(encoding="utf-8") if dst.exists() else ""
    changed = force or content_hash(new) != content_hash(old)
    if not changed:
        log.info("내용 변경 없음 — 푸시 생략(생성 시각만 다름).")
        return {"status": "unchanged"}
    if dry:
        log.info("[dry] 변경 감지 — 실제 커밋·푸시는 하지 않습니다.")
        return {"status": "dry", "changed": True}

    shutil.copyfile(html_src, dst)
    (repo / ".nojekyll").write_text("", encoding="utf-8")   # Pages 의 Jekyll 처리 끄기
    _git(repo, "add", "index.html", ".nojekyll")
    if not _git(repo, "status", "--porcelain").stdout.strip():
        log.info("스테이지에 변경 없음 — 푸시 생략.")
        return {"status": "unchanged"}

    branch, msg = cfg["branch"], "Argus 공개 현황 스냅샷"
    has_commit = _git(repo, "rev-parse", "--verify", "HEAD",
                      check=False).returncode == 0
    if cfg["single_commit"] and has_commit:
        # 커밋 1개 유지: 직전 커밋을 통째로 갈아끼운다(히스토리를 쌓지 않는다).
        _git(repo, "commit", "--amend", "-m", msg)
        push = _git(repo, "push", "--force", "origin", f"HEAD:{branch}", check=False)
    else:
        _git(repo, "commit", "-m", msg)
        flag = ["--force"] if cfg["single_commit"] else []
        push = _git(repo, "push", *flag, "origin", f"HEAD:{branch}", check=False)
    if push.returncode != 0:
        log.error("푸시 실패(rc=%s): %s", push.returncode,
                  (push.stderr or "").strip()[:300])
        return {"status": "push_failed", "stderr": (push.stderr or "").strip()[:300]}
    log.info("발행 완료 — %s (%s)", cfg["remote"] or "origin", branch)
    return {"status": "published"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="생성까지만, 커밋·푸시 안 함")
    ap.add_argument("--force", action="store_true", help="변경이 없어도 푸시")
    ap.add_argument("--skip-brief", action="store_true", help="브리핑 갱신 건너뜀")
    args = ap.parse_args()
    setup_logging("INFO")

    cfg = publish_cfg()
    if not cfg["enabled"]:
        log.info("public_page.publish.enabled=false — 발행하지 않습니다.")
        return 0

    import scripts.public_page as page
    if not args.skip_brief:
        try:
            from scripts.public_brief import refresh_brief
            log.info("브리핑: %s", refresh_brief().get("status"))
        except Exception as e:      # 브리핑 실패가 발행을 막지 않게(섹션만 빠진다)
            log.warning("브리핑 갱신 실패(계속): %s", e)

    rc = page.main([])              # 유출 가드 위반이면 여기서 2를 준다
    if rc != 0:
        log.error("페이지 생성 실패(rc=%s) — 발행 중단.", rc)
        return rc

    out = Path(page.public_cfg()["out"])
    if not out.is_absolute():
        out = ROOT / out
    res = publish(out, cfg, dry=args.dry, force=args.force)
    log.info("발행 결과: %s", res)
    return 0 if res.get("status") in ("published", "unchanged", "dry", "skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
