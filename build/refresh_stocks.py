# -*- coding: utf-8 -*-
"""build/refresh_stocks.py — 종목 테크니컬 스냅샷 (클라우드 자체완결)
================================================================================
GitHub Actions 크론에서 실행. **DB·ta_lab 미의존** — data/members.json(종목리스트)를 읽고
yfinance 가격으로 표준 테크니컬 지표·교과서 매수/매도 신호를 직접 계산해 data/stocks.json 갱신.
지표 공식은 표준(공개), 신호는 교과서(공개 블로그 대표신호). 매 거래일 최신.
로컬 정본 빌더(strategy/stock_signals/build_snapshot.py, ta_lab 사용)와 출력 스키마 동일.
"""
from __future__ import annotations
import os, json, warnings
import numpy as np, pandas as pd
warnings.filterwarnings("ignore")
import yfinance as yf

HERE = os.path.dirname(os.path.abspath(__file__))
MEMBERS = os.path.join(HERE, "..", "data", "members.json")
OUT = os.path.join(HERE, "..", "data", "stocks.json")
PX_MONTHS = 37

FACTORS = {
  "rsi": ("RSI(14)", "과매수·과매도", "과매수"), "stoch": ("스토캐스틱 %K", "과매수·과매도", "과매수"),
  "mfi": ("MFI(자금흐름)", "과매수·과매도", "과매수"), "willr": ("Williams %R", "과매수·과매도", "과매수"),
  "cci": ("CCI(20)", "과매수·과매도", "과매수"), "pctb": ("볼린저 %b", "과매수·과매도", "밴드상단"),
  "pos52": ("52주 고점 위치", "과매수·과매도", "고점근접"), "adx": ("ADX(추세강도)", "추세", "강한추세"),
  "d50": ("50일선 이격도", "추세", "위"), "d200": ("200일선 이격도", "추세", "위"),
  "macdh": ("MACD 히스토그램", "추세", "상승"), "aroon": ("Aroon 오실레이터", "추세", "상승추세"),
  "roc1m": ("1개월 수익률", "모멘텀", "강세"), "roc3m": ("3개월 수익률", "모멘텀", "강세"),
  "roc6m": ("6개월 수익률", "모멘텀", "강세"), "rs3m": ("상대강도(3M, vs SPY)", "모멘텀", "시장대비강"),
  "atrp": ("ATR%(변동성)", "변동성", "고변동"), "vol": ("실현변동성(연율)", "변동성", "고변동"),
  "bbw": ("볼린저 밴드폭", "변동성", "확장"),
  "rvol": ("상대거래량(5일/60일)", "거래량", "급증"),
  "dtc": ("숏 커버일수(Days-to-Cover)", "포지셔닝", "과밀숏"),
  "sipct": ("공매도잔량 %(유동주식)", "포지셔닝", "과밀숏"),
}
COMPOSITES = {
  "overheat": ["+rsi", "+stoch", "+mfi", "+willr", "+pctb", "+pos52"],
  "trend": ["+adx", "+d50", "+d200", "+macdh", "+aroon"],
  "momentum": ["+roc1m", "+roc3m", "+roc6m", "+rs3m"], "volatility": ["+atrp", "+vol"],
  "positioning": ["+dtc"],
}


# ── 표준 지표(순수 pandas/numpy) ──
def ema(s, n): return s.ewm(span=n, adjust=False).mean()
def sma(s, n): return s.rolling(n).mean()
def rsi(c, n=14):
    d = c.diff(); up = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean(); dn = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100/(1 + up/dn.replace(0, np.nan))
def macd(c):
    line = ema(c, 12) - ema(c, 26); sig = ema(line, 9); return line, sig, line - sig
def boll(c, n=20, k=2):
    m = sma(c, n); sd = c.rolling(n).std(); up = m + k*sd; lo = m - k*sd
    return m, up, lo, (c - lo)/(up - lo).replace(0, np.nan), (up - lo)/m.replace(0, np.nan)*100
def stoch_k(h, l, c, n=14, sl=3):
    lo = l.rolling(n).min(); hi = h.rolling(n).max(); k = (c - lo)/(hi - lo).replace(0, np.nan)*100
    return k.rolling(sl).mean()
