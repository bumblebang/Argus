"""공개 페이지 발행 — force push 안전장치와 '변경 있을 때만 푸시' 규칙.

이 스크립트는 **원격 히스토리를 지운다**(push --force). 설정을 잘못 가리키면 남의
저장소가 날아가므로, 사전점검이 실제로 막는지를 고정하는 게 이 파일의 목적이다.
네트워크·실제 GitHub 접근 없음 — 로컬 git 저장소만 만들어 검증한다.
"""
import subprocess

import pytest

from scripts.publish_public import _ALLOWED, content_hash, preflight, publish

REMOTE = "https://github.com/someone/argus-public.git"


def _git(repo, *a):
    subprocess.run(["git", "-C", str(repo), *a], capture_output=True, check=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "argus-public"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "user.email", "t@t")
    _git(r, "remote", "add", "origin", REMOTE)
    (r / "index.html").write_text("<html>old</html>", encoding="utf-8")
    _git(r, "add", "index.html")
    _git(r, "commit", "-qm", "init")
    return r


def _cfg(repo, **kw):
    base = {"enabled": True, "repo_dir": str(repo), "remote": REMOTE,
            "branch": "main", "single_commit": True}
    base.update(kw)
    return base


# ── 내용 해시: 생성 시각만 다르면 '안 바뀐 것' ────────────────────────
def test_hash_ignores_timestamps():
    a = "<p>공포 17.2</p><div>2026-08-01 12:30 생성</div>"
    b = "<p>공포 17.2</p><div>2026-08-01 13:30 생성</div>"
    assert content_hash(a) == content_hash(b)


def test_hash_detects_real_change():
    a = "<p>공포 17.2</p><div>2026-08-01 12:30 생성</div>"
    b = "<p>공포 41.0</p><div>2026-08-01 12:30 생성</div>"
    assert content_hash(a) != content_hash(b)


# ── 사전점검(force push 안전장치) ───────────────────────────────────
def test_preflight_passes_on_clean_repo(repo):
    assert preflight(repo, REMOTE) == []


def test_preflight_blocks_non_repo(tmp_path):
    assert preflight(tmp_path / "없음", REMOTE)


def test_preflight_blocks_remote_mismatch(repo):
    bad = preflight(repo, "https://github.com/other/other.git")
    assert bad and "origin 불일치" in bad[0]


def test_preflight_blocks_foreign_files(repo):
    # 우리가 만들지 않은 파일이 추적 중이면 = 엉뚱한 저장소. 날려버리면 안 된다.
    (repo / "src.py").write_text("print()", encoding="utf-8")
    _git(repo, "add", "src.py")
    _git(repo, "commit", "-qm", "x")
    bad = preflight(repo, REMOTE)
    assert bad and "추적 파일" in bad[0]


def test_allowed_set_is_minimal():
    assert _ALLOWED == {"index.html", ".nojekyll", "README.md", ".gitignore"}


# ── publish 동작 ───────────────────────────────────────────────────
def test_publish_skips_when_unchanged(repo, tmp_path):
    src = tmp_path / "index.html"
    src.write_text("<html>old</html>", encoding="utf-8")
    assert publish(src, _cfg(repo))["status"] == "unchanged"


def test_publish_blocked_does_not_touch_repo(repo, tmp_path):
    src = tmp_path / "index.html"
    src.write_text("<html>새 내용</html>", encoding="utf-8")
    res = publish(src, _cfg(repo, remote="https://github.com/other/x.git"))
    assert res["status"] == "blocked"
    # 원본이 그대로여야 한다 — 사전점검 실패 시 파일도 안 건드린다
    assert (repo / "index.html").read_text(encoding="utf-8") == "<html>old</html>"


def test_publish_dry_does_not_commit(repo, tmp_path):
    src = tmp_path / "index.html"
    src.write_text("<html>새 내용</html>", encoding="utf-8")
    before = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                            capture_output=True, text=True).stdout
    assert publish(src, _cfg(repo), dry=True)["status"] == "dry"
    after = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                           capture_output=True, text=True).stdout
    assert before == after
    assert (repo / "index.html").read_text(encoding="utf-8") == "<html>old</html>"


def test_publish_amends_to_single_commit(repo, tmp_path):
    """커밋 1개 정책: 발행해도 커밋 수가 늘지 않는다(푸시는 원격이 없어 실패해도 무방)."""
    src = tmp_path / "index.html"
    src.write_text("<html>새 내용</html>", encoding="utf-8")
    res = publish(src, _cfg(repo))
    assert res["status"] == "push_failed"          # 가짜 원격이라 푸시만 실패
    n = subprocess.run(["git", "-C", str(repo), "rev-list", "--count", "HEAD"],
                       capture_output=True, text=True).stdout.strip()
    assert n == "1"                                 # amend 라 여전히 1개
    assert (repo / "index.html").read_text(encoding="utf-8") == "<html>새 내용</html>"
    assert (repo / ".nojekyll").exists()


def test_publish_skips_without_repo_dir(tmp_path):
    src = tmp_path / "index.html"
    src.write_text("<html>x</html>", encoding="utf-8")
    assert publish(src, _cfg(tmp_path, repo_dir=""))["status"] == "skipped"
