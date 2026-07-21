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
PAGES = ["index.html", "stocks.html", "regime.html", "rotation.html", "explorer.html", "archive.html"]
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
getComputedStyle structuredClone queueMicrotask""".split())
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
    # lab 앵커가 실제 항목을 가리키는지
    anames = {d["n"] for d in json.loads(re.search(r"var D=(\[.*?\]);", rd("archive.html"), re.S).group(1))}
    enames = set(re.findall(r'\{n:"([^"]+)"', rd("explorer.html")))
    def _slug(n):   # archive.html / explorer.html의 slug()와 동일 규칙이어야 딥링크가 맞는다
        return re.sub(r"^-+|-+$", "", re.sub(r"[^0-9a-z가-힣]+", "-", str(n).lower()))
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
        # 프래그먼트도 실제 앵커 규칙과 일치하는지(형식만 맞고 대상이 없는 딥링크 방지)
        want = ("a-" if page == "archive.html" else "s-") + _slug(L["t"])
        if frag and frag != want:
            errors.append(f"rotation_pool {s['id']}: lab.href 앵커 불일치 (#{frag} ≠ #{want})")

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
    _s = json.load(io.open(os.path.join(ROOT, "data", "stocks.json"), encoding="utf-8")).get("as_of")
    _h = json.load(io.open(os.path.join(ROOT, "data", "home_reco.json"), encoding="utf-8")).get("as_of")
    if _s and _h and _s != _h:
        errors.append(f"기준일 불일치: stocks.json {_s} vs home_reco.json {_h} — 워크플로가 두 파일을 함께 커밋하는지 확인")
except Exception as e:
    errors.append(f"기준일 교차검증 실패: {e}")
if qj is None or qp is None: errors.append("선별 상수(QUOTA)를 찾지 못함")
elif qj != qp: errors.append(f"QUOTA 불일치: rotation.html {qj} vs rotation_select.py {qp}")
if cj != cp: errors.append(f"CATORD 불일치: rotation.html {cj} vs rotation_select.py {cp}")

print("사이트 검증:", "통과 ✅" if not errors else f"실패 ❌ {len(errors)}건")
for e in errors: print("  -", e)
sys.exit(1 if errors else 0)
