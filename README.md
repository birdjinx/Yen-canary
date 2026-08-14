# 🐤 yen-canary

엔캐리 트레이드 청산 조기경보 모니터. FRED + Yahoo Finance 데이터를 GitHub Actions로
자동 수집해 `docs/data.json`에 저장하고, `docs/index.html` 대시보드가 이를 직접 읽어
종합 청산 리스크 점수(0~100)와 4개 흐름별 지표를 보여줍니다.

## 지표 구성 (흐름 순서)

| 흐름 | 지표 | 소스 |
|---|---|---|
| ① 조달비용 | 미 2Y·10Y, 일 10Y, 미일 금리차 근사 | FRED |
| ② 환율 트리거 | 달러/엔, 엔화 3일 강세율 | Yahoo Finance |
| ③ 변동성 전이 | VIX, MOVE | Yahoo Finance |
| ④ 크레딧 경로 | 美 HY OAS, IG OAS | FRED |

가중치: 엔 3일 강세율 25% · 미일 금리차 20% · 일 10Y 15% · HY OAS 15% ·
MOVE 13% · VIX 12%. (`scripts/collect_data.py`의 `weights`에서 조정)

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

## Claude 진단 코멘트

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
