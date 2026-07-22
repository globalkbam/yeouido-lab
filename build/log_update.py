# -*- coding: utf-8 -*-
"""갱신 피드(data/updates.json) 기록 — 날짜 + 시각(KST).

지금까지 커밋마다 인라인 파이썬으로 이벤트를 밀어 넣었다. 매번 다시 쓰면 형식이 갈리므로
여기 한 곳으로 모은다. 시각을 붙이는 이유는 하루에 여러 번 갱신될 때 순서가 보이지 않아서다.

  python build/log_update.py <target> "<제목>"        # 지금 시각(KST)으로 기록
  python build/log_update.py <target> "<제목>" 14:35  # 시각 지정

target: rotation|explorer|archive|stocks|regime|sentiment|holdings
"""
from __future__ import annotations
import io, json, os, re, sys
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
P = os.path.join(ROOT, "data", "updates.json")
TARGETS = {"rotation", "explorer", "archive", "stocks", "regime", "sentiment", "holdings"}


def add(target: str, title: str, hm: str | None = None) -> dict:
    if target not in TARGETS:
        raise SystemExit(f"target은 {sorted(TARGETS)} 중 하나여야 한다: {target}")
    now = datetime.now(KST)
    if hm is None:
        hm = now.strftime("%H:%M")
    if not re.fullmatch(r"[0-2]\d:[0-5]\d", hm):
        raise SystemExit(f"시각 형식은 HH:MM: {hm}")
    doc = json.load(io.open(P, encoding="utf-8"))
    ev = {"dt": now.strftime("%Y-%m-%d"), "hm": hm, "target": target, "title": title}
    # 같은 날·같은 대상·같은 제목이 이미 있으면 시각만 갱신(중복 누적 방지)
    for e in doc["events"]:
        if (e["dt"], e["target"], e["title"]) == (ev["dt"], ev["target"], ev["title"]):
            e["hm"] = hm
            break
    else:
        doc["events"].insert(0, ev)
    # 최신순 정렬 — 시각이 없는 옛 이벤트는 그날 마지막으로 취급(하루 안 순서만 영향)
    doc["events"].sort(key=lambda e: (e["dt"], e.get("hm") or "99:99"), reverse=True)
    doc["updated"] = ev["dt"]
    json.dump(doc, io.open(P, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return ev


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    e = add(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    print(f"기록: {e['dt']} {e['hm']} [{e['target']}] {e['title']}")
