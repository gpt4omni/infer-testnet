#!/usr/bin/env python3
"""Hard-attack drills against the NEW layers. Each must be CAUGHT.

Attack 1 (hardcoder): read public TASKS tests, hardcode exactly those
  answers. Passes the OLD static check (proving the 2/10 hole), dies on
  secret fresh canaries.
Attack 2 (epoch memorize): audit transparency reveals epoch-0 tasks, so
  attacker memorizes them. Rotation to epoch-1 seed kills the memory.
Attack 3 (too-fast): 5ms answers claiming a 7B model trip the latency
  floor -> mandatory spot-check -> fail -> slash.
Attack 4 (profile collapse): registered (1,1,1), swapped model measures
  (0,0,0): L1 distance 3.0 >> 0.25 threshold. Honest re-check: 0.0.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

from ledger import connect, register_miner, slash
from miner import MockBackend
from money import to_watts, fmt_watts
from eval import TASKS, check
from canary import gen_tasks, run_tests, epoch_commit
from verify import CapabilityProfile, latency_plausible

DB = os.path.join(os.path.dirname(__file__), "ledger-verify-hard.db")

HARDCODED = {
    # exact answers lifted from public TASKS — the old hole
    "add": "def solve(a, b):\n    return {(1, 2): 3, (0, 0): 0, (-5, 5): 0}.get((a, b), 0)\n",
    "fib": "def solve(n):\n    return {0: 0, 1: 1, 10: 55}.get(n, 0)\n",
    "reverse": "def solve(s):\n    return {'abc': 'cba', '': ''}.get(s, '')\n",
    "sortnums": "def solve(xs):\n    return []\n",
    "vowels": "def solve(s):\n    return 0\n",
}


class HardcodeBackend:
    def generate(self, prompt):
        import time
        t0 = time.time()
        time.sleep(0.005)
        return HARDCODED.get(prompt, "def solve(x):\n    return None\n"), (time.time() - t0) * 1000


def canary_pass_rate(backend, seed, n_per_tier=4) -> float:
    tasks = gen_tasks(seed, n_per_tier)
    ok = 0
    for t in tasks:
        code, _ = backend.generate(t["prompt"])
        ok += run_tests(code, t["tests"])
    return ok / len(tasks)


def main():
    if os.path.exists(DB):
        os.remove(DB)
    c = connect(DB)
    register_miner(c, "mallory", "qwen2.5-7b-Q4", 0.02, to_watts(500))
    good, hard = MockBackend("good"), HardcodeBackend()

    print("--- 1: hardcoder vs OLD static check vs NEW secret canaries ---")
    old = all(check(HARDCODED[k], TASKS[k]["tests"]) for k in ("add", "fib", "reverse"))
    print(f"old static probes: {'PASS (hole confirmed)' if old else 'fail'}")
    assert old  # the 2/10 hole, demonstrated
    rate = canary_pass_rate(hard, seed="epoch-7", n_per_tier=4)
    print(f"secret fresh canaries: pass rate {rate:.2f} -> {'CAUGHT' if rate < 0.5 else 'PASS (BAD!)'}")
    assert rate < 0.5

    print("--- 2: epoch rotation kills memorization ---")
    ep0 = gen_tasks("epoch-7", n_per_tier=4)
    memorized = {t["id"]: t["tests"][0][1] for t in ep0}  # attacker saw the audit reveal
    ep1 = gen_tasks("epoch-8", n_per_tier=4)
    reused = sum(1 for t in ep1 if t["id"] in memorized)  # ids bind the seed
    print(f"epoch-0 answers reusable in epoch-1: {reused}/{len(ep1)} -> "
          f"{'CAUGHT (memory dead)' if reused == 0 else 'PASS (BAD!)'}")
    assert reused == 0
    print(f"epoch commits differ: {epoch_commit('epoch-7') != epoch_commit('epoch-8')}")

    print("--- 3: too-fast tripwire -> spot-check -> slash ---")
    _, lat = hard.generate("add")
    ok, why = latency_plausible("qwen2.5-7b-Q4", lat)
    print(f"7B model answering in {lat:.1f}ms: {why}")
    assert not ok  # trips mandatory spot-check
    rate = canary_pass_rate(hard, seed="epoch-8", n_per_tier=4)
    if rate < 0.5:
        slash(c, "mallory", "miner", to_watts(25), "tripwire spot-check failed on fresh canaries")
        print(f"spot-check failed ({rate:.2f}) -> slashed {fmt_watts(to_watts(25))}")

    print("--- 4: capability profile collapse (no false positive) ---")
    prof = CapabilityProfile()
    reg = prof.register("mallory", good, seed="calib-1")
    print(f"registered: {tuple(round(x, 2) for x in reg)}")
    ok, dist, vec = prof.spot_check("mallory", good, seed="calib-2")
    print(f"honest re-check: dist={dist:.2f} -> {'OK, no false positive' if ok else 'FALSE POSITIVE (BAD!)'}")
    assert ok
    ok, dist, vec = prof.spot_check("mallory", MockBackend("cheat"), seed="calib-2")
    print(f"swapped model: vec={tuple(round(x, 2) for x in vec)} dist={dist:.2f} -> "
          f"{'CAUGHT: substitution' if not ok else 'PASS (BAD!)'}")
    assert not ok

    print("\nALL HARD ATTACKS CAUGHT. Rating now ~5-6/10 (see limits).")


if __name__ == "__main__":
    main()
