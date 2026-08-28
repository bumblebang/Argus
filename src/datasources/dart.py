"""DART(전자공시) 국내 재무 소스.

종목코드 -> 고유번호(corp_code) 매핑(corpCode.zip 1회 캐시) 후
단일회사 주요계정(fnlttSinglAcnt)에서 최근 사업보고서의 매출액·당기순이익을 가져온다.
연결재무제표(CFS) 우선. 토스 API 와 무관(쿨다운 영향 없음).
"""
from __future__ import annotations

import io
import json
import re
import time
import zipfile
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

from .base import DataSource, SourceContext
from ..logging_setup import get_logger

log = get_logger("src.dart")

CORP_URL = "https://opendart.fss.or.kr/api/corpCode.xml"
FNLTT_URL = "https://opendart.fss.or.kr/api/fnlttSinglAcnt.json"
LIST_URL = "https://opendart.fss.or.kr/api/list.json"
DOCUMENT_URL = "https://opendart.fss.or.kr/api/document.xml"


def fetch_recent_disclosures(api_key: str, *, date_yyyymmdd: str | None = None,
                             count: int = 100, timeout: int = 10) -> list[dict]:
    """오늘(또는 지정일) 전 상장사 최신 공시 목록 — 공시 워처의 감시층(1콜).

    corp_code 필터 없이 시장 전체를 접수시각 내림차순으로 가져온다(page 1 = 최신 count 건).
    반환: [{rcept_no, stock_code, corp_name, report_nm, rcept_dt}] (비상장=stock_code 빈값 포함).
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    day = date_yyyymmdd or datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
    r = requests.get(LIST_URL, params={
        "crtfc_key": api_key, "bgn_de": day, "end_de": day,
        "page_no": 1, "page_count": count,
    }, timeout=timeout)
    try:
        r.raise_for_status()
    except requests.HTTPError:
        from ..http_sanitize import response_error_brief
        raise requests.HTTPError(response_error_brief(r), response=r) from None
    body = r.json()
    if body.get("status") != "000":          # '013'=조회 결과 없음(휴일 등) — 정상 취급
        if body.get("status") != "013":
            log.warning("DART list 응답 이상: %s %s", body.get("status"), body.get("message"))
        return []
    return [{"rcept_no": it.get("rcept_no"), "stock_code": (it.get("stock_code") or "").strip(),
             "corp_name": it.get("corp_name"), "report_nm": (it.get("report_nm") or "").strip(),
             "rcept_dt": it.get("rcept_dt")} for it in body.get("list", [])]


def _to_num(s) -> float | None:
    try:
        return float(str(s).replace(",", ""))
    except (ValueError, AttributeError):
        return None


# 재무상태표(BS) 정확 계정명 → 반환 키. 유동자산/유동부채는 부분매칭이면 '비유동자산/
# 비유동부채'에 오탐하므로 정확 계정명으로만 잡는다(자산/부채/자본 총계도 정확명).
_BS_ACCOUNTS = {
    "자산총계": "total_assets", "부채총계": "total_liabilities",
    "유동자산": "current_assets", "유동부채": "current_liabilities",
    "자본총계": "equity",
}
# fetch_financials 가 채우는 계정 키(BS+IS). fiscal_year 는 별도.
_FIN_ACCOUNTS = ("revenue", "operating_income", "net_income", "equity",
                 "total_assets", "total_liabilities",
                 "current_assets", "current_liabilities")


def fetch_financials(api_key: str, corp_code: str, year: int, timeout: int = 20) -> dict | None:
    """DART 사업보고서에서 BS+IS 주요 계정(연결 우선). 하나도 없으면 None.

    반환 {fiscal_year, revenue, operating_income, net_income, equity, total_assets,
    total_liabilities, current_assets, current_liabilities}. 손익계산서(IS/CIS)에서
    매출·영업이익·당기순이익을, 재무상태표(BS)에서 자산/부채/자본 총계·유동자산/유동부채를
    뽑는다 — 각 값은 연결(CFS)을 별도(OFS)보다 우선(DART 응답이 CFS 행을 먼저 내려주므로
    '첫 매칭 + CFS 는 OFS 를 덮는다'로 연결 우선을 구현). IS 는 부분매칭('매출'/'영업이익'/
    '당기순이익' in nm — '영업이익(손실)' 등 수용), BS 는 정확 계정명(비유동 오탐 방지).
    fnlttSinglAcnt 응답에는 현금흐름표(CF)가 없어 CF 계정은 다루지 않는다.
    """
    r = requests.get(FNLTT_URL, params={
        "crtfc_key": api_key, "corp_code": corp_code,
        "bsns_year": str(year), "reprt_code": "11011",  # 사업보고서(연간)
    }, timeout=timeout)
    if r.status_code != 200:
        return None
    body = r.json()
    if body.get("status") != "000":          # '013'=없음 등 → None(폴백은 호출측 몫)
        return None
    vals: dict[str, float | None] = {k: None for k in _FIN_ACCOUNTS}
    from_cfs: dict[str, bool] = {k: False for k in _FIN_ACCOUNTS}  # 현재 값이 연결(CFS)에서 왔는지
    for row in body.get("list", []):
        sj = row.get("sj_div")
        cfs = row.get("fs_div") == "CFS"
        nm = (row.get("account_nm") or "").strip()
        key = None
        if sj in ("IS", "CIS"):                # 손익계산서 계열 — 부분매칭
            if "매출" in nm:
                key = "revenue"
            elif "영업이익" in nm:
                key = "operating_income"
            elif "당기순이익" in nm:
                key = "net_income"
        elif sj == "BS":                       # 재무상태표 — 정확 계정명
            key = _BS_ACCOUNTS.get(nm)
        if key and (vals[key] is None or (cfs and not from_cfs[key])):
            vals[key], from_cfs[key] = _to_num(row.get("thstrm_amount")), cfs
    if all(v is None for v in vals.values()):
        return None
    return {"fiscal_year": year, **vals}


def load_corp_map(api_key: str, cache: str | Path = "data/dart_corpcode.json") -> dict[str, str]:
    """상장 종목코드(6자리) -> DART 고유번호(8자리). corpCode.zip 1회 캐시."""
    import json
    cache = Path(cache)
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    r = requests.get(CORP_URL, params={"crtfc_key": api_key}, timeout=30)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        xml = z.read(z.namelist()[0])
    out: dict[str, str] = {}
    for item in ET.fromstring(xml).iter("list"):
        stock = (item.findtext("stock_code") or "").strip()
        corp = (item.findtext("corp_code") or "").strip()
        if stock and corp:
            out[stock] = corp
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out), encoding="utf-8")
    log.info("DART 고유번호 맵 %d개 캐시", len(out))
    return out


# ── 잠정실적 document.xml (ZIP→XML/HTML TABLE) ───────────────────────
_ACTUAL_LABELS = (
    ("매출액", "revenue"),
    ("영업수익", "revenue"),
    ("영업이익", "op_profit"),
    ("당기순이익", "net_income"),
    ("분기순이익", "net_income"),
)
# 013=조회없음·014=파일없음 — 공시 직후 document 인덱스가 늦을 때. 재시도 대상.
_RETRYABLE_DART_STATUS = {"013", "014"}
_SKIP_AMOUNT_TOKENS = ("당해실적", "당기실적", "누계실적", "구분",
                       "적자전환", "흑자전환", "흑자적자전환여부")


def _empty_actuals(rcept_no: str | None = None, *, retryable: bool = False) -> dict:
    out = {"revenue": None, "op_profit": None, "net_income": None,
           "unit": "unknown", "scope": "unknown", "parse_ok": False,
           "retryable": bool(retryable)}
    if rcept_no:
        out["rcept_no"] = rcept_no
    return out


def _clean_amount(s: str) -> float | None:
    """표 셀 텍스트 → float. 괄호 음수·콤마·단위 접미사 제거. 증감율(%) 는 버림."""
    if not s:
        return None
    t = str(s).strip().replace(",", "").replace(" ", "").replace("\xa0", "")
    t = t.replace("\u2212", "-").replace("\uff0d", "-")
    if t.startswith("△"):
        t = "-" + t[1:]
    if not t or t in ("-", "—", "N/A", "△"):
        return None
    if t.endswith("%"):
        return None
    neg = False
    if t.startswith("(") and t.endswith(")"):
        neg, t = True, t[1:-1]
    for suf in ("백만원", "억원", "천원", "원"):
        if t.endswith(suf):
            t = t[: -len(suf)]
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def _decode_doc_bytes(raw: bytes) -> str:
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    for enc in ("utf-8", "euc-kr", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _strip_markup(text: str) -> str:
    """HTML/XML 태그를 공백으로 지워 표 텍스트만 남긴다."""
    t = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", text)
    t = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", t)
    t = re.sub(r"(?i)<br\s*/?>", "\n", t)
    t = re.sub(r"(?i)</tr>", "\n", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = t.replace("&nbsp;", " ").replace("&#160;", " ")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{2,}", "\n", t)
    return t


def _iter_xml_texts(root: ET.Element):
    for el in root.iter():
        if el.text and el.text.strip():
            yield el.text.strip()
        if el.tail and el.tail.strip():
            yield el.tail.strip()


def parse_earnings_document_xml(xml_bytes: bytes) -> dict:
    """잠정실적 XML/HTML → {revenue, op_profit, net_income, unit, scope, parse_ok}.

    공정공시는 ZIP 안에 euc-kr/utf-8 HTML 표를 넣는 경우가 많다. 서식 편차가 커서
    실패해도 예외를 올리지 않는다 — parse_ok=False + 빈 값.
    """
    empty = _empty_actuals(retryable=False)
    text = _decode_doc_bytes(xml_bytes)
    plain = _strip_markup(text)
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return _parse_earnings_from_text(plain, base=empty)

    blob = " ".join(_iter_xml_texts(root))
    for table in root.iter():
        tag = (table.tag or "").upper()
        if not (tag.endswith("TABLE") or "TABLE" in tag):
            continue
        for tr in table.iter():
            if not (tr.tag or "").upper().endswith("TR"):
                continue
            cells = []
            for td in tr.iter():
                if (td.tag or "").upper().endswith(("TD", "TH")):
                    cells.append("".join(td.itertext()).strip())
            if cells:
                blob += " " + " ".join(cells)
                _fill_from_labeled_cells(cells, empty)

    out = _parse_earnings_from_text(blob + "\n" + plain, base=empty)
    return out


def _fill_from_labeled_cells(cells: list[str], out: dict) -> None:
    if len(cells) < 2:
        return
    label = cells[0]
    for needle, key in _ACTUAL_LABELS:
        if needle in label and out.get(key) is None:
            # 당기는 보통 라벨 다음 첫 숫자(당해실적). 마지막 숫자는 전년/증감율.
            for cell in cells[1:]:
                if cell in _SKIP_AMOUNT_TOKENS:
                    continue
                v = _clean_amount(cell)
                if v is not None:
                    out[key] = v
                    break


def _parse_earnings_from_text(text: str, base: dict | None = None) -> dict:
    out = dict(base or _empty_actuals())
    if "연결" in text:
        out["scope"] = "consolidated"
    elif "별도" in text:
        out["scope"] = "separate"
    if "백만원" in text:
        out["unit"] = "백만원"
    elif "억원" in text:
        out["unit"] = "억원"
    elif "원" in text:
        out["unit"] = "원"

    for needle, key in _ACTUAL_LABELS:
        if out.get(key) is not None:
            continue
        # 공정공시: "매출액 당해실적 2,139,622 …" / 단순표: "매출액 1,234"
        m = re.search(
            rf"{needle}\s+(?:당해실적|당기실적)?\s*[:：]?\s*([\(\)\d,.\-]+)",
            text)
        if m:
            v = _clean_amount(m.group(1))
            if v is not None:
                out[key] = v

    out["parse_ok"] = any(out.get(k) is not None
                          for k in ("revenue", "op_profit", "net_income"))
    if out["parse_ok"]:
        out["retryable"] = False
    return out


def surprise_vs_consensus(actuals: dict, consensus: dict | None) -> dict:
    """실제 vs 네이버 컨센서스 → surprise_pct. 컨센서스 단위(억원)와 actuals.unit 맞춤.

    단위가 다르면 환산 시도(백만원→억원 /100, 원→억원 /1e8). 불확실하면 해당 키 생략.
    """
    if not actuals or not consensus or not actuals.get("parse_ok"):
        return {}
    unit = actuals.get("unit") or "unknown"

    def _to_eok(v, u):
        if v is None:
            return None
        if u == "억원":
            return float(v)
        if u == "백만원":
            return float(v) / 100.0
        if u == "원":
            return float(v) / 1e8
        return None  # unknown — 서프라이즈 계산 안 함(오해 방지)

    out: dict = {}
    mapping = [("revenue", "revenue"), ("op_profit", "op_profit"),
               ("net_income", "net_income")]
    for akey, ckey in mapping:
        act = _to_eok(actuals.get(akey), unit)
        est = consensus.get(ckey)
        if act is None or est is None:
            continue
        try:
            if float(est) == 0:
                continue
            out[f"{akey}_surprise_pct"] = round(
                (float(act) - float(est)) / abs(float(est)) * 100, 2)
        except (TypeError, ValueError):
            continue
    return out


def _parse_opendart_error(content: bytes) -> dict | None:
    """ZIP이 아닌 응답에서 DART status/message 를 뽑는다. 해당 없으면 None."""
    if not content:
        return None
    if content[:1] == b"{":
        try:
            body = json.loads(content.decode("utf-8", errors="replace"))
        except ValueError:
            return None
        st, msg = str(body.get("status") or ""), body.get("message") or ""
        if st:
            return {"status": st, "message": str(msg)}
        return None
    if content[:2] == b"PK":
        return None
    text = _decode_doc_bytes(content[:4000])
    m = re.search(r"<status>\s*(\d+)\s*</status>", text, re.I)
    if not m:
        return None
    mm = re.search(r"<message>\s*([^<]+)</message>", text, re.I)
    return {"status": m.group(1), "message": (mm.group(1).strip() if mm else "")}


def fetch_earnings_actuals(api_key: str, rcept_no: str, *,
                           timeout: int = 30) -> dict:
    """DART document.xml(ZIP) → 잠정실적 수치. 실패 시 parse_ok=False (예외 안 던짐).

    공시 직후엔 파일이 아직 없어 XML 에러(013/014)가 온다 — retryable=True.
    ZIP 안의 공정공시는 HTML 표인 경우가 많다.
    """
    empty = _empty_actuals(rcept_no, retryable=False)
    if not api_key or not rcept_no:
        return empty
    try:
        r = requests.get(DOCUMENT_URL, params={
            "crtfc_key": api_key, "rcept_no": rcept_no,
        }, timeout=timeout)
        r.raise_for_status()
        err = _parse_opendart_error(r.content)
        if err:
            retryable = err["status"] in _RETRYABLE_DART_STATUS
            log.warning("DART document 에러(%s) status=%s %s retryable=%s",
                        rcept_no, err["status"], err.get("message"), retryable)
            out = _empty_actuals(rcept_no, retryable=retryable)
            out["dart_status"] = err["status"]
            return out
        if r.content[:2] != b"PK":
            preview = _decode_doc_bytes(r.content[:120]).replace("\n", " ")
            log.warning("DART document 비ZIP(%s): %s", rcept_no, preview[:80])
            return _empty_actuals(rcept_no, retryable=True)
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            names = sorted(z.namelist(),
                           key=lambda n: z.getinfo(n).file_size, reverse=True)
            best = empty
            for name in names:
                if not name.lower().endswith((".xml", ".html", ".htm")):
                    continue
                raw = z.read(name)
                parsed = parse_earnings_document_xml(raw)
                parsed["rcept_no"] = rcept_no
                if parsed.get("parse_ok"):
                    return parsed
                best = parsed
            return best
    except zipfile.BadZipFile as e:
        log.warning("DART document 비ZIP(%s): %s", rcept_no, e)
        return _empty_actuals(rcept_no, retryable=True)
    except Exception as e:
        log.warning("DART document 파싱 실패(%s): %s", rcept_no, e)
        return _empty_actuals(rcept_no, retryable=True)


class DartSource(DataSource):
    name = "dart"

    def __init__(self, api_key: str, symbols: list[str], year: int | None = None,
                 cache_dir: str | Path = "data", spacing_sec: float = 0.2):
        self.api_key = api_key
        self.symbols = symbols
        self.year = year or (date.today().year - 1)  # 최근 사업보고서(전년도)
        self.cache = Path(cache_dir) / "dart_corpcode.json"
        self.spacing = spacing_sec

    def _corp_map(self) -> dict[str, str]:
        return load_corp_map(self.api_key, self.cache)

    def _financials(self, corp: str, year: int) -> dict | None:
        r = requests.get(FNLTT_URL, params={
            "crtfc_key": self.api_key, "corp_code": corp,
            "bsns_year": str(year), "reprt_code": "11011",  # 사업보고서(연간)
        }, timeout=20)
        if r.status_code != 200:
            return None
        body = r.json()
        if body.get("status") != "000":
            return None
        revenue = net_income = None
        for row in body.get("list", []):
            if row.get("sj_div") not in ("IS", "CIS"):  # 손익계산서 계열
                continue
            if row.get("fs_div") != "CFS" and revenue is not None:
                continue  # 연결 우선, 이미 연결로 채웠으면 별도는 무시
            nm = row.get("account_nm", "")
            amt = _to_num(row.get("thstrm_amount"))
            if "매출" in nm and revenue is None:
                revenue = amt
            elif "당기순이익" in nm and net_income is None:
                net_income = amt
        if revenue is None and net_income is None:
            return None
        margin = (net_income / revenue) if (revenue and net_income is not None) else None
        return {"fiscal_year": year, "revenue": revenue, "net_income": net_income,
                "net_margin": round(margin, 4) if margin is not None else None}

    def fetch(self, ctx: SourceContext) -> dict:
        if ctx.dry:
            return {"fundamentals": {s: {"fiscal_year": self.year, "revenue": 1.0e12,
                                         "net_income": 1.0e11, "net_margin": 0.1}
                                     for s in self.symbols}}
        try:
            cmap = self._corp_map()
        except Exception as e:
            log.warning("DART 고유번호 맵 실패: %s", e)
            return {"fundamentals": {}}
        out: dict[str, dict] = {}
        for i, sym in enumerate(self.symbols):
            corp = cmap.get(sym)
            if not corp:
                continue
            try:
                fin = self._financials(corp, self.year) or self._financials(corp, self.year - 1)
                if fin:
                    out[sym] = fin
                    log.info("[%s] FY%s 매출 %.3g 순이익 %.3g 순이익률 %s", sym,
                             fin["fiscal_year"], fin["revenue"] or 0, fin["net_income"] or 0,
                             fin["net_margin"])
            except Exception as e:
                log.warning("[%s] DART 조회 실패: %s", sym, e)
            if self.spacing and i < len(self.symbols) - 1:
                time.sleep(self.spacing)
        return {"fundamentals": out}