def atr(h, l, c, n=14):
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()
def adx(h, l, c, n=14):
    up = h.diff(); dn = -l.diff()
    pdm = np.where((up > dn) & (up > 0), up, 0.0); mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    a = atr(h, l, c, n); pdi = 100*pd.Series(pdm, index=h.index).ewm(alpha=1/n, adjust=False).mean()/a
    mdi = 100*pd.Series(mdm, index=h.index).ewm(alpha=1/n, adjust=False).mean()/a
    dx = 100*(pdi - mdi).abs()/(pdi + mdi).replace(0, np.nan); return dx.ewm(alpha=1/n, adjust=False).mean(), pdi, mdi
def cci(h, l, c, n=20):
    tp = (h + l + c)/3; m = tp.rolling(n).mean(); md = (tp - m).abs().rolling(n).mean()
    return (tp - m)/(0.015*md.replace(0, np.nan))
def willr(h, l, c, n=14):
    hi = h.rolling(n).max(); lo = l.rolling(n).min(); return -100*(hi - c)/(hi - lo).replace(0, np.nan)
def mfi(h, l, c, v, n=14):
    tp = (h + l + c)/3; mf = tp*v; pos = mf.where(tp > tp.shift(), 0.0).rolling(n).sum()
    neg = mf.where(tp < tp.shift(), 0.0).rolling(n).sum(); return 100 - 100/(1 + pos/neg.replace(0, np.nan))
def aroon(h, l, n=25):
    up = h.rolling(n+1).apply(lambda x: x.argmax()/n*100, raw=True); dn = l.rolling(n+1).apply(lambda x: x.argmin()/n*100, raw=True)
    return up, dn
def _f(x):
    try:
        if hasattr(x, "iloc"): x = x.iloc[-1]
        return float(x)
    except Exception: return np.nan


def indicators(o):
    h, l, c, v = o["High"], o["Low"], o["Close"], o["Volume"]
    c = c.dropna(); h = h.reindex(c.index); l = l.reindex(c.index); v = v.reindex(c.index)
    if len(c) < 200: return None
    s50, s200 = sma(c, 50), sma(c, 200); ml, ms, mh = macd(c); _, bu, bl, pb, bw = boll(c)
    au, ad = aroon(h, l); ax, _, _ = adx(h, l, c); ret = c.pct_change()
    def rr(n): return _f(c.iloc[-1]/c.iloc[-1-n]-1) if len(c) > n else np.nan
    return {
        "rsi": _f(rsi(c)), "stoch": _f(stoch_k(h, l, c)), "mfi": _f(mfi(h, l, c, v)),
        "willr": _f(willr(h, l, c))+100, "cci": _f(cci(h, l, c)), "pctb": _f(pb),
        "pos52": _f(c.iloc[-1]/c.tail(252).max()*100), "adx": _f(ax),
        "d50": _f((c.iloc[-1]/s50.iloc[-1]-1)*100), "d200": _f((c.iloc[-1]/s200.iloc[-1]-1)*100),
        "macdh": _f(mh), "aroon": _f(au)-_f(ad),
        "roc1m": rr(21)*100, "roc3m": rr(63)*100, "roc6m": rr(126)*100,
        "atrp": _f(atr(h, l, c)/c.iloc[-1]*100), "vol": _f(ret.tail(252).std()*np.sqrt(252)*100), "bbw": _f(bw),
        "rvol": (_f(v.tail(5).mean()/v.tail(60).mean()) if float(v.tail(60).mean() or 0) > 0 else np.nan),
    }, c


