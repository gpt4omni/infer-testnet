#!/usr/bin/env python3
"""Railway reset runs once; torn tails recover; competing writers are refused."""
import glob
import json
import os
import socket
import subprocess
import sys
import tempfile
from unittest.mock import patch

import chain as C
from ledger import connect
from net import GatewayServer
from storage import append_block, chain_lock, reset_state_once

HERE = os.path.dirname(os.path.abspath(__file__))


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def write_bytes(path, data):
    with open(path, "wb") as f:
        f.write(data)


def tail_proof(directory):
    path = os.path.join(directory, "chain.dat")
    g = C.mine_block("00" * 32, 0, C.MAX_TARGET, [], timestamp=1000000000)
    b1 = C.mine_block(C.header_hash(g["header"]), 1, C.MAX_TARGET, [], timestamp=1000000001)
    b2 = C.mine_block(C.header_hash(b1["header"]), 2, C.MAX_TARGET, [], timestamp=1000000002)
    prefix = C.block_record(g) + C.block_record(b1)
    torn = prefix + C.block_record(b2)[:-17]
    write_bytes(path, torn)
    try:
        C.load_chain(path)
        raise AssertionError("strict loader accepted a torn record")
    except ValueError:
        pass
    with chain_lock(path):
        ch = C.load_chain(path, recover_tail=True)
        assert ch.tip == C.header_hash(b1["header"])
        assert read_bytes(path) == prefix
        backups = glob.glob(path + ".torn-*.bak")
        assert len(backups) == 1 and read_bytes(backups[0]) == torn
        append_block(path, b2)
    assert C.load_chain(path).height == 2
    # A complete JSON record missing only its newline must not concatenate
    # with the next block on append.
    write_bytes(path, prefix.rstrip(b"\n"))
    with chain_lock(path):
        assert C.load_chain(path, recover_tail=True).height == 1
        append_block(path, b2)
    assert C.load_chain(path).height == 2
    for bad in (b'{"header":broken}\n', C.block_record(dict(b2, txs=[{"type": "mint", "amount": 999}]))):
        original = C.block_record(g) + bad + C.block_record(b1)
        write_bytes(path, original)
        try:
            C.load_chain(path, recover_tail=True)
            raise AssertionError("interior corruption was skipped")
        except ValueError:
            pass
        assert read_bytes(path) == original
    write_bytes(path, prefix)
    real_write, calls = os.write, 0
    def failed_write(fd, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, data[:19])
        raise OSError("injected disk write failure")
    with patch("storage.os.write", side_effect=failed_write):
        try:
            append_block(path, b2)
            raise AssertionError("injected write failure was swallowed")
        except OSError:
            pass
    assert read_bytes(path) == prefix, "failed append left a partial record"
    assert C.load_chain(path).height == 1
    c = connect(os.path.join(directory, "failure.db"), check_same_thread=False)
    gateway = GatewayServer(c, port=0, chain_path=path)
    try:
        with patch("storage.append_block", side_effect=OSError("injected fsync failure")):
            try:
                gateway.persist(b2)
                raise AssertionError("persistence failure was swallowed")
            except OSError:
                pass
        assert gateway._stop.is_set()
        try:
            gateway.serve_forever()
            raise AssertionError("failed writer kept serving")
        except RuntimeError as e:
            assert "persistence failed" in str(e)
    finally:
        gateway.close()
        c.close()
    print("storage PASS: torn tail backed up/recovered, missing newline repaired, interior corruption refused, failed writer stopped")


def reset_proof(directory):
    path, db = os.path.join(directory, "reset.dat"), os.path.join(directory, "reset.db")
    original = {path: b'old corrupt chain', db: b'old database'}
    for suffix in ("-wal", "-shm", "-journal"):
        original[db + suffix] = suffix.encode()
    for name, data in original.items():
        write_bytes(name, data)
    keys = {os.path.join(directory, "operator.json"): b'keep operator key',
            os.path.join(directory, "gateway-kem.pem"): b'keep KEM key'}
    for name, data in keys.items():
        write_bytes(name, data)
    with chain_lock(path):
        marker = reset_state_once(db, path, "railway-chain-store-v2")
    assert marker and all(not os.path.exists(name) for name in original)
    doc = json.loads(read_bytes(marker))
    assert all(read_bytes(doc["files"][name]) == data for name, data in original.items())
    assert all(read_bytes(name) == data for name, data in keys.items())
    write_bytes(path, b'new chain retained')
    write_bytes(db, b'new ledger retained')
    with chain_lock(path):
        assert reset_state_once(db, path, "railway-chain-store-v2") is None
    assert read_bytes(path) == b'new chain retained' and read_bytes(db) == b'new ledger retained'
    print("reset PASS: old chain + ledger/WAL moved together, keys retained, second restart preserves new state")


def boot_proof(directory):
    from demo_onboarding import wait_for
    data = os.path.join(directory, "railway")
    os.makedirs(data)
    import pq
    key = pq.generate()
    operator_path = os.path.join(data, "operator.json")
    operator_data = json.dumps(key).encode()
    write_bytes(operator_path, operator_data)
    write_bytes(os.path.join(data, "chain.dat"), b'{"header":bad old deployment data\n')
    c = connect(os.path.join(data, "gateway.db"))
    c.execute("INSERT INTO transfers(from_id,to_id,amount_watts,ts) VALUES('subsidy',?,1000,0)", (key["addr"],))
    c.commit()
    c.close()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = dict(os.environ, DATA_DIR=data, PORT=str(port))
    log_path = os.path.join(directory, "railway.log")
    procs = []
    try:
        with open(log_path, "a") as log:
            proc = subprocess.Popen(["sh", "railway_boot.sh"], cwd=HERE, env=env, stdout=log, stderr=log)
            procs.append(proc)
            from net import gateway_rpc
            peer = f"127.0.0.1:{port}"
            def ready():
                assert proc.poll() is None, read_bytes(log_path).decode()
                return gateway_rpc(peer, {"op": "get_tip"}, timeout=2)
            wait_for(ready)
            assert gateway_rpc(peer, {"op": "balance", "addr": key["addr"]})["balance_watts"] == 0
            block = C.mine_block("00" * 32, 0, C.MAX_TARGET, [], timestamp=1000000000)
            assert gateway_rpc(peer, {"op": "submit_block", "block": block})["ok"]
            # A second runner cannot reset or write the shared volume, even on a different port.
            contender = subprocess.run([sys.executable, "gateway_node.py", "--port", "0", "--db",
                                        os.path.join(data, "gateway.db"), "--reset-state-once", "another-reset"],
                                       cwd=HERE, capture_output=True, text=True, timeout=10)
            assert contender.returncode != 0 and "already in use" in contender.stderr, contender
            proc.terminate()
            proc.wait(timeout=10)
            proc = subprocess.Popen(["sh", "railway_boot.sh"], cwd=HERE, env=env, stdout=log, stderr=log)
            procs.append(proc)
            tip = wait_for(ready)
            assert tip["tip"] == C.header_hash(block["header"])
            assert read_bytes(operator_path) == operator_data
            assert read_bytes(log_path).count(b"testnet state reset:") == 1
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)
    print("Railway boot PASS: old corrupt state reset once, operator unchanged, new tip survives restart, second writer rejected")


def main():
    with tempfile.TemporaryDirectory(prefix="infer-storage-") as directory:
        tail_proof(directory)
        reset_proof(directory)
        boot_proof(directory)
    print("STORAGE DEMO PASS.")


if __name__ == "__main__":
    main()
