# -*- coding: utf-8 -*-
"""
refresh_sentiment.py — 여두 시장심리 지수(0~100) 빌더 (self-contained, yfinance + FRED, 무료).

설계 확정본(sentiment_design.md) 그대로 구현한다.
  · 점수 반영 3개 : VIX 확장백분위(0.65) · VIX/VIX3M 원값비율 구간매핑(0.25) · MOVE 3년롤링백분위(0.10)
  · 표시 전용 4개 : HYG−IEF(20일) · RSP−SPY(20일) · NFCI · b200(200일선 위 비율)  ← weight 0
  · 모든 백분위는 causal(오늘 제외 확장창/롤링). 룩어헤드 금지.
  · 결측은 가용 가중합(avail_w)으로 재정규화, avail_w < 0.50 이면 산출불가(score=None).

정직 고지: 자체 백테스트에서 이 합성 지수는 VIX 단독을 이기지 못했다(IC60 −0.189 vs −0.201).
"상태 요약 지표"이지 수익 예측 신호가 아니며, 사이트 문구도 그렇게 쓴다.

출력: data/sentiment.json  (GitHub Actions 일간 크론)
"""
import os, sys, json, time, datetime as dt
import numpy as np
import pandas as pd

try: sys.stdout.reconfigure(encoding="utf-8")   # Windows 콘솔 cp949 방지
except Exception: pass

import yfinance as yf

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "data", "sentiment.json")
STOCKS = os.path.join(HERE, "..", "data", "stocks.json")

FRED_KEY = os.getenv("FRED_API_KEY")            # ★ 하드코딩 금지 (없으면 NFCI 패널만 결측 처리)

W = {"vix": 0.65, "vix_ts": 0.25, "move": 0.10}  # 합 1.00 — 백테스트 최적화가 아니라 증거등급 사전결정
BURN_VIX, BURN_SCORE = 1260, 756                 # 5년 / 3년 burn-in
HIST_YEARS = 3                                   # history 절단(최소 2년 요구, 3년 권장)

# ──────────────────────────────────────────────────────────────────────
# 0) 데이터 로드 (재시도·예외처리)
# ──────────────────────────────────────────────────────────────────────
def yf_close(ticker, tries=3, pause=3.0):
    """yfinance 종가 시리즈(period=max). 실패 시 빈 시리즈."""
    for k in range(tries):
        try:
            df = yf.download(ticker, period="max", interval="1d",
                             auto_adjust=True, progress=False, threads=False)
            if df is None or not len(df):
                raise ValueError("빈 응답")
            s = df["Close"]
            if isinstance(s, pd.DataFrame):      # MultiIndex 대응
                s = s.iloc[:, 0]
            s = pd.Series(s.values.astype(float), index=pd.to_datetime(s.index)).dropna()
            s.index = s.index.tz_localize(None) if getattr(s.index, "tz", None) else s.index
            if len(s) < 100:
                raise ValueError(f"관측 {len(s)}개 — 너무 짧음")
            print(f"  {ticker:<8} {len(s):6d}개  {s.index.min().date()}~{s.index.max().date()}")
            return s
        except Exception as e:
            print(f"  {ticker} 실패({k+1}/{tries}): {e}")
            if k < tries - 1: time.sleep(pause * (k + 1))
    return pd.Series(dtype=float)


def fred_series(code, tries=3, pause=3.0):
    """FRED 시리즈. 키 미설정·실패 시 빈 시리즈(패널만 결측)."""
    if not FRED_KEY:
        print(f"  FRED {code} 건너뜀 — FRED_API_KEY 미설정")
        return pd.Series(dtype=float)
    for k in range(tries):
        try:
            from fredapi import Fred
            s = Fred(api_key=FRED_KEY).get_series(code).dropna()
            s.index = pd.to_datetime(s.index)
            print(f"  FRED {code:<8} {len(s):6d}개  {s.index.min().date()}~{s.index.max().date()}")
            return s.astype(float)
        except Exception as e:
            print(f"  FRED {code} 실패({k+1}/{tries}): {e}")
            if k < tries - 1: time.sleep(pause * (k + 1))
    return pd.Series(dtype=float)


