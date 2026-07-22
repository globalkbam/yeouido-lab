# -*- coding: utf-8 -*-
"""전략 성과지표 엔진 v2 — 전략과 벤치마크를 **같은 기준으로** 산출한다.

왜 필요한가 (실측으로 확인한 결함 4건)
 1) Sharpe가 초과수익이 아니었다. 저장값이 `월간평균×12 ÷ 연변동성`과 소수 3자리까지 일치 →
    rf=0 산출. 왜곡폭 ≈ 연rf/vol 이므로 **저변동 전략일수록 부당하게 유리**했다.
 2) MDD만 일별 기준이었다. 같은 전략에 낙폭이 3개(일별 −29.6 / 일별의 월말샘플 −27.7 / 월말 −25.9)
    존재했고, 차트의 낙폭 곡선과 숫자 카드의 MDD가 서로 다른 계열이었다.
 3) 승률이 `r>0` 엄격 부등호가 아니었다(0에 가까운 월을 승리로 계수).
 4) CAGR만 실경과일수(365.25) 기준이고 나머지는 월간 기준이었다 → Calmar 분자로 전파.
 5) 마지막 월(2026-07 등)이 **미완결**인데 완전한 월로 연환산에 들어갔다. 랩 1순위 원칙(기준일 통일) 위반.

해법: 전 지표를 **월간 수익률 기준·연환산(√12)·무위험금리 차감 초과수익**으로 통일하고,
      전략·벤치1·벤치2를 **대칭 블록**으로 산출한다(사용자 요청: "sharpe도 mdd처럼 bm이랑 비교").

설계 원칙
 · 외부 의존 0 — 표준 라이브러리만 사용한다. GitHub Actions 러너는 Tailscale 밖이라
   사내 Postgres에 도달할 수 없다. 입력은 **이미 커밋된 월말 시계열**과 **FRED 공개 CSV**뿐이다.
 · 분모가 붕괴하는 지표(현금성 벤치의 베타·포착률 등)는 **숫자를 만들지 않고 사유를 남긴다.**
   무의미한 값(예: down_capture −11874%)을 화면에 띄우지 않는 것이 이 엔진의 계약이다.
 · 차이를 '우위'라 부르려면 표본이 그것을 판정할 수 있어야 한다 → sharpe_se·JK-Memmel·
   min_detectable_d_sharpe_95 를 항상 함께 굽는다.

사용법
    python build/strategy_metrics.py            # rf 갱신 + 재계산 + 기록
    python build/strategy_metrics.py --offline  # 네트워크 없이 캐시된 rf로만 재계산
    python build/strategy_metrics.py --dry-run  # 기록하지 않고 before/after만 출력
"""
import io, json, math, os, sys, argparse, datetime as dt, urllib.request

try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
RF_CACHE = os.path.join(DATA, "rf_monthly.json")
BT_PATH = os.path.join(DATA, "strategy_backtests.json")

# 무위험금리: FRED 3개월 국채(월평균 → 월복리). 사내 DB에는 단기금리 계열이 없다(10년물뿐).
FRED_IDS = ["DGS3MO", "DTB3", "TB3MS"]
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s"

MIN_MONTHS_FOR_COMPARE = 12      # 이보다 짧으면 vs-벤치 비교 자체를 만들지 않는다
CASHLIKE_VOL_PCT = 1.0           # 연변동성이 이 미만이면 회귀 기반 지표 분모 붕괴 → 생략
MIN_CAPTURE_MONTHS = 6           # 상승/하락 포착률 최소 표본
MIN_CAPTURE_DENOM = 1e-4         # 벤치 기하평균 |값|이 이 미만이면 포착률 분모 붕괴 → 생략


# ── 통계 헬퍼 (표준 라이브러리만) ──────────────────────────────
def _mean(a): return sum(a) / len(a)


def _std(a, ddof=1):
    n = len(a)
    if n - ddof <= 0: return float("nan")
    m = _mean(a)
    return math.sqrt(sum((x - m) ** 2 for x in a) / (n - ddof))


