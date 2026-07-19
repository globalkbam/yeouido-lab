# 여의도 전략 랩 — 사이트

퀀트 전략 리서치 결과를 공개하는 정적 사이트. **전략 코드·리서치는 비공개 repo(Yeouido)에 있고, 이 repo엔 공개 HTML과 데이터만** 있습니다.

**라이브:** https://globalkbam.github.io/yeouido-lab/

## 구성 (두 도구)
| 파일 | 내용 |
|---|---|
| `index.html` | 랜딩 허브 |
| `explorer.html` | **전략 탐색기** — 51개 전략 인터랙티브 브라우저(성과·판정·장단점·활용방안·활용 아키타입) |
| `stocks.html` | **종목 시그널** — NDX/SPX 테크니컬(과매수/과매도·추세·모멘텀·변동성) + 매수/매도 타이밍 + 기간선택 차트 |
| `data/stocks.json` | 종목 시그널 데이터 (일별) |
| `data/strategy_detail.json` | 전략별 장단점·활용방안 |
| `data/members.json` | NDX/SPX 종목 리스트(티커·회사명·섹터) |
| `build/refresh_stocks.py` | 종목 시그널 갱신 스크립트 (자체완결·클라우드) |

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
