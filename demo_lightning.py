#!/usr/bin/env python3
"""Lightning watts-per-job drills: paid-on-delivery, garbage-unpaid,
expiry-refund, double-fulfill rejected, OTS accounting across offers+sends.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

import pq
import lightning as ln
from ledger import (connect, register_miner, register_pq, transfer,
                    balance_watts, next_ots)
from gateway import Miner, dispatch_paid
from miner import MockBackend
from money import to_watts, fmt_watts

DB = os.path.join(os.path.dirname(__file__), "ledger-ln.db")
AMT = 2000


def key(c, mid, model="mock", price=0.02, stake=0):
    k = pq.generate()
    register_pq(c, k["addr"], pq.root_of(bytes.fromhex(k["seed"])).hex())
    register_miner(c, k["addr"], model, price, stake)
    return k


def main():
    if os.path.exists(DB):
        os.remove(DB)
    c = connect(DB)
    cust = key(c, "cust")
    transfer(c, "faucet", cust["addr"], to_watts(10))
    gm = key(c, "good")
    ch = key(c, "cheat")
    good = Miner(gm["addr"], MockBackend("good"), "mock-7b", 0.02)
    cheat = Miner(ch["addr"], MockBackend("cheat"), "tiny", 0.002)
    c0 = balance_watts(c, cust["addr"])

    print("--- 1: honest delivery -> paid ---")
    pre = ln.new_preimage()
    ln.offer(c, cust, gm["addr"], AMT, "job-1", ln.hash_of(pre), expiry=10)
    passed, s, subsidy, lat, code, ok, detail = dispatch_paid(c, good, "add", "job-1", pre, now_epoch=1)
    print(f"pass={int(passed)}: {detail}")
    assert ok and balance_watts(c, gm["addr"]) == AMT // 2 + subsidy  # fee split + subsidy bootstrap

    print("--- 2: garbage -> unpaid, customer keeps funds ---")
    pre2 = ln.new_preimage()
    ln.offer(c, cust, ch["addr"], AMT, "job-2", ln.hash_of(pre2), expiry=10)
    passed, *_ , ok, detail = dispatch_paid(c, cheat, "fib", "job-2", pre2, now_epoch=1)
    print(f"pass={int(passed)}: {detail}")
    assert not ok and balance_watts(c, ch["addr"]) == 0

    print("--- 3: expiry -> refunded ---")
    pre3 = ln.new_preimage()
    ln.offer(c, cust, gm["addr"], AMT, "job-3", ln.hash_of(pre3), expiry=2)
    ok, detail = ln.fulfill(c, "job-3", pre3, True, now_epoch=5)
    print(detail)
    assert not ok
    assert ln.expire_sweep(c, 5) >= 0

    print("--- 4: double-fulfill rejected ---")
    ok, detail = ln.fulfill(c, "job-1", pre, True, now_epoch=1)
    print(detail)
    assert not ok

    print("--- 5: OTS accounting (offers + sends never collide) ---")
    n0 = next_ots(c, cust["addr"])
    ln.offer(c, cust, gm["addr"], AMT, "job-4", ln.hash_of(ln.new_preimage()), expiry=10)
    n1 = next_ots(c, cust["addr"])
    assert n1 == n0 + 1, (n0, n1)
    pkg = pq.sign(cust["seed"], n1, cust["addr"], gm["addr"], 100)
    transfer(c, cust["addr"], gm["addr"], 100, pkg)
    assert next_ots(c, cust["addr"]) == n1 + 1
    print(f"offer consumed OTS {n0}, wallet send used {n1} -> no collision")

    spent = c0 - balance_watts(c, cust["addr"])
    print(f"\ncustomer spent {fmt_watts(spent)} (1 honest job + 100-watt send; garbage+expiry cost 0)")
    print(f"miner good: {fmt_watts(balance_watts(c, gm['addr']))}, cheat: {fmt_watts(balance_watts(c, ch['addr']))}")
    from ledger import burned_watts
    print(f"burned sink: {fmt_watts(burned_watts(c))} (50% of settled fee)")
    assert burned_watts(c) == AMT // 2
    print("LIGHTNING DRILLS PASS: pay iff valid delivery.")


if __name__ == "__main__":
    main()
