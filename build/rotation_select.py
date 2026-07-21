# -*- coding: utf-8 -*-
"""오늘(UTC)의 로테이션 9선 id/name 출력 — rotation.html의 pick()과 동일 알고리즘(FNV1a seed + LCG shuffle).
헤드리스 Claude 일일 갱신 작업이 '오늘 표시되는 9개'만 최근동향을 갱신하도록 사용."""
import json, io, os, sys, datetime
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass
HERE = os.path.dirname(os.path.abspath(__file__))
POOL = os.path.join(HERE, "..", "data", "rotation_pool.json")

def pick(arr, n, seed_str):
    seed = 2166136261
    for ch in seed_str:
        seed ^= ord(ch); seed = (seed * 16777619) & 0xffffffff
    idx = list(range(len(arr)))
    def rnd():
        nonlocal seed; seed = (seed * 1664525 + 1013904223) & 0xffffffff; return seed / 4294967296
    for i in range(len(idx) - 1, 0, -1):
        j = int(rnd() * (i + 1)); idx[i], idx[j] = idx[j], idx[i]
    return [arr[k] for k in idx[:min(n, len(arr))]]

def main():
    d = json.load(io.open(POOL, encoding="utf-8"))
    S = d["strategies"]
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")   # rotation.html은 UTC(toISOString) 기준
    sel = pick(S, 9, today)
    print(f"UTC_DATE={today}")
    for s in sel:
        print(f'{s["id"]}\t{s["name"]}')

if __name__ == "__main__":
    main()
