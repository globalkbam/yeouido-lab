# 여두 전략 랩 — 사이트

퀀트 전략 리서치 결과를 공개하는 정적 사이트. **전략 코드·리서치는 비공개 repo(Yeouido)에 있고, 이 repo엔 공개 HTML과 데이터만** 있습니다.

**라이브:** https://ydyoon4578.github.io/yeodoo-lab/

## 구성 (여섯 페이지)
| 파일 | 내용 |
|---|---|
| `index.html` | 랜딩 허브 — 주목 종목(스윙 저점매수·고점매도) 요약 |
| `explorer.html` | **전략 탐색기** — 구현·적대검증한 22개 전략(배포 3 · 제한적 유효 17 · 비교용 기각 2) + 실제 백테스트 차트 |
| `stocks.html` | **종목 시그널** — NDX/SPX 512종목 테크니컬(22지표)·매매 타이밍·252봉 차트 |
| `regime.html` | **시장 국면** — FRED 매크로 39지표(성장·노동·물가·금융·금리·주택) + 추세 대비 서프라이즈 |
| `rotation.html` | **오늘의 로테이션** — 웹 리서치 전략 풀 71종에서 날짜(KST) 시드 10선 · 미검증·외부 출처 |
| `archive.html` | **기각 아카이브** — 배포 부적합 판정 40개 전략과 사유 |

## 데이터·자동화
| 경로 | 생성기 | 갱신 |
|---|---|---|
| `data/stocks.json`(슬림) + `data/sd/<티커>.json`(상세, 지연 로드) + `data/home_reco.json` | `build/refresh_stocks.py` | 매일 07:35 KST + 08:10 백업 |
| `data/regime.json` | `build/refresh_regime.py` | 매일 07:45 KST + 08:20 백업 |
| `data/rotation_pool.json` | 헤드리스 리서치 잡(로컬) | 매일 07:50 KST (10선 + 방치 3종) |
| `data/sentiment.json` | `build/refresh_sentiment.py` | 매일 08:00 KST + 08:35 백업 |
| `data/strategy_holdings.json` | `build/refresh_holdings.py` | 매월 1일 07:55 KST |
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

## 누적 스토어 (Postgres `yeodoo` 스키마)

원본은 git의 `data/*.json`이고, DB는 **조회용 누적 미러**다(GitHub Actions 러너는 tailnet 밖이라 DB에
접근하지 않는다 — 사이트 생성은 DB에 의존하지 않는다). 적재는 tailnet 안 머신에서 `build/db_load.py`가
git 이력을 되짚어 수행하므로, 적재 머신이 며칠 꺼져 있어도 `--backfill` 한 번으로 복구된다.

| 테이블 | 내용 | 소스 |
|---|---|---|
| `stock_daily` | 종목 일별 스냅샷(판정·컴포짓·플래그) | stocks.json |
| `fundamental_daily` | 펀더멘털 20지표 | stocks.json |
| `target_daily` | 애널리스트 목표주가 | stocks.json · target_history.json |
| `swing_marker` | 스윙 타점(최초 관측·확정 시점 추적) | stocks.json |
| `regime_daily` · `sentiment_daily` | 시장 국면 · 심리 지수 | regime/sentiment.json |
| `rotation_strategy` | 전략 풀 일별 스냅샷(최근동향 갱신일·랩 판정) | rotation_pool.json |
| `strategy_perf` | 전략 백테스트 지표(집계 성과만) | strategy_backtests.json |
| `strategy_holding` | 전략 구성(종목·비중, free/db 구분) | strategy_holdings*.json |
| `universe_member` | 지수 구성 스냅샷(편입·제외 추적) | members.json |
| `screen_daily` | 펀더멘털 스크리닝 통과 종목·순위 | stocks.json + screens.json |
| `site_update` | 사이트 갱신 피드 | updates.json |
| `load_log` | 적재 이력(소스·기준일·행수·상태) | — |

`data/screens.json`은 **화면과 로더가 함께 읽는 단일 소스**다. 정의를 코드에 복제하면 조용히 어긋나므로
(로테이션 9선 FNV 사고와 같은 유형) CI가 인라인 복제를 차단한다.
