# -*- coding: utf-8 -*-
"""
refresh_regime.py — 시장 국면(레짐) 스냅샷 빌더 (self-contained, FRED + yfinance, 무료).
랩의 기존 regime/macro_dashboard.py 3축 분류(성장×물가×금융)를 FRED 공개데이터로 재현.
출력: data/regime.json (현재 레짐·축별 지표·히스토리·레짐별 자산 조건부 성과).
GitHub Actions 주간 크론으로 갱신.
"""
import os, json
import numpy as np, pandas as pd
from fredapi import Fred
import yfinance as yf

FRED_KEY = os.getenv("FRED_API_KEY", "64c41debf1ed074a809f5871e571c1fc")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "data", "regime.json")
f = Fred(api_key=FRED_KEY)

def series(code):
    try:
        s = f.get_series(code).dropna(); s.index = pd.to_datetime(s.index); return s
    except Exception as e:
        print("  FRED 실패", code, e); return pd.Series(dtype=float)

def zscore(s, win):
    return (s - s.rolling(win).mean()) / s.rolling(win).std()

def main():
    print("FRED 매크로 로드…")
    S = {c: series(c) for c in ["PAYEMS","UNRATE","CFNAIMA3","SAHMREALTIME","CPIAUCSL",
                                 "T10YIE","NFCI","BAMLH0A0HYM2","T10Y2Y","T10Y3M","VIXCLS","DFII10","DGS10"]}
    asof = max(s.index.max() for s in S.values() if len(s)).date().isoformat()

    # ── 파생 시계열 ──
    cpi_yoy = (S["CPIAUCSL"]/S["CPIAUCSL"].shift(12) - 1)*100          # CPI YoY(%)
    nfp_3m = S["PAYEMS"].diff().rolling(3).mean()                       # 3M 평균 월 고용증감(천명)
    unrate_chg = S["UNRATE"] - S["UNRATE"].rolling(12).min()           # 실업률 저점대비 상승폭(Sahm 유사)
    last = lambda s: float(s.dropna().iloc[-1]) if len(s.dropna()) else None

    # ── 축1: 성장 ──
    nfp, sahm, cfnai, dU = last(nfp_3m), last(S["SAHMREALTIME"]), last(S["CFNAIMA3"]), last(unrate_chg)
    growth_score = 0
    if nfp is not None: growth_score += 1 if nfp > 100 else (-1 if nfp < 0 else 0)
    if cfnai is not None: growth_score += 1 if cfnai > -0.35 else (-1 if cfnai < -0.7 else 0)
    if sahm is not None: growth_score += -1 if sahm >= 0.5 else 0
    if dU is not None: growth_score += -1 if dU >= 0.5 else 0
    growth = "EXPANSION" if growth_score >= 1 else ("CONTRACTION" if growth_score <= -2 else "SLOWING")

    # ── 축2: 물가 ──
    infl_v = last(cpi_yoy)
    inflation = "HIGH" if (infl_v or 0) >= 4 else ("MODERATE" if (infl_v or 0) >= 2.5 else "LOW")

    # ── 축3: 금융여건(스트레스) ──
    nfci, hy, vix, curve = last(S["NFCI"]), last(S["BAMLH0A0HYM2"]), last(S["VIXCLS"]), last(S["T10Y2Y"])
    hy_z = last(zscore(S["BAMLH0A0HYM2"], 756))
    fin_score = 0
    if nfci is not None: fin_score += 2 if nfci > 0.3 else (1 if nfci > 0 else 0)
    if hy_z is not None: fin_score += 2 if hy_z > 1.5 else (1 if hy_z > 0.75 else 0)
    if vix is not None: fin_score += 1 if vix > 25 else 0
    financial = "STRESSED" if fin_score >= 4 else ("ELEVATED" if fin_score >= 2 else ("EASY" if (nfci or 0) < -0.2 else "NEUTRAL"))

    # ── 성장×물가 매트릭스 → 명명 레짐 ──
    MATRIX = {
      ("EXPANSION","LOW"):("Goldilocks","🟢","완만성장·저물가 — 위험자산 우호","위험자산 풀비중 (그로스·모멘텀 유리)"),
      ("EXPANSION","MODERATE"):("Recovery","🟢","확장·물가 안정 — 순환 회복","위험자산 유지 (경기민감 편입)"),
      ("EXPANSION","HIGH"):("Overheating","🔴","과열·고물가 — 긴축 리스크","경계 — 신용스프레드·실질금리 관찰"),
      ("SLOWING","LOW"):("SoftLanding","🟡","둔화·저물가 — 연착륙 기대","균형 유지 (퀄리티 틸트)"),
      ("SLOWING","MODERATE"):("LateCycle","🟠","후기 사이클 — 둔화+물가","부분 현금·방어 준비"),
      ("SLOWING","HIGH"):("Stagflation","🔴","스태그플레이션 — 둔화+고물가","현금·실물헤지 전환"),
      ("CONTRACTION","LOW"):("Recession","🔴","수축 — 침체 위험","현금·국채 방어"),
      ("CONTRACTION","MODERATE"):("Recession","🔴","수축 — 침체 위험","현금·국채 방어"),
      ("CONTRACTION","HIGH"):("Stagflation","🔴","수축+고물가 — 최악 조합","현금·실물헤지"),
    }
    lab, emoji, desc, strat = MATRIX[(growth, inflation)]
    if financial in ("ELEVATED","STRESSED"):
        strat += f" · 금융스트레스 {financial} → 현금 강화"

    # ── 지표 패널 ──
    def stat_curve(v): return ("역전(경기침체 선행)","hot") if v is not None and v<0 else ("정상","good")
    def stat_hy(z): return ("스프레드 확대(위험회피)","hot") if (z or 0)>0.75 else (("타이트","good") if (z or 0)<-0.5 else ("보통","neut"))
    def stat_nfci(v): return ("긴축","hot") if (v or 0)>0.2 else (("완화","good") if (v or 0)<-0.2 else ("중립","neut"))
    def stat_vix(v): return ("고변동","hot") if (v or 0)>25 else (("저변동","good") if (v or 0)<15 else ("보통","neut"))
    def stat_cpi(v): return ("고물가","hot") if (v or 0)>=4 else (("저물가","good") if (v or 0)<2.5 else ("중간","watch"))
    def stat_nfp(v): return ("견조","good") if (v or 0)>100 else (("위축","hot") if (v or 0)<0 else ("둔화","watch"))
    def stat_sahm(v): return ("침체 트리거","hot") if (v or 0)>=0.5 else ("정상","good")
    def stat_real(v): return ("고실질금리(긴축)","hot") if (v or 0)>2 else (("완화","good") if (v or 0)<0.5 else ("중립","neut"))
    indicators = [
      {"k":"T10Y2Y","label":"수익률곡선 (10Y−2Y)","group":"금융","v":round(curve,2) if curve is not None else None,"u":"%p","st":stat_curve(curve),"d":"장단기 금리차. 음(역전)이면 역사적으로 침체 선행."},
      {"k":"BAMLH0A0HYM2","label":"하이일드 스프레드","group":"금융","v":round(hy,2) if hy is not None else None,"u":"%","st":stat_hy(hy_z),"d":"하이일드 회사채 가산금리. 확대는 위험회피·신용경색 신호."},
      {"k":"NFCI","label":"시카고 금융여건 (NFCI)","group":"금융","v":round(nfci,2) if nfci is not None else None,"u":"z","st":stat_nfci(nfci),"d":"종합 금융여건. +면 긴축·−면 완화(0=평균)."},
      {"k":"VIXCLS","label":"VIX (변동성)","group":"금융","v":round(vix,1) if vix is not None else None,"u":"","st":stat_vix(vix),"d":"S&P500 내재변동성. 공포지수 — 25↑ 스트레스."},
      {"k":"DFII10","label":"10년 실질금리 (TIPS)","group":"금융","v":round(last(S['DFII10']),2) if last(S['DFII10']) is not None else None,"u":"%","st":stat_real(last(S['DFII10'])),"d":"물가연동국채 실질수익률. 그로스주 할인율의 핵심."},
      {"k":"CPIYOY","label":"소비자물가 CPI (YoY)","group":"물가","v":round(infl_v,1) if infl_v is not None else None,"u":"%","st":stat_cpi(infl_v),"d":"전년比 물가상승률. 2%↓ 저물가·4%↑ 고물가."},
      {"k":"T10YIE","label":"기대인플레 (10Y BEI)","group":"물가","v":round(last(S['T10YIE']),2) if last(S['T10YIE']) is not None else None,"u":"%","st":("높음","watch") if (last(S['T10YIE']) or 0)>2.5 else ("안정","good"),"d":"시장 반영 10년 기대인플레(브레이크이븐)."},
      {"k":"PAYEMS","label":"고용증감 (3M평균)","group":"성장","v":round(nfp) if nfp is not None else None,"u":"천명","st":stat_nfp(nfp),"d":"비농업 신규고용 3개월 평균. 노동시장 모멘텀."},
      {"k":"UNRATE","label":"실업률 저점대비","group":"성장","v":round(dU,2) if dU is not None else None,"u":"%p","st":("상승(둔화)","hot") if (dU or 0)>=0.5 else ("안정","good"),"d":"12개월 저점 대비 실업률 상승폭(Sahm 룰 유사)."},
      {"k":"CFNAIMA3","label":"시카고 활동지수 (MA3)","group":"성장","v":round(cfnai,2) if cfnai is not None else None,"u":"","st":("위축","hot") if (cfnai or 0)<-0.7 else (("둔화","watch") if (cfnai or 0)<-0.35 else ("확장","good")),"d":"85개 지표 종합 실물활동. 0=추세성장·음(−)이면 둔화."},
      {"k":"SAHMREALTIME","label":"Sahm 침체룰 (실시간)","group":"성장","v":round(sahm,2) if sahm is not None else None,"u":"%p","st":stat_sahm(sahm),"d":"실업률 3M평균이 12M저점 +0.5%p면 침체 개시 신호."},
    ]

    # ── 히스토리(월별 레짐 라벨, ~15년) + 자산 조건부 성과 ──
    hist, asset_perf = build_history(S, cpi_yoy)

    out = {"as_of": asof, "source": "FRED (무료) + yfinance",
           "regime": {"label": lab, "emoji": emoji, "growth": growth, "inflation": inflation,
                      "financial": financial, "desc": desc, "strategy": strat},
           "indicators": indicators, "history": hist, "asset_perf": asset_perf}
    json.dump(out, open(OUT, "w"), ensure_ascii=False, separators=(",", ":"))
    print(f"→ {OUT} · 레짐 {emoji}{lab} (성장 {growth}·물가 {inflation}·금융 {financial}) · 기준일 {asof}")


