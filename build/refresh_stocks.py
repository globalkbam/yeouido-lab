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
TPHIST = os.path.join(HERE, "..", "data", "target_history.json")   # 애널리스트 목표주가 스냅샷 이력(직접 누적)
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


def overheat_series(h, l, c, v):
    """봉별 과열도 0~100 (오실레이터 평균: RSI·스토캐스틱·MFI·Williams·볼린저%b). 낮음=과매도(저점 근처)·높음=과매수(고점 근처)."""
    r = rsi(c); k = stoch_k(h, l, c); m = mfi(h, l, c, v); w = willr(h, l, c) + 100
    _, _, _, pb, _ = boll(c); pbc = (pb.clip(0, 1) * 100)
    return pd.concat([r, k, m, w, pbc], axis=1).mean(axis=1)


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
    if len(c) < 210: return [], [], None, None   # 호출부가 4개로 언패킹 — 상장 1년 미만 종목 편입 시 잡 전체가 죽던 버그
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


def trend_signals(o, K=3, trig_win=7):
    """추세정렬 매매 신호(재설계 2026-07) — 역추세 매매 폐기.
    핵심: 상승추세(종가>200MA)에서만 매수, 하락추세(종가<200MA)에서만 매도(약세 주의).
      · 하락추세 과매도를 함부로 매수(떨어지는 칼)하지 않고, 상승추세 과열을 함부로 매도하지 않음.
      · 매수 트리거 = 200MA 회복·골든크로스·MACD 상향돌파·상승추세 눌림저점·저RSI 반등(하락→상승 전환).
      · 매도 트리거 = 200MA 이탈·데드크로스·MACD 하향·하락추세 과열 되돌림.
    반환 BUY/SELL = 차트 마커(확정 피봇저점·골든 / 반등고점·데드), tstate = timing, B = 스코어링용 불리언.
    백테스트(512종목·5y·주간): 롱숏 스프레드 −0.4%→+2.9%, 하락장 꼬리 −24%→−15%(떨어지는 칼 회피),
      매수 top6 fwd20 초과수익 +1.2%→+3.3%. ⚠ 참고용(배포 알파 주장 아님) — 매도는 '약세/비중축소 주의'."""
    h = o["High"].dropna(); l = o["Low"].dropna(); c = o["Close"].dropna()
    idx = c.index; h = h.reindex(idx); l = l.reindex(idx)
    z = pd.Series(False, index=idx)
    if len(c) < 260: return z, z, "중립", {"up": False}
    s50, s200 = sma(c, 50), sma(c, 200); _, _, mh = macd(c); r = rsi(c); ax, _, _ = adx(h, l, c)
    up = c > s200
    reclaim = up & (c.shift(10) < s200.shift(10)); lose = (~up) & (c.shift(10) >= s200.shift(10))
    golden = (s50 > s200) & (s50.shift(20) <= s200.shift(20)); death = (s50 < s200) & (s50.shift(20) >= s200.shift(20))
    macd_bull = (mh > 0) & (mh.shift(5) <= 0); macd_bear = (mh < 0) & (mh.shift(5) >= 0)
    adx_up = ax > ax.shift(10); rsi_up = r > r.shift(3)
    win = 2*K + 1   # 확정형 중심 피봇(timing 트리거용) — 마커엔 미사용
    piv_lo = (c == c.rolling(win, center=True, min_periods=win).min())
    piv_hi = (c == c.rolling(win, center=True, min_periods=win).max())
    # 차트 매수/매도 타점(▲▼) = 추세 전환 이벤트만(당일 크로스) — 희소·유의미. 잔파동 눌림/반등엔 안 찍음.
    xup200 = up & (c.shift(1) <= s200.shift(1))              # 200MA 상향 돌파(당일)
    xdn200 = (~up) & (c.shift(1) >= s200.shift(1))           # 200MA 하향 이탈(당일)
    gcross = (s50 > s200) & (s50.shift(1) <= s200.shift(1))  # 골든크로스(50/200, 당일)
    dcross = (s50 < s200) & (s50.shift(1) >= s200.shift(1))  # 데드크로스(당일)
    BUY = xup200 | gcross      # 매수 타점 = 200MA 돌파 · 골든크로스 (추세 진입)
    SELL = xdn200 | dcross     # 매도 타점 = 200MA 이탈 · 데드크로스 (추세 이탈)
    buy_trig = bool((reclaim | golden | macd_bull | (piv_lo & up) | (rsi_up & (r < 45))).tail(trig_win).any())
    sell_trig = bool(((lose | death | macd_bear | (r > 60)) & (~up)).tail(trig_win).any())
    up_now = bool(up.iloc[-1]); d200 = _f(c.iloc[-1]/s200.iloc[-1] - 1)*100; adx_now = _f(ax.iloc[-1])
    if abs(d200) < 2 and (adx_now != adx_now or adx_now < 18): tstate = "중립"   # 200MA 부근 저추세 = 방향성 없음
    elif up_now: tstate = "매수" if buy_trig else "매수우세"
    else: tstate = "매도" if sell_trig else "매도우세"
    B = {k: bool(s.iloc[-1]) for k, s in [("reclaim", reclaim), ("golden", golden), ("macd_bull", macd_bull),
         ("adx_up", adx_up), ("lose", lose), ("death", death), ("macd_bear", macd_bear)]}
    B["up"] = up_now
    return BUY, SELL, tstate, B


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
    # 깊은눌림 = 장기 상승추세(200MA 위)인데 단기 조정으로 50일선 아래까지 밀린 과매도 상태.
    #   '눌림목'은 50MA 위만 봐서 얕은 되돌림만 잡고, 이렇게 깊게 눌린 종목은 어떤 화면에도 안 걸렸다(MU·SNDK 사례).
    #   ⚠ 상태 표시일 뿐 매매 신호가 아니다 — 이 프로필의 매매 엣지는 41번째 기각(강세추세 과매도 스냅백)으로 이미 부정됐다.
    if s.get("d200", -1) > 0 and s.get("d50", 1) < 0 and s.get("rsi", 50) < 45: f.append("깊은눌림")
    if s.get("pos52", 0) >= 92 and s.get("macdh", -1) > 0: f.append("52주돌파")
    if s.get("d200", 1) < 0: f.append("200일이탈")
    if s.get("dtc", 0) >= 7 or s.get("sipct", 0) >= 15: f.append("과밀숏")
    return f


