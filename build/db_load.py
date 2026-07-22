#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
yeouido-lab · Postgres 누적 적재기 (schema: yeodoo)
=====================================================================
왜 이런 구조인가
  GitHub Actions 러너는 Tailscale tailnet 밖이라 100.88.75.91 에 도달할 수 없다.
  그래서 사이트 생성기(refresh_*.py)는 DB를 전혀 모르게 두고, 이 로더만
  tailnet 안 머신에서 돌린다. 매 영업일 산출물은 git에 커밋되므로 git 이력이
  곧 일별 아카이브 → 적재 머신이 며칠 꺼져 있어도 --backfill 한 번으로 복구된다.
  DB가 죽어도 사이트는 멀쩡하고, 사이트가 죽어도 DB는 멀쩡하다.

사용법
  python3 build/db_load.py --init        # 스키마·테이블 생성 (최초 1회, 멱등)
  python3 build/db_load.py               # 워킹트리의 data/*.json 적재
  python3 build/db_load.py --backfill    # git 이력 전체를 되짚어 과거분 적재
  python3 build/db_load.py --backfill --force   # 이미 적재된 as_of도 덮어쓰기
  python3 build/db_load.py --stats       # 적재 현황 요약

접속 정보 (⚠ 이 저장소는 공개다 — 비밀번호를 여기 절대 넣지 말 것)
  1순위 환경변수 YEOUIDO_DB_HOST/PORT/NAME/USER/PASS
  2순위 ~/.yeouido_db.env          (KEY=VALUE 줄 나열, 저장소 밖)
  3순위 연구 repo의 util/variables.py  (자격증명 단일 출처 — 복제하지 않는다)
        경로는 YEOUIDO_REPO 로 조정, 기본 ~/Project/Yeouido
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")

# ── 접속 정보 ────────────────────────────────────────────────────────
ENV_FILE = os.path.expanduser("~/.yeouido_db.env")


def _from_research_repo():
    """연구 repo(util/variables.py)의 DB 설정을 재사용. 자격증명을 두 곳에
    복제하지 않기 위한 폴백 — 이 파일에도, 저장소 어디에도 비밀번호는 없다."""
    repo = os.path.expanduser(os.getenv("YEOUIDO_REPO") or "~/Project/Yeouido")
    if not os.path.exists(os.path.join(repo, "util", "variables.py")):
        return {}
    sys.path.insert(0, repo)
    try:
        import util.variables as V           # noqa: WPS433
        return {"YEOUIDO_DB_NAME": getattr(V, "database", None),
                "YEOUIDO_DB_USER": getattr(V, "user", None),
                "YEOUIDO_DB_PASS": getattr(V, "password", None)}
    except Exception:
        return {}
    finally:
        sys.path.remove(repo)


def _conn_params():
    cfg = {}
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip("'\"")
    # 환경변수가 파일보다 우선. GitHub Actions는 미설정 시크릿을 빈 문자열로 넣으므로
    # os.getenv(k, default) 가 아니라 `or` 로 폴백해야 한다(과거 FRED 키 사고의 교훈).
    def g(k, d=None):
        return os.getenv(k) or cfg.get(k) or d

    if not g("YEOUIDO_DB_USER") or not g("YEOUIDO_DB_PASS"):
        for k, v in _from_research_repo().items():
            if v and not cfg.get(k):
                cfg[k] = str(v)
    p = dict(
        host=g("YEOUIDO_DB_HOST", "100.88.75.91"),
        port=int(g("YEOUIDO_DB_PORT", "5432")),
        dbname=g("YEOUIDO_DB_NAME", "postgres"),
        user=g("YEOUIDO_DB_USER"),
        password=g("YEOUIDO_DB_PASS"),
        connect_timeout=10,
    )
    if not p["user"] or not p["password"]:
        sys.exit(
            "✗ DB 접속 정보 없음. 다음 중 하나를 준비하세요:\n"
            "    · 환경변수 YEOUIDO_DB_USER / YEOUIDO_DB_PASS\n"
            f"    · {ENV_FILE} (KEY=VALUE)\n"
            "    · 연구 repo util/variables.py (YEOUIDO_REPO 로 경로 지정)\n"
        )
    return p


