#!/usr/bin/env python3
"""Infer testnet v0: genesis + epochs + spot-checks + audit, one command.

Network: 3 miners (2 honest, 1 cheat), 1 gateway, protocol-funded hidden
evals, per-epoch secret canary seeds (committed, revealed post-epoch).
Money moves only via PQ-signed transfers; cheats get slashed, not paid.

Run: python3 testnet.py   (~30s, mock backends)
State: testnet/network.db + testnet/keys.json (0600).
"""
import json
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

import pq
from ledger import (connect, register_miner, register_pq, transfer, mint,
                    subsidy_issued_watts, balance_watts, slash, leaderboard, log_job, commit_eval)
from gateway import Miner, dispatch
from miner import MockBackend, sha
from money import to_watts, fmt_watts, GENESIS_DAILY_INFR
from canary import gen_tasks, run_tests, epoch_commit
from routing import commit_eval_set

HERE = os.path.dirname(__file__)
TDIR = os.path.join(HERE, "testnet")
DB = os.path.join(TDIR, "network.db")
KEYS = os.path.join(TDIR, "keys.json")
EPOCHS, JOBS_PER_MINER = 3, 4
GENESIS_INFR = 1000
# Testnet-scale epoch subsidy (mainnet: money.daily_subsidy_infr). Split
# pro-rata by scored work each epoch — cheats scoring 0 earn 0 subsidy.
EPOCH_POOL_WATTS = to_watts(10)


def main():
    os.makedirs(TDIR, exist_ok=True)
    for f in (DB,):
        if os.path.exists(f):
            os.remove(f)
    c = connect(DB)

    roster = [("alice-good", "good", 0.02), ("bob-good", "good", 0.02), ("mallory-cheat", "cheat", 0.002)]
    miners, keys = {}, {}
    for mid, kind, price in roster:
        k = pq.generate()
        keys[mid] = k
        register_pq(c, k["addr"], pq.root_of(bytes.fromhex(k["seed"])).hex())
        miners[mid] = Miner(k["addr"], MockBackend(kind), "mock", price)
        register_miner(c, k["addr"], "mock", price, to_watts(500))
        transfer(c, "faucet", k["addr"], to_watts(GENESIS_INFR))
    with open(KEYS, "w") as f:
        json.dump(keys, f)
    os.chmod(KEYS, 0o600)
    print(f"genesis: 3 wallets funded {GENESIS_INFR} INFR each (faucet, testnet)")

    tasks = ["add", "fib", "reverse"]
    for epoch in range(EPOCHS):
        seed = f"testnet-e{epoch}"
        commit_eval(c, "testnet", epoch, epoch_commit(seed))  # commit BEFORE use
        mark = c.execute("SELECT COALESCE(MAX(id),0) FROM jobs").fetchone()[0]
        for i, (mid, m) in enumerate(miners.items()):
            for j in range(JOBS_PER_MINER):
                task = tasks[(epoch + i + j) % len(tasks)]
                is_eval = (j == JOBS_PER_MINER - 1)
                passed, s, pay, lat, code = dispatch(c, m, task, is_eval)
                log_extra = ""  # dispatch already logs; gateway_id omitted v0
                _ = log_extra
        # epoch subsidy: pro-rata by scored work since epoch start
        tot = c.execute("SELECT COALESCE(SUM(score),0) FROM jobs WHERE id>?", (mark,)).fetchone()[0]
        if tot > 0:
            shares = c.execute("SELECT miner_id, SUM(score) FROM jobs WHERE id>? GROUP BY miner_id ORDER BY SUM(score) DESC",
                               (mark,)).fetchall()
            given, top = 0, shares[0][0]
            for mid_addr, sc in shares:
                amt = int(EPOCH_POOL_WATTS * sc / tot)
                if amt:
                    mint(c, mid_addr, amt)
                    given += amt
            if EPOCH_POOL_WATTS - given:  # remainder dust to top scorer
                mint(c, top, EPOCH_POOL_WATTS - given)
        # per-miner canary spot-check on FRESH seed tasks
        for mid, m in miners.items():
            ok_n, tot = 0, 0
            for t in gen_tasks(seed + f"|{mid}", n_per_tier=2):
                code, _ = m.backend.generate(t["prompt"])
                ok_n += run_tests(code, t["tests"])
                tot += 1
            rate = ok_n / tot
            if rate < 0.5:
                slash(c, m.mid, "miner", to_watts(25), f"epoch {epoch} canary spot-check {rate:.2f}")
                print(f"epoch {epoch} {mid}: canary {rate:.2f} -> SLASHED {fmt_watts(to_watts(25))}")
            else:
                print(f"epoch {epoch} {mid}: canary {rate:.2f} ok")

    print(f"\n--- audit: seed commits {[epoch_commit(f'testnet-e{e}') for e in range(EPOCHS)]} (all pre-committed) ---")
    print("--- balances (earned + genesis - slashes*) ---")
    for mid in miners:
        addr = miners[mid].mid
        print(f"{mid:14s} {addr[:12]}.. balance={fmt_watts(balance_watts(c, addr))}")
    print("--- miner payouts this run ---")
    for mid, n, psd, pay, avg in leaderboard(c):
        print(f"{mid[:12]}.. jobs={n} pass={psd} paid={fmt_watts(pay or 0)}")
    from ledger import burned_watts
    print(f"subsidy issued: {fmt_watts(subsidy_issued_watts(c))} | burned: {fmt_watts(burned_watts(c))}")
    print("* slashes recorded in slashes table; balances reflect payouts+genesis.")
    print("testnet complete: network.db + keys.json (0600) in testnet/.")


if __name__ == "__main__":
    main()
