# -*- coding: utf-8 -*-
"""매크로 지표 39개를 종합해 '지금 시장이 어떤 상황인지' 한 문장으로 굽는다 — 단일 소스.

【왜 빌드 타임인가】
지표의 상태 톤(good/neut/watch/hot)은 refresh_regime.py의 판정 람다가 **지표마다 손으로** 정한다.
그 의미를 점수로 옮기는 이 매핑은 39개 지표 의미를 두 번째로 선언하는 자리라, 화면(JS)에서 하면
지표를 추가·수정할 때마다 다른 언어·다른 파일에서 같은 걸 두 번 고쳐야 하고 반드시 갈린다.
그래서 여기 한 곳에서만 만들고 regime.json의 summary에 구워, 화면은 text를 그대로 출력만 한다.
validate_site.py가 이 build()를 재실행해 regime.json.summary와 비교한다(위조 불가능한 데이터 계약).

【톤이 그룹을 가로질러 일관되지 않다 — 그래서 선언적 매핑】
· 임금(CES0500000003)은 노동 그룹 카드지만 톤은 물가 렌즈(임금인플레) → 집계는 물가축으로 옮긴다.
· 금리축(DFF·DGS…)은 '낮으면 good'이라 침체에서 가장 좋아 보인다 → 실물 판정에 합산하지 않고
  긴축/완화 어휘로만 서술한다.
· M2SL·CSUSHPINSA는 구조적으로 good에 도달 못 한다(스케일 비대칭) → 카드는 표시, 점수화만 제외.
방향(sp/서프라이즈)은 쓰지 않는다 — z와 chg 부호가 39개 중 10개에서 어긋난다(레벨 톤만 쓴다).

이 모듈은 외부 호출이 전혀 없다(indicators/regime dict만 받는다). 사내 식별자·경로 유출 표면 없음.
"""
from __future__ import annotations

import re

# ── 점수·밴드 ────────────────────────────────────────────────────────────
EXCLUDE = {"M2SL": "확장(>6%)조차 중립 — good 도달 불가", "CSUSHPINSA": "good 도달 불가 + 렌즈 모순"}
SCORE = {"good": 1.0, "neut": 0.0, "watch": -0.5, "hot": -1.0}
CUT = (0.50, 0.15, -0.15, -0.55)   # 5밴드 경계. 음쪽 끝이 -0.55인 이유: watch만 모인 축이
#   최강 부정어에 닿으면 안 된다(hot이 섞여야 도달). MIN_N/혼조/히스테리시스 상수는 아래.
MIN_N, MIX_SHARE, MIX_RNG, DEAD, MAX_S1 = 2, 0.30, 1.5, 0.08, 3

# 밴드 5단계 × 축별 (명사형, 서술형). 자동 활용 금지 — "완만한 개선한 반면" 같은 비문 방지.
R_REAL = [("견조", "견조합니다"), ("완만", "완만한 개선세입니다"), ("보통", "보통 수준입니다"),
          ("둔화", "둔화 흐름입니다"), ("부진", "부진합니다")]
AXES = [
    ("소비", "real", 1, R_REAL, ["RSAFS", "UMCSENT"]),
    ("고용", "real", 1, R_REAL, ["PAYEMS", "UNRATE", "SAHMREALTIME", "ICSA", "JTSJOL", "CCSA"]),
    ("생산", "real", 1, R_REAL, ["CFNAIMA3", "INDPRO", "TCU"]),
    ("주택", "real", 0, R_REAL, ["HOUST", "PERMIT", "CSUSHPINSA"]),
    ("물가", "cond", 1,
     [("안정", "안정적입니다"), ("대체로 안정", "대체로 안정적입니다"), ("혼조", "혼조입니다"),
      ("상방 압력", "상방 압력이 남아 있습니다"), ("높은 수준", "높은 수준입니다")],
     ["CPIYOY", "CPILFESL", "PCEPILFE", "PCEPI", "PPIACO", "T10YIE", "T5YIFR",
      "DCOILWTICO", "GASREGW", "CES0500000003"]),
    ("금융여건", "cond", 1,
     [("완화적", "완화적입니다"), ("다소 완화", "다소 완화적입니다"), ("중립", "중립입니다"),
      ("다소 긴축", "다소 긴축적입니다"), ("긴축적", "긴축적입니다")],
     ["NFCI", "BAMLH0A0HYM2", "BAMLC0A0CM", "T10Y2Y", "T10Y3M", "VIXCLS", "DFII10", "DTWEXBGS"]),
    ("금리", "cond", 0,
     [("낮은 수준", "낮은 수준입니다"), ("다소 낮음", "다소 낮습니다"), ("중립", "중립입니다"),
      ("다소 높음", "다소 높습니다"), ("높은 수준", "높은 수준입니다")],
     ["DFF", "DGS2", "DGS3MO", "DGS10", "DGS30", "MORTGAGE30US"]),
]