def _corr(a, b):
    ma, mb = _mean(a), _mean(b)
    sa = math.sqrt(sum((x - ma) ** 2 for x in a))
    sb = math.sqrt(sum((x - mb) ** 2 for x in b))
    if sa == 0 or sb == 0: return float("nan")
    return sum((a[i] - ma) * (b[i] - mb) for i in range(len(a))) / (sa * sb)


def _ncdf(z): return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _nppf(p):
    """표준정규 역함수 (Acklam 근사, |오차|<1.15e-9). scipy 없이 DSR·임계치를 굽기 위함."""
    if p <= 0.0 or p >= 1.0: return float("nan")
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00]
    pl, ph = 0.02425, 1 - 0.02425
    if p < pl:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > ph:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q, r = p - 0.5, (p - 0.5) ** 2
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def _ols(y, x):
    """단순회귀 y = a + b·x. 반환 (a, b, se_a, t_a). 표본이 부족하면 None."""
    n = len(y)
    if n < 3: return None
    mx, my = _mean(x), _mean(y)
    sxx = sum((v - mx) ** 2 for v in x)
    if sxx <= 0: return None
    beta = sum((x[i] - mx) * (y[i] - my) for i in range(n)) / sxx
    alpha = my - beta * mx
    resid = [y[i] - alpha - beta * x[i] for i in range(n)]
    s2 = sum(r * r for r in resid) / (n - 2)
    se_a = math.sqrt(max(s2, 0.0) * (1.0 / n + mx * mx / sxx))
    return alpha, beta, se_a, (alpha / se_a if se_a > 0 else float("nan"))


def _geo(a):
    """기하평균 월수익 — 포착률 분자·분모. 전액손실(−100%) 월이 있으면 정의 불가."""
    p = 1.0
    for r in a:
        if r <= -1.0: return None
        p *= (1.0 + r)
    return p ** (1.0 / len(a)) - 1.0


def _r(v, nd=2):
    if v is None: return None
    try:
        if math.isnan(v) or math.isinf(v): return None
    except TypeError:
        return None
    return round(v, nd)


# ── 무위험금리 (FRED, 인증 불필요 CSV) ─────────────────────────
def _fetch_fred(sid, timeout=60):
    with urllib.request.urlopen(FRED_URL % sid, timeout=timeout) as r:
        txt = r.read().decode("utf-8", "replace")
    out = []
    for ln in txt.splitlines()[1:]:
        p = ln.split(",")
        if len(p) < 2: continue
        try: v = float(p[1])
        except ValueError: continue          # 결측은 '.'
        out.append((p[0].strip()[:10], v))
    if len(out) < 100: raise RuntimeError("FRED %s: 관측 %d개 — 비정상" % (sid, len(out)))
    return out


def load_rf(offline=False):
    """월간 무위험수익률 {'YYYY-MM': 월복리 수익률} 을 돌려준다.

    일별 연율 → 월평균 → (1+y)^(1/12)−1. **y/12 단순분할 금지**(5%대에서 연 0.1%p 이상 어긋난다).
    네트워크 실패 시 캐시로 폴백하고 stale 플래그를 남긴다 — 조용히 rf=0으로 떨어지지 않는다.
    """
    cache = None
    if os.path.exists(RF_CACHE):
        try: cache = json.load(io.open(RF_CACHE, encoding="utf-8"))
        except Exception: cache = None
    if offline:
        if not cache: raise SystemExit("--offline 인데 data/rf_monthly.json 캐시가 없다")
        cache["stale"] = True
        return cache
    err = []
    for sid in FRED_IDS:
        try:
            obs = _fetch_fred(sid)
        except Exception as e:
            err.append("%s: %s" % (sid, e)); continue
        by_m = {}
        for d, v in obs:
            by_m.setdefault(d[:7], []).append(v / 100.0)
        monthly = {m: round((1.0 + _mean(vs)) ** (1.0 / 12.0) - 1.0, 8) for m, vs in sorted(by_m.items())}
        doc = {"source": "FRED " + sid, "series_id": sid,
               "url": FRED_URL % sid,
               "fetched": dt.date.today().isoformat(),
               "first_obs": obs[0][0], "last_obs": obs[-1][0],
               "n_obs": len(obs), "stale": False,
               "convert": "일별 연율 → 월평균 → (1+y)^(1/12)-1 (월복리)",
               "monthly": monthly}
        return doc
    if cache:
        cache["stale"] = True
        cache["stale_reason"] = "; ".join(err)
        sys.stderr.write("⚠ FRED 조달 실패 — 캐시 사용(stale): %s\n" % cache["stale_reason"])
        return cache
    raise SystemExit("무위험금리 조달 실패, 캐시도 없음: %s" % "; ".join(err))