# ──────────────────────────────────────────────────────────────────────
# 1) causal 변환
# ──────────────────────────────────────────────────────────────────────
def pctl_exp(s, burn):
    """확장창 백분위 — 오늘 값은 '오늘 이전' 관측에만 비교(룩어헤드 차단). burn 미만이면 NaN."""
    v = s.values.astype(float)
    out = np.full(len(v), np.nan)
    for i in range(burn, len(v)):
        past = v[:i]                      # ← i 미포함이 핵심
        past = past[~np.isnan(past)]
        if len(past) >= burn and not np.isnan(v[i]):
            out[i] = (past < v[i]).mean()
    return pd.Series(out, index=s.index)


def pctl_roll(s, win=756, minp=252):
    """3년 롤링 백분위(오늘 제외). 레벨 드리프트가 큰 MOVE용."""
    return s.rolling(win, min_periods=minp).apply(lambda x: (x[:-1] < x[-1]).mean(), raw=True)


def ts_map(x):
    """VIX/VIX3M 원값 비율 → 0~100 (백분위화하면 IC가 떨어진다 — 경제적 의미 고정 임계 유지)."""
    xs = [0.80, 0.85, 0.92, 0.97, 1.00, 1.05, 1.10]
    ys = [100,    85,   65,   45,   30,   15,    0]
    return float(np.interp(x, xs, ys))     # 구간 밖은 끝값 클립


def label_of(s):
    if s is None or not np.isfinite(s): return "산출불가", "na"
    if s < 20:  return "극단공포", "extreme_fear"
    if s < 40:  return "공포", "fear"
    if s < 60:  return "중립", "neutral"
    if s < 80:  return "탐욕", "greed"
    return "극단탐욕", "extreme_greed"


