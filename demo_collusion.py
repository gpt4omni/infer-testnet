#!/usr/bin/env python3
"""Collusion demo: gw-collude steers to friend-cheat, ev-collude upvotes him.
Audit replays deterministic rule + hidden-eval ground truth, slashes both."""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))
from ledger import (connect, register_miner, log_job, log_route, commit_eval,
                    slash, audit_steering, leaderboard)
from gateway import Miner, dispatch
from miner import MockBackend, sha
from routing import select_miner, assign_evaluator, commit_eval_set
from eval import TASKS, check
from money import to_watts, fmt_watts

DB = os.path.join(os.path.dirname(__file__), "ledger-collude.db")

MINERS = [
    {"id": "good-1080ti", "model": "qwen2.5-7b-Q4", "price": 0.02,
     "stake": to_watts(500), "ema": 1.0, "backend": "good"},
    {"id": "friend-cheat", "model": "tiny-0.5b", "price": 0.002,
     "stake": to_watts(50), "ema": 0.1, "backend": "cheat"},
]
EVS = ["ev-honest", "ev-collude"]
EPOCHS, JOBS_PER_EPOCH = 2, 6


def main():
    if os.path.exists(DB):
        os.remove(DB)
    c = connect(DB)
    live = {}
    for m in MINERS:
        register_miner(c, m["id"], m["model"], m["price"], m["stake"])
        live[m["id"]] = Miner(m["id"], MockBackend(m["backend"]), m["model"], m["price"])
    commit_eval(c, "ev-honest", 0, commit_eval_set({k: "v1" for k in TASKS}))
    commit_eval(c, "ev-collude", 0, commit_eval_set({k: "v1" for k in TASKS}))

    truth_log = []  # (evaluator, reported, truth) on hidden evals
    seq = 0
    tasks = list(TASKS)
    for epoch in range(EPOCHS):
        for j in range(JOBS_PER_EPOCH):
            task = tasks[(epoch * JOBS_PER_EPOCH + j) % len(tasks)]
            is_eval = (j % 2 == 1)
            job_id = f"e{epoch}-j{j}"
            want, rule = select_miner(job_id, MINERS)
            ev = assign_evaluator(epoch, seq, EVS)
            seq += 1
            for gw, actual in (("gw-honest", want), ("gw-collude", "friend-cheat")):
                passed, s, pay, lat, code = dispatch(c, live[actual], task, is_eval)
                truth = passed
                if is_eval and ev == "ev-collude" and actual == "friend-cheat":
                    passed, pay, s = True, 500, 99.0  # fake upvote
                jid = log_job(c, actual, sha(task), sha(code), max(1, len(code) // 4),
                              lat, is_eval, passed, s, pay, gw, ev, epoch)
                log_route(c, jid, gw, actual, want, rule)
                if is_eval:
                    truth_log.append((ev, passed, truth, gw, actual))

    print("--- routing audit (gateway, routes, honest) ---")
    for gw, n, honest in audit_steering(c):
        print(f"{gw:12s} routes={n} honest={honest} -> "
              f"{'OK' if honest == n else 'STEERING DETECTED'}")
    ev_acc = {}
    for ev, rep, tru, _, _ in truth_log:
        a = ev_acc.setdefault(ev, [0, 0])
        a[1] += 1
        a[0] += (rep == tru)
    print("--- evaluator accuracy on hidden evals ---")
    for ev, (ok, n) in ev_acc.items():
        print(f"{ev:12s} {ok}/{n} correct -> {'OK' if ok == n else 'LYING DETECTED'}")

    slash(c, "gw-collude", "gateway", to_watts(100), "systematic steering off deterministic rule")
    slash(c, "ev-collude", "evaluator", to_watts(75), "upvoting failed hidden evals")
    slash(c, "friend-cheat", "miner", to_watts(25), "beneficiary of steering+upvotes")
    print("--- slashes applied ---")
    for entity, kind, amount, reason in c.execute("SELECT entity,kind,amount_watts,reason FROM slashes"):
        print(f"{entity} [{kind}] {fmt_watts(amount)}: {reason}")
    print("--- leaderboard (payout still favors good despite collusion) ---")
    for mid, n, psd, pay, avg in leaderboard(c):
        print(f"{mid:14s} jobs={n} reported_pass={psd} payout={fmt_watts(pay)}")


if __name__ == "__main__":
    main()