def fetch_fund(t):
    """yfinance .info에서 trailing/forward EPS·PE·유동주식수 + 애널리스트 목표주가 (무료). 실패시 None.
    ⚠ 목표주가는 **같은 .info 응답에서 꺼내므로 추가 API 호출 0**. 새로 Ticker를 만들지 말 것.
    ⚠⚠ 목표주가/상승여력은 **표기 전용**이다. bscore·sscore·flags·timing 어디에도 넣지 않는다.
        검증(512종목 전수 + 애널리스트 이벤트 168,523건): 상승여력 단면분산의 64%가 최근 주가경로만으로 설명되고
        (목표가 갱신 시차 ≈1~3개월), 우리 매수점수와 스피어만 −0.71, 6M 모멘텀과 −0.60(55개월 100% 음수).
        20/60일 예측력 없음(Q5−Q1 60일 +0.83%, t=0.89). 상승여력 상위20은 90%가 200일선 아래 = 떨어지는 칼."""
    try:
        info = yf.Ticker(t).info
        return t, {"teps": info.get("trailingEps"), "feps": info.get("forwardEps"),
                   "tpe": info.get("trailingPE"), "fpe": info.get("forwardPE"),
                   "float": info.get("floatShares") or info.get("sharesOutstanding"),
                   # ── 애널리스트 목표주가(참고 표기용, 신호 아님) ──
                   "tpm": info.get("targetMeanPrice"), "tph": info.get("targetHighPrice"),
                   "tpl": info.get("targetLowPrice"), "nan": info.get("numberOfAnalystOpinions"),
                   "rk": info.get("recommendationKey")}
    except Exception:
        return t, None


# ── 목표주가 이력 누적 ──────────────────────────────────────────────────────
# 검증에서 확인: 컨센서스 목표가의 포인트-인-타임 스냅샷은 무료로 소급 취득이 불가능하다
# (yfinance .info는 현재값만, upgrades_downgrades는 '액션을 낸 증권사'만 남는 재구성이라 컨센서스가 아니다).
# → 미래 forward 검증을 하려면 오늘부터 우리가 직접 쌓는 수밖에 없다. 이 파일이 그 자산이다.
# 정책: 주 1회(HIST_MIN_DAYS) 또는 목표가가 유의미하게 바뀐 종목이 일정 비율 이상일 때만 스냅샷 추가.
#       최대 HIST_MAX_SNAPS개 유지(오래된 것부터 폐기) — 파일 무한 증식 방지.
HIST_MIN_DAYS = 7        # 최소 기록 간격(일)
HIST_CHG_PCT = 2.0       # '변화'로 볼 목표가 변동폭(%)
HIST_CHG_FRAC = 0.10     # 변화 종목 비중이 이 이상이면 간격 전이라도 기록
HIST_MAX_SNAPS = 210     # 스냅샷 상한(주1회 ≈ 4년). 초과분은 오래된 것부터 제거. 512종목 기준 스냅 1개≈6KB → 파일 상한 ≈1.3MB