def rnd(v, n=1):
    try:
        v = float(v)
        return None if not np.isfinite(v) else round(v, n)
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────
# 2) 백테스트 (SPY, gross·무비용)
# ──────────────────────────────────────────────────────────────────────
def backtest(score, score_pctl, comp_vix, spy, ws=None):
    """ws = 날짜별 가용가중합. 3지표가 다 있는 구간(ws≈1.0)과 그렇지 않은 구간을 구분해 정직하게 고지한다.
    (적대검토 지적: 1998~2003 구간은 가용가중 0.65로 수학적으로 VIX 단독과 동일 — '28년 검증'으로 읽히면 안 됨)"""
    f20 = (spy.shift(-20) / spy - 1).reindex(score.index)
    f60 = (spy.shift(-60) / spy - 1).reindex(score.index)
    d = pd.DataFrame({"s": score, "p": score_pctl, "f20": f20, "f60": f60}).dropna(subset=["s"])
    if ws is not None: d["ws"] = ws.reindex(d.index)
    dv = d.dropna(subset=["f20", "f60"])
    if len(dv) < 500:
        raise SystemExit(f"백테스트 표본 부족 n={len(dv)} — 갱신 중단(이전본 유지)")

    def agg(g):
        return {"n": int(len(g)), "fwd20": rnd(g.f20.mean() * 100, 2), "win20": rnd((g.f20 > 0).mean() * 100, 1),
                "fwd60": rnd(g.f60.mean() * 100, 2), "win60": rnd((g.f60 > 0).mean() * 100, 1)}

    LAB = ["0-20 극단공포", "20-40 공포", "40-60 중립", "60-80 탐욕", "80-100 극단탐욕"]
    BINS = [0, 20, 40, 60, 80, 100.001]
    dv = dv.copy()
    dv["b"] = pd.cut(dv.s, BINS, labels=LAB, right=False, include_lowest=True)
    buckets = [dict(bucket=l, **agg(dv[dv.b == l])) for l in LAB if (dv.b == l).any()]

    # ── 분위 버킷(지수 자신의 확장 백분위) — 유의성이 살아남는 유일한 구획 ──
    dp = dv.dropna(subset=["p"]).copy()
    # 라벨은 '명목 10%'가 아니라 임계값 서술 — 확장백분위라 실제 빈도가 균등하지 않다(적대검토 지적: 실측 3.2%/19.8%).
    PLAB = ["백분위 0-10%(가장 겁먹음)", "10-25%", "25-75%", "75-90%", "백분위 90-100%(가장 탐욕)"]
    dp["pb"] = pd.cut(dp.p, [0, .10, .25, .75, .90, 1.0001], labels=PLAB, right=False, include_lowest=True)
    base60 = dp.f60.mean()

    def episode_t(frame, mask, base):
        """연속 구간의 '진입일'만 표본으로 t검정(중복 관측 제거)."""
        m = mask.values.astype(int)
        starts = np.where(np.diff(np.r_[0, m]) == 1)[0]
        vals = np.array([frame.f60.iloc[i] for i in starts if np.isfinite(frame.f60.iloc[i])])
        if len(vals) < 5: return int(len(vals)), None, None
        try:
            from scipy import stats
            tt = stats.ttest_1samp(vals - base, 0)
            return int(len(vals)), rnd(tt.statistic, 2), rnd(tt.pvalue, 3)
        except Exception as e:
            print("  t검정 생략:", e); return int(len(vals)), None, None

    # 3지표가 모두 가용한 구간(ws≥0.99)만의 부분표본 — 유의성이 표본 외에서 살아남는지 확인용
    dp3 = dp[dp.ws >= 0.99] if "ws" in dp.columns else dp.iloc[0:0]
    base60_3 = dp3.f60.mean() if len(dp3) else np.nan

    buckets_pctl = []
    for l in PLAB:
        g = dp[dp.pb == l]
        if not len(g): continue
        row = dict(bucket=l, **agg(g))
        row["share_pct"] = rnd(len(g) / len(dp) * 100, 1)     # 실제 표본 비중(명목 구간폭과 다름)
        if l in (PLAB[0], PLAB[-1]):        # 극단 꼬리만 에피소드 t검정
            ep, t, p = episode_t(dp, dp.pb == l, base60)
            row["episodes"] = ep
            if t is not None: row["t60"], row["p60"] = t, p
            # 3지표 전부 가용 구간에서 같은 검정 — 유의성이 표본 외에서 소멸하는지 정직 노출
            if len(dp3) > 200:
                g3 = dp3[dp3.pb == l]
                ep3, t3, p3 = episode_t(dp3, dp3.pb == l, base60_3)
                row["full3"] = {"n_days": int(len(g3)), "episodes": ep3,
                                "fwd60": rnd(g3.f60.mean() * 100, 2) if len(g3) else None,
                                "excess60": rnd((g3.f60.mean() - base60_3) * 100, 2) if len(g3) else None,
                                "t60": t3, "p60": p3}
        buckets_pctl.append(row)

    # ── 합성 vs VIX 단독 (Spearman IC60) ──
    # 설계 문서와 동일하게 '각 신호의 causal 확장백분위' 기준으로, 같은 표본에서 비교한다
    # (레벨 그대로 비교하면 비정상성 때문에 두 값이 함께 축소되어 공정 비교가 안 된다).
    vix_pctl = pctl_exp(comp_vix.dropna(), 756).reindex(score.index)
    ic = pd.DataFrame({"c": score_pctl, "v": vix_pctl, "f60": f60}).dropna()
    ic60_c = float(ic.c.corr(ic.f60, method="spearman"))
    ic60_v = float(ic.v.corr(ic.f60, method="spearman"))
    verdict = ("합성 지수가 VIX 단독을 이기지 못함. 개선 주장 없음."
               if ic60_c >= ic60_v else
               "합성이 VIX 단독보다 근소 우위이나 노이즈 범위 — 개선 주장 없음.")

    # 축퇴(가용가중 0.65 = VIX 단독과 수학적으로 동일) 일수 — '28년 3지표 검증'으로 오독되지 않게 고지
    degen = int((dv.ws < 0.70).sum()) if "ws" in dv.columns else None
    full3 = dv[dv.ws >= 0.99] if "ws" in dv.columns else dv.iloc[0:0]
    sample_full3 = (None if not len(full3) else
                    {"sample": f"{full3.index.min().date()}~{full3.index.max().date()}", "n_days": int(len(full3)),
                     "fwd60": rnd(full3.f60.mean() * 100, 2),
                     "desc": "3개 지표(VIX·기간구조·MOVE)가 모두 가용한 구간만"})
    return {
        "asset": "SPY",
        "sample": f"{dv.index.min().date()}~{dv.index.max().date()}",
        "n_days": int(len(dv)),
        "sample_note": (None if degen is None else
                        f"초기 {degen}일({degen/len(dv)*100:.0f}%)은 VIX3M·MOVE 미제공으로 가용가중 0.65 — "
                        f"이 구간의 지수는 수학적으로 VIX 단독과 동일하다. 3지표 전 구간 통계는 sample_full3 참조."),
        "sample_full3": sample_full3,
        "base": {"fwd20": rnd(dv.f20.mean() * 100, 2), "fwd60": rnd(dv.f60.mean() * 100, 2),
                 "win20": rnd((dv.f20 > 0).mean() * 100, 1), "win60": rnd((dv.f60 > 0).mean() * 100, 1)},
        "buckets": buckets,
        "base_pctl": {"fwd20": rnd(dp.f20.mean() * 100, 2), "fwd60": rnd(base60 * 100, 2), "n_days": int(len(dp))},
        "buckets_pctl": buckets_pctl,
        "vs_vix_only": {"ic60_composite": rnd(ic60_c, 3), "ic60_vix_only": rnd(ic60_v, 3),
                        "n": int(len(ic)), "basis": "각 신호의 causal 확장백분위 vs 이후 60일 SPY 수익, Spearman",
                        "verdict": verdict},
    }