def classify_month(g, i, fin):
    M = {("EXPANSION","LOW"):"Goldilocks",("EXPANSION","MODERATE"):"Recovery",("EXPANSION","HIGH"):"Overheating",
         ("SLOWING","LOW"):"SoftLanding",("SLOWING","MODERATE"):"LateCycle",("SLOWING","HIGH"):"Stagflation",
         ("CONTRACTION","LOW"):"Recession",("CONTRACTION","MODERATE"):"Recession",("CONTRACTION","HIGH"):"Stagflation"}
    return M[(g,i)]


def build_history(S, cpi_yoy):
    """월말 리샘플로 과거 레짐 라벨 시퀀스 + 레짐별 다음달 자산수익 평균."""
    idx = pd.date_range("2009-01-31", cpi_yoy.dropna().index.max(), freq="ME")
    nfp3 = S["PAYEMS"].diff().rolling(3).mean()
    dU = S["UNRATE"] - S["UNRATE"].rolling(12).min()
    def asof(s, d):
        s2 = s.dropna(); s2 = s2[s2.index <= d]; return float(s2.iloc[-1]) if len(s2) else None
    labels = {}
    for d in idx:
        nfp, cf, sh, du, cp = asof(nfp3,d), asof(S["CFNAIMA3"],d), asof(S["SAHMREALTIME"],d), asof(dU,d), asof(cpi_yoy,d)
        gs = 0
        if nfp is not None: gs += 1 if nfp>100 else (-1 if nfp<0 else 0)
        if cf is not None: gs += 1 if cf>-0.35 else (-1 if cf<-0.7 else 0)
        if sh is not None: gs += -1 if sh>=0.5 else 0
        if du is not None: gs += -1 if du>=0.5 else 0
        g = "EXPANSION" if gs>=1 else ("CONTRACTION" if gs<=-2 else "SLOWING")
        inf = "HIGH" if (cp or 0)>=4 else ("MODERATE" if (cp or 0)>=2.5 else "LOW")
        labels[d] = classify_month(g, inf, None)
    hist = [{"dt": d.date().isoformat(), "r": labels[d]} for d in idx]

    # 자산 조건부 성과: 각 자산 월수익을 레짐별 평균
    perf = {}
    try:
        px = yf.download(["SPY","QQQ","TLT","GLD","DBC"], start="2008-12-01", auto_adjust=True, progress=False)["Close"]
        mret = px.resample("ME").last().pct_change()*100
        lab_s = pd.Series(labels)
        for a in ["SPY","QQQ","TLT","GLD","DBC"]:
            if a not in mret: continue
            r = mret[a].reindex(lab_s.index)
            perf[a] = {rg: round(float(r[lab_s==rg].mean()), 2) for rg in set(labels.values()) if (lab_s==rg).sum()>=3 and pd.notna(r[lab_s==rg].mean())}
    except Exception as e:
        print("  자산성과 실패", e)
    return hist, perf


if __name__ == "__main__":
    main()
