#!/usr/bin/env python3
"""Coding eval: run solve() from generated code against hidden tests.
Sandboxed-ish: exec with timeout via multiprocessing, no imports allowed
except builtins. Pass/fail is objective — no LLM-judges-LLM."""
import multiprocessing as mp
_CTX = mp.get_context("fork")

TASKS = {
    "add": {"prompt": "add",
            "tests": [((1, 2), 3), ((0, 0), 0), ((-5, 5), 0)]},
    "fib": {"prompt": "fib",
            "tests": [((0,), 0), ((1,), 1), ((10,), 55)]},
    "reverse": {"prompt": "reverse",
                "tests": [(("abc",), "cba"), (("",), "")]},
}


def _run(q, code, args):
    try:
        ns = {"__builtins__": {"range": range, "len": len}}
        exec(code, ns)
        q.put(ns["solve"](*args))
    except Exception as e:
        q.put(e)


def check(code: str, tests, timeout=2.0) -> bool:
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


def score_job(passed: bool, tokens: int, price_per_1k: float,
              latency_ms: float, latency_slo_ms: float = 5000.0) -> tuple[float, int]:
    """Quality-gated payout in watts (1 INFR = 1e6 watts, see money.py).
    price_per_1k is INFR per 1k tokens. Fail => 0."""
    if not passed:
        return 0.0, 0
    lat_pen = 1.0 if latency_ms <= latency_slo_ms else latency_slo_ms / latency_ms
    score = lat_pen / max(price_per_1k, 1e-9)
    payout = int(tokens * price_per_1k * 1000 * lat_pen)  # == tokens/1k * price * 1e6
    return score, payout
