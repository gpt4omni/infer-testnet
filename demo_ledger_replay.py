#!/usr/bin/env python3
"""Atomic apply/replay markers, root recovery, and concurrent OTS reservation."""
from concurrent.futures import ThreadPoolExecutor
import os
import sqlite3
import tempfile

import chain as C
import pq
from ledger import balance_watts, connect, register_miner, register_pq, transfer
from net import GatewayServer


def main():
    with tempfile.TemporaryDirectory(prefix="infer-ledger-") as directory:
        db = os.path.join(directory, "ledger.db")
        c = connect(db, check_same_thread=False)
        k = pq.generate()
        mint = {"type": "mint", "from": "faucet", "to": k["addr"], "amount": 10000}
        c.execute("CREATE TABLE applied_txid(txid TEXT PRIMARY KEY)")
        c.execute("CREATE TRIGGER fail_marker BEFORE INSERT ON applied_txid "
                  "BEGIN SELECT RAISE(FAIL, 'injected marker failure'); END")
        c.commit()
        try:
            C.apply_tx(c, mint)
            raise AssertionError("injected failure did not fire")
        except sqlite3.IntegrityError:
            pass
        assert balance_watts(c, k["addr"]) == 0, "state committed before replay marker"
        assert c.execute("SELECT COUNT(*) FROM transfers").fetchone()[0] == 0
        c.execute("DROP TRIGGER fail_marker")
        c.commit()
        assert C.apply_tx(c, mint) == "ok"
        c.close()
        c = connect(db, check_same_thread=False)
        assert C.apply_tx(c, mint) == "dup"
        assert balance_watts(c, k["addr"]) == 10000
        tx = {"type": "transfer", "from": k["addr"], "to": "pq1recipient", "amount": 500,
              "sig": pq.sign(k["seed"], 0, k["addr"], "pq1recipient", 500)}
        evil = dict(tx, amount=501)
        try:
            C.apply_tx(c, evil)
            raise AssertionError("tampered signature accepted via root recovery")
        except ValueError:
            pass
        assert c.execute("SELECT COUNT(*) FROM pq_accounts").fetchone()[0] == 0
        assert C.apply_tx(c, tx) == "ok", "unregistered signed sender did not replay"
        assert balance_watts(c, "pq1recipient") == 500
        assert C.apply_tx(c, tx) == "dup"
        # Orphaning a transaction never frees its OTS for a different message.
        c.execute("DELETE FROM transfers WHERE from_id=? AND ots=0", (k["addr"],))
        c.execute("DELETE FROM applied_txid WHERE txid=?", (C.txid(tx),))
        c.commit()
        reused = dict(tx, to="pq1different", sig=pq.sign(k["seed"], 0, k["addr"], "pq1different", 500))
        try:
            C.apply_tx(c, reused)
            raise AssertionError("orphaned OTS was freed for a different message")
        except ValueError as e:
            assert "reused" in str(e), e
        assert C.apply_tx(c, tx) == "ok", "identical authorized transfer could not replay after reorg"
        try:
            register_pq(c, k["addr"], "00" * 32)
            raise AssertionError("mismatched root overwrote sender account")
        except ValueError:
            pass
        root = c.execute("SELECT root_hex FROM pq_accounts WHERE addr=?", (k["addr"],)).fetchone()[0]
        assert root == pq.signature_root(tx["sig"])
        gateway = GatewayServer(c, port=0, chain_path=os.path.join(directory, "chain.dat"))
        try:
            queued = {"type": "transfer", "from": k["addr"], "to": "pq1recipient", "amount": 100,
                      "sig": pq.sign(k["seed"], 1, k["addr"], "pq1recipient", 100)}
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(gateway.mempool_add, [queued] * 8))
            assert sum(ok for ok, _ in results) == 1, results
            assert len(gateway.mempool) == 1
        finally:
            gateway.close()
            c.close()
        c = connect(os.path.join(directory, "fork.db"), check_same_thread=False)
        register_miner(c, "pq1miner", "mock", 0.02)
        transfer(c, "faucet", "pq1offchain", 1000)
        gateway = GatewayServer(c, port=0, chain_path=os.path.join(directory, "fork.dat"))
        try:
            g = C.mine_block("00" * 32, 0, C.MAX_TARGET, [], timestamp=1000000000)
            b1 = C.mine_block(C.header_hash(g["header"]), 1, C.MAX_TARGET,
                              [{"type": "mint", "from": "subsidy", "to": "pq1miner", "amount": 1000, "h": 1}],
                              timestamp=1000000001)
            b2 = C.mine_block(C.header_hash(b1["header"]), 2, C.MAX_TARGET,
                              [{"type": "mint", "from": "subsidy", "to": "pq1orphan", "amount": 500, "h": 2},
                               {"type": "slash", "entity": "pq1miner", "kind": "miner", "amount": 100, "reason": "orphan"}],
                              timestamp=1000000002)
            for blk in (g, b1, b2):
                assert gateway.chain.add(blk)[0]
            with gateway.lock:
                gateway.apply_tip_locked()
            assert balance_watts(c, "pq1miner") == 900
            assert balance_watts(c, "pq1orphan") == 500
            fork = C.mine_block(C.header_hash(b1["header"]), 2, C.MAX_TARGET // 2, [], timestamp=1000000003)
            assert gateway.chain.add(fork)[0]
            with gateway.lock:
                gateway.apply_tip_locked()
            assert balance_watts(c, "pq1miner") == 1000, "orphan slash survived or reward was duplicated"
            assert balance_watts(c, "pq1orphan") == 0, "orphan coinbase survived"
            assert balance_watts(c, "pq1offchain") == 1000, "out-of-chain faucet issuance lost"
        finally:
            gateway.close()
            c.close()
    print("LEDGER REPLAY PASS: atomic apply, idempotent restart, authenticated root recovery, reorg never frees OTS, one queued OTS wins.")


if __name__ == "__main__":
    main()
