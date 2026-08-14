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

    # 가중치 (합 = 1.0)  — 조달비용·환율 트리거에 무게.
    weights = {
        "rate_gap": 0.20,
        "jp10y":    0.15,
        "yen_3d":   0.25,
        "vix":      0.12,
        "move":     0.13,
        "hy_oas":   0.15,
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
        "[③ 변동성 전이]",
        line("vix", "vix"), line("move", "move"),
        "[④ 크레딧 경로]",
        line("hy_oas", "hy_oas"), line("ig_oas"),
    ])

    prompt = f"""너는 엔캐리 트레이드 청산 리스크를 감시하는 매크로 애널리스트다.
아래는 방금 수집된 지표다. 종합 청산 리스크 점수는 {data['score']}/100 (등급: {data['level']}).

{facts}

[판정 기준]
- 엔 3일 강세율 3% 돌파 = 마진콜 도미노 임계 (2024.8 사례)
- VIX+MOVE 동반 상승 = 단순 조정이 아닌 캐리청산형 충격 신호
- 미일 금리차 '축소 속도'가 청산 압력의 선행지표
- 일본 10Y 금리 상승 = 일본 기관의 자국 회귀(해외자산 매도) 유인
- HY 스프레드 확대 = 사모신용·CLO 스트레스의 선행 프록시
- 통상 ①②가 먼저 악화되고 ③④가 뒤따름

[작성 규칙]
- 한국어. 200~300자 내외로 압축.
- 지금 데이터에서 '가장 경계할 지표 1~2개'를 수치와 함께 구체적으로 짚어라.
- 4개 흐름 중 어느 단계까지 진행됐는지 한 문장으로 판정하라.
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
