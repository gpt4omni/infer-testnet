#!/usr/bin/env python3
"""Local process proof, or pinned convergence check against two real LAN hosts.

  python demo_onboarding.py
  python demo_onboarding.py --peer HOST_A:19191@PIN_A --peer HOST_B:19191@PIN_B
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time

import chain as C
from net import gateway_rpc

HERE = os.path.dirname(os.path.abspath(__file__))
BITS = C.MAX_TARGET


def split_peer(value):
    peer, pin = value.rsplit("@", 1)
    if len(pin) != 64 or any(c not in "0123456789abcdefABCDEF" for c in pin):
        raise ValueError("need HOST:PORT@KEM_PIN")
    return peer, pin.lower()


def wait_for(fn, timeout=30):
    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        try:
            result = fn()
            if result:
                return result
        except (OSError, ConnectionError) as e:
            last = e
        time.sleep(0.1)
    raise AssertionError(f"timed out: {last}")


def convergence(peers, timeout, min_height=0, addr=""):
    def same_tip():
        tips = [gateway_rpc(peer, {"op": "get_tip"}, pin) for peer, pin in peers]
        return tips[0] if (tips[0]["tip"] and tips[0]["height"] >= min_height and
                           all(t == tips[0] for t in tips)) else None
    tip = wait_for(same_tip, timeout)
    # Revalidate the common history, rather than trusting a status string.
    peer, pin = peers[0]
    blocks, h = [], tip["tip"]
    while h:
        r = gateway_rpc(peer, {"op": "get_blocks", "hash": h}, pin)
        assert r.get("ok") and r["blocks"], r
        for blk in r["blocks"]:
            assert C.header_hash(blk["header"]) == h, "wrong history hash"
            blocks.append(blk)
            h = blk["header"]["prev_hash"] if blk["header"]["height"] else ""
    chain = C.Chain()
    for blk in reversed(blocks):
        ok, msg = chain.add(blk)
        assert ok, msg
    assert chain.tip == tip["tip"]
    assert chain.verify()[0]
    assert same_tip(), "tips changed during history verification; retry with mining paused"
    if addr:
        balances = [gateway_rpc(p, {"op": "balance", "addr": addr}, k)["balance_watts"] for p, k in peers]
        assert len(set(balances)) == 1, balances
        print(f"matching balance: {balances[0]} watts")
    print(f"converged: height={tip['height']} tip={tip['tip']} ({len(blocks)} validated blocks)")
    return tip


class Node:
    def __init__(self, directory, seeds=()):
        self.directory = directory
        os.makedirs(directory)
        self.db = os.path.join(directory, "gateway.db")
        r = subprocess.run([sys.executable, "gateway_node.py", "--db", self.db, "--show-pin"],
                           cwd=HERE, capture_output=True, text=True, check=True)
        self.pin = r.stdout.strip().split()[-1]
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.peer, self.seeds = f"127.0.0.1:{self.port}", seeds
        self.proc = None

    def start(self):
        self.log = open(os.path.join(self.directory, "gateway.log"), "a+")
        args = [sys.executable, "gateway_node.py", "--host", "127.0.0.1", "--port", str(self.port),
                "--db", self.db, "--pq", "--sync-interval", "1"]
        for peer, pin in self.seeds:
            args += ["--seed", f"{peer}@{pin}"]
        self.proc = subprocess.Popen(args, cwd=HERE, stdout=self.log, stderr=subprocess.STDOUT)
        def ready():
            assert self.proc.poll() is None, "gateway exited at startup"
            return self.rpc({"op": "get_tip"})
        wait_for(ready)

    def rpc(self, obj):
        return gateway_rpc(self.peer, obj, self.pin, timeout=3)

    def stop(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(timeout=10)
            self.proc = None
            self.log.close()


def local_proof():
    import pq
    with tempfile.TemporaryDirectory(prefix="infer-onboarding-") as directory:
        a = Node(os.path.join(directory, "a"))
        b = Node(os.path.join(directory, "b"), [(a.peer, a.pin)])
        k = pq.generate()
        genesis = C.mine_block("00" * 32, 0, BITS,
                               [{"type": "mint", "from": "faucet", "to": k["addr"], "amount": 10000}],
                               timestamp=1000000000)
        blocks, prev = [genesis], C.header_hash(genesis["header"])
        for height in range(1, 121):
            blk = C.mine_block(prev, height, BITS,
                               [{"type": "mint", "from": "subsidy", "to": "pq1operator",
                                 "amount": 1000, "h": height}], timestamp=1000000000 + height)
            blocks.append(blk)
            prev = C.header_hash(blk["header"])
        with open(os.path.join(a.directory, "chain.dat"), "wb") as f:
            for blk in blocks:
                C.save_block(f, blk)
        peers = [(a.peer, a.pin), (b.peer, b.pin)]
        try:
            a.start()
            b.start()
            convergence(peers, 30, 120, "pq1operator")
            assert a.rpc({"op": "balance", "addr": "pq1operator"})["balance_watts"] == 120000
            # No account registration on B: roots travel in WOTS + Merkle proofs.
            tx = {"type": "transfer", "from": k["addr"], "to": "pq1recipient", "amount": 500,
                  "sig": pq.sign(k["seed"], 0, k["addr"], "pq1recipient", 500)}
            blk = C.mine_block(prev, 121, BITS, [tx], timestamp=1000000121)
            assert a.rpc({"op": "submit_block", "block": blk})["ok"]
            convergence(peers, 30, 121, "pq1recipient")
            assert b.rpc({"op": "balance", "addr": "pq1recipient"})["balance_watts"] == 500
            # A shorter, harder fork must be considered even at equal height.
            fork = C.mine_block(prev, 121, BITS // 2, [], timestamp=1000000122)
            assert a.rpc({"op": "submit_block", "block": fork})["ok"]
            tip = convergence(peers, 30, 121, "pq1recipient")
            assert tip["tip"] == C.header_hash(fork["header"])
            assert b.rpc({"op": "balance", "addr": "pq1recipient"})["balance_watts"] == 0
            # Reapply the same signed transfer on the winning branch after reorg.
            blk = C.mine_block(tip["tip"], 122, BITS // 2, [tx], timestamp=1000000123)
            assert a.rpc({"op": "submit_block", "block": blk})["ok"]
            convergence(peers, 30, 122, "pq1recipient")
            assert b.rpc({"op": "balance", "addr": "pq1recipient"})["balance_watts"] == 500
            try:
                gateway_rpc(a.peer, {"op": "get_tip"}, "0" * 64)
                raise AssertionError("wrong KEM pin accepted")
            except ValueError as e:
                assert "pin mismatch" in str(e), e
            # A slow/fragmented prefix must not stall other connections.
            with socket.create_connection(("127.0.0.1", a.port)) as slow:
                slow.sendall(b"P")
                start = time.monotonic()
                assert a.rpc({"op": "get_tip"})["tip"]
                assert time.monotonic() - start < 2, "slow connection blocked accept"
            size = os.path.getsize(os.path.join(a.directory, "chain.dat"))
            assert a.rpc({"op": "submit_block", "block": blk})["ok"]
            assert os.path.getsize(os.path.join(a.directory, "chain.dat")) == size
            evil = {"header": blk["header"], "txs": blk["txs"] + [{"type": "mint", "amount": 999}]}
            assert not b.rpc({"op": "submit_block", "block": evil})["ok"]
            keyfile = os.path.join(directory, "wallet-miner.json")
            with os.fdopen(os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
                json.dump(k, f)
            miner = subprocess.run([sys.executable, "miner_node.py", "--gateway", a.peer,
                                    "--pin", a.pin, "--keyfile", keyfile, "--mine", "--mine-max-blocks", "1"],
                                   cwd=HERE, capture_output=True, text=True, timeout=30)
            assert miner.returncode == 0, miner.stdout + miner.stderr
            convergence(peers, 30, 123, k["addr"])
            assert b.rpc({"op": "balance", "addr": k["addr"]})["balance_watts"] == 5009500
            for node in (a, b):
                node.stop()
                node.start()
                assert node.rpc({"op": "balance", "addr": "pq1operator"})["balance_watts"] == 120000
                assert node.rpc({"op": "balance", "addr": "pq1recipient"})["balance_watts"] == 500
            convergence(peers, 30, 123)
            assert a.pin != b.pin, "nodes shared a KEM identity"
            print("ONBOARDING PASS: separate local processes, pinned PQ sync >50 blocks, reorg, restart, tamper rejection.")
        except Exception:
            for node in (a, b):
                if os.path.exists(os.path.join(node.directory, "gateway.log")):
                    with open(os.path.join(node.directory, "gateway.log")) as f:
                        print(f.read())
            raise
        finally:
            a.stop()
            b.stop()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--peer", action="append", default=[], help="HOST:PORT@KEM_PIN; exactly two")
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--min-height", type=int, default=1)
    ap.add_argument("--balance-addr", default="")
    a = ap.parse_args()
    if a.peer:
        if len(a.peer) != 2:
            ap.error("supply exactly two --peer values")
        peers = [split_peer(p) for p in a.peer]
        assert peers[0][0] != peers[1][0], "need two different endpoints"
        convergence(peers, a.timeout, a.min_height, a.balance_addr)
        print("PEER CONVERGENCE PASS (confirm these endpoints belong to different LAN machines).")
    else:
        local_proof()


if __name__ == "__main__":
    main()
