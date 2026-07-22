# -*- coding: utf-8 -*-
"""사이트 정적 검증 — 브라우저 없이 '깨진 배포'를 막는 최소 안전망. CI(.github/workflows/validate.yml)에서 실행.

검사 항목
 1) 인라인 JS 괄호 균형(문자열·정규식·주석 제거 후)
 2) 정의되지 않은 함수 호출(오타) — 파일 단위 휴리스틱
 3) data/*.json 파싱 + rotation_pool 필수 필드
 4) rotation lab.href 앵커가 archive/explorer에 실제 존재하는지
 5) rotation.html의 선별 상수(CATORD·QUOTA)와 build/rotation_select.py의 상수 일치
    — 어긋나면 화면의 9선과 일일잡 갱신 대상이 달라진다(이 저장소의 핵심 불변식)
실패 시 exit 1.
"""
import re, io, os, sys, json

try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGES = ["index.html", "stocks.html", "regime.html", "rotation.html", "explorer.html", "archive.html", "sources.html"]
errors = []


def rd(p): return io.open(os.path.join(ROOT, p), encoding="utf-8").read()


# ── JS 리터럴 제거 ─────────────────────────────────────────────
def strip_js(js):
    out, i, n, prev = [], 0, len(js), ""
    while i < n:
        c, nx = js[i], (js[i + 1] if i + 1 < n else "")
        if c == "/" and nx == "/":
            i = js.find("\n", i); i = n if i < 0 else i; continue
        if c == "/" and nx == "*":
            j = js.find("*/", i + 2); i = n if j < 0 else j + 2; continue
        if c in "\"'`":
            q = c; i += 1
            while i < n:
                if js[i] == "\\": i += 2; continue
                if js[i] == q: i += 1; break
                i += 1
            prev = "x"; continue
        if c == "/" and prev not in ("x", ")", "]"):
            i += 1; incls = False
            while i < n:
                ch = js[i]
                if ch == "\\": i += 2; continue
                if ch == "[": incls = True
                elif ch == "]": incls = False
                elif ch == "/" and not incls: i += 1; break
                elif ch == "\n": break
                i += 1
            while i < n and js[i].isalpha(): i += 1
            prev = "x"; continue
        out.append(c)
        if not c.isspace(): prev = "x" if (c.isalnum() or c in "_$") else c
        i += 1
    return "".join(out)


BUILTIN = set("""if for while switch catch function return typeof new else do try delete void in of instanceof
fetch parseInt parseFloat isNaN isFinite Number String Boolean Math JSON Date Array Object Symbol Error
setTimeout clearTimeout setInterval clearInterval requestAnimationFrame cancelAnimationFrame
decodeURIComponent encodeURIComponent decodeURI encodeURI escape unescape matchMedia alert confirm prompt
console document window localStorage sessionStorage location history navigator
RegExp Set Map WeakMap WeakSet Promise Proxy Reflect Intl Blob URL URLSearchParams
MutationObserver IntersectionObserver ResizeObserver Image Event CustomEvent DOMParser AbortController
getComputedStyle structuredClone queueMicrotask requestIdleCallback cancelIdleCallback""".split())
DEFPAT = [r"function\s+([A-Za-z_$][\w$]*)\s*\(", r"(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=",
          r"([A-Za-z_$][\w$]*)\s*=\s*function", r"([A-Za-z_$][\w$]*)\s*:\s*function",
          r"function\s*\(([^)]*)\)", r"catch\s*\(\s*([A-Za-z_$][\w$]*)\s*\)"]

