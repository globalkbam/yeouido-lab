# 여두 전략 랩 — 사이트

퀀트 전략 리서치 결과를 공개하는 정적 사이트. **전략 코드·리서치는 비공개 repo(Yeouido)에 있고, 이 repo엔 공개 HTML과 데이터만** 있습니다.

**라이브:** https://ydyoon4578.github.io/yeodoo-lab/

## 구성 (여섯 페이지)
| 파일 | 내용 |
|---|---|
| `index.html` | 랜딩 허브 — 주목 종목(스윙 저점매수·고점매도) 요약 |
| `explorer.html` | **전략 탐색기** — 구현·적대검증한 22개 전략(배포 3·marginal 17·비교용 기각 2) + 실제 백테스트 차트 |
| `stocks.html` | **종목 시그널** — NDX/SPX 512종목 테크니컬(22지표)·매매 타이밍·252봉 차트 |
| `regime.html` | **시장 국면** — FRED 매크로 39지표(성장·노동·물가·금융·금리·주택) + 추세 대비 서프라이즈 |
| `rotation.html` | **오늘의 로테이션** — 웹 리서치 전략 풀 63종에서 날짜(KST) 시드 10선 · 미검증·외부 출처 |
| `archive.html` | **기각 아카이브** — 배포 부적합 판정 40개 전략과 사유 |

## 데이터·자동화
| 경로 | 생성기 | 갱신 |
|---|---|---|
| `data/stocks.json`(슬림) + `data/sd/<티커>.json`(상세, 지연 로드) + `data/home_reco.json` | `build/refresh_stocks.py` | 매일 08:05 KST + 08:40 백업 |
| `data/regime.json` | `build/refresh_regime.py` | 매일 08:15 KST + 08:50 백업 |
| `data/rotation_pool.json` | 헤드리스 리서치 잡(로컬) | 매일 08:20 KST (10선 + 방치 3종) |
| `data/strategy_backtests.json`·`strategy_detail.json` | 비공개 repo에서 정적 생성 | 수시 |

`build/validate_site.py`가 푸시마다 CI에서 JS 괄호·미정의 호출·JSON 스키마·딥링크 앵커·선별 상수(프론트↔잡)·
기준일 정합을 검사합니다.

## 배포
GitHub Pages(branch source: `main` / root). `main`에 푸시하면 자동 재빌드.

## 종목 시그널 자동 갱신
`.github/workflows/refresh-stocks.yml` 크론(평일)이 `build/refresh_stocks.py`를 실행 —
`data/members.json`을 읽고 yfinance 가격으로 표준 테크니컬 지표·교과서 매수/매도 신호를 직접 계산해
`data/stocks.json`을 갱신·커밋 → Pages 자동 재빌드. **DB·사내 라이브러리 불필요**, 매 거래일 최신.

전략 탐색기 데이터(`strategy_detail.json`)는 리서치 산출물이라 갱신 빈도가 낮음 — 비공개 repo에서 생성해 커밋.

## 로컬 미리보기
```bash
python3 -m http.server 8080   # → http://localhost:8080
```
