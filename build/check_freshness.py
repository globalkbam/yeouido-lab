# -*- coding: utf-8 -*-
"""갱신 신선도 게이트 — 자동 갱신 잡이 '돌았는데 낡은 데이터'를 남기면 잡을 실패시킨다.

왜 필요한가
    GitHub 스케줄 크론은 지연·드롭이 잦다(실측: regime 정시 07:45 안 뜨고 백업 08:20이 24분 늦게
    발화, stocks·sentiment는 아예 커밋 없이 07-21에 고착). 잡이 '성공'으로 끝나거나 '변경 없음'으로
    조용히 스킵하면 아무도 모른다. 이 스크립트를 갱신 스텝 뒤에 두어, 산출물의 기준일이 오늘 기준으로
    2영업일 이상 밀려 있으면 **exit 1**로 잡을 빨갛게 만든다 → GitHub 기본 실패 알림 메일이 소유자에게 간다
    (외부 알림 채널 없음 — 텔레그램 등은 사내망 보안으로 미채택).

    '아예 안 뜬' 케이스(스케줄러가 잡을 시작조차 안 함)는 이걸로 못 잡는다(잡이 안 돌았으니 검사도 못 함).
    그건 사이트 자체의 신선도 배지(sources.html)가 접속 시 눈에 보이게 드러낸다 — 두 층이 상보적이다.

영업일 계산은 기존 페이지의 '3영업일 지연 경고' JS와 같은 규약이다(as_of 다음날부터 오늘 직전까지,
주말 제외). 해외(미국) 데이터는 해외=국내 T-1이라 정상 일일 갱신의 기준일이 '어제'가 되어 n=1이고,
2영업일 이상(n>=2)은 갱신이 최소 한 번 이상 누락됐다는 뜻이다.

  python build/check_freshness.py <json_path> <label> [max_biz_days=2]
"""
from __future__ import annotations

import io
import json
import sys
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))


def biz_days_behind(as_of: str, today) -> int:
    """(as_of, today] 구간의 영업일 수. 기존 페이지 '3영업일 지연 경고' JS와 동일 규약.

    해외=국내 T-1이라 정상 일일 갱신은 기준일=어제 → n=1. 갱신이 한 번 누락되면 n=2.
    (주말·월요일 경계 검증 완료: 토요일 조회 n=0, 월요일 정상 n=1.)
    """
    y, m, d = (int(x) for x in str(as_of)[:10].split("-"))
    cur = datetime(y, m, d, tzinfo=KST).date()
    n = 0
    while cur < today and n < 60:
        cur = cur + timedelta(days=1)
        if cur.weekday() < 5:      # 0=월 … 4=금
            n += 1
    return n


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    path, label = sys.argv[1], sys.argv[2]
    max_biz = int(sys.argv[3]) if len(sys.argv) > 3 else 2
    try:
        doc = json.load(io.open(path, encoding="utf-8"))
    except Exception as e:
        print(f"::error::[{label}] {path} 읽기 실패: {e}")
        return 1
    as_of = doc.get("as_of")
    if not as_of:
        print(f"::error::[{label}] as_of 없음 — 산출물이 비었거나 스키마가 바뀌었다")
        return 1
    today = datetime.now(KST).date()
    n = biz_days_behind(as_of, today)
    if n >= max_biz:
        print(f"::error::[{label}] 기준일 {as_of} — 오늘({today}) 기준 {n}영업일 지연(허용 {max_biz-1}). "
              f"갱신이 누락됐다. Actions에서 해당 워크플로를 재실행하라.")
        return 1
    print(f"[{label}] 기준일 {as_of} · {n}영업일 — 신선(OK)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