# ── 월간 계열 ─────────────────────────────────────────────────
def month_end(ym):
    y, m = int(ym[:4]), int(ym[5:7])
    return (dt.date(y + (m == 12), (m % 12) + 1, 1) - dt.timedelta(days=1))


def complete_upto(dates, end_iso):
    """미완결 월을 잘라낸 뒤의 마지막 인덱스. (_common.drop_incomplete_month 와 같은 규약)

    end_iso 가 그 달의 말일보다 앞서면 마지막 월은 진행 중이므로 지표에서 제외한다.
    """
    if not dates: return -1
    last = dates[-1]
    try: e = dt.date.fromisoformat(str(end_iso)[:10])
    except Exception: return len(dates) - 1
    if e < month_end(last): return len(dates) - 2
    return len(dates) - 1


def rets(nav):
    return [nav[i] / nav[i - 1] - 1.0 for i in range(1, len(nav))]


def dd_series(nav):
    out, pk = [], nav[0]
    for v in nav:
        if v > pk: pk = v
        out.append(v / pk - 1.0)
    return out


def _mdd_block(nav):
    dd = dd_series(nav)
    i_tr = min(range(len(dd)), key=lambda i: dd[i])
    mdd = dd[i_tr]
    i_pk = max(range(i_tr + 1), key=lambda i: nav[i]) if i_tr > 0 else 0
    rec, uw = None, None
    for j in range(i_tr + 1, len(nav)):
        if nav[j] >= nav[i_pk]:
            rec = j - i_tr; break
    if rec is None:
        uw = (len(nav) - 1) - i_pk
    return mdd, rec, uw, dd


# ── 단일 계열 지표 블록 (전략·벤치1·벤치2 대칭) ────────────────
def series_block(nav, months, rf_m, label=None):
    """nav: 월말 NAV(첫 점이 기준 100), months: nav와 같은 길이의 'YYYY-MM',
    rf_m: {'YYYY-MM': 월복리 rf}. 수익률 r[i]는 months[i+1] 달의 것으로 정렬한다."""
    r = rets(nav)
    N = len(r)
    rf = [rf_m.get(months[i + 1], 0.0) for i in range(N)]
    ex = [r[i] - rf[i] for i in range(N)]

    vol = _std(r) * math.sqrt(12) * 100
    cagr = ((math.prod([1 + x for x in r])) ** (12.0 / N) - 1) * 100 if N else float("nan")
    cum = (nav[-1] / nav[0] - 1) * 100
    dn = math.sqrt(_mean([min(x, 0.0) ** 2 for x in ex])) * math.sqrt(12) * 100
    mdd, rec, uw, dd = _mdd_block(nav)
    ulcer = math.sqrt(_mean([(d * 100) ** 2 for d in dd]))
    sd_ex = _std(ex) * math.sqrt(12)
    sharpe = (_mean(ex) * 12) / sd_ex if sd_ex > 0 else float("nan")
    # Lo(2002) SE 식 √((1+SR²/2)/N) 은 **주기(월간) Sharpe**의 표준오차다.
    # 연환산 Sharpe를 그대로 넣으면 눈금이 섞여 SE가 2.7~3.0배 과소 산출된다
    # (몬테카를로 20,000회 검산: 실측 0.3230 vs 잘못된 식 0.1217 vs 올바른 식 0.3207).
    # 같은 파일 dsr_chance_sharpe()는 √(1/N)…×√12로 이미 올바르게 연환산하고 있었다.
    _srm = sharpe / math.sqrt(12)                      # 월간 Sharpe로 환산 후 SE 계산
    se = (math.sqrt(12 * (1 + _srm ** 2 / 2) / N)
          if N > 0 and not math.isnan(sharpe) else float("nan"))
    sortino = (_mean(ex) * 12 * 100) / dn if dn > 0 else None
    w12 = [nav[i] / nav[i - 12] - 1 for i in range(12, len(nav))]
    rf_ann = (math.prod([1 + x for x in rf]) ** (12.0 / N) - 1) * 100

    return {
        "label": label,
        "n_months": N,
        # 현금성 계열은 초과수익 분산이 거의 0이라 Sharpe류 비율이 발산한다 — 비교층에서 걸러내기 위한 표식
        "cashlike": bool(vol < CASHLIKE_VOL_PCT),
        "cagr": _r(cagr), "cum": _r(cum, 1),
        "vol": _r(vol), "downside_dev": _r(dn),
        "mdd": _r(mdd * 100), "ulcer": _r(ulcer), "recovery_m": rec, "underwater_m": uw,
        "sharpe": _r(sharpe, 3), "sharpe_se": _r(se, 3),
        "sortino": _r(sortino, 3),
        "calmar": _r(cagr / abs(mdd * 100), 3) if mdd < 0 else None,
        "martin": _r(cagr / ulcer, 3) if ulcer > 0 else None,
        "worst12": _r(min(w12) * 100) if w12 else None,
        "best12": _r(max(w12) * 100) if w12 else None,
        "hit": _r(100.0 * sum(1 for x in r if x > 0) / N, 1),
        "rf_ann": _r(rf_ann),
        "_ex": ex, "_r": r,          # 관계 지표 계산용(직렬화 전에 제거)
    }