def connect():
    import psycopg2
    return psycopg2.connect(**_conn_params())


# ── git 헬퍼 ────────────────────────────────────────────────────────
def _git(*args):
    return subprocess.run(["git", "-C", ROOT, *args],
                          capture_output=True, text=True)


def read_at(sha, relpath):
    """특정 커밋의 파일 내용을 JSON으로. 없으면 None (예외 없음)."""
    if sha is None:                                  # 워킹트리
        try:
            with open(os.path.join(ROOT, relpath), encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return None
    r = _git("show", f"{sha}:{relpath}")
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except Exception:
        return None


def commits_for(relpath):
    """해당 파일을 건드린 커밋을 과거→현재 순으로."""
    r = _git("log", "--reverse", "--format=%H", "--", relpath)
    return r.stdout.split() if r.returncode == 0 else []


# ── 값 정리 ─────────────────────────────────────────────────────────
def num(v):
    """NaN/inf/문자를 전부 None으로. Postgres double precision은 NaN을 받지만
    이후 집계에서 조용히 오염시키므로 입구에서 잘라낸다."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def _log(cur, source, asof, sha, n, status, msg=""):
    cur.execute(
        "insert into yeodoo.load_log(source,asof,git_sha,n_rows,status,message)"
        " values(%s,%s,%s,%s,%s,%s)",
        (source, asof, (sha or "worktree")[:12], n, status, msg[:500]))


# ── 적재: 종목 / 펀더멘털 / 목표주가 / 스윙마커 ───────────────────────
_HEAD_ASOF = []


def head_asof():
    """HEAD(=현재 검증된 빌드)의 as_of. 이보다 미래인 스냅샷은 철회분뿐이다."""
    if not _HEAD_ASOF:
        _HEAD_ASOF.append((read_at(None, "data/stocks.json") or {}).get("as_of"))
    return _HEAD_ASOF[0]


def load_stocks(cur, doc, sha, force=False):
    from psycopg2.extras import Json, execute_values
    asof = doc.get("as_of")
    rows = doc.get("stocks") or []
    if not asof or not rows:
        return 0
    # 철회된 미래 as_of는 아예 넣지 않는다. (넣고 purge하면 매 백필마다 512행
    # 삽입→삭제 churn이 돌고 ⚠ 경고가 매일 찍혀 무뎌진다.)
    if head_asof() and asof > head_asof():
        return 0
    if not force:
        cur.execute("select 1 from yeodoo.stock_daily where asof=%s limit 1", (asof,))
        if cur.fetchone():
            return 0

    dates = doc.get("pxd_dates") or []
    sd, fd, td, mk = [], [], [], []
    for s in rows:
        t = s.get("t")
        if not t:
            continue
        c = s.get("comp") or {}
        sd.append((asof, t, s.get("name"), s.get("sector"), s.get("idx") or [],
                   s.get("timing"), num(c.get("overheat")), num(c.get("trend")),
                   num(c.get("momentum")), num(c.get("volatility")),
                   num(c.get("positioning")), num(s.get("bscore")), num(s.get("sscore")),
                   s.get("flags") or [], Json(s)))

        f = s.get("fund") or {}
        if f:
            fd.append((asof, t, num(f.get("teps")), num(f.get("feps")),
                       num(f.get("tpe")), num(f.get("fpe")), num(f.get("gr")), Json(f)))
            if f.get("tpm") is not None:
                n_an = f.get("nan")
                td.append((asof, t, num(f.get("tpm")), num(f.get("tph")), num(f.get("tpl")),
                           int(n_an) if isinstance(n_an, (int, float)) and n_an == n_an else None,
                           f.get("rk"), num(f.get("up"))))

        # 스윙 마커: bms/sms=확정, bmw/smw=잠정. 값은 pxd 인덱스 → pxd_dates로 환산.
        for key, side, prov in (("bms", "buy", False), ("bmw", "buy", True),
                                ("sms", "sell", False), ("smw", "sell", True)):
            for pos in (s.get(key) or []):
                if isinstance(pos, int) and 0 <= pos < len(dates):
                    mk.append((t, dates[pos], side, asof, prov))

    execute_values(cur, """
        insert into yeodoo.stock_daily
          (asof,ticker,name,sector,idx,timing,overheat,trend,momentum,volatility,
           positioning,bscore,sscore,flags,raw)
        values %s
        on conflict (asof,ticker) do update set
          name=excluded.name, sector=excluded.sector, idx=excluded.idx,
          timing=excluded.timing, overheat=excluded.overheat, trend=excluded.trend,
          momentum=excluded.momentum, volatility=excluded.volatility,
          positioning=excluded.positioning, bscore=excluded.bscore,
          sscore=excluded.sscore, flags=excluded.flags, raw=excluded.raw,
          loaded_at=now()""", sd, page_size=500)
    _log(cur, "stocks", asof, sha, len(sd), "ok")

    if fd:
        execute_values(cur, """
            insert into yeodoo.fundamental_daily(asof,ticker,teps,feps,tpe,fpe,gr,raw)
            values %s
            on conflict (asof,ticker) do update set
              teps=excluded.teps, feps=excluded.feps, tpe=excluded.tpe,
              fpe=excluded.fpe, gr=excluded.gr, raw=excluded.raw, loaded_at=now()""",
                       fd, page_size=500)
        _log(cur, "fundamental", asof, sha, len(fd), "ok")

    if td:
        execute_values(cur, """
            insert into yeodoo.target_daily
              (asof,ticker,tp_mean,tp_high,tp_low,n_analyst,rec_key,upside_pct)
            values %s
            on conflict (asof,ticker) do update set
              tp_mean=excluded.tp_mean, tp_high=excluded.tp_high, tp_low=excluded.tp_low,
              n_analyst=excluded.n_analyst, rec_key=excluded.rec_key,
              upside_pct=excluded.upside_pct""", td, page_size=500)
        _log(cur, "target", asof, sha, len(td), "ok")

    if mk:
        # 같은 스냅샷 안에서 (ticker,bar_date,side) 중복 제거 — 확정이 잠정을 이긴다.
        # (ON CONFLICT는 한 statement 안의 중복 키를 처리하지 못한다)
        best = {}
        for t, bd, side, a, prov in mk:
            k = (t, bd, side)
            if k not in best or (best[k][4] and not prov):
                best[k] = (t, bd, side, a, prov)
        execute_values(cur, """
            insert into yeodoo.swing_marker
              (ticker,bar_date,side,first_seen,last_seen,ever_provisional,first_confirmed)
            select x.ticker,x.bar_date,x.side,x.asof,x.asof,x.prov,
                   case when x.prov then null else x.asof end
            from (values %s) as x(ticker,bar_date,side,asof,prov)
            on conflict (ticker,bar_date,side) do update set
              last_seen = greatest(yeodoo.swing_marker.last_seen, excluded.last_seen),
              first_seen = least(yeodoo.swing_marker.first_seen, excluded.first_seen),
              ever_provisional = yeodoo.swing_marker.ever_provisional or excluded.ever_provisional,
              first_confirmed = coalesce(yeodoo.swing_marker.first_confirmed,
                                         excluded.first_confirmed)""",
                       list(best.values()),
                       template="(%s,%s::date,%s,%s::date,%s::boolean)", page_size=500)
        _log(cur, "swing", asof, sha, len(best), "ok")
    return len(sd)


def load_simple(cur, doc, sha, table, cols, force=False):
    """regime_daily / sentiment_daily 처럼 as_of 1행짜리 문서."""
    from psycopg2.extras import Json
    asof = doc.get("as_of") if doc else None
    if not asof:
        return 0
    if head_asof() and asof > head_asof():      # 철회된 미래 스냅샷 차단
        return 0
    if not force:
        cur.execute(f"select 1 from yeodoo.{table} where asof=%s", (asof,))
        if cur.fetchone():
            return 0
    vals = [asof] + [cols[c](doc) for c in cols] + [Json(doc)]
    names = ",".join(["asof"] + list(cols) + ["raw"])
    ph = ",".join(["%s"] * len(vals))
    upd = ",".join(f"{c}=excluded.{c}" for c in list(cols) + ["raw"])
    cur.execute(f"insert into yeodoo.{table}({names}) values({ph}) "
                f"on conflict(asof) do update set {upd}, loaded_at=now()", vals)
    _log(cur, table.replace("_daily", ""), asof, sha, 1, "ok")
    return 1


def load_target_history(cur, doc, sha):
    """target_history.json 의 과거 스냅샷 — git 이전 시점까지 커버하는 유일한 소스."""
    from psycopg2.extras import execute_values
    n = 0
    for snap in (doc or {}).get("snaps") or []:
        d, tp = snap.get("d"), snap.get("tp") or {}
        if not d or not tp:
            continue
        cur.execute("select 1 from yeodoo.target_daily where asof=%s limit 1", (d,))
        if cur.fetchone():                     # 이미 있는 날짜는 건너뛴다(로그 부풀림 방지)
            continue
        rows = [(d, t, num(v)) for t, v in tp.items() if num(v) is not None]
        if not rows:
            continue
        # 이미 stocks 경로로 들어온 풍부한 행(고가·저가·애널리스트수 포함)은 덮지 않는다.
        execute_values(cur, """
            insert into yeodoo.target_daily(asof,ticker,tp_mean) values %s
            on conflict (asof,ticker) do nothing""", rows, page_size=500)
        n += len(rows)
    if n:
        _log(cur, "target_history", None, sha, n, "ok")
    return n


REGIME_COLS = {"regime": lambda d: (d.get("regime") or {}).get("label")
               if isinstance(d.get("regime"), dict) else d.get("regime")}
SENT_COLS = {"score": lambda d: num(d.get("score")),
             "score_pctl": lambda d: num(d.get("score_pctl")),
             "label": lambda d: d.get("label")}


def purge_retracted(cur):
    """철회된 as_of 제거.

    사고 사례: 미 동부 장중에 크론이 돌아 미확정 당일 봉으로 as_of=07-21 스냅샷이
    커밋됐다가, 가드 수정 후 다시 07-20으로 되돌아갔다. git 이력을 훑으면 이 철회된
    스냅샷까지 적재된다. HEAD(=현재 검증된 상태)의 as_of보다 미래인 행은 철회분
    말고는 존재할 수 없으므로 안전하게 지운다. 사용자의 1순위 원칙(기준일 통일)."""
    head = (read_at(None, "data/stocks.json") or {}).get("as_of")
    if not head:
        return
    for tbl in ("stock_daily", "fundamental_daily", "target_daily"):
        cur.execute(f"delete from yeodoo.{tbl} where asof > %s", (head,))
        n = cur.rowcount                     # _log의 INSERT가 rowcount를 덮으므로 먼저 붙잡는다
        if n:
            _log(cur, tbl.replace("_daily", ""), head, None, n,
                 "retracted", f"HEAD as_of {head} 이후 철회분 삭제")
            print(f"  ⚠ {tbl}: 철회된 미래 as_of {n}행 삭제 (기준일 통일)")
    cur.execute("delete from yeodoo.swing_marker where first_seen > %s", (head,))
    for tbl, src in (("regime_daily", "data/regime.json"),
                     ("sentiment_daily", "data/sentiment.json")):
        h = (read_at(None, src) or {}).get("as_of")
        if h:
            cur.execute(f"delete from yeodoo.{tbl} where asof > %s", (h,))



# ── 신규 적재(2026-07-22): 사이트 산출물 전체를 yeodoo에 정리 ──────────
def load_rotation(cur, doc, sha, force=False):
    """로테이션 전략 풀 일별 스냅샷 — '언제 어떤 전략이 있었나·최근동향 갱신 시점'."""
    from psycopg2.extras import Json, execute_values
    asof = (doc or {}).get("generated")
    if not asof or not doc.get("strategies"):
        return 0
    if not force:
        cur.execute("select 1 from yeodoo.rotation_strategy where asof=%s limit 1", (asof,))
        if cur.fetchone():
            return 0
    rows = []
    for st in doc["strategies"]:
        lab = (st.get("lab") or {}).get("v")
        rows.append((asof, st.get("id"), st.get("cat"), st.get("cat_label"), st.get("name"),
                     st.get("recent_at") or None, len(st.get("sources") or []), lab, Json(st)))
    execute_values(cur, "insert into yeodoo.rotation_strategy"
                        "(asof,sid,cat,cat_label,name,recent_at,n_sources,lab_verdict,raw) values %s"
                        " on conflict(asof,sid) do update set name=excluded.name,"
                        " recent_at=excluded.recent_at,n_sources=excluded.n_sources,"
                        " lab_verdict=excluded.lab_verdict,raw=excluded.raw,loaded_at=now()", rows)
    _log(cur, "rotation", asof, sha, len(rows), "ok")
    return len(rows)


def load_updates(cur, doc, sha):
    """갱신 피드 — 중복은 PK(dt,target,title)로 자연 흡수."""
    from psycopg2.extras import execute_values
    evs = (doc or {}).get("events") or []
    rows = [(e.get("dt"), e.get("target"), e.get("title")) for e in evs
            if e.get("dt") and e.get("target") and e.get("title")]
    if not rows:
        return 0
    execute_values(cur, "insert into yeodoo.site_update(dt,target,title) values %s"
                        " on conflict do nothing", rows)
    _log(cur, "updates", (doc or {}).get("updated"), sha, len(rows), "ok")
    return len(rows)


def load_strategy_perf(cur, doc, sha, force=False):
    """전략 백테스트 지표(집계 성과만 — 시계열은 용량이 커 raw에서 제외)."""
    from psycopg2.extras import Json, execute_values
    asof = (doc or {}).get("generated")
    S = (doc or {}).get("strategies") or {}
    if not asof or not S:
        return 0
    if not force:
        cur.execute("select 1 from yeodoo.strategy_perf where asof=%s limit 1", (asof,))
        if cur.fetchone():
            return 0
    rows = []
    for nm, b in S.items():
        m = b.get("metrics") or {}
        lean = {k: v for k, v in b.items() if k not in ("dates", "nav", "bench", "bench2", "dd", "dd_b", "dd_b2")}
        rows.append((asof, nm, b.get("bench_label"), b.get("bench2_label"),
                     b.get("start"), b.get("end"), num(m.get("cagr")), num(m.get("vol")),
                     num(m.get("sharpe")), num(m.get("mdd")), num(b.get("mdd_b")), num(b.get("mdd_b2")),
                     num(m.get("calmar")), num(m.get("hit")), Json(lean)))
    execute_values(cur, "insert into yeodoo.strategy_perf(asof,strategy,bench_label,bench2_label,"
                        "start_dt,end_dt,cagr,vol,sharpe,mdd,mdd_bench,mdd_bench2,calmar,hit,raw) values %s"
                        " on conflict(asof,strategy) do update set cagr=excluded.cagr,vol=excluded.vol,"
                        " sharpe=excluded.sharpe,mdd=excluded.mdd,mdd_bench=excluded.mdd_bench,"
                        " mdd_bench2=excluded.mdd_bench2,calmar=excluded.calmar,hit=excluded.hit,"
                        " raw=excluded.raw,loaded_at=now()", rows)
    _log(cur, "strategy_perf", asof, sha, len(rows), "ok")
    return len(rows)


def load_holdings(cur, doc, sha, src):
    """전략 구성 — 파일별 as_of가 전략마다 다르므로 전략 단위로 넣는다."""
    from psycopg2.extras import execute_values
    S = (doc or {}).get("strategies") or {}
    rows = []
    for nm, st in S.items():
        asof = st.get("as_of")
        if not asof:
            continue
        for pos in st.get("positions") or []:
            if pos.get("t") is None:
                continue
            rows.append((asof, nm, pos["t"], pos.get("name"), num(pos.get("w")), src))
    if not rows:
        return 0
    execute_values(cur, "insert into yeodoo.strategy_holding(asof,strategy,ticker,name,weight,src) values %s"
                        " on conflict(asof,strategy,ticker) do update set weight=excluded.weight,"
                        " name=excluded.name,src=excluded.src,loaded_at=now()", rows)
    _log(cur, f"holdings_{src}", (doc or {}).get("generated"), sha, len(rows), "ok")
    return len(rows)


def load_members(cur, doc, sha, force=False):
    """유니버스 스냅샷 — 지수 편입/제외를 나중에 되짚기 위한 이력."""
    from psycopg2.extras import execute_values
    asof = (doc or {}).get("as_of_members")
    M = (doc or {}).get("members") or {}
    if not asof or not M:
        return 0
    if not force:
        cur.execute("select 1 from yeodoo.universe_member where asof=%s limit 1", (asof,))
        if cur.fetchone():
            return 0
    rows = [(asof, t, v.get("name"), v.get("sector"), v.get("idx") or []) for t, v in M.items()]
    execute_values(cur, "insert into yeodoo.universe_member(asof,ticker,name,sector,idx) values %s"
                        " on conflict(asof,ticker) do update set name=excluded.name,"
                        " sector=excluded.sector,idx=excluded.idx,loaded_at=now()", rows)
    _log(cur, "members", asof, sha, len(rows), "ok")
    return len(rows)


def load_screens(cur, stocks_doc, screens_doc, sha, force=False):
    """펀더멘털 스크리닝 통과 종목 — **정의는 data/screens.json 단일 소스**(화면과 동일 파일).
    화면은 JS로, 여기서는 파이썬으로 같은 규칙을 적용한다. 정의를 코드에 복제하지 않는 게 핵심."""
    from psycopg2.extras import execute_values
    asof = (stocks_doc or {}).get("as_of")
    ST = (stocks_doc or {}).get("stocks") or []
    SC = (screens_doc or {}).get("screens") or {}
    DIR = (screens_doc or {}).get("dir") or {}
    if not asof or not ST or not SC:
        return 0
    if not force:
        cur.execute("select 1 from yeodoo.screen_daily where asof=%s limit 1", (asof,))
        if cur.fetchone():
            return 0
    # 지표별 백분위(0=최저 … 100=최고)
    pct = {}
    for k in DIR:
        vals = sorted(((num((s.get("fund") or {}).get(k)), s["t"]) for s in ST
                       if num((s.get("fund") or {}).get(k)) is not None))
        n = len(vals)
        pct[k] = {t: (i / (n - 1) * 100 if n > 1 else 50.0) for i, (_, t) in enumerate(vals)}

    def good(t, k):
        v = pct.get(k, {}).get(t)
        if v is None:
            return None
        return 100 - v if DIR.get(k) == "low" else v

    rows = []
    for key, sc in SC.items():
        cand = []
        for s in ST:
            t = s["t"]
            if any((good(t, k) is None or good(t, k) < th) for k, th in (sc.get("qualify") or {}).items()):
                continue
            gs = [good(t, k) for k in (sc.get("keys") or []) if good(t, k) is not None]
            cand.append((sum(gs) / len(gs) if gs else 0.0, t))
        cand.sort(reverse=True)
        rows += [(asof, key, t, sc_, i + 1) for i, (sc_, t) in enumerate(cand)]
    if not rows:
        return 0
    execute_values(cur, "insert into yeodoo.screen_daily(asof,screen,ticker,score,rnk) values %s"
                        " on conflict(asof,screen,ticker) do update set score=excluded.score,"
                        " rnk=excluded.rnk,loaded_at=now()", rows)
    _log(cur, "screens", asof, sha, len(rows), "ok")
    return len(rows)


def load_snapshot(cur, sha, force=False):
    """한 커밋(또는 워킹트리)의 모든 산출물을 적재. 반환: 종목 행 수."""
    n = load_stocks(cur, read_at(sha, "data/stocks.json") or {}, sha, force)
    load_simple(cur, read_at(sha, "data/regime.json"), sha, "regime_daily", REGIME_COLS, force)
    load_simple(cur, read_at(sha, "data/sentiment.json"), sha, "sentiment_daily", SENT_COLS, force)
    load_rotation(cur, read_at(sha, "data/rotation_pool.json"), sha, force)
    load_updates(cur, read_at(sha, "data/updates.json"), sha)
    load_strategy_perf(cur, read_at(sha, "data/strategy_backtests.json"), sha, force)
    load_holdings(cur, read_at(sha, "data/strategy_holdings.json"), sha, "free")
    load_holdings(cur, read_at(sha, "data/strategy_holdings_db.json"), sha, "db")
    load_members(cur, read_at(sha, "data/members.json"), sha, force)
    load_screens(cur, read_at(sha, "data/stocks.json") or {}, read_at(sha, "data/screens.json"), sha, force)
    return n


# ── 엔트리포인트 ────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true", help="스키마·테이블 생성(멱등)")
    ap.add_argument("--backfill", action="store_true", help="git 이력 전체 적재")
    ap.add_argument("--force", action="store_true", help="기존 as_of도 덮어쓰기")
    ap.add_argument("--stats", action="store_true", help="적재 현황만 출력")
    a = ap.parse_args()

    cn = connect()
    cn.autocommit = False
    cur = cn.cursor()

    if a.init:
        with open(os.path.join(HERE, "db_schema.sql"), encoding="utf-8") as fh:
            cur.execute(fh.read())
        cn.commit()
        print("✓ yeodoo 스키마 준비 완료")

    if a.stats:
        for t in ("stock_daily", "fundamental_daily", "target_daily",
                  "regime_daily", "sentiment_daily", "swing_marker"):
            cur.execute(f"select count(*), min(asof), max(asof) from yeodoo.{t}"
                        if t != "swing_marker" else
                        "select count(*), min(bar_date), max(bar_date) from yeodoo.swing_marker")
            c, lo, hi = cur.fetchone()
            print(f"  {t:20s} {c:>8,}행  {lo} ~ {hi}")
        cur.execute("select side,n_total,n_evaluable,n_promoted,promote_pct,"
                    "avg_days_to_confirm,n_obs_days from yeodoo.v_swing_promotion")
        rows = cur.fetchall()
        for side, tot, ev, pr, pct, days, nobs in rows:
            warn = "  ← 표본 부족, 인용 금지" if (ev or 0) < 200 else ""
            print(f"  [잠정→확정] {side:4s} 잠정 {tot}건 중 평가가능 {ev}건 · "
                  f"승격 {pr}건({pct}%) · 평균 {days}일{warn}")
        if rows:
            print(f"  (관측일 {rows[0][6]}일 누적 — 절단된 최신 코호트는 분모에서 제외)")
        else:
            cur.execute("select count(distinct first_seen) from yeodoo.swing_marker")
            print(f"  [잠정→확정] 평가 가능 표본 0건 — 관측일 {cur.fetchone()[0]}일뿐이라 "
                  "승격 여부를 판정할 시간이 지나지 않음. 며칠 더 적재 후 재확인.")
        cn.close()
        return

    total = 0
    if a.backfill:
        # 같은 as_of를 여러 번 커밋한 날이 있으므로, 커밋을 과거→현재로 훑으며
        # 마지막 값이 이기게 둔다(--force). 미적재 as_of만 채울 땐 첫 값에서 멈춘다.
        shas = commits_for("data/stocks.json")
        print(f"git 이력 {len(shas)}개 커밋 훑는 중…")
        for i, sha in enumerate(shas, 1):
            n = load_snapshot(cur, sha, a.force)
            if n:
                cn.commit()
                total += n
                print(f"  [{i}/{len(shas)}] {sha[:8]} → {n}종목")
        # git 이전 시점의 목표주가 스냅샷 보강
        th = load_target_history(cur, read_at(None, "data/target_history.json"), None)
        cn.commit()
        if th:
            print(f"  목표주가 이력 보강 {th:,}행")

    n = load_snapshot(cur, None, a.force)      # 워킹트리 = 최신
    purge_retracted(cur)
    cn.commit()
    total += n
    print(f"✓ 적재 완료 — 종목 {total:,}행" if total else "변경 없음 — 스킵")
    cn.close()


if __name__ == "__main__":
    main()
