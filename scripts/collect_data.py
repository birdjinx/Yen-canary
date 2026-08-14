#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
yen-canary : 엔캐리 트레이드 청산 조기경보 모니터
데이터 수집 스크립트

수집 경로
  - FRED API   : 미/일 국채금리, 크레딧 스프레드 (무료 API 키 필요)
  - Yahoo Fin. : USD/JPY 환율, VIX, MOVE (키 불필요)

출력
  - docs/data.json  : 대시보드(docs/index.html)가 직접 fetch

환경변수
  FRED_API_KEY      : GitHub Actions Secrets 에 등록 (필수)
  ANTHROPIC_API_KEY : Claude 분석 코멘트용 (선택 — 없으면 코멘트만 생략)
"""

import os
import json
import time
import datetime as dt
from urllib.parse import urlencode

import requests

FRED_KEY = os.environ.get("FRED_API_KEY", "").strip()
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
# 저비용 고정: Haiku 4.5 ($1/$5 per MTok). 회당 약 0.4센트.
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
UA = {"User-Agent": "Mozilla/5.0 (yen-canary monitor)"}
TIMEOUT = 20

# ----------------------------------------------------------------------
# 공통 유틸
# ----------------------------------------------------------------------
def _retry(fn, *args, tries=3, wait=2, **kwargs):
    """네트워크 호출을 몇 번 재시도. 실패하면 None."""
    for i in range(tries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa
            print(f"   ! attempt {i+1}/{tries} failed: {e}")
            time.sleep(wait)
    return None


# ----------------------------------------------------------------------
# 수동 입력값  (docs/manual.json)  — FRED 근사치보다 우선 적용
# ----------------------------------------------------------------------
def load_manual():
    """
    docs/manual.json 에서 수동 지정값을 읽는다. 없으면 빈 dict.
    형식 예:
      { "jp10y": {"value": 1.62, "asof": "2026-08-14"} }
    """
    path = os.path.join("docs", "manual.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:  # noqa
        print(f"   ! manual.json 읽기 실패(무시): {e}")
        return {}


# ----------------------------------------------------------------------
# FRED
# ----------------------------------------------------------------------
FRED_SERIES = {
    # 미국
    "US10Y": "DGS10",        # 미 국채 10년
    "US2Y":  "DGS2",         # 미 국채 2년
    # 크레딧 스프레드 (미국)
    "HY_OAS":  "BAMLH0A0HYM2",   # ICE BofA US High Yield OAS (%)
    "IG_OAS":  "BAMLC0A0CM",     # ICE BofA US Corporate OAS (%)
    # 단기 자금시장 스트레스
    "SOFR":  "SOFR",         # SOFR
    # 일본
    "JP10Y": "IRLTLT01JPM156N",  # 일본 장기국채금리(월간, OECD) - 폴백용
}


def fred_latest(series_id):
    """FRED 시계열의 최신 유효값 1개 반환 (value, date)."""
    if not FRED_KEY:
        return None, None
    base = "https://api.stlouisfed.org/fred/series/observations"
    q = {
        "series_id": series_id,
        "api_key": FRED_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 10,
    }
    url = base + "?" + urlencode(q)

    def _call():
        r = requests.get(url, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()

    data = _retry(_call)
    if not data:
        return None, None
    for obs in data.get("observations", []):
        v = obs.get("value", ".")
        if v not in (".", "", None):
            try:
                return float(v), obs.get("date")
            except ValueError:
                continue
    return None, None


def fred_history(series_id, limit=800):
    """FRED 시계열의 최근 관측 다수를 (date, value) 리스트로. 최신순."""
    if not FRED_KEY:
        return []
    base = "https://api.stlouisfed.org/fred/series/observations"
    q = {"series_id": series_id, "api_key": FRED_KEY, "file_type": "json",
         "sort_order": "desc", "limit": limit}
    url = base + "?" + urlencode(q)

    def _call():
        r = requests.get(url, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()

    data = _retry(_call)
    out = []
    if not data:
        return out
    for obs in data.get("observations", []):
        v = obs.get("value", ".")
        if v not in (".", "", None):
            try:
                out.append((obs.get("date"), float(v)))
            except ValueError:
                continue
    return out


# ----------------------------------------------------------------------
# CFTC  (Traders in Financial Futures, Combined) — 키 불필요 (Socrata)
#   엔 선물의 레버리지펀드(헤지펀드) 순숏 = 캐리 롱 '연료량'
# ----------------------------------------------------------------------
CFTC_TFF_URL = "https://publicreporting.cftc.gov/resource/yw9f-hn96.json"


def cftc_yen_positioning(weeks=160):
    """
    엔 선물 레버리지펀드 순포지션을 최근 weeks주 가져와,
    최신 순숏과 3년 백분위(청산 연료의 극단성)를 계산.
    반환: dict 또는 None.
    """
    params = {
        "$where": "market_and_exchange_names like '%JAPANESE YEN%'",
        "$select": ("report_date_as_yyyy_mm_dd,lev_money_positions_long,"
                    "lev_money_positions_short,open_interest_all"),
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": weeks,
    }
    url = CFTC_TFF_URL + "?" + urlencode(params)

    def _call():
        r = requests.get(url, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()

    rows = _retry(_call, tries=3)
    if not rows:
        print("   ! CFTC 응답 없음 — 지표 생략")
        return None

    # net = long - short  (음수일수록 순숏 = 캐리 롱)
    nets = []
    latest = None
    for row in rows:
        try:
            lng = float(row.get("lev_money_positions_long", "nan"))
            sht = float(row.get("lev_money_positions_short", "nan"))
        except (TypeError, ValueError):
            continue
        if lng != lng or sht != sht:  # nan 체크
            continue
        net = lng - sht
        date = row.get("report_date_as_yyyy_mm_dd", "")[:10]
        nets.append(net)
        if latest is None:
            latest = {"date": date, "net": net, "long": lng, "short": sht}

    if latest is None or not nets:
        return None

    # 순숏 규모(양수화) = -net (net이 음수면 숏 우위)
    net_short = -latest["net"]

    # 3년(≈156주) 순숏 백분위: 과거 대비 지금 숏이 얼마나 극단적인가
    short_vals = [-n for n in nets]           # 순숏 크기들
    below = sum(1 for x in short_vals if x <= net_short)
    pctile = round(below / len(short_vals) * 100, 1) if short_vals else None

    print(f"CFTC 엔 net={latest['net']:.0f} net_short={net_short:.0f} "
          f"pctile={pctile} (n={len(nets)}주)")
    return {
        "date": latest["date"],
        "net": latest["net"],
        "net_short": net_short,
        "pctile": pctile,
        "n_weeks": len(nets),
    }


# ----------------------------------------------------------------------
# 달러 조달 스트레스 프록시  (진짜 XCCY basis는 무료 소스 없음)
#   SOFR - 3M T-bill 스프레드로 단기 달러자금 경색을 근사.
#   값이 커질수록(또는 급변할수록) 달러 조달 압력 상승 신호.
# ----------------------------------------------------------------------
def funding_stress_proxy():
    """
    SOFR 와 3개월 T-bill(DTB3) 격차를 달러 조달 스트레스 프록시로.
    반환: (spread_bp, asof) 또는 (None, None).
    주: 진짜 크로스커런시 베이시스가 아니라 근사 프록시임을 대시보드에 명시.
    """
    sofr, sofr_d = fred_latest("SOFR")
    tb3, _ = fred_latest("DTB3")
    if sofr is None or tb3 is None:
        return None, None
    spread_bp = round((sofr - tb3) * 100, 1)  # %p → bp
    return spread_bp, sofr_d


# ----------------------------------------------------------------------
# Yahoo Finance  (키 불필요)
# ----------------------------------------------------------------------
def yahoo_series(symbol, rng="1mo", interval="1d"):
    """
    Yahoo Finance chart API.
    반환: (closes[list], last_close, prev_close_3d)
    query1 이 막히면 query2 로 폴백.
    """
    hosts = [
        "https://query1.finance.yahoo.com",
        "https://query2.finance.yahoo.com",
    ]
    params = {"range": rng, "interval": interval}

    for host in hosts:
        url = f"{host}/v8/finance/chart/{symbol}?" + urlencode(params)

        def _call():
            r = requests.get(url, headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()

        data = _retry(_call, tries=2)
        if not data:
            continue
        try:
            res = data["chart"]["result"][0]
            closes = res["indicators"]["quote"][0]["close"]
            closes = [c for c in closes if c is not None]
            if closes:
                return closes
        except (KeyError, IndexError, TypeError):
            continue
    return []


def yahoo_last(symbol, rng="1mo"):
    closes = yahoo_series(symbol, rng=rng)
    return closes[-1] if closes else None


# ----------------------------------------------------------------------
# 지표 계산
# ----------------------------------------------------------------------
def pct_change_ndays(closes, n=3):
    """최근 n거래일 누적 변화율(%). 데이터 부족 시 None."""
    if not closes or len(closes) <= n:
        return None
    old = closes[-1 - n]
    new = closes[-1]
    if old in (0, None) or new is None:
        return None
    return (new - old) / old * 100.0


def collect():
    print("=== yen-canary : collecting ===")
    now = dt.datetime.utcnow().replace(microsecond=0)

    metrics = {}
    manual = load_manual()

    # --- 1. 조달비용: 미일 금리차 ------------------------------------
    us2y, us2y_d = fred_latest(FRED_SERIES["US2Y"])
    us10y, _ = fred_latest(FRED_SERIES["US10Y"])
    print(f"US2Y={us2y} US10Y={us10y}")

    # 일본 10Y: 수동값(manual.json) 우선, 없으면 FRED 월간(OECD) 근사.
    jp10y_fred, jp10y_fred_d = fred_latest(FRED_SERIES["JP10Y"])
    jp_manual = manual.get("jp10y") or {}
    jp_manual_val = jp_manual.get("value")

    if isinstance(jp_manual_val, (int, float)):
        jp10y = float(jp_manual_val)
        jp10y_d = jp_manual.get("asof")
        jp10y_src = "manual"
        jp10y_label = "일 국채 10년(수동)"
        print(f"JP10Y(수동)={jp10y}  (FRED근사={jp10y_fred})")
    else:
        jp10y = jp10y_fred
        jp10y_d = jp10y_fred_d
        jp10y_src = "fred"
        jp10y_label = "일 국채 10년(월간·근사)"
        print(f"JP10Y(FRED월간)={jp10y}")

    us_jp_2y_gap = None
    if us2y is not None and jp10y is not None:
        # 엄밀히는 JP2Y 가 이상적이나 무료 실시간 제약으로 JP10Y 근사.
        # 스프레드의 '수준'보다 '추세'를 보는 용도.
        us_jp_2y_gap = round(us2y - jp10y, 3)

    metrics["us2y"] = {"label": "미 국채 2년", "value": us2y, "unit": "%", "asof": us2y_d}
    metrics["us10y"] = {"label": "미 국채 10년", "value": us10y, "unit": "%"}
    metrics["jp10y"] = {"label": jp10y_label, "value": jp10y, "unit": "%",
                        "asof": jp10y_d, "src": jp10y_src, "fred_ref": jp10y_fred}
    metrics["rate_gap"] = {"label": "미일 금리차(2Y-JP10Y 근사)", "value": us_jp_2y_gap, "unit": "%p"}

    # --- 2. 환율 (트리거) -------------------------------------------
    jpy_closes = yahoo_series("JPY=X", rng="1mo")
    usdjpy = jpy_closes[-1] if jpy_closes else None
    jpy_3d = pct_change_ndays(jpy_closes, 3)
    # USD/JPY 하락 = 엔 강세. 엔 강세율(양수)로 변환.
    yen_strength_3d = (-jpy_3d) if jpy_3d is not None else None
    print(f"USDJPY={usdjpy} yen_strength_3d={yen_strength_3d}")

    metrics["usdjpy"] = {"label": "달러/엔", "value": round(usdjpy, 2) if usdjpy else None, "unit": "엔"}
    metrics["yen_3d"] = {"label": "엔화 3일 강세율", "value": round(yen_strength_3d, 2) if yen_strength_3d is not None else None, "unit": "%"}

    # --- 3. 변동성 (2차 전이) ---------------------------------------
    vix = yahoo_last("^VIX")
    move = yahoo_last("^MOVE")
    print(f"VIX={vix} MOVE={move}")
    metrics["vix"] = {"label": "VIX (주식 변동성)", "value": round(vix, 2) if vix else None, "unit": ""}
    metrics["move"] = {"label": "MOVE (채권 변동성)", "value": round(move, 2) if move else None, "unit": ""}

    # --- 4. 크레딧 스프레드 (신용·채권 경로) --------------------------
    hy, hy_d = fred_latest(FRED_SERIES["HY_OAS"])
    ig, _ = fred_latest(FRED_SERIES["IG_OAS"])
    print(f"HY_OAS={hy} IG_OAS={ig}")
    metrics["hy_oas"] = {"label": "美 하이일드 스프레드(OAS)", "value": hy, "unit": "%", "asof": hy_d}
    metrics["ig_oas"] = {"label": "美 투자등급 스프레드(OAS)", "value": ig, "unit": "%"}

    # --- 5. 청산 연료: CFTC 엔 순숏 포지션 (선행) --------------------
    cftc = cftc_yen_positioning()
    if cftc:
        metrics["cftc_short"] = {
            "label": "헤지펀드 엔 순숏(청산 연료)",
            "value": round(cftc["net_short"]) if cftc["net_short"] is not None else None,
            "unit": "계약", "asof": cftc["date"],
            "pctile": cftc["pctile"], "n_weeks": cftc["n_weeks"],
        }
    else:
        metrics["cftc_short"] = {"label": "헤지펀드 엔 순숏(청산 연료)",
                                 "value": None, "unit": "계약", "pctile": None}

    # --- 6. 달러 조달 스트레스 프록시 (SOFR - 3M T-bill) -------------
    fund_bp, fund_d = funding_stress_proxy()
    print(f"funding_proxy(SOFR-DTB3)={fund_bp}bp")
    metrics["funding"] = {"label": "달러 조달 스트레스(프록시)",
                          "value": fund_bp, "unit": "bp", "asof": fund_d}

    # ----------------------------------------------------------------
    # 리스크 점수화
    #   각 지표를 0~100 위험도로 정규화 → 가중합
    #   임계값은 문헌/과거 사례(2024.8 등) 기반의 러프한 기준.
    #   대시보드에서 조정 가능하도록 근거를 주석에 명시.
    # ----------------------------------------------------------------
    def band(value, low, high):
        """value 를 [low(위험0), high(위험100)] 구간으로 선형 정규화."""
        if value is None:
            return None
        if high == low:
            return 0.0
        x = (value - low) / (high - low) * 100.0
        return max(0.0, min(100.0, x))

    sub = {}

    # 금리차 축소 = 위험. 근사 스프레드가 낮을수록 위험 → 축을 뒤집음.
    # 미일 정책금리차가 좁혀질수록 캐리 수익 훼손.
    sub["rate_gap"] = band(us_jp_2y_gap, 4.0, 1.0) if us_jp_2y_gap is not None else None

    # 엔 3일 강세율: 3%↑ 가 마진콜 임계 (2024.8). 0%→위험0, 3%→위험100.
    sub["yen_3d"] = band(yen_strength_3d, 0.0, 3.0) if yen_strength_3d is not None else None

    # VIX: 평상 15 → 위험0, 40(패닉) → 위험100.
    sub["vix"] = band(vix, 15.0, 40.0) if vix is not None else None
    # MOVE: 평상 90 → 위험0, 160 → 위험100.
    sub["move"] = band(move, 90.0, 160.0) if move is not None else None

    # HY OAS: 3%(평온) → 위험0, 8%(스트레스) → 위험100.
    sub["hy_oas"] = band(hy, 3.0, 8.0) if hy is not None else None

    # 일본 10년 금리 절대수준: 1.5% → 위험0, 3.5% → 위험100.
    # (금리 상승 = 자국 회귀 유인 = 청산 압력)
    sub["jp10y"] = band(jp10y, 1.5, 3.5) if jp10y is not None else None

    # CFTC 엔 순숏 백분위: 청산 '연료량'. 백분위 자체가 0~100 위험도.
    # 순숏이 3년 고점(백분위 100)일수록 청산 시 매도 압력 큼.
    cftc_pctile = metrics.get("cftc_short", {}).get("pctile")
    sub["cftc_short"] = cftc_pctile if cftc_pctile is not None else None

    # 달러 조달 스트레스 프록시: SOFR-DTB3. 0bp→위험0, 40bp→위험100.
    # 진짜 XCCY basis 아님(무료 소스 없음). 방향성 근사용.
    sub["funding"] = band(fund_bp, 0.0, 40.0) if fund_bp is not None else None

    # 가중치 (합 = 1.0)  — 조달비용·환율 트리거·청산 연료에 무게.
    weights = {
        "rate_gap":   0.16,
        "jp10y":      0.12,
        "yen_3d":     0.20,
        "cftc_short": 0.15,   # 청산 연료 (선행)
        "vix":        0.09,
        "move":       0.10,
        "hy_oas":     0.11,
        "funding":    0.07,   # 달러 조달 스트레스 프록시
    }

    num, den = 0.0, 0.0
    for k, w in weights.items():
        s = sub.get(k)
        if s is not None:
            num += s * w
            den += w
    total = round(num / den, 1) if den > 0 else None

    if total is None:
        level = "unknown"
    elif total < 25:
        level = "calm"        # 평온
    elif total < 50:
        level = "watch"       # 경계
    elif total < 70:
        level = "elevated"    # 주의
    else:
        level = "alarm"       # 경보

    out = {
        "updated_utc": now.isoformat() + "Z",
        "score": total,
        "level": level,
        "subscores": {k: (round(v, 1) if v is not None else None) for k, v in sub.items()},
        "weights": weights,
        "metrics": metrics,
        "repo": os.environ.get("GITHUB_REPOSITORY"),          # 예: birdjinx/yen-canary
        "branch": os.environ.get("GITHUB_REF_NAME", "main"),  # 예: main
        "notes": {
            "rate_gap": "미일 2Y-JP10Y 근사 스프레드. 축소 속도가 청산 압력의 선행지표.",
            "yen_3d": "3거래일 누적 엔 강세 3% 돌파 시 마진콜 도미노 임계(2024.8 사례).",
            "vix_move": "VIX+MOVE 동반 상승 시 캐리청산형 충격 가능성.",
            "hy_oas": "사모신용·CLO는 후행. 유동성 있는 HY OAS 를 선행 프록시로 사용.",
            "jp10y": "FRED 월간(OECD) 근사. 실시간 JGB 는 무료 제약으로 지연 가능.",
            "cftc_short": "CFTC 주간 발표(화요일 기준, 금요일 공개). 헤지펀드 엔 순숏의 3년 백분위 = 청산 시 풀릴 매도 압력의 크기.",
            "funding": "SOFR-3M T-bill 스프레드. 진짜 크로스커런시 베이시스가 아닌 달러 조달 스트레스 근사 프록시(무료 소스 제약).",
        },
    }

    return out


# ----------------------------------------------------------------------
# Claude API 분석 코멘트  (Haiku 4.5, 저비용)
# ----------------------------------------------------------------------
def build_analysis_prompt(data):
    """수집 결과를 근거 있는 진단이 나오도록 구조화해 프롬프트로."""
    m = data["metrics"]
    s = data["subscores"]

    def line(key, skey=None):
        mm = m.get(key, {})
        val = mm.get("value")
        unit = mm.get("unit", "")
        risk = s.get(skey) if skey else None
        r = f" [위험기여 {risk}/100]" if risk is not None else ""
        return f"  - {mm.get('label', key)}: {val}{unit}{r}"

    facts = "\n".join([
        "[① 조달비용]",
        line("us2y"), line("us10y"),
        line("jp10y", "jp10y"), line("rate_gap", "rate_gap"),
        "[② 환율 트리거]",
        line("usdjpy"), line("yen_3d", "yen_3d"),
        "[③ 청산 연료]",
        line("cftc_short", "cftc_short"),
        "[④ 변동성 전이]",
        line("vix", "vix"), line("move", "move"),
        "[⑤ 크레딧·조달 경로]",
        line("hy_oas", "hy_oas"), line("ig_oas"), line("funding", "funding"),
    ])

    prompt = f"""너는 엔캐리 트레이드 청산 리스크를 감시하는 매크로 애널리스트다.
