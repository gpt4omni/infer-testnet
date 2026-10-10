#!/usr/bin/env python3
"""Gateway: route customer + hidden eval jobs, measure, score, pay, log."""
from miner import sha
from eval import TASKS, check, score_job
from ledger import log_job


class Miner:
    def __init__(self, mid, backend, model, price_per_1k):
        self.mid, self.backend, self.model = mid, backend, model
        self.price = price_per_1k


def dispatch(conn, miner: Miner, task_name: str, is_eval: bool):
    task = TASKS[task_name]
    code, lat = miner.backend.generate(task["prompt"])
    passed = check(code, task["tests"])
    tokens = max(1, len(code) // 4)
    s, pay = score_job(passed, tokens, miner.price, lat)
    # customers don't pay for failed work in v0; evals never pay customer price
    if not is_eval and not passed:
        pay = 0
    log_job(conn, miner.mid, sha(task["prompt"]), sha(code), tokens, lat,
            is_eval, passed, s, pay)
    return passed, s, pay, lat, code


def dispatch_paid(conn, miner: Miner, task_name: str, htlc_job_id: str,
                  preimage: bytes, now_epoch: int):
    """Customer job settled through its HTLC: serve, quality-gate, release.
    Subsidy accounting (score/pay log) still recorded; the CUSTOMER's watts
    move only via lightning.fulfill — valid output or no payment."""
    import lightning as _ln
    passed, s, pay, lat, code = dispatch(conn, miner, task_name, False)
    ok, detail = _ln.fulfill(conn, htlc_job_id, preimage, passed, now_epoch)
    return passed, s, pay, lat, code, ok, detail
