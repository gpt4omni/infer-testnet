#!/usr/bin/env python3
"""Jury drills: median overrules the liar, challenges pay truth-tellers,
rotating rubrics kill hill-climbers, reputation starts at zero."""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

import pq
from ledger import connect, register_pq, transfer, balance_watts, staked_watts
from money import to_watts, fmt_watts
from scoring import (Jury, Rubric, draw_scorers, commit_score, open_challenge,
                     settle_challenge, reputation, route_weight, CHALLENGE_BOND_WATTS)

DB = "/tmp/opencode/jury.db"


def main():
    if os.path.exists(DB):
        os.remove(DB)
    c = connect(DB)
    jury = Jury()

    print("--- 1: median overrules liar (commit-reveal) ---")
    scorers = ["s1", "s2", "liar"]
    assert draw_scorers("job-9", scorers) == draw_scorers("job-9", scorers), "draw must be deterministic"
    print("draw for job-9:", draw_scorers("job-9", scorers + ["s4", "s5"]))
    votes = {"s1": (0.82, "a"), "s2": (0.79, "b"), "liar": (0.05, "c")}
    for s, (v, salt) in votes.items():
        jury.commit("job-9", s, commit_score("job-9", v, salt))
    # liar sees honest commits, tries to move closer post-hoc
    ok, why = jury.reveal("job-9", "liar", 0.70, "c")
    print(f"liar edits after seeing others: {why} -> {'CAUGHT' if not ok else 'PASS (BAD!)'}")
    assert not ok
    ok, _ = jury.reveal("job-9", "liar", 0.05, "c")
    assert ok
    for s in ("s1", "s2"):
        assert jury.reveal("job-9", s, votes[s][0], votes[s][1])[0]
    med = jury.median("job-9")
    print(f"median={med:.2f} (liar wanted 0.05)")
    assert med == 0.79

    print("--- 2: challenge market pays truth ---")
    ch = pq.generate()
    ac = pq.generate()
    for k in (ch, ac):
        register_pq(c, k["addr"], pq.root_of(bytes.fromhex(k["seed"])).hex())
        transfer(c, "faucet", k["addr"], to_watts(10))
    cid = open_challenge(c, ch, "job-9", "liar scored 0.05, panel said 0.79")
    print(f"bond locked: {fmt_watts(CHALLENGE_BOND_WATTS)}")
    won, detail = settle_challenge(c, cid, {"f1": 0.81, "f2": 0.78, "f3": 0.83},
                                   0.05, ch["addr"], ac["addr"])
    print(detail)
    assert won
    print(f"challenger bal: {fmt_watts(balance_watts(c, ch['addr']))} | accused slashed, stake burned")
    # frivolous challenge burns instead
    cid2 = open_challenge(c, ch, "job-10", "grief")
    won2, detail2 = settle_challenge(c, cid2, {"f1": 0.80, "f2": 0.82}, 0.81, ch["addr"], ac["addr"])
    print(detail2)
    assert not won2

    print("--- 3: rotating rubric kills hill-climber ---")
    rub = Rubric()
    commit = rub.publish(7, "epoch-seven-seed")
    assert rub.verify(7, "epoch-seven-seed") and not rub.verify(7, "wrong")
    ep7 = {(t["kind"], str(t["tests"][0][0])) for t in rub.probes(7, 3)}
    rub.publish(8, "epoch-eight-seed")
    ep8 = {(t["kind"], str(t["tests"][0][0])) for t in rub.probes(8, 3)}
    overlap = len(ep7 & ep8)
    print(f"epoch commit {commit[:12]}.. | memorized tasks reusable next epoch: {overlap}/{len(ep7)}")
    assert overlap == 0

    print("--- 4: reputation starts at zero ---")
    w_new = route_weight(reputation(c, "pq1nobody"), None)
    c.execute("INSERT INTO jobs(miner_id,prompt_hash,output_hash,tokens,latency_ms,is_eval,passed,score,payout_watts,ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
              ("pq1vet", "a", "b", 10, 5.0, 0, 1, 50.0, 500, 0.0))
    c.commit()
    w_vet = route_weight(reputation(c, "pq1vet"), 0.8)
    print(f"newcomer weight={w_new:.2f} veteran weight={w_vet:.2f}")
    assert w_new == 0.0 and w_vet > 0
    print("JURY DEMO PASS: no appointed judges, only bonds and math.")


if __name__ == "__main__":
    main()
