#!/usr/bin/env python3
"""Attack drills: substitution, copycat, garbage+challenge, replay.
Every attack must be CAUGHT. If one isn't, the layer is broken."""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

from coincurve import PrivateKey

from ledger import connect, register_miner
from miner import MockBackend
from money import to_watts, fmt_watts
from verify import (CommitStore, commit, redundant_check, FingerprintDB,
                    sign_output, verify_output, open_challenge)

DB = os.path.join(os.path.dirname(__file__), "ledger-verify.db")


def main():
    if os.path.exists(DB):
        os.remove(DB)
    c = connect(DB)
    register_miner(c, "good-1080ti", "qwen2.5-7b-Q4", 0.02, to_watts(500))
    register_miner(c, "mallory", "qwen2.5-7b-Q4", 0.02, to_watts(500))  # claims good model
    good, cheat = MockBackend("good"), MockBackend("cheat")

    print("--- A: model substitution (claims 7B, serves tiny) ---")
    fpdb = FingerprintDB()
    fpdb.register("mallory", good)  # registered honestly...
    ok, frac = fpdb.spot_check("mallory", cheat)  # ...then swapped backends
    print(f"fingerprint match {frac:.2f} -> {'PASS (BAD!)' if ok else 'CAUGHT: model swapped'}")
    assert not ok

    print("--- B: copycat (reveals honest output without committing) ---")
    cs = CommitStore()
    honest_out, _ = good.generate("add")
    cs.submit_commit("good-1080ti", "job-1", commit("good-1080ti", "job-1", honest_out))
    ok, why = cs.reveal("mallory", "job-1", honest_out)  # saw it, tries to claim it
    print(f"copycat reveal: {why} -> {'PASS (BAD!)' if ok else 'CAUGHT'}")
    assert not ok
    ok, why = cs.reveal("good-1080ti", "job-1", honest_out)
    print(f"honest reveal: {why} -> {'OK' if ok else 'BROKEN'}")
    assert ok

    print("--- C: garbage + open challenge ---")
    garbage, _ = cheat.generate("fib")
    caught, detail = open_challenge(c, "watcher-1", "mallory", "fib", garbage,
                                    good, to_watts(25))
    print(detail)
    assert caught
    row = c.execute("SELECT entity,amount_watts FROM slashes").fetchone()
    print(f"slash recorded: {row[0]} {fmt_watts(row[1])}")

    print("--- D: cross-job replay (signed output reused on new job) ---")
    sk = PrivateKey()
    sig = sign_output(sk, "job-1", honest_out)
    ok_old = verify_output(sk.public_key.format(compressed=True), sig, "job-1", honest_out)
    ok_new = verify_output(sk.public_key.format(compressed=True), sig, "job-2", honest_out)
    print(f"original job verifies: {ok_old} (must be True); replayed job verifies: {ok_new} (must be False)")
    assert ok_old and not ok_new

    print("--- E: redundancy dispute (good vs cheat on fib) ---")
    go, _ = good.generate("fib")
    ch, _ = cheat.generate("fib")
    winner, reason = redundant_check("fib", go, ch)
    print(f"winner={winner}: {reason}")
    assert winner == "A"

    print("\nALL ATTACKS CAUGHT. Layers hold (mock backends; §limits in verify.py).")


if __name__ == "__main__":
    main()