def signals_fired(o):
    """최근 5거래일 발동한 교과서 매수/매도 이벤트 신호."""
    h, l, c, v = o["High"].dropna(), o["Low"].dropna(), o["Close"].dropna(), o["Volume"].dropna()
    idx = c.index; h = h.reindex(idx); l = l.reindex(idx); v = v.reindex(idx)
    if len(c) < 210: return [], []
    ml, ms, mh = macd(c); _, bu, bl, pb, _ = boll(c); k = stoch_k(h, l, c); kd = k.rolling(3).mean()
    s50, s200 = sma(c, 50), sma(c, 200); au, ad = aroon(h, l)
    dcu = h.rolling(20).max(); dcl = l.rolling(20).min(); z = pd.Series(0.0, index=idx)
    cu = lambda a, b: (a > b) & (a.shift(1) <= b.shift(1)); cd = lambda a, b: (a < b) & (a.shift(1) >= b.shift(1))
    B = {"RSI<30": rsi(c) < 30, "Stoch %K<20 골든": cu(k, kd) & (k < 25), "CCI<-100": cci(h, l, c) < -100,
         "Williams<-80": willr(h, l, c) < -80, "MFI<20": mfi(h, l, c, v) < 20, "BB %b<0": pb < 0,
         "골든크로스 50/200": cu(s50, s200), "MACD 골든(0선아래)": cu(ml, ms) & (ml < 0), "MACD 0선 상향": cu(ml, z),
         "Donchian20 돌파": c >= dcu.shift(1), "Aroon 업 교차": cu(au, ad)}
    S = {"RSI>70": rsi(c) > 70, "Stoch %K>80 데드": cd(k, kd) & (k > 75), "CCI>+100": cci(h, l, c) > 100,
         "Williams>-20": willr(h, l, c) > -20, "MFI>80": mfi(h, l, c, v) > 80, "BB %b>1": pb > 1,
         "데드크로스 50/200": cd(s50, s200), "MACD 데드(0선위)": cd(ml, ms) & (ml > 0), "MACD 0선 하향": cd(ml, z),
         "Donchian20 이탈": c <= dcl.shift(1), "Aroon 다운 교차": cd(au, ad)}
    buy = [n for n, s in B.items() if bool(s.tail(5).any())]; sell = [n for n, s in S.items() if bool(s.tail(5).any())]
    b_day = pd.DataFrame(B).astype(int).sum(axis=1); s_day = pd.DataFrame(S).astype(int).sum(axis=1)
    return buy, sell, b_day, s_day


def zigzag(a, rsi_a, theta):
    """퍼센트 임계 지그재그 → 확정 스윙 전환점 [[idx,'H'/'L'],...] + 다이버전스('bull'/'bear'/None).
    theta = 반전 임계(소수). a=윈도 종가배열, rsi_a=동일길이 RSI."""
    a = np.asarray(a, float); n = len(a)
    if n < 20 or np.isnan(a).any(): return [], None
    piv = []; direction = 0; hi = lo = a[0]; hii = loi = 0
    for i in range(1, n):
        if direction >= 0:
            if a[i] > hi: hi, hii = a[i], i
            elif a[i] <= hi * (1 - theta):
                piv.append([hii, "H"]); direction = -1; lo, loi = a[i], i; continue
        if direction <= 0:
            if a[i] < lo: lo, loi = a[i], i
            elif a[i] >= lo * (1 + theta):
                piv.append([loi, "L"]); direction = 1; hi, hii = a[i], i
    tail = [hii, "H"] if direction >= 0 else [loi, "L"]   # 미확정 최근 극점
    if not piv or piv[-1][0] != tail[0]: piv.append(tail)
    dvg = None
    lows = [i for i, t in piv if t == "L"]; highs = [i for i, t in piv if t == "H"]
    if len(lows) >= 2 and a[lows[-1]] < a[lows[-2]] and rsi_a[lows[-1]] > rsi_a[lows[-2]] + 2: dvg = "bull"
    if len(highs) >= 2 and a[highs[-1]] > a[highs[-2]] and rsi_a[highs[-1]] < rsi_a[highs[-2]] - 2:
        dvg = "bear" if dvg is None else dvg
    return piv, dvg


