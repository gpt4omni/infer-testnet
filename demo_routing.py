#!/usr/bin/env python3
"""Quality routing drills: high-d1 miners get paid infer work, low-d1 miners
keep task flow; newcomers on probation; coverage when nobody better is around."""
import collections
import os
import socket
import sys
import threading
import time
sys.path.insert(0, os.path.dirname(__file__))

from ledger import connect
from miner import MockBackend
from net import GatewayServer, MinerClient, send_msg, recv_msg

DB = "/tmp/opencode/route-demo.db"

POET = "Blocks rise in the air,\nSolid structures, yet flexible,\nNature's masterpiece."
JUNK = "garbage garbage garbage"


class PoetBackend(MockBackend):
    def __init__(self):
        super().__init__("good")

    def generate_raw(self, prompt, max_tokens=256):
        import time as _t
        t0 = _t.time()
        return POET, (time.time() - t0) * 1000 + 5


def rpc(port, obj):
    s = socket.create_connection(("127.0.0.1", port), timeout=15)
    with s, s.makefile("rwb") as f:
        send_msg(f, obj)
        return recv_msg(f)


def main():
    if os.path.exists(DB):
        os.remove(DB)
    srv = GatewayServer(connect(DB, check_same_thread=False), port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    import pq
    from ledger import register_pq, transfer, stake_lock, next_ots
    from money import to_watts
    # real identities with real stakes: both lock 2 INFR serving collateral
    kp, kj = pq.generate(), pq.generate()
    c = connect(DB)
    for k in (kp, kj):
        register_pq(c, k["addr"], pq.root_of(bytes.fromhex(k["seed"])).hex())
        transfer(c, "faucet", k["addr"], to_watts(10))
        n = next_ots(c, k["addr"])
        stake_lock(c, k["addr"], to_watts(2),
                   pq.sign(k["seed"], n, k["addr"], f"stake|{k['addr']}|{to_watts(2)}|{n}", to_watts(2)))
    from judge import DJudge
    j = DJudge()
    # seed rolling histories with LIVE d1 scores (not fixtures)
    srv.d1_hist[kp["addr"]] = collections.deque(
        [j.quality("write a haiku", POET) or 0.0 for _ in range(3)], maxlen=20)
    srv.d1_hist[kj["addr"]] = collections.deque(
        [j.quality("write a haiku", JUNK) or 0.0 for _ in range(3)], maxlen=20)
    pa, ja = srv.d1_avg(kp["addr"]), srv.d1_avg(kj["addr"])
    print(f"poet avg={pa:.2f} junk avg={ja:.2f}")
    assert pa is not None and pa >= 0.5 and ja is not None and ja < 0.3

    # both miners connect (infer-capable), poet stays active
    poet = MinerClient(kp["addr"], PoetBackend(), port=srv.port, model="m", price=0.02, infer=True)
    junk = MinerClient(kj["addr"], MockBackend("cheat"), port=srv.port, model="m", price=0.002, infer=True)
    rpc(srv.port, {"op": "infer_submit", "prompt": "write a haiku", "max_tokens": 64, "customer": "t"})
    time.sleep(0.3)
    # junk polls first: good miner active -> junk gets a task, not the paid job
    kind, _, _ = junk.run_mixed_once()
    print(f"junk polled with poet active -> got {kind}")
    assert kind == "task", "low-quality miner stole paid work!"
    # poet polls -> gets the infer job
    kind, jid, res = poet.run_mixed_once()
    print(f"poet polled -> got {kind} {jid} {res}")
    assert kind == "infer" and res.get("ok")
    poet.close()
    junk.close()

    # coverage: poet long gone -> junk still serves (nobody better around)
    srv.miner_seen.clear()
    rpc(srv.port, {"op": "infer_submit", "prompt": "write a haiku", "max_tokens": 64, "customer": "t"})
    time.sleep(0.3)
    junk2 = MinerClient(kj["addr"], MockBackend("cheat"), port=srv.port, model="m", price=0.002, infer=True)
    kind, _, _ = junk2.run_mixed_once()
    print(f"junk alone -> got {kind}")
    assert kind == "infer", "coverage failed!"
    junk2.close()
    print("ROUTING DEMO PASS: quality earns the premium flow.")


if __name__ == "__main__":
    main()
