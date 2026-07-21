# -*- coding: utf-8 -*-
"""build/refresh_holdings.py — '현재 포트폴리오 구성' 생성 (클라우드 자체완결·무료 데이터만)
================================================================================
⚠ 데이터 정책: 이 파일은 **yfinance(무료 공개 가격)만** 사용한다. 사내 DB·라이선스 데이터
  (FactSet 추정치·SPG 팩터) 파생 보유(챔피언·단일팩터 top10·팩터모멘텀)는 공개하지 않는다 —
  라이선스 파생 데이터 재배포 + 배포 전략 IP 노출. 여기에 전략을 추가할 때 이 정책을 지킬 것.

무료 가격만으로 재현 가능한 3전략의 최신 리밸런스 구성:
  · 크로스에셋 RP: 12 ETF 60일 역변동성 + 위험자산 12M 추세게이트 + vol타겟 10%(0.3~2.5x)
  · 섹터 RP: 11 SPDR 섹터 ETF 60일 역변동성
  · 변동성 관리: NDX 익스포저 = min(15%/21일 실현σ, 100%)
규칙은 랩 정본(combined_portfolio_deploy.rp_sleeve · strategy_backtests_gen)과 동일.
가격 소스만 yfinance라 사내 정본과 비중이 소수점에서 다를 수 있다(방법론 동일).
출력: data/strategy_holdings.json — explorer.html이 '현재 포트폴리오 구성' 표로 렌더.
"""
from __future__ import annotations
import os, json
import numpy as np, pandas as pd
import yfinance as yf

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "data", "strategy_holdings.json")
ANN = 252

CORE = ["SPY", "QQQ", "EFA", "EEM", "TLT", "IEF", "GLD", "DBC", "UUP", "HYG", "LQD", "VNQ"]
RISKY = {"SPY", "QQQ", "EFA", "EEM", "DBC", "VNQ"}
SECT = ["XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"]
ETF_NAMES = {
    "SPY": "SPDR S&P 500 (미국 대형주)", "QQQ": "Invesco QQQ (나스닥100)",
    "EFA": "iShares MSCI EAFE (선진국 ex-US)", "EEM": "iShares MSCI EM (신흥국)",
    "TLT": "iShares 20+Y 미국 장기국채", "IEF": "iShares 7-10Y 미국 중기국채",
    "GLD": "SPDR Gold (금)", "DBC": "Invesco DB Commodity (원자재)",
    "UUP": "Invesco 달러인덱스", "HYG": "iShares 하이일드 회사채",
    "LQD": "iShares 투자등급 회사채", "VNQ": "Vanguard 미국 리츠",
    "XLB": "Materials (소재)", "XLC": "Communication Services (커뮤니케이션)",
    "XLE": "Energy (에너지)", "XLF": "Financials (금융)", "XLI": "Industrials (산업재)",
    "XLK": "Technology (기술)", "XLP": "Consumer Staples (필수소비)",
    "XLRE": "Real Estate (부동산)", "XLU": "Utilities (유틸리티)",
    "XLV": "Health Care (헬스케어)", "XLY": "Consumer Discretionary (경기소비)",
}


def fetch_close(tickers, start="2019-01-01"):
    df = yf.download(tickers, start=start, auto_adjust=True, progress=False)["Close"]
    if isinstance(df, pd.Series): df = df.to_frame(tickers[0])
    df = df.dropna(how="all")
    miss = [t for t in tickers if t not in df.columns or df[t].dropna().empty]
    if miss:
        raise SystemExit(f"yfinance 가격 결측: {miss} — 갱신 중단(이전본 유지)")
    return df


def complete_months(monthly_index, last_px_date):
    """마지막 거래일이 지난(완료된) 월말 라벨만 — 부분월 리밸 방지(하우스 규약)."""
    return [d for d in monthly_index if (d + pd.offsets.MonthEnd(0)) <= last_px_date]


