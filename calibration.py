#!/usr/bin/env python3
"""d1-vs-human agreement: measures the Goodhart gap. Run:
  python3 calibration.py           # uses live d1 on 19092
If MAE > 0.25 the judge's taste has drifted from humans — retune routing
thresholds (D1_LOW/D1_GOOD) or grow this set. Never silently trust the judge.
"""
import json
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

from judge import DJudge


def main():
    samples = json.load(open(os.path.join(os.path.dirname(__file__), "calibration.json")))
    j = DJudge()
    errs, rows = [], []
    for s in samples:
        q = j.quality(s["prompt"], s["answer"])
        if q is None:
            print("judge down — start d1 server on 19092")
            raise SystemExit(1)
        e = abs(q - s["human"])
        errs.append(e)
        rows.append((e, s["human"], q, s["prompt"][:34], s.get("why", "")))
    mae = sum(errs) / len(errs)
    worst = sorted(rows, reverse=True)[:3]
    print(f"n={len(rows)} MAE={mae:.3f}  (<=0.25 healthy, else retune)")
    for e, h, q, p, why in worst:
        print(f"  err={e:.2f} human={h:.2f} d1={q:.2f} [{p}] ({why})")
    # Pearson-ish: covariance over stds
    import statistics as _st
    hs = [r[1] for r in rows]
    qs = [r[2] for r in rows]
    mh, mq = _st.mean(hs), _st.mean(qs)
    cov = sum((a - mh) * (b - mq) for a, b in zip(hs, qs)) / len(rows)
    sh, sq = _st.pstdev(hs), _st.pstdev(qs)
    corr = cov / (sh * sq) if sh and sq else 0.0
    print(f"correlation={corr:.3f} (want > 0.8)")
    assert mae <= 0.25 and corr > 0.8, "judge disagrees with humans — do not route on it blindly"


if __name__ == "__main__":
    main()