for p in PAGES:
    scripts = [strip_js(s) for s in re.findall(r"<script>([\s\S]*?)</script>", rd(p))]
    js = "\n".join(scripts)
    for k, s in enumerate(scripts):
        for a, b in [("(", ")"), ("{", "}"), ("[", "]")]:
            d = s.count(a) - s.count(b)
            if d: errors.append(f"{p} script#{k}: 괄호 불균형 {a}{b}={d:+d}")
    known = set(BUILTIN)
    for pat in DEFPAT:
        for m in re.finditer(pat, js):
            for part in m.group(1).split(","):
                q = part.strip()
                if re.fullmatch(r"[A-Za-z_$][\w$]*", q or ""): known.add(q)
    unknown = sorted({m.group(1) for m in re.finditer(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(", js)} - known)
    if unknown: errors.append(f"{p}: 정의되지 않은 호출 {', '.join(unknown)}")

# ── 데이터 JSON ────────────────────────────────────────────────
pool = None
for f in os.listdir(os.path.join(ROOT, "data")):
    if not f.endswith(".json"): continue
    try:
        j = json.load(io.open(os.path.join(ROOT, "data", f), encoding="utf-8"))
    except Exception as e:
        errors.append(f"data/{f}: JSON 파싱 실패 {e}"); continue
    if f == "rotation_pool.json": pool = j

if pool:
    need = {"id", "cat", "cat_label", "name", "type", "target", "purpose", "principle", "entry", "performance", "recent", "sources"}
    ids = set()
    for s in pool.get("strategies", []):
        miss = need - set(s)
        if miss: errors.append(f"rotation_pool {s.get('id')}: 필드 결측 {sorted(miss)}")
        if not s.get("sources"): errors.append(f"rotation_pool {s.get('id')}: 출처 없음")
        if s["id"] in ids: errors.append(f"rotation_pool: id 중복 {s['id']}")
        ids.add(s["id"])
    # lab 앵커가 실제 항목을 가리키는지 — 앵커 키는 표시명이 아니라 **불변 id(sid)** 다.
    # 전에는 슬러그를 이름에서 즉석 생성해, 전략을 개명하는 순간 여기 19개 딥링크가 조용히 깨졌다.
    def _slug(n):   # archive.html / explorer.html의 slug()와 동일 규칙(구 슬러그 호환용)
        return re.sub(r"^-+|-+$", "", re.sub(r"[^0-9a-z가-힣]+", "-", str(n).lower()))

    def _recs(page, var="D"):
        """`var D=[ … ];` 를 대괄호 균형으로 잘라 JSON으로 읽는다(주석 허용)."""
        src = rd(page)
        m = re.search(r"var\s+%s\s*=\s*\[" % var, src)
        if not m: raise SystemExit(f"{page}: var {var}=[ 를 찾지 못함")
        i, depth, j, instr, esc = m.end() - 1, 0, m.end() - 1, False, False
        while j < len(src):
            c = src[j]
            if instr:
                if esc: esc = False
                elif c == "\\": esc = True
                elif c == '"': instr = False
            elif c == '"': instr = True
            elif c == "[": depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0: break
            j += 1
        body = re.sub(r"(?m)^\s*//.*$", "", src[i:j + 1])
        return json.loads(body)

    try:
        AREC, EREC = _recs("archive.html"), _recs("explorer.html")
    except Exception as e:
        errors.append(f"전략 배열 파싱 실패: {e}"); AREC = EREC = []
    anames = {d["n"] for d in AREC}
    enames = {d["n"] for d in EREC}

    # sid 불변식: 전 항목에 있어야 하고, 페이지 안에서 유일해야 하며, 앵커로 쓸 수 있어야 한다
    for page, recs in (("archive.html", AREC), ("explorer.html", EREC)):
        seen = {}
        for d in recs:
            sid = d.get("sid")
            if not sid:
                errors.append(f"{page} \"{d['n']}\": sid 없음 — 개명하면 딥링크가 깨진다"); continue
            if not re.fullmatch(r"[0-9a-z][0-9a-z-]*", sid):
                errors.append(f"{page} \"{d['n']}\": sid '{sid}'는 소문자 영숫자·하이픈만 허용")
            if sid in seen:
                errors.append(f"{page}: sid 중복 '{sid}' ({seen[sid]} / {d['n']})")
            seen[sid] = d["n"]

    # 구 슬러그(aka) → sid 해석표. 기존 북마크·외부 링크가 계속 도달해야 한다.
    def _resolve(recs):
        m = {}
        for d in recs:
            sid = d.get("sid") or _slug(d["n"])
            for k in [sid, _slug(d["n"])] + list(d.get("aka") or []):
                m[k] = d
        return m
    ARES, ERES = _resolve(AREC), _resolve(EREC)

    for s in pool.get("strategies", []):
        L = s.get("lab")
        if not L: continue
        href = L.get("href", "")
        page, _, frag = href.partition("#")
        if page not in ("archive.html", "explorer.html"):
            errors.append(f"rotation_pool {s['id']}: lab.href 대상 페이지가 이상함 ({href})"); continue
        if not os.path.exists(os.path.join(ROOT, page)):
            errors.append(f"rotation_pool {s['id']}: lab.href 대상 파일 없음 ({page})"); continue
        if L["t"] not in (anames if page == "archive.html" else enames):
            errors.append(f"rotation_pool {s['id']}: lab.t \"{L['t']}\"가 {page}에 없음(링크 깨짐)"); continue
        pre, res = ("a-", ARES) if page == "archive.html" else ("s-", ERES)
        if not frag:
            errors.append(f"rotation_pool {s['id']}: lab.href에 앵커(#)가 없음 ({href})"); continue
        if not frag.startswith(pre):
            errors.append(f"rotation_pool {s['id']}: 앵커 접두어가 '{pre}'가 아님 (#{frag})"); continue
        tgt = res.get(frag[len(pre):])
        if tgt is None:
            errors.append(f"rotation_pool {s['id']}: 앵커 #{frag}가 {page}의 어떤 항목에도 도달하지 못함")
        elif tgt["n"] != L["t"]:
            errors.append(f"rotation_pool {s['id']}: 앵커 #{frag}는 \"{tgt['n']}\"인데 lab.t는 \"{L['t']}\"")
        elif frag[len(pre):] != (tgt.get("sid") or ""):
            errors.append(f"rotation_pool {s['id']}: 앵커가 구 슬러그(#{frag}) — sid 기준 #{pre}{tgt.get('sid')}로 갱신할 것")

    # 구 슬러그 하위호환: 각 페이지의 해시 해석 코드가 aka를 실제로 참조하는지(문안만 남고 로직이 빠지는 사고 방지)
    if "aka" not in rd("archive.html") or "ALIAS" not in rd("archive.html"):
        errors.append("archive.html: 구 슬러그(aka) 해석 로직이 없음 — 기존 딥링크가 깨진다")
    if "aka" not in rd("explorer.html") or "_keys" not in rd("explorer.html"):
        errors.append("explorer.html: 구 슬러그(aka) 해석 로직이 없음 — 기존 딥링크가 깨진다")

# ── 선별 알고리즘 상수 일치(프론트 ↔ 일일잡) ──────────────────
rot, sel = rd("rotation.html"), rd(os.path.join("build", "rotation_select.py"))
def consts(txt):   # JS는 {A:2,…}·["A",…], 파이썬은 {"A": 2,…}·["A",…] → 따옴표 무시하고 정규화
    q = re.search(r"QUOTA\s*=\s*\{([^}]*)\}", txt); c = re.search(r"CATORD\s*=\s*\[([^\]]*)\]", txt)
    return (dict(re.findall(r'["\']?([A-Z])["\']?\s*:\s*(\d+)', q.group(1))) if q else None,
            re.findall(r"[A-Z]", c.group(1)) if c else None)
qj, cj = consts(rot)
qp, cp = consts(sel)
# 상수가 같아도 **산술**이 다르면 9선이 갈린다(실제 사고): JS의 seed*16777619는 2^53을 넘겨 float64 정밀도를
# 잃으므로 파이썬의 정확한 32비트 연산과 다른 시드가 됐다. Math.imul 사용을 강제한다.
if not re.search(r"Math\.imul\s*\(\s*seed\s*,\s*16777619\s*\)", rot):
    errors.append("rotation.html FNV 해시가 Math.imul을 쓰지 않음 — float64 정밀도 손실로 rotation_select.py와 9선이 달라진다")
if re.search(r"seed\s*\*\s*16777619", rot):
    errors.append("rotation.html에 `seed*16777619` 잔존 — Math.imul로 교체할 것")
# 날짜 기준(KST)도 양쪽이 같아야 한다 — UTC였을 때 갱신 대상과 표시 대상이 오전 9시에 어긋났다
if "9*3600e3" not in rot:
    errors.append("rotation.html today()가 KST 보정(9*3600e3)을 하지 않음 — 일일잡과 날짜가 어긋난다")
if "hours=9" not in sel:
    errors.append("rotation_select.py가 KST(hours=9)를 쓰지 않음 — rotation.html과 날짜가 어긋난다")

# 홈은 home_reco.json만 fetch하므로 stocks.json과 기준일이 어긋나면 홈이 낡은 채 고착된다(워크플로가 한쪽만 커밋한 사고)
try:
    _sj = json.load(io.open(os.path.join(ROOT, "data", "stocks.json"), encoding="utf-8"))
    _s = _sj.get("as_of")
    _h = json.load(io.open(os.path.join(ROOT, "data", "home_reco.json"), encoding="utf-8")).get("as_of")
    if _s and _h and _s != _h:
        errors.append(f"기준일 불일치: stocks.json {_s} vs home_reco.json {_h} — 워크플로가 두 파일을 함께 커밋하는지 확인")
    # 상세 분리 불변식: ①슬림 본체에 상세 필드가 재유입되면 페이로드가 도로 1.9MB로 부푼다
    #                 ②종목별 상세 파일이 없거나 기준일이 어긋나면 상세 패널이 낡거나 빈다
    _sd = os.path.join(ROOT, "data", "sd")
    _fat = [s["t"] for s in _sj.get("stocks", []) if any(k in s for k in ("sig", "pxd", "vd"))]
    if _fat:
        errors.append(f"stocks.json 슬림 위반: {len(_fat)}종목에 상세 필드(sig/pxd/vd) 잔존 (예: {_fat[:3]}) — 생성기 분리 로직 확인")
    _miss, _stale = [], []
    for s in _sj.get("stocks", []):
        p = os.path.join(_sd, s["t"] + ".json")
        if not os.path.exists(p):
            _miss.append(s["t"]); continue
        try:
            _d = json.load(io.open(p, encoding="utf-8"))
            if _d.get("as_of") != _s: _stale.append(s["t"])
            if not _d.get("sig"): _miss.append(s["t"])
        except Exception:
            _miss.append(s["t"])
    if _miss: errors.append(f"data/sd 상세 결측/손상 {len(_miss)}종목 (예: {_miss[:5]}) — 워크플로가 data/sd를 커밋하는지 확인")
    if _stale: errors.append(f"data/sd 기준일 불일치 {len(_stale)}종목 (예: {_stale[:5]}) — 슬림과 상세가 다른 날짜")
except Exception as e:
    errors.append(f"기준일 교차검증 실패: {e}")

# 보유 구성(무료 + DB요약): 키가 explorer D 배열 이름과 다르면 화면에 조용히 안 뜬다 + 비중합·비공개 정책 검사
try:
    for _fn in ("strategy_holdings.json", "strategy_holdings_db.json"):
        _hp = os.path.join(ROOT, "data", _fn)
        if not os.path.exists(_hp): continue
        _hd = json.load(io.open(_hp, encoding="utf-8"))
        for nm, st in (_hd.get("strategies") or {}).items():
            if nm not in enames:
                errors.append(f"{_fn} \"{nm}\": explorer.html D 배열에 없는 이름(화면 미표시)")
            if st.get("private"):
                # 종목 비공개 항목 — positions가 비어 있어야 정상(티커 유출 방지)
                if st.get("positions"):
                    errors.append(f"{_fn} \"{nm}\": private인데 positions 존재 — 티커 유출 의심")
            else:
                _ws = sum(p.get("w", 0) for p in st.get("positions", []))
                if abs(_ws - 1.0) > 0.01:
                    errors.append(f"{_fn} \"{nm}\": 비중합 {_ws:.4f} ≠ 1")
            if not st.get("as_of") or not st.get("note"):
                errors.append(f"{_fn} \"{nm}\": as_of/note 누락")
except Exception as e:
    errors.append(f"strategy_holdings 검증 실패: {e}")

# ── 스크리닝 정의(screens.json): 화면과 DB 로더가 **같은 파일**을 읽어야 한다 ──
#    정의를 코드에 복제하면 조용히 어긋난다(로테이션 9선 FNV 사고와 같은 유형).
try:
    _sp = os.path.join(ROOT, "data", "screens.json")
    if os.path.exists(_sp):
        _sj = json.load(io.open(_sp, encoding="utf-8"))
        _dir, _scr = _sj.get("dir") or {}, _sj.get("screens") or {}
        if not _scr:
            errors.append("screens.json: screens 비어 있음")
        for _k, _v in _scr.items():
            for _f in ("name", "keys", "qualify", "note"):
                if not _v.get(_f): errors.append(f"screens.json[{_k}]: {_f} 누락")
            for _m in list(_v.get("keys") or []) + list((_v.get("qualify") or {}).keys()) + list((_v.get("qualify_max") or {}).keys()):
                if _m not in _dir:
                    errors.append(f"screens.json[{_k}]: 지표 '{_m}'의 방향(dir) 정의 없음")
            for _m, _th in list((_v.get("qualify") or {}).items()) + list((_v.get("qualify_max") or {}).items()):
                if not isinstance(_th, (int, float)) or not (0 <= _th <= 100):
                    errors.append(f"screens.json[{_k}]: 임계 {_m}={_th} 는 0~100 백분위여야 함")
        _sh = rd("stocks.html")
        # 주석에 파일명을 적어둔 것만으로 통과하지 않도록, 실제 fetch 호출을 확인한다
        if not re.search(r"""fetch\(\s*['"]data/screens\.json""", _sh):
            errors.append("stocks.html이 screens.json을 읽지 않음 — 정의가 코드에 복제되면 로더와 어긋난다")
        if "var SCREENS={qval" in _sh or "qualify:function(s){return good(s,'fpe')" in _sh:
            errors.append("stocks.html에 스크린 정의가 인라인으로 남아 있음 — screens.json 단일 소스 위반")
        # 판정 계산은 build/screens_apply.py 한 곳뿐이어야 한다. 화면이 다시 계산하면 동점 처리 같은
        # 미세한 차이로 목록이 갈린다(실측: CMCSA가 화면 69종 / 로더 70종으로 어긋났다).
        if re.search(r"function\s+(fpct|scoreOf)\s*\(", _sh):
            errors.append("stocks.html이 스크린 판정을 자체 계산함 — 구현이 둘이면 DB·화면이 어긋난다")
        sys.path.insert(0, os.path.join(ROOT, "build"))
        import screens_apply
        _st = json.load(io.open(os.path.join(ROOT, "data", "stocks.json"), encoding="utf-8"))
        _res = _st.get("screens")
        if not _res:
            errors.append("stocks.json에 스크린 판정 결과(screens) 없음 — build/screens_apply.py 실행 필요")
        else:
            if set(_res) != set(_scr):
                errors.append(f"스크린 목록 불일치 — 정의 {sorted(_scr)} vs 결과 {sorted(_res)}")
            _fp = screens_apply.fingerprint(_sj)
            if _st.get("screens_fp") != _fp:
                errors.append(f"스크린 정의 지문 불일치({_st.get('screens_fp')}≠{_fp}) — 정의를 고친 뒤 stocks.json을 다시 굽지 않았다")
            _tk = {x["t"] for x in _st.get("stocks") or []}
            for _k, _lst in _res.items():
                _bad = [r["t"] for r in _lst if r["t"] not in _tk]
                if _bad:
                    errors.append(f"스크린 결과[{_k}]에 커버 밖 종목: {_bad[:3]}")
                _sc = [r.get("s") for r in _lst]
                if _sc != sorted(_sc, reverse=True):
                    errors.append(f"스크린 결과[{_k}]가 적합도 내림차순이 아님 — 화면은 이 순서를 그대로 그린다")
except Exception as e:
    errors.append(f"screens.json 검증 실패: {e}")

# ── 판정 원장(verdicts.json): 홈이 숫자를 손으로 적지 않는지 ──
#    '기각 41'처럼 HTML에 박아두면 전략 등재일에 조용히 틀린다 — 정직성 페이지가 틀린 숫자를 자랑하는 게 최악이다.
try:
    sys.path.insert(0, os.path.join(ROOT, "build"))
    import verdicts_gen
    _vp = os.path.join(ROOT, "data", "verdicts.json")
    if not os.path.exists(_vp):
        errors.append("data/verdicts.json 없음 — python build/verdicts_gen.py 실행 필요")
    else:
        _cur = json.load(io.open(_vp, encoding="utf-8"))
        _fresh = verdicts_gen.build(ROOT)
        if _cur != _fresh:
            errors.append("판정 원장이 전략 배열과 어긋남 — python build/verdicts_gen.py 로 다시 구울 것")
        _ih = rd("index.html")
        # 홈 본문(스크립트 제외)에 판정 수치가 하드코딩돼 있으면 드리프트한다
        _body = re.sub(r"(?s)<script.*?</script>", "", _ih)
        # 아카이브 statline도 전에 '제한적 유효 20개'를 손으로 적어두고 이관 때 틀렸다 — 스크립트까지 검사
        _ah = rd("archive.html")
        for _n, _lab in ((_fresh["deploy_n"] + _fresh["marginal_n"], "배포+제한적 유효 합계"),):
            if re.search(r"제한적 유효 \d+개", _ah) and not re.search(r"제한적 유효 <b>", _ah):
                errors.append("archive.html이 배포·제한적 유효 개수를 하드코딩함 — verdicts.json에서 읽을 것")
        for _n, _lab in ((_fresh["archive_n"], "기각 아카이브 건수"), (_fresh["explorer_n"], "전략 총수"),
                         (_fresh["marginal_n"], "제한적 유효 건수")):
            if re.search(r"(?<![0-9])%d\s*(개|종|건)" % _n, _body):
                errors.append(f"index.html에 {_lab}({_n})가 하드코딩됨 — verdicts.json에서 읽을 것")
        if "data/verdicts.json" not in _ih:
            errors.append("index.html이 verdicts.json을 읽지 않음 — 판정 수치를 손으로 적고 있다")
        # 배포 딥링크 슬러그가 explorer에서 실제로 선택되는지(규칙 동일성)
        _eh = rd("explorer.html")
        for _d in _fresh["deploy"]:
            if _d["n"] not in _eh:
                errors.append(f"배포 전략명이 explorer.html에 없음: {_d['n']}")
        # 배포 전략에 백테스트가 있는지 — 이름을 고치면 explorer와 backtests가 조용히 어긋난다
        _bt = json.load(io.open(os.path.join(ROOT, "data", "strategy_backtests.json"), encoding="utf-8"))
        _bs = (_bt or {}).get("strategies") or {}
        for _d in _fresh["deploy"]:
            if _d["n"] not in _bs:
                errors.append(f"배포 전략에 백테스트가 없음: {_d['n']} — 이름 불일치 의심")
except Exception as e:
    errors.append(f"판정 원장 검증 실패: {e}")

# ── 성과지표 v2 스키마(build/strategy_metrics.py 산출) ─────────
# explorer의 지표표는 metrics.s/.b/.basis 를 읽는다. 시계열만 다시 굽고 지표를 안 구우면
# 화면이 조용히 폴백 문구로 바뀐다 — 그 상태를 배포하지 않도록 여기서 잡는다.
try:
    _bt = json.load(io.open(os.path.join(ROOT, "data", "strategy_backtests.json"), encoding="utf-8"))
    if _bt.get("metrics_schema") != "v2":
        errors.append("strategy_backtests.json: metrics_schema가 v2가 아님 — python build/strategy_metrics.py 실행 필요")
    for _nm, _b in (_bt.get("strategies") or {}).items():
        # 키는 explorer D 배열의 표시명과 **글자 단위로** 같아야 조인된다(개명 사고 방지)
        if _nm not in enames:
            errors.append(f"strategy_backtests.json \"{_nm}\": explorer.html D 배열에 없는 이름 — 차트·지표가 통째로 사라진다")
        _m = _b.get("metrics") or {}
        if not (_m.get("s") and _m.get("b") and _m.get("basis")):
            errors.append(f"{_nm}: 지표 v2 블록(s/b/basis) 없음 — build/strategy_metrics.py 로 다시 구울 것"); continue
        _ba = _m["basis"]
        if not _ba.get("excess") or not _ba.get("rf_source"):
            errors.append(f"{_nm}: 초과수익 기준(rf) 표기가 없음 — rf=0 Sharpe 게시 금지")
        if _ba.get("mdd_basis") != "monthly_nav":
            errors.append(f"{_nm}: MDD 기준이 월말 NAV가 아님 — 차트 낙폭 곡선과 어긋난다")
        for _k in ("dd", "dd_b"):
            if len(_b.get(_k) or []) != len(_b.get("dates") or []):
                errors.append(f"{_nm}: {_k} 길이가 dates와 다름")
        if _b.get("mdd_b") is not None and _m["b"].get("mdd") is not None and abs(_b["mdd_b"] - _m["b"]["mdd"]) > 0.011:
            errors.append(f"{_nm}: mdd_b({_b['mdd_b']})와 metrics.b.mdd({_m['b']['mdd']})가 어긋남")
        # N은 화면에 반드시 노출돼야 한다(전략별 3배 차이) — 값이 비면 표가 거짓말을 한다
        if not _ba.get("n_months") or _ba["n_months"] != _m["s"].get("n_months"):
            errors.append(f"{_nm}: basis.n_months 결측/불일치")
    _rf = os.path.join(ROOT, "data", "rf_monthly.json")
    if not os.path.exists(_rf):
        errors.append("data/rf_monthly.json 없음 — 무위험금리 캐시가 커밋되지 않았다(FRED 장애 시 폴백 불가)")
    else:
        _rfj = json.load(io.open(_rf, encoding="utf-8"))
        if len(_rfj.get("monthly") or {}) < 100:
            errors.append("data/rf_monthly.json: 월간 관측이 100개 미만 — 비정상")
    if "metrics.s" not in rd("explorer.html") and "m.s" not in rd("explorer.html"):
        errors.append("explorer.html이 지표 v2(metrics.s)를 읽지 않음")
except Exception as e:
    errors.append(f"성과지표 v2 검증 실패: {e}")

# ── 성격(kind) 축 · 대조군 역할 · 프록시 고지 · 기준월 통일 ─────
# 전략마다 '무엇으로 판단하는가'가 다르다는 것이 explorer의 발표 축이다. 그 축이 비면
# 화면은 도로 CAGR 한 줄로 모든 전략을 재단한다 — 그 상태를 배포하지 않도록 여기서 잡는다.
try:
    _kp = os.path.join(ROOT, "data", "strategy_kinds.json")
    if not os.path.exists(_kp):
        errors.append("data/strategy_kinds.json 없음 — 성격 축 정의가 커밋되지 않았다")
    else:
        _kj = json.load(io.open(_kp, encoding="utf-8"))
        _kinds = set((_kj.get("kinds") or {}).keys())
        _roles = set((_kj.get("bench_roles") or {}).keys())
        _mlab = _kj.get("metric_labels") or {}
        if not _kinds: errors.append("strategy_kinds.json: kinds가 비어 있음")
        for _k, _v in (_kj.get("kinds") or {}).items():
            for _need in ("one_liner", "primary", "list_metric", "list_label", "read_note"):
                if not _v.get(_need):
                    errors.append(f"strategy_kinds.json {_k}: {_need} 결측 — 화면이 빈칸으로 렌더된다")
            for _pk in (_v.get("primary") or []):
                if _pk not in _mlab:
                    errors.append(f"strategy_kinds.json {_k}: primary '{_pk}'의 metric_labels 라벨이 없음")
        _det = json.load(io.open(os.path.join(ROOT, "data", "strategy_detail.json"), encoding="utf-8"))
        _bt2 = json.load(io.open(os.path.join(ROOT, "data", "strategy_backtests.json"), encoding="utf-8"))
        _bs2 = (_bt2 or {}).get("strategies") or {}
        _endmonths = set()
        for _d in EREC:
            _nm = _d["n"]
            _dd = _det.get(_nm) or {}
            # (a)(b) 성격 축
            if not _dd.get("kind"):
                errors.append(f"strategy_detail.json \"{_nm}\": kind 없음 — 목록·카드가 '미분류'로 떨어진다")
            elif _dd["kind"] not in _kinds:
                errors.append(f"strategy_detail.json \"{_nm}\": kind '{_dd['kind']}'가 strategy_kinds.json에 없음")
            # (e) 용도 축 미분류 0건
            if not _dd.get("archetype"):
                errors.append(f"strategy_detail.json \"{_nm}\": archetype 없음 — 용도 필터에 '미분류' 칩이 생긴다")
            _b = _bs2.get(_nm)
            if not _b: continue
            # 조인 키 이중화 — 표시명 하나로만 묶여 있으면 개명하는 순간 차트가 통째로 사라진다
            if not _b.get("sid"):
                errors.append(f"strategy_backtests.json \"{_nm}\": sid 병기 없음 — 개명 시 조인이 끊긴다")
            elif _b["sid"] != _d.get("sid"):
                errors.append(f"strategy_backtests.json \"{_nm}\": sid '{_b['sid']}'가 explorer의 '{_d.get('sid')}'와 다름")
            # (c) 대조군 역할
            for _rk, _lk in (("bench_role", "bench_label"), ("bench2_role", "bench2_label")):
                if _b.get(_lk) and not _b.get(_rk):
                    errors.append(f"strategy_backtests.json \"{_nm}\": {_rk} 없음 — 화면이 대조군 성격을 설명하지 못한다")
                elif _b.get(_rk) and _b[_rk] not in _roles:
                    errors.append(f"strategy_backtests.json \"{_nm}\": {_rk}='{_b[_rk]}'가 허용값 {sorted(_roles)}에 없음")
            # (d) 프록시 고지 — 원본인 척하면 안 된다
            if _b.get("is_proxy") and not _b.get("proxy_note"):
                errors.append(f"strategy_backtests.json \"{_nm}\": is_proxy=true인데 proxy_note가 없음 — 프록시를 원본인 척 게시할 수 없다")
            # 위기 구간·성격 보조 수치는 엔진 산출물이다(손으로 적은 값이 섞이면 조용히 틀린다)
            _m = _b.get("metrics") or {}
            if not _m.get("crisis"):
                errors.append(f"{_nm}: metrics.crisis 없음 — build/strategy_metrics.py 로 다시 구울 것")
            if _m.get("profile") is None:
                errors.append(f"{_nm}: metrics.profile 없음 — build/strategy_metrics.py 로 다시 구울 것")
            if _b.get("effective_from") and not _b.get("effective_note"):
                errors.append(f"{_nm}: effective_from만 있고 effective_note가 없음 — 겹친 구간이 차트 버그로 읽힌다")
            _ba2 = _m.get("basis") or {}
            if _ba2.get("end_month"): _endmonths.add(_ba2["end_month"])
        # (f) 기준일 통일 = 사용자 1순위 원칙
        if len(_endmonths) > 1:
            errors.append(f"지표 기준월이 전략마다 다름: {sorted(_endmonths)} — 기준일 통일이 이 랩의 1순위 원칙이다")
        # explorer가 성격 축을 실제로 읽는지(문안만 남고 로직이 빠지는 사고 방지)
        _ex = rd("explorer.html")
        for _needs, _msg in ((("strategy_kinds.json",), "성격 축 정의를 읽지 않음"),
                             (("metrics.crisis", "m.crisis"), "위기 구간을 화면에 쓰지 않음"),
                             (("crisisTable",), "위기 구간 표 컴포넌트가 없음"),
                             (("min_detectable_d_sharpe_95",), "'구별 불가'의 판정 범위를 설명하지 않음"),
                             (("kindOf", "kmeta"), "성격별 1차 지표 분기가 없음")):
            if not any(_n in _ex for _n in _needs):
                errors.append(f"explorer.html이 {' / '.join(_needs)} 를 참조하지 않음 — {_msg}")
except Exception as e:
    errors.append(f"성격 축·대조군 역할 검증 실패: {e}")

# ── 기각 재검 부기 — 전건 '기각 유지'다. 이게 전략처럼 보이면 랩의 신뢰가 무너진다 ──
try:
    _ap = os.path.join(ROOT, "data", "archive_backtests.json")
    if os.path.exists(_ap):
        _ab = json.load(io.open(_ap, encoding="utf-8"))
        _asrc = rd("archive.html")
        if _ab.get("metrics_schema") != "v2":
            errors.append("archive_backtests.json: metrics_schema가 v2가 아님 — strategy_metrics.py 실행 필요")
        # 분모를 게시 건수로 잡으면 보정이 실제보다 관대해진다(고른 뒤에 세는 것 = selection 무시)
        _nt = _ab.get("n_tests_total")
        if not _nt or _nt < len(_ab.get("strategies") or {}):
            errors.append("archive_backtests.json: n_tests_total 결측/과소 — 다중검정 분모는 게시 건수가 아니라 재검 총 건수")
        # archive.html의 sid와 조인되지 않으면 부기가 통째로 사라진다(개명 사고 유형)
        _asids = set(re.findall(r'"sid":\s*"([^"]+)"', _asrc))
        for _sid, _b in (_ab.get("strategies") or {}).items():
            if _sid not in _asids:
                errors.append(f"archive_backtests.json \"{_sid}\": archive.html D 배열에 없는 sid — 부기가 렌더되지 않는다")
            _m = _b.get("metrics") or {}
            if not (_m.get("s") and _m.get("b") and _m.get("basis")):
                errors.append(f"{_sid}: 부기 지표 v2 블록 없음"); continue
            if not _m["basis"].get("excess"):
                errors.append(f"{_sid}: 부기가 초과수익 기준이 아님 — rf=0 게시 금지")
            _mu = _m.get("multiplicity") or {}
            if _mu.get("n_tests") != _nt:
                errors.append(f"{_sid}: 다중검정 n_tests({_mu.get('n_tests')})가 n_tests_total({_nt})과 다름")
            if _mu.get("passed"):
                errors.append(f"{_sid}: 부기가 다중검정을 통과한 것으로 표시됨 — 재검 판정은 전건 '기각 유지'다. "
                              "실제로 통과했다면 아카이브가 아니라 탐색기로 승격 검토가 먼저다")
        if "기각 유지" not in _asrc:
            errors.append("archive.html 부기에 '기각 유지' 문구가 없음 — 표만 보면 부활한 것으로 읽힌다")
except Exception as e:
    errors.append(f"기각 재검 부기 검증 실패: {e}")

# ── 폭 토큰: 페이지 이동 시 콘텐츠 폭이 튀지 않게 세 가지로만 ──
try:
    _want = {"stocks.html": "--w-wide", "index.html": "--w-base", "explorer.html": "--w-base",
             "regime.html": "--w-base", "rotation.html": "--w-base", "archive.html": "--w-base",
             "sources.html": "--w-read"}
    for _f, _tok in _want.items():
        _s = rd(_f)
        _m = re.search(r"\.wrap\{[^}]*?max-width:\s*([^;]+);", _s)
        if not _m:
            errors.append(f"{_f}: .wrap max-width 없음")
        elif _m.group(1).strip() != f"var({_tok})":
            errors.append(f"{_f}: 폭이 var({_tok})가 아님({_m.group(1).strip()}) — 폭 토큰 밖으로 나갔다")
except Exception as e:
    errors.append(f"폭 토큰 검증 실패: {e}")

# ── 모바일 가로스크롤 방지 ──
#    body에 word-break:keep-all(한글 단어 보전)만 걸면 끊을 곳 없는 라틴 문자열이 통째로 붙들려
#    페이지를 가로로 밀어낸다(실측: regime.html의 "(Goldilocks·…·Recession)" 527px → 문서폭 547).
#    overflow-wrap:break-word는 넘칠 때만 끊으므로 한글 보전과 양립한다.
try:
    for _p in PAGES:
        _s = rd(_p)
        # ⚠ 주석에 문자열이 있는 것만으로 통과하면 안 된다(스크린 검사에서 이미 겪은 함정) —
        #    두 속성이 **한 선언 안에 붙어 있는지**를 본다.
        if "word-break:keep-all" in _s and not re.search(r"word-break:\s*keep-all\s*;\s*overflow-wrap:\s*break-word", _s):
            errors.append(f"{_p}: word-break:keep-all에 overflow-wrap:break-word 안전망이 없음 — 모바일 가로스크롤 위험")
except Exception as _e3:
    errors.append(f"줄바꿈 안전망 검증 실패: {_e3}")

# ── 갱신 피드(updates.json): 홈 '최근 업데이트'·각 페이지 배지의 소스 ──
#    시각(hm)은 선택 필드지만, 있으면 HH:MM이어야 한다 — 형식이 깨지면 화면에 그대로 노출된다.
try:
    _up = json.load(io.open(os.path.join(ROOT, "data", "updates.json"), encoding="utf-8"))
    for _e in _up.get("events") or []:
        _hm = _e.get("hm")
        if _hm is not None and not re.fullmatch(r"[0-2]\d:[0-5]\d", str(_hm)):
            errors.append(f"updates.json: 시각 형식 이상 {_e.get('dt')} {_hm}")
except Exception as _e2:
    errors.append(f"updates.json 시각 검증 실패: {_e2}")
try:
    _up = os.path.join(ROOT, "data", "updates.json")
    if os.path.exists(_up):
        _u = json.load(io.open(_up, encoding="utf-8"))
        _ok_t = {"rotation", "explorer", "archive", "stocks", "regime", "sentiment", "holdings"}
        _evs = _u.get("events")
        if not isinstance(_evs, list) or not _evs:
            errors.append("updates.json: events 비어 있음")
        else:
            _prev = None
            for i, e in enumerate(_evs):
                for k in ("dt", "target", "title"):
                    if not e.get(k): errors.append(f"updates.json[{i}]: {k} 누락")
                if e.get("target") and e["target"] not in _ok_t:
                    errors.append(f"updates.json[{i}]: 알 수 없는 target '{e['target']}' (허용: {sorted(_ok_t)})")
                if _prev and e.get("dt") and e["dt"] > _prev:
                    errors.append(f"updates.json[{i}]: 정렬 오류 — 최신순이어야 함({e['dt']} > {_prev})")
                _prev = e.get("dt") or _prev
except Exception as e:
    errors.append(f"updates.json 검증 실패: {e}")

# ── 일자 정합(데이터 정책 3): 알려진 날짜 필드가 전부 파싱되고 미래가 아니어야 한다 ──
# (실사고: members.json에 미래 날짜 07-23이 들어가 있었음. TZ 여유로 +1일 허용.)
import datetime as _dt
_tomorrow = (_dt.date.today() + _dt.timedelta(days=1)).isoformat()
def _dates_of(fn, j):
    if fn == "stocks.json": return [("as_of", j.get("as_of"))]
    if fn == "home_reco.json": return [("as_of", j.get("as_of"))]
    if fn == "regime.json": return [("as_of", j.get("as_of"))]
    if fn == "members.json": return [("as_of_members", j.get("as_of_members"))]
    if fn == "updates.json":
        return [("updated", j.get("updated"))] + [(f"events[{i}].dt", e.get("dt")) for i, e in enumerate(j.get("events") or [])]
    if fn == "rotation_pool.json": return [("generated", j.get("generated"))]
    if fn in ("strategy_holdings.json", "strategy_holdings_db.json"):
        return [("generated", j.get("generated"))] + [(f"{nm}.as_of", st.get("as_of")) for nm, st in (j.get("strategies") or {}).items()]
    if fn == "rf_monthly.json": return [("fetched", j.get("fetched")), ("last_obs", j.get("last_obs"))]
    if fn == "strategy_backtests.json":
        out = [("generated", j.get("generated"))]
        for nm, b in (j.get("strategies") or {}).items():
            out += [(f"{nm}.start", b.get("start")), (f"{nm}.end", b.get("end"))]
        return out
    return []
for _fn in ("stocks.json", "home_reco.json", "regime.json", "members.json", "rotation_pool.json", "updates.json",
            "strategy_holdings.json", "strategy_holdings_db.json", "strategy_backtests.json", "rf_monthly.json"):
    _p = os.path.join(ROOT, "data", _fn)
    if not os.path.exists(_p): continue
    try:
        _j = json.load(io.open(_p, encoding="utf-8"))
    except Exception:
        continue   # 파싱 오류는 위의 JSON 검사가 이미 보고
    for k, v in _dates_of(_fn, _j):
        if not v:
            errors.append(f"{_fn}: 날짜 필드 {k} 비어 있음"); continue
        try:
            _dt.date.fromisoformat(str(v)[:10])
        except Exception:
            errors.append(f"{_fn}: {k}={v} 날짜 파싱 불가"); continue
        if str(v)[:10] > _tomorrow:
            errors.append(f"{_fn}: {k}={v} 미래 날짜 — 일자 꼬임")
if qj is None or qp is None: errors.append("선별 상수(QUOTA)를 찾지 못함")
elif qj != qp: errors.append(f"QUOTA 불일치: rotation.html {qj} vs rotation_select.py {qp}")
if cj != cp: errors.append(f"CATORD 불일치: rotation.html {cj} vs rotation_select.py {cp}")

# ── 시장 국면: 종합 요약(summary)·블록 슬롯 ──────────────────────────────
#   요약 문장은 build/regime_summary.py 한 곳에서만 만든다(verdicts_gen과 같은 재계산 비교).
#   화면이 문장을 직접 쓰면 지표가 바뀌어도 요약만 옛말을 하는 '조용히 틀린' 사고가 난다.
try:
    sys.path.insert(0, os.path.join(ROOT, "build"))
    import regime_summary
    _rj = json.load(io.open(os.path.join(ROOT, "data", "regime.json"), encoding="utf-8"))
    _cur = _rj.get("summary")
    if not _cur:
        errors.append("regime.json에 summary 없음 — build/refresh_regime.py 재실행 필요")
    else:
        # 자기 축을 prev로 넘겨 재계산 → 히스테리시스가 no-op이 되어 결정적. 지표만 갱신되고
        #   요약이 안 구워졌으면 재계산이 달라져 잡힌다.
        _fresh = regime_summary.build(_rj.get("indicators") or [], _rj.get("regime") or {},
                                      prev_axes=_cur.get("axes"))
        if _fresh != _cur:
            errors.append("종합 요약이 지표와 어긋남 — regime_summary.build() 결과와 불일치(지표만 갱신되고 요약이 안 구워졌다)")
        _txt = (_cur.get("text") or "")
        if _txt and regime_summary.guard(_txt):
            errors.append(f"종합 요약 금칙어/규칙 위반: {regime_summary.guard(_txt)}")
        # 요약 문장이 HTML에 하드코딩되면 드리프트한다(주석 제거 후 검사)
        _rh = strip_js(rd("regime.html"))
        if _txt and len(_txt) > 12 and _txt in _rh:
            errors.append(f'regime.html에 요약 문장이 하드코딩됨: "{_txt[:20]}…" — regime.json에서 읽을 것')
except SystemExit as e:
    errors.append(f"종합 요약 빌드 규칙 위반(금칙어 등): {e}")
except Exception as e:
    errors.append(f"종합 요약 검증 실패: {e}")

# ── 시장 국면: #main을 세 슬롯으로 쪼갠 뒤 렌더/실패 경로가 모두 슬롯을 채우는가 ──────
try:
    _rh = rd("regime.html")
    for _slot in ("summary", "regimeperf", "macro"):
        if not re.search(r'id="%s"' % _slot, _rh):
            errors.append(f"regime.html: 슬롯 #{_slot}가 없음 — 블록 재배치가 반쯤 적용됐다")
        elif not re.search(r"el\('%s'\)\.innerHTML" % _slot, _rh):
            errors.append(f"regime.html: #{_slot}에 렌더하는 코드가 없음 — 영구 '불러오는 중…' 슬롯")
    # 실패 경로가 세 슬롯을 한 번에 채우는 fail() 헬퍼를 쓰는지(개별 el('main')= 잔재 금지)
    if "el('main')" in _rh:
        errors.append("regime.html: el('main') 잔재 — #main은 슬롯 3개로 분리됐다")
    if not re.search(r"function fail\(", _rh):
        errors.append("regime.html: 실패 경로 헬퍼 fail()가 없음 — 로드 실패 시 일부 슬롯이 조용히 빈다")
except Exception as e:
    errors.append(f"슬롯 렌더 검증 실패: {e}")

print("사이트 검증:", "통과 ✅" if not errors else f"실패 ❌ {len(errors)}건")
for e in errors: print("  -", e)
sys.exit(1 if errors else 0)