def main():
    S = {}

    # ── 크로스에셋 RP (정본 rp_sleeve와 동일 규칙·가격만 yfinance) ──
    print("크로스에셋 RP…", flush=True)
    px = fetch_close(CORE)[CORE]
    px = px.reindex(pd.bdate_range(px.index.min(), px.index.max())).ffill().dropna(how="all")
    r = px.pct_change(); pxm = px.resample("ME").last(); last = px.index.max()
    rebals = complete_months(pxm.index, last)
    vol60 = r.rolling(60).std().resample("ME").last(); tr = pxm / pxm.shift(12) - 1
    d0 = rebals[-1]
    iv = (1 / vol60.loc[d0]).dropna(); g = tr.loc[d0].reindex(iv.index)
    keep = iv[(g > 0) | (~iv.index.isin(RISKY))]
    w = (keep / keep.sum()).reindex(CORE).fillna(0.0)
    # vol타겟 레버(정본과 동일): 최신 리밸 비중을 ffill 적용한 포트 수익의 30일 실현변동성
    W = {}
    for d in rebals:
        iv_ = (1 / vol60.loc[d]).dropna(); g_ = tr.loc[d].reindex(iv_.index)
        k_ = iv_[(g_ > 0) | (~iv_.index.isin(RISKY))]
        if len(k_): W[d] = k_ / k_.sum()
    Wdf = pd.DataFrame(W).T.reindex(columns=px.columns).fillna(0.0).sort_index()
    wd = Wdf.reindex(r.index, method="ffill").shift(1).fillna(0.0)
    port = (wd * r).sum(axis=1)
    lev = (0.10 / (port.rolling(30).std() * np.sqrt(ANN))).clip(0.3, 2.5)
    excluded = {t: f"12M 추세 {tr.loc[d0, t]*100:+.1f}% ≤ 0 → 추세게이트 제외(위험자산)"
                for t in CORE if t in RISKY and w[t] == 0}
    S["크로스에셋 리스크패리티 + vol타겟"] = {
        "as_of": str(d0.date()), "kind": "assets",
        "note": "12개 자산 ETF를 60일 역변동성 가중(월말리밸). 위험자산은 12M 추세>0일 때만 편입, "
                "목표변동성 10%로 익스포저 레버 조절(0.3~2.5x, 익일 적용). 가격 소스=yfinance(무료).",
        "positions": sorted([{"t": t, "name": ETF_NAMES[t], "w": round(float(w[t]), 4)} for t in CORE],
                            key=lambda x: -x["w"]),
        "extra": {"vol타겟_레버리지": round(float(lev.dropna().iloc[-1]), 2), "목표변동성": "10%",
                  "추세게이트_제외": excluded if excluded else "없음(전 위험자산 추세 양호)"},
    }

    # ── 섹터 RP ──
    print("섹터 RP…", flush=True)
    spx_ = fetch_close(SECT)[SECT]
    rs = spx_.pct_change(); ivs = 1.0 / rs.rolling(60).std()
    mw = ivs.resample("ME").last(); mw = mw.div(mw.sum(axis=1), axis=0).fillna(0)
    md = complete_months(mw.index, spx_.index[-1])[-1]
    S["섹터 리스크패리티 (역변동성)"] = {
        "as_of": str(md.date()), "kind": "assets",
        "note": "11개 SPDR 섹터 ETF를 최근 60일 역변동성으로 가중(합=1), 월말리밸. 가격 소스=yfinance(무료).",
        "positions": sorted([{"t": t, "name": ETF_NAMES[t], "w": round(float(mw.loc[md, t]), 4)} for t in SECT],
                            key=lambda x: -x["w"]),
        "extra": {"룩백": "60거래일 실현변동성"},
    }

    # ── 변동성 관리 (NDX) ──
    print("변동성 관리…", flush=True)
    ndx = fetch_close(["^NDX"])["^NDX"].dropna()
    rv = ndx.pct_change().fillna(0)
    realized = rv.rolling(21).std() * np.sqrt(ANN)
    me_idx = rv.resample("ME").last().index
    d1 = complete_months(me_idx, ndx.index[-1])[-1]
    rv_at = float(realized[realized.index <= d1].iloc[-1])
    ex = min(0.15 / rv_at, 1.0) if rv_at > 0 else 0.0
    S["변동성 관리 (σ타겟 익스포저)"] = {
        "as_of": str(d1.date()), "kind": "assets",
        "note": "NDX 익스포저 = min(목표σ15% / 21일 실현σ, 100%), 나머지는 현금. 월말 산출·익일 적용(무레버). "
                "가격 소스=yfinance(무료).",
        "positions": [{"t": "NDX", "name": "나스닥100 지수", "w": round(ex, 4)},
                      {"t": "CASH", "name": "현금", "w": round(1 - ex, 4)}],
        "extra": {"실현변동성_21일": f"{rv_at*100:.1f}%", "목표변동성": "15%"},
    }

    out = {"generated": pd.Timestamp.utcnow().strftime("%Y-%m-%d"),
           "note": "무료 공개 데이터(yfinance)로 재현 가능한 전략만 보유 구성을 공개합니다. "
                   "라이선스 데이터 파생 전략(챔피언·단일팩터 top10·팩터모멘텀)의 보유는 비공개.",
           "strategies": S}
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"→ {OUT} ({os.path.getsize(OUT)//1024}KB)")

    # 자기검증
    chk = json.load(open(OUT, encoding="utf-8"))
    errs = []
    for nm, st in chk["strategies"].items():
        for k in ("as_of", "kind", "note", "positions"):
            if k not in st: errs.append(f"{nm}: {k} 누락")
        ws = sum(p["w"] for p in st["positions"])
        if abs(ws - 1.0) > 0.01: errs.append(f"{nm}: 비중합 {ws:.4f}")
    if len(chk["strategies"]) != 3: errs.append(f"전략 수 {len(chk['strategies'])} ≠ 3")
    if errs:
        raise SystemExit("자기검증 실패: " + "; ".join(errs))
    print("자기검증 통과 ✓")
    for nm, st in chk["strategies"].items():
        top3 = ", ".join(f"{p['t']} {p['w']*100:.1f}%" for p in st["positions"][:3])
        print(f"  {nm[:30]:<32} as_of {st['as_of']}  top3: {top3}")


if __name__ == "__main__":
    main()
