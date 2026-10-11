#!/usr/bin/env python3
"""WAN proof: gateway + 2 miners as SEPARATE OS processes over PQ sockets
with PQ-signed submits. Loopback here; same binaries run across LAN/WAN."""
import json
import os
import re
import subprocess
import sys
import time
sys.path.insert(0, os.path.dirname(__file__))

HERE = os.path.dirname(__file__)
D = os.path.join(HERE, "wan-demo")
PORT = 19193


def main():
    import shutil
    if os.path.exists(D):
        shutil.rmtree(D)
    os.makedirs(D)
    import pq
    keys = {}
    for name in ("m-good", "m-cheat"):
        k = pq.generate()
        keys[name] = k
        with open(os.path.join(D, f"{name}.json"), "w") as f:
            json.dump(k, f)

    gw = subprocess.Popen(
        [sys.executable, "gateway_node.py", "--host", "127.0.0.1", "--port", str(PORT),
         "--db", os.path.join(D, "gateway.db"), "--chain", os.path.join(D, "chain.dat"), "--tls",
         "--mine", "--mine-interval", "1", "--bits", hex(2 ** 248)],
        cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    pin = None
    t0 = time.time()
    buf = []
    import select
    while time.time() - t0 < 60:
        if gw.poll() is not None:  # gateway died: fail fast with its output
            buf.append(gw.stdout.read())
            break
        r, _, _ = select.select([gw.stdout], [], [], 5.0)
        if not r:
            continue  # no output yet; re-check timeout/poll (never block forever)
        line = gw.stdout.readline()
        if not line:
            time.sleep(0.2)
            continue
        buf.append(line)
        print("gw:", line.strip(), flush=True)
        m = re.search(r"pin\)?: ([0-9a-f]{64})", line)
        if m:
            pin = m.group(1)
            break
    assert pin, "gateway did not print TLS pin:\n" + "".join(buf)[-2000:]
    time.sleep(1)

    try:
        procs = []
        for name, backend in (("m-good", "mock"), ("m-cheat", "cheat")):
            p = subprocess.Popen(
                [sys.executable, "miner_node.py", "--gateway", f"127.0.0.1:{PORT}",
                 "--pin", pin, "--keyfile", os.path.join(D, f"{name}.json"),
                 "--backend", backend, "--jobs", "4",
                 "--price", "0.02" if backend == "mock" else "0.002"],
                cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            procs.append((name, p))
        for name, p in procs:
            out = p.communicate(timeout=300)[0]
            print(f"--- {name} ---\n{out}")
            assert p.returncode == 0, f"{name} failed"
        from net import gateway_rpc
        deadline = time.monotonic() + 30
        while gateway_rpc(f"127.0.0.1:{PORT}", {"op": "mempool"}, pin)["queued"]:
            assert time.monotonic() < deadline, "jobs did not settle into blocks"
            time.sleep(0.2)
    finally:
        gw.terminate()
        gw.wait(timeout=10)

    from ledger import connect, balance_watts
    from money import fmt_watts
    c = connect(os.path.join(D, "gateway.db"))
    bg = balance_watts(c, keys["m-good"]["addr"])
    bc = balance_watts(c, keys["m-cheat"]["addr"])
    print(f"gateway ledger: good={fmt_watts(bg)} cheat={fmt_watts(bc)}")
    assert bg > 0 and bc == 0
    print("WAN DEMO PASS: separate processes, ML-KEM, signed submits, block settlement.")


if __name__ == "__main__":
    main()