# ──────────────────────────────────────────────────────────────────────
def main():
    print("yfinance 로드…")
    VIX   = yf_close("^VIX")
    VIX3M = yf_close("^VIX3M")
    MOVE  = yf_close("^MOVE")
    SPY   = yf_close("SPY")
    HYG   = yf_close("HYG")
    IEF   = yf_close("IEF")
    RSP   = yf_close("RSP")

    print("FRED 로드…")
    VIXCLS = fred_series("VIXCLS")          # ^VIX 결측 보강(상관 1.00000)
    NFCI   = fred_series("NFCI")            # 주간·패널 전용

    # ── 부분 실패 게이트 ────────────────────────────────────────────
    if not len(VIX) and not len(VIXCLS):
        raise SystemExit("VIX 로드 실패(yfinance·FRED 모두) — 지수 산출 불가, 갱신 중단(이전본 유지)")
    if not len(SPY):
        raise SystemExit("SPY 로드 실패 — 거래일 캘린더·백테스트 불가, 갱신 중단(이전본 유지)")
    if len(VIXCLS):                          # 1990~ 이력 보강(어느 쪽이든 값은 동일)
        VIX = VIX.combine_first(VIXCLS) if len(VIX) else VIXCLS

    # ★ 미확정 당일 봉 제거 — 랩 최우선 규칙 '기준일 통일'. 장중 실행 시 yfinance 마지막 봉이 실시간이라
    #   점수가 흔들리고 사이트 다른 데이터(stocks·regime)보다 기준일이 하루 앞서는 문제가 있었다.
    #   미 동부 16:15 이전이면 당일 봉은 미확정으로 보고 버린다(크론은 마감 후라 영향 없음).
    try:
        from zoneinfo import ZoneInfo
        _now_et = pd.Timestamp.now(tz=ZoneInfo("America/New_York"))
        _cut = _now_et.date() if _now_et.hour * 60 + _now_et.minute >= 16 * 60 + 15 else None
        _drop = pd.Timestamp(_now_et.date())
        if _cut is None and len(SPY) and SPY.index[-1].normalize() == _drop:
            SPY = SPY.iloc[:-1]
            print(f"  미확정 당일 봉({_drop.date()}) 제외 — 기준일 통일(미 동부 {_now_et:%H:%M} 장중)")
    except Exception as e:
        print("  당일 봉 판정 생략:", e)
    idx = SPY.index[SPY.index >= "1993-02-01"]

    # ── 원값(거래일 reindex + ffill 한도) ──────────────────────────
    raw = pd.DataFrame(index=idx)
    raw["vix"]  = VIX.reindex(idx).ffill(limit=5)
    raw["vix_ts"] = ((VIX / VIX3M).replace([np.inf, -np.inf], np.nan).dropna()
                     .reindex(idx).ffill(limit=5)) if len(VIX3M) else np.nan
    raw["move"] = MOVE.reindex(idx).ffill(limit=5) if len(MOVE) else np.nan
    raw["hyg_ief"] = ((np.log(HYG).diff(20) - np.log(IEF).diff(20)).dropna()
                      .reindex(idx).ffill(limit=5)) if (len(HYG) and len(IEF)) else np.nan
    raw["rsp_spy"] = ((np.log(RSP).diff(20) - np.log(SPY).diff(20)).dropna()
                      .reindex(idx).ffill(limit=5)) if len(RSP) else np.nan
    # NFCI: 주간 발표. 발표지연을 보수적으로 1관측 시프트 후 ffill(limit=10)
    raw["nfci"] = NFCI.shift(1).reindex(idx).ffill(limit=10) if len(NFCI) else np.nan

    # ── causal 변환 → 0~100 탐욕 방향 ──────────────────────────────
    print("causal 백분위 계산…")
    comp = pd.DataFrame(index=idx)
    p_vix = pctl_exp(raw["vix"].dropna(), BURN_VIX).reindex(idx)
    comp["vix"] = 100 * (1 - p_vix)                                    # VIX 높으면 공포 → 낮은 점수
    comp["vix_ts"] = raw["vix_ts"].apply(lambda v: np.nan if pd.isna(v) else ts_map(v))
    p_move = pctl_roll(raw["move"].dropna()).reindex(idx) if raw["move"].notna().any() else pd.Series(np.nan, index=idx)
    comp["move"] = 100 * (1 - p_move)
    comp["hyg_ief"] = 100 * pctl_exp(raw["hyg_ief"].dropna(), 756).reindex(idx) if raw["hyg_ief"].notna().sum() > 800 else np.nan
    comp["rsp_spy"] = 100 * pctl_exp(raw["rsp_spy"].dropna(), 756).reindex(idx) if raw["rsp_spy"].notna().sum() > 800 else np.nan
    comp["nfci"] = 100 * (1 - pctl_exp(raw["nfci"].dropna(), 520).reindex(idx)) if raw["nfci"].notna().sum() > 600 else np.nan

    # ── 가중 합성 + 결측 재정규화 ──────────────────────────────────
    w = pd.Series(W)
    avail = comp[list(W)].notna()
    ws = avail.mul(w, axis=1).sum(axis=1)                              # 그날 가용 가중 합
    score = comp[list(W)].fillna(0).mul(w, axis=1).sum(axis=1) / ws.replace(0, np.nan)
    score[ws < 0.50] = np.nan                                          # ★ 하한 게이트(VIX 결측 시 자동 발동)
    score = score.clip(0, 100).round(1)
    if not score.notna().any():
        raise SystemExit("지수 산출 결과가 전부 결측 — 갱신 중단(이전본 유지)")

    score_pctl = pctl_exp(score.dropna(), BURN_SCORE).reindex(idx)     # 지수 자신의 확장 백분위
    sd = score.dropna()
    last = sd.index[-1]
    cur = float(sd.iloc[-1])
    lab, lab_en = label_of(cur)
    wav = float(ws.loc[last])
    print(f"기준일 {last.date()} · score {cur:.1f} ({lab}) · 가용가중 {wav:.2f}")

    # ── 컴포넌트 블록 ──────────────────────────────────────────────
    ld = last.date().isoformat()
    def cval(k):  return None if pd.isna(comp[k].loc[last]) else float(comp[k].loc[last])
    def rval(k):  return None if pd.isna(raw[k].loc[last]) else float(raw[k].loc[last])
    def mk(key, label, rawv, rawfmt, weight, desc, pctl=None, asof=ld):
        sc = cval(key)
        c = None if (sc is None or wav <= 0 or weight == 0) else rnd(sc * weight / wav, 1)
        if weight == 0: c = 0.0
        return {"key": key, "label": label, "raw": rawv, "raw_fmt": rawfmt, "pctl": pctl,
                "score": rnd(sc, 1), "weight": weight, "contrib": c, "desc": desc, "as_of": asof}

    v_vix, v_ts, v_mv = rval("vix"), rval("vix_ts"), rval("move")
    s_vix, s_mv = cval("vix"), cval("move")
    components = [
        mk("vix", "VIX 수준(확장 백분위)", rnd(v_vix, 2), f"{v_vix:.2f}" if v_vix is not None else "—", 0.65,
           "1990년 이후 전 이력 대비 오늘 VIX의 위치. 백분위가 높을수록 공포. 5년 burn-in·오늘 제외.",
           None if s_vix is None else rnd(1 - s_vix / 100, 3)),
        mk("vix_ts", "VIX 기간구조(VIX/VIX3M)", rnd(v_ts, 4), f"{v_ts:.3f}" if v_ts is not None else "—", 0.25,
           "1개월/3개월 내재변동성 비율. 1.0 초과(백워데이션)면 단기 스트레스. 향후 변동성·낙폭 게이지이며 매수 신호가 아님.",
           None),
        mk("move", "MOVE 채권변동성(3년 롤링 백분위)", rnd(v_mv, 2), f"{v_mv:.1f}" if v_mv is not None else "—", 0.10,
           "미국 국채 내재변동성. 주식 변동성과 상관 0.32로 사실상 직교한 금리·유동성 스트레스 축.",
           None if s_mv is None else rnd(1 - s_mv / 100, 3)),
    ]

    v_hi, v_rs, v_nf = rval("hyg_ief"), rval("rsp_spy"), rval("nfci")
    s_hi, s_rs, s_nf = cval("hyg_ief"), cval("rsp_spy"), cval("nfci")
    panel = [
        mk("hyg_ief", "신용 위험선호(HYG−IEF 20일)", rnd((v_hi or 0) * 100, 2) if v_hi is not None else None,
           f"{v_hi*100:+.2f}%" if v_hi is not None else "—", 0.0,
           "하이일드채−중기국채 20일 상대수익. 예측력 검증 실패(통제 후 t≈0) — 점수 미반영, 상태 표시용.",
           None if s_hi is None else rnd(s_hi / 100, 3)),
        mk("rsp_spy", "동일가중 대비 시총가중(RSP−SPY 20일)", rnd((v_rs or 0) * 100, 2) if v_rs is not None else None,
           f"{v_rs*100:+.2f}%" if v_rs is not None else "—", 0.0,
           "시장 폭의 가격판. 다른 지표와 |ρ|≤0.20으로 유일하게 직교하나 예측력은 없음 — 점수 미반영.",
           None if s_rs is None else rnd(s_rs / 100, 3)),
        mk("nfci", "시카고연준 금융여건지수(NFCI)", rnd(v_nf, 3), f"{v_nf:+.3f}" if v_nf is not None else "—", 0.0,
           "주간 발표 금융여건. 음수=완화적. 예측력 없음 — 점수 미반영, 배경 설명용.",
           None if s_nf is None else rnd(1 - s_nf / 100, 3)),
    ]

    # b200: 자체 512종목 패널(data/stocks.json의 '200일이탈' 플래그로 역산)
    b200 = {"key": "b200", "label": "시장 폭(200일선 위 비율)", "raw": None, "raw_fmt": "—", "pctl": None,
            "score": None, "weight": 0.0, "contrib": 0.0,
            "desc": "S&P500 512종목 중 200일 이동평균 위 비율. 현 구성종목 기준이라 생존편향 존재, 5년 표본으로 예측력 검증 실패 — 점수 미반영.",
            "as_of": ld}
    try:
        sj = json.load(open(STOCKS, encoding="utf-8"))
        st = sj.get("stocks") or []
        if len(st) >= 100:
            below = sum(1 for s in st if "200일이탈" in (s.get("flags") or []))
            v = (1 - below / len(st)) * 100
            b200.update(raw=rnd(v, 1), raw_fmt=f"{v:.1f}%", as_of=sj.get("as_of") or ld)
            print(f"  b200 {v:.1f}% (stocks.json {len(st)}종목, {b200['as_of']})")
    except Exception as e:
        print("  b200 산출 실패(패널 표시만 결측):", e)
    panel.append(b200)

    n_ok = sum(1 for c in components if c["score"] is not None)
    print(f"컴포넌트 반영 {n_ok}/3 · 패널 {sum(1 for p in panel if p['raw'] is not None)}/4")

    # ── 백테스트 ───────────────────────────────────────────────────
    print("백테스트…")
    bt = backtest(score, score_pctl, comp["vix"], SPY, ws=ws)

    # ── history (3년 절단, 최소 2년) ───────────────────────────────
    cut = last - pd.Timedelta(days=int(365.25 * HIST_YEARS))
    hist = [{"dt": i.date().isoformat(), "score": float(v)} for i, v in sd.loc[cut:].items()]
    if len(hist) < 480:
        raise SystemExit(f"history {len(hist)}일 — 최소 2년 미달, 갱신 중단(이전본 유지)")

    prev = {"d1": rnd(sd.iloc[-2]) if len(sd) > 1 else None,
            "w1": rnd(sd.iloc[-6]) if len(sd) > 5 else None,
            "m1": rnd(sd.iloc[-22]) if len(sd) > 21 else None}
    spc = score_pctl.loc[last]

    out = {
        "as_of": ld,
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "score": rnd(cur, 1), "score_pctl": rnd(spc, 3),
        "label": lab, "label_en": lab_en,
        "prev": prev,
        "weights_used": dict(W),
        "weight_available": rnd(wav, 2),
        "components": components, "panel": panel,
        "history": hist,
        "backtest": bt,
        "sources": [
            {"key": "vix",     "name": "CBOE VIX",         "provider": "yfinance ^VIX / FRED VIXCLS", "since": "1990-01-02"},
            {"key": "vix_ts",  "name": "CBOE VIX3M",       "provider": "yfinance ^VIX3M",             "since": "2006-07-17"},
            {"key": "move",    "name": "ICE BofA MOVE",    "provider": "yfinance ^MOVE",              "since": "2002-11-12"},
            {"key": "hyg_ief", "name": "HYG, IEF",         "provider": "yfinance",                    "since": "2007-04-11"},
            {"key": "rsp_spy", "name": "RSP, SPY",         "provider": "yfinance",                    "since": "2003-05-01"},
            {"key": "nfci",    "name": "Chicago Fed NFCI", "provider": "FRED NFCI",                   "since": "1971-01-08"},
            {"key": "b200",    "name": "자체 512종목 패널", "provider": "yeouido-lab",                  "since": "2022-05-02"},
        ],
        "note": ("이 지수는 '현재 시장이 얼마나 겁먹었는가'를 요약한 상태 지표이며 수익 예측 신호가 아닙니다. "
                 "자체 백테스트 결과 (1) 극단공포 구간의 이후 60일 수익은 평균보다 높았으나 위험(변동성·낙폭)도 약 3배여서 "
                 "위험조정 후 우위는 사라졌고, (2) 합성 지수가 VIX 단독보다 낫지 않았으며, (3) 극단탐욕='매도'는 통계적 근거가 "
                 "없었습니다. 유효 표본은 일수가 아니라 독립 에피소드 수로 읽어야 하며, GFC를 포함한 2004~2009 구간에서는 "
                 "초과수익 부호가 뒤집혔습니다. 참고용이며 매매 권유가 아닙니다."),
        "disclaimer": "참고용 리서치입니다. 투자 권유·매매 신호가 아니며, 과거 통계가 미래를 보장하지 않습니다.",
    }

    # ── 완전성 게이트: 부분 장애 결과를 조용히 덮어쓰지 않는다(이전본 유지 + 워크플로 빨간불) ──
    try:
        old = json.load(open(OUT, encoding="utf-8"))
    except Exception:
        old = None
    if old:
        o_ok = sum(1 for c in (old.get("components") or []) if c.get("score") is not None)
        if n_ok < o_ok:
            raise SystemExit(f"반영 컴포넌트 급감 {o_ok}→{n_ok} (데이터 부분 장애 의심) — 갱신 중단, 이전본 유지")
        o_pn = sum(1 for p in (old.get("panel") or []) if p.get("raw") is not None)
        n_pn = sum(1 for p in panel if p["raw"] is not None)
        if n_pn < o_pn - 1:
            raise SystemExit(f"패널 지표 급감 {o_pn}→{n_pn} — 갱신 중단, 이전본 유지")
        o_h = len(old.get("history") or [])
        if o_h and len(hist) < o_h * 0.8:
            raise SystemExit(f"history 급감 {o_h}→{len(hist)}일 — 갱신 중단, 이전본 유지")
        if old.get("as_of") and old["as_of"] > ld:
            raise SystemExit(f"기준일 역행 {old['as_of']}→{ld} — 갱신 중단, 이전본 유지")

    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"→ {OUT} · 기준일 {ld} · score {out['score']} ({lab}, pctl {out['score_pctl']}) · "
          f"가용가중 {out['weight_available']} · history {len(hist)}일 · "
          f"IC60 합성 {bt['vs_vix_only']['ic60_composite']} vs VIX단독 {bt['vs_vix_only']['ic60_vix_only']}")


if __name__ == "__main__":
    main()