# 과대 서술 방지 — 면책문 대신 금칙어로 막는다(걸리면 SystemExit).
BAN = ["전망", "예상", "기대되", "유리", "불리", "매수", "매도", "비중확대", "시사", "가능성", "반등",
       "임박", "때문", "추천", "권장", "목표가", "보입니다", "전환될", "진입할", "회복될"]


def _jong(w: str) -> int:
    w = re.sub(r"[^가-힣]+$", "", (w or "").strip())
    if not w:
        return 0
    c = ord(w[-1])
    return (c - 0xAC00) % 28 if 0xAC00 <= c <= 0xD7A3 else 0


def _J(w: str, pair: str) -> str:
    """받침 판정으로 조사 생성. '위축로'→'위축으로', '소비은'→'소비는'."""
    a, b = pair.split("/")
    j = _jong(w)
    if pair == "으로/로":
        return w + ("로" if j in (0, 8) else "으로")   # 받침없음·ㄹ → '로'
    return w + (a if j else b)


def _band(m: float) -> int:
    return 0 if m >= CUT[0] else 1 if m >= CUT[1] else 2 if m > CUT[2] else 3 if m > CUT[3] else 4


def _short(label: str) -> str:
    return (label or "").split(" (")[0].strip()


def _axes(indicators, prev_axes=None):
    ind = {i.get("k"): i for i in (indicators or [])}
    pm = {a["name"]: a for a in (prev_axes or [])}
    out = []
    for name, kind, lead, W, codes in AXES:
        xs = []
        for c in codes:
            if c in EXCLUDE:
                continue
            it = ind.get(c)
            if not it or it.get("v") is None:
                continue
            t = (it.get("st") or ["", ""])[1]
            if t in SCORE:
                xs.append({"k": c, "label": it.get("label", c), "st": (it.get("st") or ["", ""])[0],
                           "tone": t, "s": SCORE[t]})
        if len(xs) < MIN_N:
            out.append({"name": name, "kind": kind, "lead": lead, "n": len(xs),
                        "skip": True, "mixed": False})
            continue
        m = sum(x["s"] for x in xs) / len(xs)
        b = _band(m)
        pb = (pm.get(name) or {}).get("band")
        # 히스테리시스: 밴드 전환은 경계를 DEAD만큼 넘겨야 성립(지표 하나로 문구가 흔들리는 휩소 억제)
        if pb is not None and b != pb and _band(m - DEAD if b < pb else m + DEAD) == pb:
            b = pb
        pos = sum(1 for x in xs if x["s"] > 0) / len(xs)
        neg = sum(1 for x in xs if x["s"] < 0) / len(xs)
        rng = max(x["s"] for x in xs) - min(x["s"] for x in xs)
        best, worst = max(xs, key=lambda x: x["s"]), min(xs, key=lambda x: x["s"])
        out.append({"name": name, "kind": kind, "lead": lead, "n": len(xs), "m": round(m, 3),
                    "band": b, "word": W[b][0], "wordv": W[b][1], "spread": round(rng, 3),
                    "mixed": min(pos, neg) >= MIX_SHARE and rng >= MIX_RNG, "skip": False,
                    "best": {"label": best["label"], "st": best["st"]},
                    "worst": {"label": worst["label"], "st": worst["st"]}})
    return out


def _clause(groups):
    parts = []
    for i, (nms, w, wv) in enumerate(groups):
        nm = "·".join(nms)
        parts.append(f"{_J(nm, '은/는')} " + (wv if i == len(groups) - 1 else w))
    return ", ".join(parts)


def _merge(axs):
    g = []
    for a in axs:
        if g and g[-1][1] == a["word"]:
            g[-1][0].append(a["name"])
        else:
            g.append(([a["name"]], a["word"], a["wordv"]))
    return g