def flags(s):
    f = []
    if s.get("rsi", 50) >= 70 or s.get("pctb", .5) >= .95: f.append("과매수")
    if s.get("rsi", 50) <= 30 or s.get("pctb", .5) <= .05: f.append("과매도")
    up = s.get("d50", -1) > 0 and s.get("d200", -1) > 0 and s.get("adx", 0) >= 20
    if up: f.append("상승추세")
    if up and s.get("rsi", 50) < 45 and s.get("pctb", 1) < .3: f.append("눌림목")
    if s.get("pos52", 0) >= 92 and s.get("macdh", -1) > 0: f.append("52주돌파")
    if s.get("d200", 1) < 0: f.append("200일이탈")
    if s.get("dtc", 0) >= 7 or s.get("sipct", 0) >= 15: f.append("과밀숏")
    return f


def fetch_fund(t):
    """yfinance .info에서 trailing/forward EPS·PE·유동주식수 (무료). 실패시 None."""
    try:
        info = yf.Ticker(t).info
        return t, {"teps": info.get("trailingEps"), "feps": info.get("forwardEps"),
                   "tpe": info.get("trailingPE"), "fpe": info.get("forwardPE"),
                   "float": info.get("floatShares") or info.get("sharesOutstanding")}
    except Exception:
        return t, None


def fetch_short_interest():
    """FINRA 최신 공매도잔량(무료·격주). 반환 {ticker:{dtc,sish,chg}} + _asof."""
    import urllib.request, datetime as _dt
    URL = "https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest"
    def q(date):
        # 페이지네이션(API는 요청당 5000행 상한) — 전 종목 수집
        rows = []; offset = 0
        while True:
            body = {"limit": 5000, "offset": offset,
                    "compareFilters": [{"fieldName": "settlementDate", "fieldValue": date, "compareType": "equal"}]}
            req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "Accept": "application/json"})
            page = json.loads(urllib.request.urlopen(req, timeout=45).read())
            if not page: break
            rows.extend(page)
            if len(page) < 5000: break
            offset += 5000
        return rows
    # 후보 settlement date: 최근 3개월 15일·말일(주말→직전 영업일), 발표지연 10일+
    today = _dt.date.today(); cands = set()
    for mo in range(4):
        y, m = today.year, today.month - mo
        while m <= 0: m += 12; y -= 1
        d15 = _dt.date(y, m, 15)
        nm = _dt.date(y + 1, 1, 1) if m == 12 else _dt.date(y, m + 1, 1)
        dlast = nm - _dt.timedelta(days=1)
        for d in (d15, dlast):
            while d.weekday() >= 5: d -= _dt.timedelta(days=1)
            if (today - d).days >= 10: cands.add(d)
    out = {}
    for d in sorted(cands, reverse=True):
        try:
            rows = q(d.isoformat())
            if not rows: continue
            for x in rows:
                sym = x.get("symbolCode"); dtc = x.get("daysToCoverQuantity"); sish = x.get("currentShortPositionQuantity")
                if not sym: continue
                prev = x.get("previousShortPositionQuantity") or 0
                chg = ((sish - prev) / prev * 100) if prev else None
                out[sym] = {"dtc": dtc, "sish": sish, "chg": chg}
            out["_asof"] = d.isoformat()
            print(f"공매도잔량(FINRA) {len(out)-1}종목 · settlement {d.isoformat()}")
            return out
        except Exception as e:
            print(f"  SI {d} 실패 {str(e)[:40]}"); continue
    return out


