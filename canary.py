#!/usr/bin/env python3
"""Secret rotating canary pool. Why this beats static probes:

Static probes (old CANARY_PROBES) are public source: attacker hardcodes
those exact answers and coasts. Here tasks are derived from a SECRET
epoch seed via a PRNG, so the task space is effectively infinite and
unpredictable. Prompts carry only the function signature ("add"), never
the test inputs — every probe looks identical to the miner, so attackers
can't even tell probes apart, let alone precompute them. Ground truth is
computed by plain Python at generation time: no reference model needed,
nothing to steal.

Seed lifecycle: epoch seed hash committed publicly BEFORE the epoch
(routing.commit_eval_set pattern), seed revealed AFTER for audit.
Spot-checks sample k random tasks live; memorization dies at rotation.
"""
import hashlib
import multiprocessing as mp

_CTX = mp.get_context("fork")

KINDS = ("add", "fib", "reverse", "sortnums", "vowels")
TIERS = (1, 2, 3)

# Broader than eval.check (needs sorted/sum), still no imports.
CANARY_BUILTINS = {"range": range, "len": len, "sorted": sorted,
                   "sum": sum, "str": str, "list": list, "abs": abs}


def _rng(seed) -> "random.Random":
    import random
    h = hashlib.sha256(f"canary|{seed}".encode()).hexdigest()
    return random.Random(int(h, 16) % 2**32)


def _rand_str(rng, n):
    import string
    return "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(n))


def gen_tasks(seed, n_per_tier=6) -> list[dict]:
    """Fresh task set from seed. Each task: {id, kind, prompt, tests, tier}.
    Tests are SECRET (never shown to the miner)."""
    rng = _rng(seed)
    tasks, tid = [], 0
    for tier in TIERS:
        for _ in range(n_per_tier):
            kind = rng.choice(KINDS)
            if kind == "add":
                lo, hi = (0, 20) if tier == 1 else (-10**6, 10**6) if tier == 2 else (-10**18, 10**18)
                a, b = rng.randint(lo, hi), rng.randint(lo, hi)
                tests = [((a, b), a + b)]
            elif kind == "fib":
                n = rng.randint(0, 8) if tier == 1 else rng.randint(9, 20) if tier == 2 else rng.randint(21, 30)
                want = _fib(n)
                tests = [((n,), want)]
            elif kind == "reverse":
                s = _rand_str(rng, rng.randint(1, 5) if tier == 1 else rng.randint(6, 20) if tier == 2 else rng.randint(21, 60))
                tests = [((s,), s[::-1])]
            elif kind == "sortnums":
                m = rng.randint(2, 5) if tier == 1 else rng.randint(6, 15) if tier == 2 else rng.randint(16, 50)
                xs = [rng.randint(-1000, 1000) for _ in range(m)]
                tests = [((xs,), sorted(xs))]
            else:  # vowels
                s = _rand_str(rng, rng.randint(1, 10) if tier == 1 else rng.randint(11, 30) if tier == 2 else rng.randint(31, 80))
                tests = [((s,), sum(1 for ch in s if ch in "aeiouAEIOU"))]
            tasks.append({"id": f"{seed}:{tid}", "kind": kind, "prompt": kind,
                          "tests": tests, "tier": tier})
            tid += 1
    return tasks


def _fib(n):
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a


def _run(q, code, args):
    try:
        ns = {"__builtins__": CANARY_BUILTINS}
        exec(code, ns)
        q.put(ns["solve"](*args))
    except Exception as e:
        q.put(e)


def run_tests(code: str, tests, timeout=2.0) -> bool:
    for args, want in tests:
        q = _CTX.Queue()
        p = _CTX.Process(target=_run, args=(q, code, args))
        p.start()
        p.join(timeout)
        if p.is_alive():
            p.terminate()
            return False
        got = q.get() if not q.empty() else None
        if isinstance(got, Exception) or got != want:
            return False
    return True


def epoch_commit(seed: str) -> str:
    return hashlib.sha256(f"epoch-canary|{seed}".encode()).hexdigest()[:16]
