#!/usr/bin/env python3
"""Chain-security drills: deep reorg refused, MTP enforced, checkpoint holds.
Attacker has MORE hashpower than the honest chain in every drill."""
import os
import sys
import tempfile
import time
sys.path.insert(0, os.path.dirname(__file__))

import chain as C
import pq

BITS = 2 ** 242


def blank(prev, h, ts):
    return C.mine_block(prev, h, BITS, [], timestamp=ts)


def main():
    base = time.time()
    # honest chain: 8 blocks
    ch = C.Chain()
    g = C.genesis([], BITS)
    assert ch.add(g)[0]
    tip = C.header_hash(g["header"])
    for h in range(1, 8):
        b = blank(tip, h, base + h * 30)
        assert ch.add(b)[0], f"honest {h}"
        tip = C.header_hash(b["header"])
    print(f"honest tip: height={ch.height}")

    # 1. heavier deep fork (11 blocks from genesis) must NOT win (depth 7 > 6)
    fprev = C.header_hash(g["header"])
    fbs = []
    for h in range(1, 12):
        b = blank(fprev, h, base + 1000 + h)
        fbs.append(b)
        fprev = C.header_hash(b["header"])
    for b in fbs:
        ok, msg = ch.add(b)
        assert ok  # stored (valid PoW), ...
    assert ch.height == 7, f"deep reorg took over! height={ch.height}"
    print("drill 1 CAUGHT: heavier 11-block deep fork stored, tip unmoved")

    # 2. backdated block: regression fires first, MTP stands behind it
    bad = C.mine_block(tip, 8, BITS, [], timestamp=base)  # far below median
    ok, msg = ch.add(bad)
    print(f"drill 2: backdated block -> {'CAUGHT' if not ok else 'ACCEPTED (BAD!)'} ({msg})")
    assert not ok

    # 3. checkpoint: pin height 5, heavier fork below it refused
    import subprocess
    import json as _j
    k = pq.generate()
    cp_hash = C.header_hash(ch.path_to(ch.tip)[5]["header"])
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as keyfile:
        _j.dump(k, keyfile)
        keyfile.flush()
        r = subprocess.run([sys.executable, "checkpoint_tool.py", keyfile.name, "5", cp_hash],
                           capture_output=True, text=True, cwd=os.path.dirname(__file__))
    assert r.returncode == 0, r.stderr
    doc = _j.loads(r.stdout)
    ch2 = C.Chain()
    for b in ch.path_to(ch.tip):
        assert ch2.add(b)[0]
    # verify tool output against pinned root, then enforce
    assert pq.verify(doc["addr"], doc["root"], doc["sig"], f"checkpoint|5|{cp_hash}", 5)
    assert doc["root"] == pq.root_of(bytes.fromhex(k["seed"])).hex()
    ch2.set_checkpoint(5, cp_hash)
    ok, msg = ch2.verify()
    assert ok, msg
    # attacker fork from block 2, heavier than main
    fprev = C.header_hash(ch2.path_to(ch2.tip)[2]["header"])
    fl = []
    for h in range(3, 14):
        b = blank(fprev, h, base + 2000 + h)
        fl.append(b)
        fprev = C.header_hash(b["header"])
    for b in fl:
        ch2.add(b)
    assert ch2.height == 7, f"checkpoint bypassed! height={ch2.height}"
    print("drill 3 CAUGHT: heavier fork below checkpoint refused, history pinned")

    # 4. tampered checkpoint sig rejected by loader path
    evil = dict(doc, hash="00" * 32)
    assert not pq.verify(evil["addr"], evil["root"], evil["sig"], "checkpoint|5|" + "00" * 32, 5)
    print("drill 4 CAUGHT: forged checkpoint fails PQ verify")
    cold = C.Chain()
    cold.set_checkpoint(5, cp_hash)
    for b in ch.path_to(ch.tip):
        assert cold.add(b)[0]
    assert cold.tip == ch.tip and cold.verify()[0], "checkpoint stopped valid cold sync"
    print("drill 5 PASS: checkpoint installed before cold sync permits the pinned history")
    print("SECURITY DRILLS PASS.")


if __name__ == "__main__":
    main()