def main():
    M = json.load(open(MEMBERS)); mem = M["members"]; tickers = sorted(mem.keys())
    print(f"멤버 {len(tickers)}종목 · yfinance…")
    px = {}
    allt = tickers + ["SPY"]
    for i in range(0, len(allt), 120):
        ch = allt[i:i+120]
        df = yf.download(ch, period="3y", auto_adjust=True, progress=False, group_by="ticker", threads=True)
        for t in ch:
            try:
                sub = (df[t] if len(ch) > 1 else df).dropna(how="all")
                if len(sub) > 200: px[t] = sub
            except Exception: pass
    spy = px.get("SPY", {}).get("Close") if "SPY" in px else None
    spy3 = _f(spy.iloc[-1]/spy.iloc[-1-63]-1) if spy is not None and len(spy) > 63 else 0.0
    raw = {}; as_of = None
    for t in tickers:
        if t not in px: continue
        r = indicators(px[t])
        if r is None: continue
        sig, c = r; sig["rs3m"] = (sig["roc3m"]/100 - spy3)*100 if sig.get("roc3m") == sig.get("roc3m") else np.nan
        buy, sell, b_day, s_day = signals_fired(px[t])
        nb, ns = len(buy), len(sell)
        tstate = ("매수" if nb >= 4 else "매수우세") if (nb > ns and nb >= 2) else ("매도" if ns >= 4 else "매도우세") if (ns > nb and ns >= 2) else "중립"
        raw[t] = {"sig": sig, "close": c, "buy": buy[:8], "sell": sell[:8], "timing": tstate, "b_day": b_day, "s_day": s_day}
        d = c.index.max()
        if as_of is None or d > pd.Timestamp(as_of): as_of = str(pd.Timestamp(d).date())
    print(f"지표 {len(raw)}종목 · 기준일 {as_of}")
    # 펀더멘털(trailing/forward EPS·PE) — yfinance .info 병렬 수집
    from concurrent.futures import ThreadPoolExecutor
    fund = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for t, f in ex.map(fetch_fund, list(raw.keys())):
            if f: fund[t] = f
    print(f"펀더멘털 {len(fund)}종목")
    # 공매도 포지셔닝(FINRA) → 지표에 주입(dtc·sipct)
    short = fetch_short_interest(); si_asof = short.get("_asof")
    for t in raw:
        s = short.get(t)
        if not s: continue
        if s.get("dtc") is not None: raw[t]["sig"]["dtc"] = float(s["dtc"])
        fl = (fund.get(t) or {}).get("float")
        if s.get("sish") and fl: raw[t]["sig"]["sipct"] = float(s["sish"]) / float(fl) * 100
    vdf = pd.DataFrame({t: raw[t]["sig"] for t in raw}).T; pct = vdf.rank(pct=True)*100
    # 일별 종가 패널 (최근 252거래일 ≈ 1년) — 기간선택(1주~1년) 슬라이스용
    daily = pd.DataFrame({t: raw[t]["close"] for t in raw}).sort_index().tail(252)
    pxd_dates = [d.strftime("%Y-%m-%d") for d in daily.index]
    def comp(spec, rp):
        vs = [rp[s[1:]] if s[0] == "+" else 100 - rp[s[1:]] for s in spec if s[1:] in rp and pd.notna(rp[s[1:]])]
        return round(float(np.mean(vs)), 0) if vs else None
    stocks = []
    for t in raw:
        sg, rp = raw[t]["sig"], pct.loc[t]
        sig = {k: {"v": round(float(sg[k]), 2), "pct": round(float(rp[k]), 0), "dt": (si_asof if k in ("dtc", "sipct") else as_of)} for k in FACTORS if k in sg and pd.notna(sg[k])}
        comps = {c2: comp(spec, rp) for c2, spec in COMPOSITES.items()}
        dser = daily[t] if t in daily.columns else None
        pxd = [None if dser is None or pd.isna(x) else round(float(x), 2) for x in (dser if dser is not None else [None]*len(pxd_dates))]
        vser = px[t]["Volume"].reindex(daily.index) if t in px else None
        vd = [None if vser is None or pd.isna(x) else int(round(float(x)/1e6)) for x in (vser if vser is not None else [None]*len(pxd_dates))]  # 백만주 단위
        # 스윙 전환점(지그재그) — 확정 고점/저점 구조 + 다이버전스
        tp = None
        if dser is not None and not dser.isna().any() and len(dser) >= 40:
            wr = rsi(raw[t]["close"]).reindex(daily.index).values
            theta = min(0.30, max(0.12, 6.0 * (sg.get("atrp") or 5) / 100))   # 감도↓↓ = 주요 전환점 3~4개만
            piv, dvg = zigzag(dser.values, wr, theta)
            if len(piv) >= 2: tp = {"zz": piv, "dvg": dvg}
        # 매수/매도 타점(마커) — 일별 순매수/순매도 강신호(≥2, 우세)일을 표시
        bd = raw[t]["b_day"].reindex(daily.index).fillna(0); sd = raw[t]["s_day"].reindex(daily.index).fillna(0)
        bd = bd.to_numpy(); sd = sd.to_numpy()
        braw = set(i for i in range(len(pxd_dates)) if bd[i] >= 2 and bd[i] > sd[i])
        sraw = set(i for i in range(len(pxd_dates)) if sd[i] >= 2 and sd[i] > bd[i])
        bstart = [i for i in sorted(braw) if (i - 1) not in braw]    # 클러스터 시작(진입)일
        sstart = [i for i in sorted(sraw) if (i - 1) not in sraw]
        # 강도: 발동 신호 3개+ = 강, 2개 = 약
        bms = [i for i in bstart if bd[i] >= 3]; bmw = [i for i in bstart if bd[i] < 3]
        sms = [i for i in sstart if sd[i] >= 3]; smw = [i for i in sstart if sd[i] < 3]
        info = mem.get(t, {})
        fd = fund.get(t) or {}
        def r2(x): return round(float(x), 2) if x is not None and x == x else None
        fnd = {"teps": r2(fd.get("teps")), "feps": r2(fd.get("feps")), "tpe": r2(fd.get("tpe")), "fpe": r2(fd.get("fpe"))}
        if fnd["teps"] and fnd["feps"] and fnd["teps"] != 0:
            fnd["gr"] = round((fnd["feps"]/fnd["teps"] - 1)*100, 1)   # 12M 선행 EPS 성장률(%)
        stocks.append({"t": t, "name": info.get("name"), "sector": info.get("sector"), "idx": info.get("idx", []),
                       "comp": {k: v for k, v in comps.items() if v is not None}, "flags": flags(sg),
                       "timing": raw[t]["timing"], "buy": raw[t]["buy"], "sell": raw[t]["sell"],
                       "bms": bms, "bmw": bmw, "sms": sms, "smw": smw, "sig": sig,
                       "fund": {k: v for k, v in fnd.items() if v is not None}, "pxd": pxd, "vd": vd,
                       **({"tp": tp} if tp else {})})
    stocks.sort(key=lambda s: -(s["comp"].get("momentum") or 0))
    out = {"as_of": as_of, "source": "yfinance + 표준 테크니컬 (cloud)", "n_stocks": len(stocks), "pxd_dates": pxd_dates,
           "factor_defs": {k: {"label": FACTORS[k][0], "group": FACTORS[k][1], "hi": FACTORS[k][2], "as_of": (si_asof if k in ("dtc", "sipct") else as_of)} for k in FACTORS},
           "fund_defs": {"teps": "주당순이익 TTM (최근 12개월 실적)", "feps": "선행 EPS (향후 12개월 애널리스트 추정)",
                         "gr": "선행 EPS 성장률 (forward/trailing−1, %)", "tpe": "P/E (TTM)", "fpe": "선행 P/E (forward)"},
           "composite_defs": {"overheat": "과열도 — RSI·스토캐스틱·MFI·Williams·%b·52주", "trend": "추세강도 — ADX·이동평균·MACD·Aroon",
                              "momentum": "모멘텀 — 1/3/6M 수익률·상대강도", "volatility": "변동성 — ATR%·실현변동성",
                              "positioning": "포지셔닝 — 공매도 커버일수(격주 FINRA)"},
           "flag_defs": {"과매수": "RSI≥70 또는 %b≥0.95", "과매도": "RSI≤30 또는 %b≤0.05", "상승추세": "종가>50일선>200일선 & ADX≥20",
                         "눌림목": "상승추세 중 RSI<45 & %b<0.3", "52주돌파": "52주고점 92%↑ & MACD 상승", "200일이탈": "종가<200일선",
                         "과밀숏": "숏 커버일수≥7 또는 공매도잔량≥유동주식 15%"},
           "stocks": stocks}
    json.dump(out, open(OUT, "w"), ensure_ascii=False, separators=(",", ":"))
    print(f"→ {OUT} ({len(stocks)}종목 · {os.path.getsize(OUT)//1024}KB · 기준일 {as_of})")


if __name__ == "__main__":
    main()
