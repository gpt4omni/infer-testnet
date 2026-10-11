#!/usr/bin/env python3
"""Compare optimized mining/chain work against the old algorithms, byte for byte."""
import copy
import os
import subprocess
import sys
import tempfile
import time

import chain as C


def slow_mine(prev, height, bits, txs, timestamp):
    h = {"height": height, "prev_hash": prev, "merkle_root": C.merkle_root([C.txid(t) for t in txs]),
         "timestamp": timestamp, "bits": bits, "nonce": 0}
    while int(C.header_hash(h), 16) > bits:
        h["nonce"] += 1
    return {"header": h, "txs": txs}


class LegacyWork(C.Chain):
    def work(self, tip):
        total = 0.0
        while tip:
            blk = self.blocks[tip]
            total += C.MAX_TARGET / max(1, blk["header"]["bits"])
            tip = blk["header"]["prev_hash"] if blk["header"]["height"] else ""
        return total

    def _ancestor(self, a, b):
        seen = set()
        while a:
            seen.add(a)
            blk = self.blocks[a]
            a = blk["header"]["prev_hash"] if blk["header"]["height"] else ""
        while b:
            if b in seen:
                return b
            blk = self.blocks[b]
            b = blk["header"]["prev_hash"] if blk["header"]["height"] else ""
        return ""


def main():
    cases = [("00" * 32, 0, 2 ** 242, [], 0),
             ("12" * 32, 7, 2 ** 242 - 123, [{"type": "job", "x": "nonce:0"}], 1000000000.125)]
    old_time, new_time = 0, 0
    for args in cases:
        start = time.perf_counter()
        old = slow_mine(*args)
        old_time += time.perf_counter() - start
        start = time.perf_counter()
        new = C.mine_block(*args)
        new_time += time.perf_counter() - start
        assert old == new, "optimized miner changed the header or winning nonce"
        assert C.valid_pow(new)
    print(f"same nonce/header: old={old_time:.4f}s optimized={new_time:.4f}s speedup={old_time / new_time:.2f}x")

    blocks, prev = [], "00" * 32
    for height in range(800):
        blk = C.mine_block(prev, height, C.MAX_TARGET, [], timestamp=1000000000 + height * 30)
        blocks.append(blk)
        prev = C.header_hash(blk["header"])
    timings, tips = [], []
    for cls in (LegacyWork, C.Chain):
        chain = cls()
        start = time.perf_counter()
        for blk in blocks:
            ok, msg = chain.add(blk)
            assert ok, msg
        timings.append(time.perf_counter() - start)
        tips.append(chain.tip)
    assert tips[0] == tips[1]
    assert chain.verify()[0]
    print(f"800-block validation: legacy work={timings[0]:.4f}s cached={timings[1]:.4f}s speedup={timings[0] / timings[1]:.2f}x")
    for i in (0, 1, 9, 10, 20, 799):
        tip = C.header_hash(blocks[i]["header"])
        assert chain.next_bits(tip) == C.retarget(chain.path_to(tip))
    # Fractional difficulty and ties retain the old float-summation behavior.
    ref, opt, prev = LegacyWork(), C.Chain(), "00" * 32
    for height in range(40):
        bits = C.MAX_TARGET // (3 if height % 2 else 7) - 13
        blk = C.mine_block(prev, height, bits, [], timestamp=1000000000 + height)
        assert ref.add(blk) == opt.add(blk)
        prev = C.header_hash(blk["header"])
        assert ref.work(prev) == opt.work(prev)
        assert ref.tip == opt.tip
    for value in (0, -1, True, C.MAX_TARGET + 1):
        bad = copy.deepcopy(blocks[0])
        bad["header"]["bits"] = value
        assert not C.Chain().add(bad)[0]
    bad = C.mine_block(C.header_hash(blocks[0]["header"]), 1, C.MAX_TARGET,
                       [{"type": "mint", "from": "subsidy", "to": "pq1x", "amount": 6000000},
                        {"type": "mint", "from": "subsidy", "to": "pq1x", "amount": -1000000}],
                       timestamp=1000000030)
    one = C.Chain()
    assert one.add(blocks[0])[0]
    assert not one.add(bad)[0], "negative mint bypassed subsidy cap"
    # A corrupted file must stop the runner, without overwriting its history.
    with tempfile.TemporaryDirectory(prefix="infer-corrupt-") as directory:
        path = os.path.join(directory, "chain.dat")
        corrupt = b'{"header":\n'
        with open(path, "wb") as f:
            f.write(corrupt)
        r = subprocess.run([sys.executable, "gateway_node.py", "--db", os.path.join(directory, "gateway.db"),
                            "--port", "0"], capture_output=True, text=True,
                           cwd=os.path.dirname(os.path.abspath(__file__)), timeout=10)
        assert r.returncode != 0 and "chain.dat corrupt" in r.stderr, r
        with open(path, "rb") as f:
            assert f.read() == corrupt
    print("CHAIN FAST PASS: canonical PoW unchanged, work/retarget agree, malformed blocks and corrupt startup refused.")


if __name__ == "__main__":
    main()