# ── 관계 지표 (전략 vs 벤치) ──────────────────────────────────
def vs_block(S, B):
    """정의상 '전략 vs 벤치'가 값 자체인 지표들(베타·알파·TE·IR·포착률·ΔSharpe).
    분모가 붕괴하는 경우 **숫자를 만들지 않고** omitted 사유를 남긴다."""
    N = S["n_months"]
    out = {"bm": B["label"], "n_months": N, "omitted": {}}
    if N < MIN_MONTHS_FOR_COMPARE:
        out["omitted"]["all"] = "표본 %d개월 — %d개월 미만이라 벤치 비교를 산출하지 않음" % (N, MIN_MONTHS_FOR_COMPARE)
        return out

    es, eb = S["_ex"], B["_ex"]
    rs, rb = S["_r"], B["_r"]
    rho = _corr(es, eb)
    out["corr"] = _r(rho, 3)

    if B.get("cashlike"):
        # 벤치 초과수익 분산이 거의 0 → Sharpe가 0/0에 가까워 발산한다. 비교 자체를 만들지 않는다.
        out["omitted"]["d_sharpe"] = ("벤치마크 연변동성이 %.2f%%인 현금성 계열이라 Sharpe 비교가 "
                                      "0/0에 가까워 발산 — 산출을 생략함" % (B["vol"] or 0))
        out["omitted"]["regression"] = out["omitted"]["d_sharpe"]
        te0 = _std([rs[i] - rb[i] for i in range(N)]) * math.sqrt(12) * 100
        out["te"] = _r(te0)
        out["ir"] = _r(_mean([rs[i] - rb[i] for i in range(N)]) * 12 * 100 / te0, 3) if te0 > 0 else None
        return out

    # ΔSharpe — Jobson-Korkie / Memmel(2003) 상관 반영 검정
    srs_m = (S["sharpe"] or 0) / math.sqrt(12)
    srb_m = (B["sharpe"] or 0) / math.sqrt(12)
    d = (S["sharpe"] or 0) - (B["sharpe"] or 0)
    out["d_sharpe"] = _r(d, 3)
    if not math.isnan(rho):
        v = (2 - 2 * rho + 0.5 * (srs_m ** 2 + srb_m ** 2 - 2 * srs_m * srb_m * rho ** 2)) / N
        if v > 0:
            z = (srs_m - srb_m) / math.sqrt(v)
            out["d_sharpe_z"] = _r(z, 3)
            out["d_sharpe_p"] = _r(2 * (1 - _ncdf(abs(z))), 4)
    # 이 표본이 판정할 수 있는 최소 ΔSharpe(보수적 상한: 상관 미반영)
    if S["sharpe_se"] is not None and B["sharpe_se"] is not None:
        out["min_detectable_d_sharpe_95"] = _r(1.96 * math.sqrt(S["sharpe_se"] ** 2 + B["sharpe_se"] ** 2), 3)
    p = out.get("d_sharpe_p")
    md = out.get("min_detectable_d_sharpe_95")
    out["d_sharpe_sig"] = bool(p is not None and p < 0.05 and md is not None and abs(d) >= md)
    out["d_sharpe_note"] = ("우위/열위를 말할 수 있음" if out["d_sharpe_sig"]
                            else "N=%d개월 표본에서 이 차이는 우연과 구별되지 않음(최소 판정치 ±%s)"
                                 % (N, "—" if md is None else ("%.2f" % md)))

    # 추적오차·정보비율은 현금성 벤치에서도 정의된다(회귀가 아니라 차분)
    te = _std([rs[i] - rb[i] for i in range(N)]) * math.sqrt(12) * 100
    out["te"] = _r(te)
    out["ir"] = _r(_mean([rs[i] - rb[i] for i in range(N)]) * 12 * 100 / te, 3) if te > 0 else None

    # 회귀 기반 지표 — 벤치가 현금성이면 분모(벤치 분산)가 붕괴한다
    if (B["vol"] or 0) < CASHLIKE_VOL_PCT:
        out["omitted"]["regression"] = ("벤치마크 연변동성이 %.2f%%인 현금성 계열이라 베타·알파·"
                                        "상승/하락 포착률의 분모가 붕괴 — 산출을 생략함" % (B["vol"] or 0))
        return out

    o = _ols(es, eb)
    if o:
        a_m, beta, _se, t = o
        out["beta"] = _r(beta, 3)
        out["alpha"] = _r(((1 + a_m) ** 12 - 1) * 100)
        out["alpha_t"] = _r(t, 2)
        out["alpha_p"] = _r(2 * (1 - _ncdf(abs(t))), 4)

    up = [i for i in range(N) if rb[i] > 0]
    dnm = [i for i in range(N) if rb[i] < 0]
    for key, idx, nm in (("up_capture", up, "상승"), ("down_capture", dnm, "하락")):
        if len(idx) < MIN_CAPTURE_MONTHS:
            out["omitted"][key] = "벤치 %s월이 %d개뿐 — 최소 %d개 미만이라 생략" % (nm, len(idx), MIN_CAPTURE_MONTHS)
            continue
        gb = _geo([rb[i] for i in idx])
        gs = _geo([rs[i] for i in idx])
        if gb is None or gs is None or abs(gb) < MIN_CAPTURE_DENOM:
            out["omitted"][key] = ("벤치 %s월 기하평균이 %.4f%%로 0에 근접 — 비율이 발산하므로 생략"
                                   % (nm, (gb or 0) * 100))
            continue
        out[key] = _r(gs / gb * 100, 1)
    return out


