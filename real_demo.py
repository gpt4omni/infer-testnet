#!/usr/bin/env python3
"""Real-GPU demo: your 1080 Ti (Gemma 12B via atomic-chat llama-server)
vs cheat-tiny mock. Same gateway/eval/scoring as demo.py."""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))
from ledger import connect, register_miner, leaderboard
from gateway import Miner, dispatch
from miner import MockBackend, LlamaCppBackend
from money import fmt_watts

DB = os.path.join(os.path.dirname(__file__), "ledger-real.db")
if os.path.exists(DB):
    os.remove(DB)
c = connect(DB)

real = Miner("real-1080ti-gemma12b", LlamaCppBackend(), "gemma-4-12B-Q4", price_per_1k=0.05)
cheat = Miner("cheat-tiny", MockBackend("cheat"), "tiny-0.5b", price_per_1k=0.002)
for m in (real, cheat):
    register_miner(c, m.mid, m.model, m.price)


def main():
    for task, is_eval in [("add", False), ("fib", True), ("reverse", False)]:
        for m in (real, cheat):
            passed, s, pay, lat, code = dispatch(c, m, task, is_eval)
            tag = "EVAL " if is_eval else "CUST "
            print(f"{tag} {m.mid:20s} {task:8s} pass={int(passed)} "
                  f"lat={lat:7.1f}ms pay={fmt_watts(pay)} toks={max(1,len(code)//4)}", flush=True)
    print("\n--- leaderboard ---")
    for mid, n, passed, pay, avg in leaderboard(c):
        print(f"{mid:20s} jobs={n} passed={passed} payout={fmt_watts(pay)} avg_score={avg:.3f}")


if __name__ == "__main__":
    main()
