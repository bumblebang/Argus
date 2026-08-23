"""라이브 OpenAPI 스펙을 내려받아 엔드포인트 경로를 확인한다.

  python scripts/fetch_openapi_spec.py

토스 문서가 가리키는 정식 스펙(openapi.json)을 받아 method+path 목록을 출력하고
data/openapi.json 에 저장한다. 출력 결과로 src/toss_client.py 의 ENDPOINTS 를 맞춘다.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

# 정식 스펙은 API 호스트에 있음 (developers 호스트 아님).
BASE = os.getenv("TOSS_DOCS_BASE", "https://openapi.tossinvest.com")
CANDIDATES = [
    "/openapi-docs/latest/openapi.json",
]


def main() -> int:
    spec = None
    for path in CANDIDATES:
        url = BASE + path
        try:
            r = requests.get(url, timeout=15)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("application"):
                spec = r.json()
                print(f"[ok] {url}")
                break
            print(f"[skip] {url} -> {r.status_code}")
        except Exception as e:
            print(f"[err] {url} -> {e}")
    if spec is None:
        print("스펙을 찾지 못했습니다. 토스 개발자 문서에서 정확한 openapi.json URL 을 확인하세요.")
        return 1

    out = Path("data/openapi.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n저장: {out}\n")

    print("=== 엔드포인트 목록 ===")
    for p, methods in sorted(spec.get("paths", {}).items()):
        for m in methods:
            if m.lower() in ("get", "post", "put", "delete", "patch"):
                print(f"{m.upper():6} {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