아래는 방금 수집된 지표다. 종합 청산 리스크 점수는 {data['score']}/100 (등급: {data['level']}).

{facts}

[판정 기준]
- 엔 3일 강세율 3% 돌파 = 마진콜 도미노 임계 (2024.8 사례)
- 헤지펀드 엔 순숏 백분위가 높을수록 = 청산 시 풀릴 매도 '연료'가 많음(선행)
- VIX+MOVE 동반 상승 = 단순 조정이 아닌 캐리청산형 충격 신호
- 미일 금리차 '축소 속도'가 청산 압력의 선행지표
- 일본 10Y 금리 상승 = 일본 기관의 자국 회귀(해외자산 매도) 유인
- HY 스프레드 확대 = 사모신용·CLO 스트레스의 선행 프록시
- 달러 조달 스트레스(프록시) 상승 = 달러 자금 경색 조짐
- 통상 ①②③(연료 축적)이 먼저 쌓이고 ④⑤가 뒤따름

[작성 규칙]
- 한국어. 200~300자 내외로 압축.
- 지금 데이터에서 '가장 경계할 지표 1~2개'를 수치와 함께 구체적으로 짚어라.
- 특히 청산 연료(CFTC 순숏 백분위)가 높은데 트리거(엔 강세)가 붙는 조합을 주의 깊게 보라.
- 어느 단계까지 진행됐는지 한 문장으로 판정하라.
- 다음에 지켜볼 트리거(구체적 수치 조건)를 한 가지 제시하라.
- 투자 조언·매매 지시는 하지 마라. 상태 진단만.
- 불릿 없이 자연스러운 산문 2~3문장으로."""
    return prompt


def claude_analysis(data):
    """Claude Haiku 로 진단 코멘트 생성. 실패 시 None (대시보드는 정상 동작)."""
    if not ANTHROPIC_KEY:
        print("   (ANTHROPIC_API_KEY 없음 — 분석 코멘트 생략)")
        return None

    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 500,
        "messages": [
            {"role": "user", "content": build_analysis_prompt(data)}
        ],
    }

    def _call():
        r = requests.post(url, headers=headers, json=body, timeout=40)
        r.raise_for_status()
        return r.json()

    resp = _retry(_call, tries=2, wait=3)
    if not resp:
        print("   ! Claude 분석 실패 — 코멘트 생략")
        return None
    try:
        parts = [b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text"]
        text = "".join(parts).strip()
        usage = resp.get("usage", {})
        print(f"   Claude ok (in={usage.get('input_tokens')} out={usage.get('output_tokens')})")
        return text or None
    except Exception as e:  # noqa
        print(f"   ! Claude 파싱 실패: {e}")
        return None


def main():
    data = collect()

    # Claude 진단 코멘트 (선택). 지표 수집이 끝난 뒤 실행.
    print("=== Claude analysis ===")
    comment = claude_analysis(data)
    data["analysis"] = {
        "text": comment,
        "model": ANTHROPIC_MODEL if comment else None,
        "generated_utc": (dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z") if comment else None,
    }

    os.makedirs("docs", exist_ok=True)
    with open("docs/data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("=== written docs/data.json ===")
    print(f"score={data['score']} level={data['level']} analysis={'yes' if comment else 'no'}")


if __name__ == "__main__":
    main()
