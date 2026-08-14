# 🐤 yen-canary

엔캐리 트레이드 청산 조기경보 모니터. FRED + Yahoo Finance 데이터를 GitHub Actions로
자동 수집해 `docs/data.json`에 저장하고, `docs/index.html` 대시보드가 이를 직접 읽어
종합 청산 리스크 점수(0~100)와 4개 흐름별 지표를 보여줍니다.

## 지표 구성 (흐름 순서)

| 흐름 | 지표 | 소스 |
|---|---|---|
| ① 조달비용 | 미 2Y·10Y, 일 10Y, 미일 금리차 근사 | FRED |
| ② 환율 트리거 | 달러/엔, 엔화 3일 강세율 | Yahoo Finance |
| ③ 청산 연료 | 헤지펀드 엔 순숏(3년 백분위) | CFTC (무료) |
| ④ 변동성 전이 | VIX, MOVE | Yahoo Finance |
| ⑤ 크레딧·조달 | 美 HY OAS, IG OAS, 달러 조달 스트레스 프록시 | FRED |

가중치: 엔 3일 강세율 20% · 미일 금리차 16% · 청산 연료 15% · 일 10Y 12% ·
HY OAS 11% · MOVE 10% · VIX 9% · 조달 프록시 7%.
(`scripts/collect_data.py`의 `weights`에서 조정)

**청산 연료(CFTC)**: 헤지펀드(레버리지펀드)의 엔 선물 순숏이 3년 백분위 상단일수록,
청산이 시작될 때 풀릴 매도 압력이 큽니다. "연료가 쌓인 상태 + 엔 강세 트리거"
조합이 가장 위험합니다. CFTC Socrata API(무료·키 불필요, 주간 발표)에서 가져옵니다.

**달러 조달 스트레스**는 SOFR-3M T-bill 기반 **근사 프록시**입니다. 진짜
크로스커런시 베이시스(XCCY basis)는 무료 자동 수집 소스가 없어, 달러 자금
경색의 방향성만 근사합니다.

## 설치

1. 이 저장소를 본인 계정에 만들고 파일을 그대로 올립니다(웹 인터페이스로 업로드 가능).
2. **FRED API 키 발급** — https://fred.stlouisfed.org/docs/api/api_key.html (무료).
3. 저장소 **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `FRED_API_KEY` / Value: 발급받은 키
   - (선택) Name: `ANTHROPIC_API_KEY` / Value: Claude API 키
     → 등록하면 매 수집마다 지표 기반 종합 진단 코멘트가 대시보드에 표시됩니다.
       없으면 코멘트만 생략되고 나머지는 정상 동작합니다.
4. **Settings → Pages** → Source를 `main` 브랜치 `/docs` 폴더로 지정.
5. **Actions 탭 → collect → Run workflow**로 최초 1회 수동 실행.
   완료되면 `docs/data.json`이 갱신되고 Pages 주소에서 대시보드가 뜹니다.

이후에는 평일 하루 2회(도쿄·뉴욕 장 마감 근처) 자동 갱신됩니다.

## 일본 10년물 수동 입력

일본 10년물은 FRED 월간(OECD) 근사치가 기본값입니다. 정확한 값을 쓰려면
대시보드에서 바로 확정할 수 있습니다.

**대시보드에서 확정 (원클릭)**
1. 일본 10년물 카드의 입력칸에 실제 금리(%)를 넣습니다. 입력하는 즉시
   금리차·점수·등급이 화면에서 미리 계산됩니다(미리보기).
2. **"이 값으로 확정 →"** 버튼을 누르면, 그 값이 이미 채워진 GitHub 편집
   화면이 새 탭에 열립니다.
3. 초록색 **Commit changes**만 누르면 끝입니다. `manual.json`이 갱신되고,
   커밋을 감지해 **재수집·Claude 분석이 자동 실행**됩니다(1~2분).

토큰이나 별도 로그인 설정은 필요 없습니다. GitHub에 로그인된 브라우저면
편집 화면이 바로 뜹니다. 근사치로 되돌리려면 편집 화면에서 `value`를
`null`로 바꿔 커밋하면 됩니다.

> 확정 버튼은 저장소 정보(`repo`)가 data.json에 있어야 활성화됩니다.
> Actions로 최소 1회 수집하면 자동으로 채워집니다.

**직접 편집도 가능**
`docs/manual.json`을 GitHub 웹에서 열어 `jp10y.value`와 `asof`를 고쳐
커밋해도 동일하게 동작합니다.

```json
{ "jp10y": { "value": 1.62, "asof": "2026-08-14" } }
```

`ANTHROPIC_API_KEY`를 등록하면, 수집된 지표·위험기여도·판정기준을 Claude에
넘겨 200~300자 진단 코멘트를 생성해 대시보드 상단에 표시합니다.

- **모델**: Claude Haiku 4.5 (`claude-haiku-4-5-20251001`) — 가장 저렴한 현행 모델.
- **비용**: 회당 입력 ≈450토큰 + 출력 ≈600토큰 → **약 0.4센트**.
  평일 2회 × 월 22일 ≈ **월 20센트** 수준.
- 프롬프트·모델은 `scripts/collect_data.py`의 `build_analysis_prompt()`,
  `ANTHROPIC_MODEL`에서 조정할 수 있습니다. 더 깊은 분석이 필요하면
  모델을 Sonnet으로 올리면 되지만 비용이 3배가 됩니다.

## 참고

- 일본 10년물은 무료 실시간 소스 제약으로 FRED 월간(OECD) 근사치를 씁니다.
  실시간 JGB를 원하면 해당 지표만 별도 소스로 교체하세요.
- 임계값(각 지표의 위험 정규화 구간)은 문헌·과거 사례(2024.8 등) 기반의
  러프한 기준입니다. `collect_data.py`의 `band(...)` 호출부에서 조정하세요.
- 투자 조언이 아닙니다.