# ── 다중검정 (같은 파일 안의 전략을 한 번에 비교하면 m=전략수) ──
def dsr_chance_sharpe(M, N):
    """진짜 Sharpe=0인 전략을 M회 시도해 최고치를 고를 때 기대되는 연환산 Sharpe
    (Bailey & López de Prado). '이보다 낮으면 우연의 기댓값에도 못 미친다'는 눈금."""
    if M < 2 or N < 2: return None
    g = 0.5772156649
    v = math.sqrt(1.0 / N)
    e = math.e
    return _r(v * ((1 - g) * _nppf(1 - 1.0 / M) + g * _nppf(1 - 1.0 / (M * e))) * math.sqrt(12), 3)


def apply_multiplicity(entries, q=0.10):
    """entries: [(key, p_raw, N)]. BH-FDR(q) + Bonferroni 문턱을 각 전략에 되돌려준다."""
    valid = [(k, p, n) for k, p, n in entries if p is not None]
    m = len(valid)
    out = {}
    if not m: return out
    ranked = sorted(valid, key=lambda t: t[1])
    for rank, (k, p, n) in enumerate(ranked, start=1):
        thr = q * rank / m
        out[k] = {"source": "사이트 게시 전략 일괄 비교 (벤치1 대비 ΔSharpe)",
                  "n_tests": m, "p_raw": p, "bh_rank": rank, "bh_q": q,
                  "bh_threshold": _r(thr, 4), "passed": bool(p <= thr),
                  "bonferroni_threshold": _r(0.05 / m, 4),
                  "dsr_chance_sharpe": dsr_chance_sharpe(m, n),
                  "note": "다중검정 미보정 상태로 '초과수익'이라 서술 금지. "
                          "dsr_chance_sharpe = 진짜 Sharpe 0인 전략을 n_tests회 시행해 "
                          "최고치를 고를 때 기대되는 연환산 Sharpe."}
    return out


