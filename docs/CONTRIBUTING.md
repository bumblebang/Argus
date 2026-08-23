# 기여

이 저장소의 기본은 **페이퍼**다. 실주문 경로를 우회하는 패치는 받지 않는다.

## 테스트

```bash
pip install -r requirements-dev.txt
python -m pytest
```

테스트는 `config.example.yaml` 만 읽는다. 운영 `config.yaml` 을 커밋하지 않는다.

## 올리지 말 것

`.env` · `config.yaml` · `data/*.db` · `data/.token.json` · `CONTEXT.md` · `argus_loop_final.png` · 키·원장·절대경로. `git add -f` 로 data 를 올리지 않는다.

이슈·PR 에도 위 파일을 붙여넣지 마라. `doctor` 출력은 계좌가 마스킹돼 있어도 키·토픽은 넣지 않는다.

## 범위

동작 변경은 테스트와 함께. 대시보드/엔진 대형 분해는 기본 범위가 아니다.
실주문 지원 범위는 KR. US 는 시세·리서치.