def _sentence(ax):
    real = [a for a in ax if a["kind"] == "real" and not a["skip"]]
    if len(real) < 3:
        return None, "INSUFFICIENT"
    conf = lambda a: a["n"] >= 3 or abs(a["m"]) >= 0.75      # n=2 축은 강한 근거일 때만 헤드라인
    pos = sorted([a for a in real if a["band"] <= 1 and conf(a)], key=lambda a: (-a["lead"], -a["m"]))
    neg = sorted([a for a in real if a["band"] >= 3 and conf(a)], key=lambda a: (-a["lead"], a["m"]))
    uni = len({a["band"] for a in real}) == 1
    if pos and neg:
        mode = "CONTRAST"
        s1 = _clause(_merge(sorted(pos, key=lambda a: -a["m"])[:2]) +
                     _merge(sorted(neg, key=lambda a: a["m"])[:2]))
        named = {a["name"] for a in (pos[:2] + neg[:2])}
    elif uni and pos and len(pos) == len(real):
        mode, s1, named = "UNIFORM_POS", f"실물 지표({'·'.join(a['name'] for a in real)})가 고르게 {real[0]['wordv']}", None
    elif uni and neg and len(neg) == len(real):
        mode, s1, named = "UNIFORM_NEG", f"실물 지표({'·'.join(a['name'] for a in real)})가 전반적으로 {neg[0]['wordv']}", None
    elif pos or neg:
        mode = "PARTIAL"
        g = _merge(sorted(real, key=lambda a: -a["m"]))
        if len(g) > MAX_S1:
            g = g[:MAX_S1 - 1] + [g[-1]]      # 절이 넘치면 중간(중립)부터 접되 양 끝은 남긴다
        s1, named = _clause(g), None
    else:
        mode, s1, named = "FLAT", "실물 지표가 어느 쪽으로도 치우치지 않은 중립 구간입니다", None

    cand = [a for a in real if a["mixed"] and (named is None or a["name"] in named)]
    s_mix = ""
    if cand:
        a = max(cand, key=lambda x: x.get("spread", 0))   # 가장 크게 갈리는 축 하나만 해설
        s_mix = (f"다만 {_J(a['name'], '은/는')} {_J(_short(a['best']['label']), '이/가')} {a['best']['st']}, "
                 f"{_J(_short(a['worst']['label']), '이/가')} {_J(a['worst']['st'], '으로/로')} 갈립니다")
    cond = [a for a in ax if a["kind"] == "cond" and a["lead"] and not a["skip"]]
    s2 = _clause([([a["name"]], a["word"], a["wordv"]) for a in cond]) if cond else ""
    txt = " ".join([s1 + "."] + ([s_mix + "."] if s_mix else []) + ([s2 + "."] if s2 else []))
    return txt, mode


def guard(text: str):
    """금칙어·미래시제·길이 검사. 위반 목록을 반환(빈 리스트=통과)."""
    hits = [w for w in BAN if w in (text or "")]
    if re.search(r"(겠[다습]|ㄹ 것|할 것|될 것|리라)", text or ""):
        hits.append("미래시제")
    if len(text or "") > 150:
        hits.append(f"길이{len(text)}")
    return hits


def build(indicators, regime=None, prev_axes=None) -> dict:
    """regime.json에 구울 summary 객체. 화면은 text를 그대로 출력하고 칩은 chips로 그린다."""
    ax = _axes(indicators, prev_axes)
    text, mode = _sentence(ax)
    if text is not None:
        hits = guard(text)
        if hits:
            raise SystemExit(f"[regime_summary] 종합 요약 금칙어/규칙 위반 {hits}: {text}")
    chips = [{"name": a["name"], "kind": a["kind"],
              "word": (a.get("word") if not a["skip"] else None), "n": a["n"], "skip": a["skip"]}
             for a in ax]
    # CI 재계산 비교용 축 상세(문장 로직을 JS로 복제하지 않게 함께 싣는다)
    axes_out = [{k: a.get(k) for k in ("name", "kind", "n", "m", "band", "word", "mixed", "skip")}
                for a in ax]
    return {"text": text, "mode": mode, "chips": chips, "axes": axes_out, "excluded": EXCLUDE}


if __name__ == "__main__":
    import io, json, os, sys
    p = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "data", "regime.json")
    d = json.load(io.open(p, encoding="utf-8"))
    s = build(d.get("indicators") or [], d.get("regime") or {})
    print("mode:", s["mode"])
    print("text:", s["text"])
    print("chips:", " · ".join(f"{c['name']} {c['word'] or '—'}" for c in s["chips"]))
