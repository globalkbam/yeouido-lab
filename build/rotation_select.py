# -*- coding: utf-8 -*-
"""오늘(KST)의 로테이션 9선 id/name 출력 — rotation.html의 pick()과 **동일 알고리즘**
(FNV1a 시드 + LCG 셔플, 카테고리 균형 A2·B2·C2·D1·E2, 날짜는 KST 고정).
⚠ rotation.html의 pick()/QUOTA를 바꾸면 이 파일도 반드시 같이 바꿀 것 — 어긋나면 화면의 9선과 갱신 대상이 달라진다.
헤드리스 Claude 일일 갱신 작업이 '오늘 표시되는 9개'만 최근동향을 갱신하도록 사용."""
import json, io, os, sys, datetime
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass
HERE = os.path.dirname(os.path.abspath(__file__))
POOL = os.path.join(HERE, "..", "data", "rotation_pool.json")
CATORD = ["A", "B", "C", "D", "E"]
QUOTA = {"A": 2, "B": 2, "C": 2, "D": 1, "E": 2}


def pick(arr, n, seed_str):
    seed = 2166136261
    for ch in seed_str:
        seed ^= ord(ch); seed = (seed * 16777619) & 0xffffffff

    def rnd():
        nonlocal seed
        seed = (seed * 1664525 + 1013904223) & 0xffffffff
        return seed / 4294967296

    def sh(a):
        a = list(a)
        for i in range(len(a) - 1, 0, -1):
            j = int(rnd() * (i + 1)); a[i], a[j] = a[j], a[i]
        return a

    by = {}
    for s in arr:
        by.setdefault(s.get("cat"), []).append(s)
    out, rest = [], []
    for c in CATORD:
        p = sh(by.get(c, [])); q = QUOTA.get(c, 0)
        out += p[:q]; rest += p[q:]
    if len(out) < n:
        out += sh(rest)[:n - len(out)]
    return out[:n]


def main():
    d = json.load(io.open(POOL, encoding="utf-8"))
    S = d["strategies"]
    KST = datetime.timezone(datetime.timedelta(hours=9))
    today = datetime.datetime.now(KST).strftime("%Y-%m-%d")   # rotation.html의 today()와 동일하게 KST 고정
    sel = pick(S, 9, today)
    print(f"KST_DATE={today}")
    for s in sel:
        print(f'{s["id"]}\t{s["name"]}')
    # 쿼터가 균형이라 카테고리별 재등장 주기가 다르다(E는 21종·쿼터2 → 카드당 ~10일).
    # 오늘의 9선에 없으면서 recent_at이 가장 오래된(또는 없는) 3종을 추가 갱신 대상으로 지정해 방치 카드를 없앤다.
    shown = {s["id"] for s in sel}
    rest = [s for s in S if s["id"] not in shown]
    rest.sort(key=lambda s: (s.get("recent_at") or "0000-00-00", s["id"]))
    print("STALE3")
    for s in rest[:3]:
        print(f'{s["id"]}\t{s["name"]}\t(최근갱신 {s.get("recent_at") or "없음"})')


if __name__ == "__main__":
    main()