# ── 전략 1건 처리 ─────────────────────────────────────────────
def compute_strategy(bt, rf_m):
    dates, nav = bt["dates"], bt["nav"]
    cut = complete_upto(dates, bt.get("end"))
    if cut < 2: raise ValueError("완결 월이 너무 적다")
    dropped = dates[cut + 1] if cut < len(dates) - 1 else None
    mo = dates[:cut + 1]

    S = series_block(nav[:cut + 1], mo, rf_m, "전략")
    B = series_block(bt["bench"][:cut + 1], mo, rf_m, bt.get("bench_label") or "벤치")
    B2 = series_block(bt["bench2"][:cut + 1], mo, rf_m, bt.get("bench2_label")) if bt.get("bench2") else None

    vs = vs_block(S, B)
    vs2 = vs_block(S, B2) if B2 else None
    for blk in (S, B, B2):
        if blk: blk.pop("_ex", None); blk.pop("_r", None)

    m = {
        # ── v1 호환 축약본(구 UI·db_load.py가 읽는 평면 키). 값은 v2와 동일 기준으로 교체됨 ──
        "cagr": S["cagr"], "vol": S["vol"], "sharpe": S["sharpe"], "mdd": S["mdd"],
        "calmar": S["calmar"], "hit": S["hit"],
        # ── v2 대칭 블록 ──
        "schema": "v2",
        "s": S, "b": B, "b2": B2,
        "bm_label": B["label"], "bm2_label": (B2 or {}).get("label"),
        "vs": vs, "vs2": vs2,
        "basis": {
            "freq": "monthly", "ann_factor": 12, "cost": "gross", "excess": True,
            "rf": "DGS3MO", "rf_source": "FRED DGS3MO", "rf_ann_pct": S["rf_ann"],
            "n_months": S["n_months"], "start_month": mo[0], "end_month": mo[-1],
            "mdd_basis": "monthly_nav",
            "dropped_incomplete_month": dropped,
            "sample_warning": bool(S["n_months"] < 60),
            "note": "전 지표 월간 수익률 기준·연환산(√12)·gross(무비용). 위험조정지표는 무위험금리 "
                    "차감 초과수익 기준. MDD·Ulcer·회복기간도 월말 NAV 기준으로 통일.",
        },
    }
    return m, cut, dropped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="네트워크 없이 캐시된 rf로만 재계산")
    ap.add_argument("--dry-run", action="store_true", help="파일에 쓰지 않고 before/after만 출력")
    a = ap.parse_args()

    rf_doc = load_rf(offline=a.offline)
    rf_m = rf_doc["monthly"]

    bt = json.load(io.open(BT_PATH, encoding="utf-8"))
    # 갱신 전 스냅샷 — 실질 내용이 안 바뀌었으면 타임스탬프도 건드리지 않기 위함(아래 참조)
    _prev_gen = bt.get("generated")
    _prev_body = json.dumps({k: v for k, v in bt.items() if k != "generated"},
                            ensure_ascii=False, sort_keys=True)
    S = bt.get("strategies") or {}
    before = {k: dict(v.get("metrics") or {}) for k, v in S.items()}

    rows, pend = [], []
    for name, b in S.items():
        m, cut, dropped = compute_strategy(b, rf_m)
        b["metrics"] = m
        # 낙폭 곡선을 지표와 **같은 계열**로 통일한다(구 dd는 일별 낙폭의 월말 샘플이라 카드값과 어긋났다)
        b["dd"] = [round(x * 100, 1) for x in dd_series(b["nav"])]
        b["dd_b"] = [round(x * 100, 1) for x in dd_series(b["bench"])]
        b["mdd_b"] = m["b"]["mdd"]
        if b.get("bench2"):
            b["dd_b2"] = [round(x * 100, 1) for x in dd_series(b["bench2"])]
            b["mdd_b2"] = m["b2"]["mdd"]
        b["dd_basis"] = "monthly_nav"
        b["partial_month"] = dropped          # 차트에는 남기되 '진행 중'으로 라벨링하기 위한 표식
        rows.append((name, m))
        pend.append((name, (m["vs"] or {}).get("d_sharpe_p"), m["basis"]["n_months"]))

    mult = apply_multiplicity(pend)
    for name, m in rows:
        m["multiplicity"] = mult.get(name)

    bt["metrics_schema"] = "v2"
    bt["metrics_basis"] = ("월간 기준·연환산 · gross(무비용) · 무위험금리(%s) 차감 초과수익 · "
                           "MDD/Ulcer/회복기간 월말 NAV 기준 · 미완결 월 제외" % rf_doc["source"])

    # generated 를 무조건 오늘로 덮으면 내용이 그대로여도 파일이 매번 바뀌어, 워크플로의
    # '변경 없음 — 스킵' 분기가 영영 도달 불가해지고 무의미한 커밋이 매주 쌓인다.
    _body = json.dumps({k: v for k, v in bt.items() if k != "generated"},
                       ensure_ascii=False, sort_keys=True)
    bt["generated"] = _prev_gen if (_body == _prev_body and _prev_gen) else dt.date.today().isoformat()

    if a.dry_run:
        print("[dry-run] 파일 미기록")
    else:
        with io.open(BT_PATH, "w", encoding="utf-8") as f:
            json.dump(bt, f, ensure_ascii=False, separators=(",", ":"))
            f.write("\n")
        with io.open(RF_CACHE, "w", encoding="utf-8") as f:
            json.dump(rf_doc, f, ensure_ascii=False, separators=(",", ":"))
            f.write("\n")

    # ── before/after 보고 ──────────────────────────────────
    print("무위험금리: %s (%s ~ %s, %d관측%s)" % (rf_doc["source"], rf_doc["first_obs"],
          rf_doc["last_obs"], rf_doc["n_obs"], ", STALE" if rf_doc.get("stale") else ""))
    hdr = "%-34s %5s %7s %7s %7s %7s %7s" % ("전략", "N", "CAGR", "Sharpe", "MDD", "Calmar", "승률")
    print(hdr); print("-" * len(hdr))
    for name, m in rows:
        o = before.get(name) or {}
        s = m["s"]
        short = (name[:16] + "…") if len(name) > 17 else name
        print("%-18s before %5s %7s %7s %7s %7s %7s" % (short, "—", o.get("cagr"), o.get("sharpe"),
              o.get("mdd"), o.get("calmar"), o.get("hit")))
        print("%-18s after  %5d %7s %7s %7s %7s %7s   Δsharpe %+.3f" % ("", s["n_months"], s["cagr"],
              s["sharpe"], s["mdd"], s["calmar"], s["hit"],
              (s["sharpe"] or 0) - (o.get("sharpe") or 0)))
        vs = m["vs"] or {}
        print("      vs %-12s ΔSharpe %+0.3f (p=%s, 최소판정 ±%s) → %s | rf %.2f%% | 제외월 %s" % (
            (m["bm_label"] or "")[:12], vs.get("d_sharpe") or 0, vs.get("d_sharpe_p"),
            vs.get("min_detectable_d_sharpe_95"), "유의" if vs.get("d_sharpe_sig") else "구별 불가",
            m["basis"]["rf_ann_pct"] or 0, m["basis"]["dropped_incomplete_month"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
