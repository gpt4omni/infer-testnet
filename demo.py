#!/usr/bin/env python3
"""End-to-end: good/small-cheap vs cheat/fast-wrong. Hidden evals + customer jobs."""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))
from ledger import connect, register_miner, leaderboard
from gateway import Miner, dispatch
from miner import MockBackend
from money import fmt_watts

def main():
    DB = os.path.join(os.path.dirname(__file__), "ledger.db")
    if os.path.exists(DB):
        os.remove(DB)
    c = connect(DB)

    miners = [
        Miner("good-1080ti", MockBackend("good"), "qwen2.5-7b-Q4", price_per_1k=0.02),
        Miner("cheat-tiny", MockBackend("cheat"), "tiny-0.5b", price_per_1k=0.002),
    ]
    for m in miners:
        register_miner(c, m.mid, m.model, m.price)

    plan = [("add", False), ("fib", True), ("reverse", False),
            ("fib", False), ("add", True), ("reverse", True)] * 2
    for task, is_eval in plan:
        for m in miners:
            passed, s, pay, lat, _ = dispatch(c, m, task, is_eval)
            tag = "EVAL " if is_eval else "CUST "
            print(f"{tag} {m.mid:12s} {task:8s} pass={int(passed)} lat={lat:6.1f}ms pay={fmt_watts(pay)}", flush=True)

    print("\n--- leaderboard (payout-weighted) ---")
    for mid, n, passed, pay, avg in leaderboard(c):
        print(f"{mid:12s} jobs={n} passed={passed} payout={fmt_watts(pay)} avg_score={avg:.3f}")
    print("\nExpect: cheat is 10x cheaper + faster but earns ~0 (fails evals, cust refunds).")
    print("Swap MockBackend->LlamaCppBackend for the 1080 Ti step; same gateway/eval.")


if __name__ == "__main__":
    main()