def update_target_history(path, as_of, tp_now):
    """data/target_history.json 에 {as_of, 티커별 목표평균} 스냅샷을 append. 반환 (기록여부, 사유, 스냅샷수)."""
    import datetime as _dt
    doc = {"schema": 1,
           "note": "애널리스트 컨센서스 목표주가(targetMeanPrice) 스냅샷 이력. 표기·향후 검증 전용이며 신호로 사용하지 않음. "
                   "주 1회 또는 목표가 변동 종목 비중 10%↑일 때 기록.",
           "snaps": []}
    try:
        old = json.load(open(path, encoding="utf-8"))
        if isinstance(old, dict) and isinstance(old.get("snaps"), list):
            doc["snaps"] = old["snaps"]
    except Exception:
        pass   # 파일 없음/손상 → 새로 시작(빌드는 절대 중단하지 않는다)
    if not tp_now:
        return False, "목표주가 데이터 없음", len(doc["snaps"])
    snaps = doc["snaps"]
    if snaps:
        last = snaps[-1]
        if last.get("d") == as_of:
            snaps.pop()   # 같은 기준일 재실행 → 덮어쓰기(중복 방지)
        else:
            try:
                d0 = _dt.date.fromisoformat(last.get("d", ""))
                gap = (_dt.date.fromisoformat(as_of) - d0).days
            except Exception:
                gap = 10**6
            prev = last.get("tp") or {}
            common = [k for k in tp_now if k in prev and prev[k]]
            chg = sum(1 for k in common if abs(tp_now[k] / prev[k] - 1) * 100 >= HIST_CHG_PCT)
            frac = (chg / len(common)) if common else 1.0
            if gap < HIST_MIN_DAYS and frac < HIST_CHG_FRAC:
                return False, f"직전 기록 {gap}일 전 · 변동종목 {frac*100:.0f}% — 기록 생략", len(snaps)
    snaps.append({"d": as_of, "n": len(tp_now), "tp": tp_now})
    if len(snaps) > HIST_MAX_SNAPS:
        del snaps[:len(snaps) - HIST_MAX_SNAPS]
    doc["snaps"] = snaps
    json.dump(doc, open(path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    return True, "기록", len(snaps)


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
    # ★ 미확정 당일 봉 제거 — 랩 최우선 규칙 '기준일 통일'. 장중 실행 시 yfinance 마지막 봉이 실시간이라
    #   종가·지표·목표주가 상승여력이 확정 전 값으로 계산되고, 사이트 다른 데이터(regime·sentiment)와 기준일이 갈린다.
    #   미 동부 16:15 이전이면 당일 봉은 미확정으로 보고 전 종목에서 버린다(크론은 마감 후라 영향 없음).
    try:
        from zoneinfo import ZoneInfo
        _et = pd.Timestamp.now(tz=ZoneInfo("America/New_York"))
        if _et.hour*60 + _et.minute < 16*60 + 15:
            _today = pd.Timestamp(_et.date())
            _cut = 0
            for _t in list(px):
                _df = px[_t]
                if len(_df) and _df.index[-1].normalize() == _today:
                    px[_t] = _df.iloc[:-1]; _cut += 1
            if _cut: print(f"  미확정 당일 봉({_today.date()}) 제외 {_cut}종목 — 기준일 통일(미 동부 {_et:%H:%M} 장중)")
    except Exception as e:
        print("  당일 봉 판정 생략:", e)

    spy = px.get("SPY", {}).get("Close") if "SPY" in px else None
    # ⚠ SPY가 없으면 rs3m이 'vs SPY'가 아니라 절대수익률이 되고 국면도 무조건 리스크온으로 고정된다.
    #   라벨은 그대로라 화면상 구분이 불가능하므로, 조용히 넘어가지 말고 중단한다(이전본 유지).
    if spy is None or len(spy) <= 200:
        raise SystemExit("SPY 로드 실패 — 상대강도·시장국면 계산 불가, 갱신 중단(이전본 유지)")
    spy3 = _f(spy.iloc[-1]/spy.iloc[-1-63]-1)
    # 시장 국면: SPY가 200MA 위=리스크온(눌림매수 관대·매도 엄격), 아래=리스크오프(매수 엄격·매도 관대)
    spy_riskon = bool(spy.iloc[-1] > sma(spy, 200).iloc[-1])
    raw = {}; as_of = None
    for t in tickers:
        if t not in px: continue
        r = indicators(px[t])
        if r is None: continue
        sig, c = r; sig["rs3m"] = (sig["roc3m"]/100 - spy3)*100 if sig.get("roc3m") == sig.get("roc3m") else np.nan
        buy, sell, b_day, s_day = signals_fired(px[t])   # 이벤트 태그(상세패널)·유지
        BUYs, SELLs, tstate, tb = trend_signals(px[t])    # 추세정렬 신호(마커·timing·스코어 불리언)
        raw[t] = {"sig": sig, "close": c, "buy": buy[:8], "sell": sell[:8], "timing": tstate,
                  "b_day": b_day, "s_day": s_day, "BUY": BUYs, "SELL": SELLs, "tb": tb}
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
    # ⚠ 아래 두 소스는 개별 실패를 예외로 삼키므로, 전량 실패해도 잡은 '성공'으로 끝나고 화면의 패널만 조용히 사라진다.
    #   커버리지가 절반 미만이면 중단해 이전본을 유지하고 워크플로를 빨간불로 알린다.
    if len(fund) < len(raw) * 0.5:
        raise SystemExit(f"펀더멘털(EPS·PER) 수집 {len(fund)}/{len(raw)} — 절반 미만, 갱신 중단(이전본 유지)")
    # 공매도 포지셔닝(FINRA) → 지표에 주입(dtc·sipct)
    short = fetch_short_interest(); si_asof = short.get("_asof")
    if not si_asof or sum(1 for t in raw if short.get(t)) < len(raw) * 0.3:
        raise SystemExit("FINRA 공매도잔량 수집 실패(커버 30% 미만) — 갱신 중단(이전본 유지)")
    for t in raw:
        s = short.get(t)
        if not s: continue
        if s.get("dtc") is not None: raw[t]["sig"]["dtc"] = float(s["dtc"])
        fl = (fund.get(t) or {}).get("float")
        if s.get("sish") and fl: raw[t]["sig"]["sipct"] = float(s["sish"]) / float(fl) * 100
    vdf = pd.DataFrame({t: raw[t]["sig"] for t in raw}).T; pct = vdf.rank(pct=True)*100
    # ── 매수/매도 랭킹 스코어 = 과열도·추세·모멘텀·변동성 조합 + 섹터 상대(크로스섹션) — index 상위 랭킹용 ──
    def cser(keys):
        cols = [k for k in keys if k in pct.columns]
        return pct[cols].mean(axis=1).reindex(vdf.index).fillna(50.0) if cols else pd.Series(50.0, index=vdf.index)
    oh_c = cser(["rsi", "stoch", "mfi", "willr", "pctb", "pos52"])   # 과열도(↑=과매수)
    tr_c = cser(["adx", "d50", "d200", "macdh", "aroon"])            # 추세강도
    mo_c = cser(["roc1m", "roc3m", "roc6m", "rs3m"])                 # 모멘텀
    vo_c = cser(["atrp", "vol"])                                     # 변동성
    sect = pd.Series({t: (mem.get(t, {}) or {}).get("sector") or "?" for t in vdf.index}, index=vdf.index)
    oh_rel = (oh_c - oh_c.groupby(sect).transform("median") + 50.0).clip(0, 100)   # 섹터 상대 과열도(피어 대비)
    # 매수 점수 = 추세+모멘텀 (저과열은 점수 아닌 '필터'로만 — 저과열 가중이 수익을 깎는 것을 실측으로 확인).
    #   그리드 검증(5y 주간): 상승추세∩과열≤60 + tr+mo top8 → ex-SPY20 +2.72%p·hit 56.2%·중앙 roc3m 25%(추격 아님)
    #   vs 구식(저과열 가중+과열≤45) +1.62%p — 상승추세 평균(+1.84)보다도 낮았음.
    buy_score = tr_c + mo_c
    sell_score = oh_rel + 0.6*(100 - tr_c) + 0.6*(100 - mo_c) + 0.3*vo_c
    # 일별 종가 패널 (최근 252거래일 ≈ 1년) — 기간선택(1주~1년) 슬라이스용
    daily = pd.DataFrame({t: raw[t]["close"] for t in raw}).sort_index().tail(252)
    pxd_dates = [d.strftime("%Y-%m-%d") for d in daily.index]
    def comp(spec, rp):
        vs = [rp[s[1:]] if s[0] == "+" else 100 - rp[s[1:]] for s in spec if s[1:] in rp and pd.notna(rp[s[1:]])]
        return round(float(np.mean(vs)), 0) if vs else None
    stocks = []
    tp_hist = {}      # {티커: 목표평균} — data/target_history.json 누적용(오늘부터 쌓아야 미래 검증 가능)
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
            theta = min(0.16, max(0.05, 3.0 * (sg.get("atrp") or 5) / 100))   # 감도 상향(2026-07): 전환점 6~10개
            # 과거엔 12~30%라 추세 위 눌림목에서 타점이 아예 안 잡혔다. 품질은 아래 필터(칼·과열)로 유지.
            piv, dvg = zigzag(dser.values, wr, theta)
            if len(piv) >= 2: tp = {"zz": piv, "dvg": dvg}
        # 매수/매도 타점(마커) — 스윙 저점≈매수·고점≈매도(지그재그 전환점), 과열도·추세·모멘텀·변동성으로 필터 + 국면 조정.
        #   저점 매수: 깊은 붕괴(200MA −KNIFE 아래 & 6M<−15%)·아직 과매수(oh>65)면 제외 → 떨어지는 칼 회피.
        #   고점 매도: 강세 지속(상승추세 & 모멘텀↑ & oh<70)·아직 과매도(oh<35)면 제외 → 강세종목 조기매도 회피.
        bms, sms = [], []
        bmw, smw = [], []      # 잠정(미확정 꼬리) 타점 — 진행 중 극점, 날짜 이동 가능. 차트에 '빈 도형'으로 표시
        bmr, smr = {}, {}      # 타점별 근거 문자열 {인덱스: "사유"}
        whyb = None
        if dser is not None and t in px:
            cf = raw[t]["close"]; hf = px[t]["High"].reindex(cf.index); lf = px[t]["Low"].reindex(cf.index); vf = px[t]["Volume"].reindex(cf.index)
            ohb = overheat_series(hf, lf, cf, vf).reindex(daily.index)
            s2b = sma(cf, 200).reindex(daily.index); s5b = sma(cf, 50).reindex(daily.index)
            r6b = cf.pct_change(126).reindex(daily.index); rsb = rsi(cf).reindex(daily.index)
            _, _, mhb = macd(cf); mhb = mhb.reindex(daily.index)
            vmb = vf.reindex(daily.index); v20 = vmb.rolling(20).mean()
            hi20 = cf.rolling(20).max().reindex(daily.index); lo20 = cf.rolling(20).min().reindex(daily.index)
            dv = dser.to_numpy(); KNIFE = 0.10 if spy_riskon else 0.05

            def _fmt(x, unit="%", nd=1):
                return "—" if x is None or x != x else f"{x:+.{nd}f}{unit}" if unit == "%" else f"{x:.{nd}f}"

            def _ctx(pos):
                """마커 시점의 상태값 — 근거 문장 재료."""
                px_ = _f(dv[pos]); s2 = _f(s2b.iloc[pos]); s5 = _f(s5b.iloc[pos])
                return {"px": px_, "d200": (px_/s2 - 1)*100 if (px_ == px_ and s2 == s2 and s2) else None,
                        "d50": (px_/s5 - 1)*100 if (px_ == px_ and s5 == s5 and s5) else None,
                        "rsi": _f(rsb.iloc[pos]), "oh": _f(ohb.iloc[pos]),
                        "dd20": (px_/_f(hi20.iloc[pos]) - 1)*100 if (px_ == px_ and _f(hi20.iloc[pos]) == _f(hi20.iloc[pos])) else None,
                        "up20": (px_/_f(lo20.iloc[pos]) - 1)*100 if (px_ == px_ and _f(lo20.iloc[pos]) == _f(lo20.iloc[pos])) else None,
                        "vr": (_f(vmb.iloc[pos])/_f(v20.iloc[pos])) if (_f(v20.iloc[pos]) == _f(v20.iloc[pos]) and _f(v20.iloc[pos])) else None,
                        "mh": _f(mhb.iloc[pos])}

            def _reason(pos, typ, kind):
                c_ = _ctx(pos); parts = []
                if c_["d200"] is not None:
                    parts.append(f"200일선 {_fmt(c_['d200'])} ({'상승추세' if c_['d200'] >= 0 else '하락추세'})")
                if c_["rsi"] == c_["rsi"]: parts.append(f"RSI {c_['rsi']:.0f}")
                if typ == "L" and c_["dd20"] is not None: parts.append(f"20일 고점 대비 {_fmt(c_['dd20'])} 눌림")
                if typ == "H" and c_["up20"] is not None: parts.append(f"20일 저점 대비 {_fmt(c_['up20'])} 반등")
                if c_["oh"] == c_["oh"]: parts.append(f"과열도 {c_['oh']:.0f}/100")
                if c_["vr"] is not None and c_["vr"] == c_["vr"]: parts.append(f"거래량 평소의 {c_['vr']:.1f}배")
                head = {"zz": "스윙 전환점 확정", "pull": "상승추세 눌림목 저점",
                        "bounce": "하락추세 반등 고점", "rev": "하락추세 과매도 반등 (고위험)"}[kind]
                tail = "" if kind == "rev" else " · 전환점은 며칠 뒤 확정되므로 표시는 사후 기준"
                return head + " — " + " · ".join(parts) + tail

            # (a) 지그재그 확정 전환점 — 마지막 원소는 '미확정 꼬리 극점'(진행 중 최저/최고, 다음날 더 내리면
            #     날짜가 옮겨가는 리페인팅)이므로 매매 마커(▲▼)에서 제외. 차트 스윙 구조(●, tp.zz)에는 그대로 표시.
            _zz = (tp or {}).get("zz") or []
            for pos, typ in (_zz[:-1] if len(_zz) >= 2 else []):
                if pos < 0 or pos >= len(pxd_dates): continue
                oh = _f(ohb.iloc[pos])
                if oh != oh: continue
                s2 = _f(s2b.iloc[pos]); r6 = _f(r6b.iloc[pos]); pxp = _f(dv[pos])
                if typ == "L":
                    knife = (s2 == s2 and pxp == pxp and pxp < s2*(1 - KNIFE)) and (r6 == r6 and r6 < -0.15)
                    if (not knife) and oh <= 65:
                        bms.append(pos); bmr[pos] = _reason(pos, "L", "zz")
                else:
                    rising = (_f(mhb.iloc[pos]) >= _f(mhb.iloc[pos-3])) if pos >= 3 else True
                    strength = (s2 == s2 and pxp == pxp and pxp > s2) and rising and oh < 70
                    if (not strength) and oh >= 35:
                        sms.append(pos); smr[pos] = _reason(pos, "H", "zz")
            # (a') 미확정 꼬리 극점 = '잠정' 타점(bmw/smw) — 확정 확률 60% 이상일 때만 표시(사용자 기준).
            #     실측(5y·512종목): 반전 진행률(theta 대비)이 지배 변수 — 60~80%면 저점 70%·고점 61% 확정, 80%+면 89%/85%.
            #     진행률 <60%는 표시하지 않음(확정 확률 20~58%로 신뢰 불가).
            if len(_zz) >= 2:
                pos, typ = _zz[-1]
                _th = min(0.16, max(0.05, 3.0 * (sg.get("atrp") or 5) / 100))
                _pxl = _f(dv[-1])
                if 0 <= pos < len(pxd_dates) and _pxl == _pxl:
                    oh = _f(ohb.iloc[pos])
                    if oh == oh:
                        s2 = _f(s2b.iloc[pos]); r6 = _f(r6b.iloc[pos]); pxp = _f(dv[pos])
                        if typ == "L" and pxp == pxp and pxp > 0:
                            prog = (_pxl/pxp - 1) / _th
                            knife = (s2 == s2 and pxp < s2*(1 - KNIFE)) and (r6 == r6 and r6 < -0.15)
                            if (not knife) and oh <= 65 and prog >= 0.5:
                                _pc = 89 if prog >= 0.8 else (70 if prog >= 0.6 else 57)
                                bmw.append(pos); bmr[pos] = "잠정 저점(미확정) — " + _reason(pos, "L", "zz").split(" — ", 1)[1].replace(" · 전환점은 며칠 뒤 확정되므로 표시는 사후 기준", "") + f" · 반전 진행률 {min(int(prog*100),99)}%(확정 임계 {int(_th*100)}%) — 과거 통계상 확정 확률 ~{_pc}% (5y·512종목)"
                        elif typ == "H" and pxp == pxp and pxp > 0:
                            prog = (1 - _pxl/pxp) / _th
                            rising = (_f(mhb.iloc[pos]) >= _f(mhb.iloc[pos-3])) if pos >= 3 else True
                            strength = (s2 == s2 and pxp > s2) and rising and oh < 70
                            if (not strength) and oh >= 35 and prog >= 0.6:
                                _pc = 85 if prog >= 0.8 else 61
                                smw.append(pos); smr[pos] = "잠정 고점(미확정) — " + _reason(pos, "H", "zz").split(" — ", 1)[1].replace(" · 전환점은 며칠 뒤 확정되므로 표시는 사후 기준", "") + f" · 반전 진행률 {min(int(prog*100),99)}%(확정 임계 {int(_th*100)}%) — 과거 통계상 확정 확률 ~{_pc}% (5y·512종목)"

            # (b) 추세 내 눌림목·반등 타점 — 지그재그가 놓치는 '추세 유지 중 되돌림'을 잡는다.
            #     확정 중심 피봇(K=5, 5봉 뒤에야 확정 — 미래참조 아님) + 추세·과열·낙폭 조건 + 간격 8봉.
            #     K 선택 근거(5y·512종목 실측): 진짜확률(확정 후 20일 미이탈) K3=41%→K5=50%, 수익차 없음 — 정확도 우선 사용자 결정(2026-07).
            K = 5; W = 2*K + 1
            cwin = pd.Series(dv, index=daily.index)
            piv_lo = cwin == cwin.rolling(W, center=True, min_periods=W).min()
            piv_hi = cwin == cwin.rolling(W, center=True, min_periods=W).max()
            def _spaced(lst, pos, gap=8):
                return all(abs(pos - q) >= gap for q in lst)
            for pos in range(K, len(pxd_dates) - K):
                cc = _ctx(pos)
                if cc["d200"] is None or cc["rsi"] != cc["rsi"]: continue
                if bool(piv_lo.iloc[pos]) and cc["d200"] > 0 and _spaced(bms, pos):
                    deep = (cc["dd20"] is not None and cc["dd20"] <= -4.0)
                    if (cc["rsi"] < 48 or deep) and (cc["oh"] != cc["oh"] or cc["oh"] <= 55):
                        bms.append(pos); bmr[pos] = _reason(pos, "L", "pull")
                if bool(piv_hi.iloc[pos]) and cc["d200"] < 0 and _spaced(sms, pos):
                    high = (cc["up20"] is not None and cc["up20"] >= 4.0)
                    if (cc["rsi"] > 55 or high) and (cc["oh"] != cc["oh"] or cc["oh"] >= 45):
                        sms.append(pos); smr[pos] = _reason(pos, "H", "bounce")
            # (b') 눌림목/반등 '잠정' 후보 — 확정(5봉 유지) 전 예고. 실측 확정확률(5y·512종목):
            #     저점 1일 52%·2일 68%·3일 80%·4일 91% / 고점 1일 46%·2일 63%·3일 77%·4일 90%.
            #     저점은 사용자 지정으로 1일부터 표시(선행성 우선, 대신 확정확률 52% = 사실상 동전던지기 — 근거 문구에 그대로 노출).
            #     고점은 1일이 46%로 50% 미만(확정보다 이동이 더 잦음)이라 2일 유지.
            _lastp = len(pxd_dates) - 1
            _PL = {1: 52, 2: 68, 3: 80, 4: 91}; _PH = {2: 63, 3: 77, 4: 90}
            for m in range(_lastp - 4, _lastp):   # age = _lastp-m ∈ {4,3,2,1}
                if m < K: continue
                age = _lastp - m
                base = _f(dv[m])
                if base != base: continue
                prev = dv[m-K:m]; fut = dv[m+1:_lastp+1]
                if np.isnan(prev.astype(float)).any() or np.isnan(fut.astype(float)).any(): continue
                cc = _ctx(m)
                if cc["d200"] is None or cc["rsi"] != cc["rsi"]: continue
                if age in _PL and base < prev.min() and (fut > base).all() and cc["d200"] > 0 and _spaced(bms, m) and m not in bmw:
                    deep = (cc["dd20"] is not None and cc["dd20"] <= -4.0)
                    if (cc["rsi"] < 48 or deep) and (cc["oh"] != cc["oh"] or cc["oh"] <= 55):
                        bmw.append(m); bmr[m] = "잠정 눌림목 저점(미확정) — " + _reason(m, "L", "pull").split(" — ", 1)[1].replace(" · 전환점은 며칠 뒤 확정되므로 표시는 사후 기준", "") + f" · {age}일 유지 — 5거래일 채우면 확정, 과거 통계상 확정 확률 ~{_PL[age]}% (5y·512종목)"
                if age in _PH and base > prev.max() and (fut < base).all() and cc["d200"] < 0 and _spaced(sms, m) and m not in smw:
                    high = (cc["up20"] is not None and cc["up20"] >= 4.0)
                    if (cc["rsi"] > 55 or high) and (cc["oh"] != cc["oh"] or cc["oh"] >= 45):
                        smw.append(m); smr[m] = "잠정 반등 고점(미확정) — " + _reason(m, "H", "bounce").split(" — ", 1)[1].replace(" · 전환점은 며칠 뒤 확정되므로 표시는 사후 기준", "") + f" · {age}일 유지 — 5거래일 채우면 확정, 과거 통계상 확정 확률 ~{_PH[age]}% (5y·512종목)"
            # (c) 하락추세 과매도 반등확인 타점은 제거(2026-07) — 잠정 예고가 불가능한 유형(+5% 회복 순간이 곧 확정)이라
            #     '잠정 없이 확정 등장 금지' 원칙과 상충하고, '추세 전환 완전 확인 후 진입' 전략과도 안 맞음.
            # 같은 위치가 확정(bms/sms)과 잠정(bmw/smw)에 중복되면 확정 우선 — 차트에서 빈 도형이 확정 위를 덮는 문제 방지
            bmw = [p for p in bmw if p not in bms]; smw = [p for p in smw if p not in sms]
            bms.sort(); sms.sort()

            # (c) 현재 상태 근거(왜 지금 이 라벨인가) + 최근 타점 사유
            last = len(pxd_dates) - 1
            cl = _ctx(last); bullets = []
            if cl["d200"] is not None:
                bullets.append(("상승추세" if cl["d200"] >= 0 else "하락추세") + f" — 종가가 200일선 {_fmt(cl['d200'])}")
            if cl["d50"] is not None: bullets.append(f"50일선 대비 {_fmt(cl['d50'])}")
            if cl["rsi"] == cl["rsi"]:
                rr = cl["rsi"]
                bullets.append(f"RSI {rr:.0f} — " + ("과매도권(반등 여지)" if rr < 35 else "과매수권(되돌림 주의)" if rr > 70 else "중립"))
            if cl["dd20"] is not None: bullets.append(f"최근 20일 고점 대비 {_fmt(cl['dd20'])}")
            if cl["oh"] == cl["oh"]: bullets.append(f"과열도 {cl['oh']:.0f}/100 (0=과매도·100=과매수)")
            if cl["vr"] is not None and cl["vr"] == cl["vr"]: bullets.append(f"거래량 평소의 {cl['vr']:.1f}배")
            whyb = {"now": bullets}
            if bms: whyb["buy"] = {"dt": pxd_dates[bms[-1]], "why": bmr.get(bms[-1], "")}
            if sms: whyb["sell"] = {"dt": pxd_dates[sms[-1]], "why": smr.get(sms[-1], "")}
            if bmw: whyb["pbuy"] = {"dt": pxd_dates[bmw[-1]], "why": bmr.get(bmw[-1], "")}
            if smw: whyb["psell"] = {"dt": pxd_dates[smw[-1]], "why": smr.get(smw[-1], "")}
        # timing(라벨·리스트)은 trend_signals의 추세기반 유지 · 스윙 마커는 차트 전용 (bmw/smw = 잠정 타점, 위에서 채움)
        info = mem.get(t, {})
        fd = fund.get(t) or {}
        def r2(x): return round(float(x), 2) if x is not None and x == x else None
        fnd = {"teps": r2(fd.get("teps")), "feps": r2(fd.get("feps")), "tpe": r2(fd.get("tpe")), "fpe": r2(fd.get("fpe"))}
        if fnd["teps"] and fnd["feps"] and fnd["teps"] != 0:
            fnd["gr"] = round((fnd["feps"]/fnd["teps"] - 1)*100, 1)   # 12M 선행 EPS 성장률(%)
        # 애널리스트 목표주가 — 참고 표기 전용(신호·랭킹·정렬 금지). 결측이면 키 자체를 넣지 않는다.
        _tpm, _tph, _tpl = r2(fd.get("tpm")), r2(fd.get("tph")), r2(fd.get("tpl"))
        if _tpm and _tpm > 0:
            fnd["tpm"] = _tpm
            if _tph and _tph > 0: fnd["tph"] = _tph
            if _tpl and _tpl > 0: fnd["tpl"] = _tpl
            _last = _f(raw[t]["close"].iloc[-1])   # 상승여력은 우리 기준일 종가 기준(정보원 currentPrice와 as-of 혼선 방지)
            if _last == _last and _last > 0:
                fnd["up"] = round((_tpm/_last - 1)*100, 1)
            _na = fd.get("nan")
            # float('nan')은 truthy라 가드를 통과하고 int()에서 ValueError → 잡 전체가 죽는다(적대검토 지적)
            if _na and _na == _na:
                try: fnd["nan"] = int(_na)
                except (ValueError, TypeError): pass
            _rk = fd.get("rk")
            if _rk and _rk != "none": fnd["rk"] = str(_rk)
            tp_hist[t] = _tpm
        _tb = raw[t]["tb"]   # 트리거는 추세 방향에 맞는 것만 노출(상승=매수트리거·하락=매도트리거)
        trig = ([k for k in ("reclaim", "golden", "macd_bull") if _tb.get(k)] if _tb.get("up")
                else [k for k in ("lose", "death", "macd_bear") if _tb.get(k)])
        stocks.append({"t": t, "name": info.get("name"), "sector": info.get("sector"), "idx": info.get("idx", []),
                       "comp": {k: v for k, v in comps.items() if v is not None}, "flags": flags(sg),
                       "timing": raw[t]["timing"], "buy": raw[t]["buy"], "sell": raw[t]["sell"],
                       "bscore": round(float(buy_score.get(t, 0.0)), 3), "sscore": round(float(sell_score.get(t, 0.0)), 3),
                       "trig": trig, "bms": bms, "bmw": bmw, "sms": sms, "smw": smw, "sig": sig,
                       **({"why": whyb} if whyb else {}),
                       "fund": {k: v for k, v in fnd.items() if v is not None}, "pxd": pxd, "vd": vd,
                       **({"tp": tp} if tp else {})})
    stocks.sort(key=lambda s: -(s["comp"].get("momentum") or 0))
    out = {"as_of": as_of, "source": "yfinance + 표준 테크니컬 (cloud)", "n_stocks": len(stocks), "pxd_dates": pxd_dates,
           "factor_defs": {k: {"label": FACTORS[k][0], "group": FACTORS[k][1], "hi": FACTORS[k][2], "as_of": (si_asof if k in ("dtc", "sipct") else as_of)} for k in FACTORS},
           "fund_defs": {"teps": "주당순이익 TTM (최근 12개월 실적)", "feps": "선행 EPS (향후 12개월 애널리스트 추정)",
                         "gr": "선행 EPS 성장률 (forward/trailing−1, %)", "tpe": "P/E (TTM)", "fpe": "선행 P/E (forward)",
                         "tpm": "애널리스트 목표주가 평균 (12개월, 참고용·신호 아님)", "tph": "목표주가 최고", "tpl": "목표주가 최저",
                         "up": "상승여력 = 목표평균/현재가−1 (%). ⚠ 매수 근거로 쓰지 말 것 — 512종목 전수검증 결과 "
                               "상승여력 분산의 64%가 '목표가는 1~3개월 시차로 느리게 갱신되는데 주가는 이미 빠진' 기계적 산물이며, "
                               "우리 매수점수와 상관 −0.71·6개월 모멘텀과 −0.60(55개월 중 100% 음수)이다. "
                               "상승여력 상위 20종목의 90%가 200일선 아래(하락추세)이고, 20/60일 예측력은 통계적으로 없다(Q5−Q1 t=0.89).",
                         "nan": "목표주가를 제시한 애널리스트 수",
                         "rk": "애널리스트 종합 추천등급. ⚠ 512종목 중 sell·strong_sell 0건 — 등급 체계 자체가 매수 편향(buy 349·hold 89·strong_buy 61)."},
           "fund_note": "목표주가·추천등급은 표기만 하고 신호로 쓰지 않는다(매수/매도 점수·플래그·타이밍에 일절 반영하지 않음). 참고용이며 매매권유가 아니다.",
           "composite_defs": {"overheat": "과열도 — RSI·스토캐스틱·MFI·Williams·%b·52주", "trend": "추세강도 — ADX·이동평균·MACD·Aroon",
                              "momentum": "모멘텀 — 1/3/6M 수익률·상대강도", "volatility": "변동성 — ATR%·실현변동성",
                              "positioning": "포지셔닝 — 공매도 커버일수(격주 FINRA)"},
           "flag_defs": {"과매수": "RSI≥70 또는 %b≥0.95", "과매도": "RSI≤30 또는 %b≤0.05", "상승추세": "종가>50일선>200일선 & ADX≥20",
                         "눌림목": "상승추세 중 RSI<45 & %b<0.3",
                         "깊은눌림": "200일선 위(장기 상승) & 50일선 아래(단기 조정) & RSI<45 — 상태 표시일 뿐 매매 신호 아님",
                         "52주돌파": "52주고점 92%↑ & MACD 상승", "200일이탈": "종가<200일선",
                         "과밀숏": "숏 커버일수≥7 또는 공매도잔량≥유동주식 15%"},
           "stocks": stocks}
    # 완전성 게이트: yfinance 부분 장애로 커버 종목이 급감한 결과를 조용히 덮어쓰지 않는다(이전본 유지 + 워크플로 실패로 알림).
    try:
        _prev = int(json.load(open(OUT, encoding="utf-8")).get("n_stocks") or 0)
    except Exception:
        _prev = 0
    if _prev and len(stocks) < _prev * 0.9:
        raise SystemExit(f"커버 종목 급감 {_prev}→{len(stocks)} (yfinance 부분 장애 의심) — 갱신 중단, 이전본 유지")
    # ── 상세 분리(지연 로드): sig·pxd·vd·tp(페이로드의 72%)는 종목별 data/sd/<티커>.json으로, 본체는 슬림하게 ──
    # ⚠ 워크플로 커밋 대상에 data/sd 포함 필수(home_reco 누락 사고와 동일 함정) · stocks.html이 선택 시 fetch.
    SD_DIR = os.path.join(HERE, "..", "data", "sd")
    os.makedirs(SD_DIR, exist_ok=True)
    _keep = set()
    for s in stocks:
        det = {"as_of": as_of, "t": s["t"]}
        for k in ("sig", "pxd", "vd", "tp", "why"):
            v = s.pop(k, None)
            if v is not None: det[k] = v
        fn = s["t"] + ".json"; _keep.add(fn)
        json.dump(det, open(os.path.join(SD_DIR, fn), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    for fn in os.listdir(SD_DIR):   # 유니버스에서 빠진 종목의 잔존 상세 파일 제거(스테일 방지)
        if fn.endswith(".json") and fn not in _keep:
            os.remove(os.path.join(SD_DIR, fn))
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"→ {OUT} ({len(stocks)}종목 · 슬림 {os.path.getsize(OUT)//1024}KB · 상세 {len(_keep)}파일 · 기준일 {as_of})")
    # ── 목표주가 스냅샷 누적(실패해도 빌드는 계속 — 부가 자산이지 배포물이 아니다) ──
    try:
        _wrote, _why, _ns2 = update_target_history(TPHIST, as_of, tp_hist)
        _sz = os.path.getsize(TPHIST)//1024 if os.path.exists(TPHIST) else 0
        print(f"목표주가 이력 {'기록' if _wrote else '생략'}({_why}) · 스냅샷 {_ns2}개 · {_sz}KB · 이번 커버 {len(tp_hist)}/{len(stocks)}종목")
    except Exception as e:
        print("  목표주가 이력 갱신 실패(무시):", e)
    # ── 홈 전용 초소형 요약(주목종목) — 홈이 대형 stocks.json 대신 이것만 fetch(LCP 개선) ──
    try:
        # 전략 재편(2026-07): 추세 전환 '완전 확정' 후 진입 — 홈은 최신 확정 스윙 타점(잠정 제외, 리페인팅 없음)
        _dts = out["pxd_dates"]; _N = len(_dts); _WIN = 10
        def _lastmk(s, key):
            a = s.get(key) or []
            return a[-1] if a else -1
        def _reco(key):
            c = [(_lastmk(s, key), s) for s in stocks]
            c = [(m, s) for m, s in c if m >= 0 and (_N - 1 - m) <= _WIN]
            c.sort(key=lambda x: -x[0])
            return ([{"t": s["t"], "name": (s.get("name") or "")[:16], "dt": _dts[m][5:], "ago": _N - 1 - m,
                      "oh": s["comp"].get("overheat")} for m, s in c[:8]], len(c))
        _bl, _nb = _reco("bms"); _sl, _ns = _reco("sms")
        HOME = os.path.join(HERE, "..", "data", "home_reco.json")
        json.dump({"as_of": as_of, "buy": _bl, "sell": _sl, "nbuy": _nb, "nsell": _ns},
                  open(HOME, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
        print(f"→ {HOME} (홈 확정 스윙 · 매수 {_nb}·매도 {_ns})")
    except Exception as e:
        print("  home_reco 생성 실패(무시):", e)


if __name__ == "__main__":
    main()
