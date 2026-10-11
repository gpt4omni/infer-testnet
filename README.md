# Infer testnet

Distributed AI inference market. Miners serve open-weight models, customers
pay per job, everything settles in PoW blocks. Money is post-quantum
(hash-based signatures, no ECDSA anywhere near funds).

Status: testnet. Funds are worthless, difficulty is trivial, history may
reset. Do not put real money or real secrets in it.

## Run a node

Gateway (mines blocks, serves API + faucet + explorer):

    python3 gateway_node.py --host 0.0.0.0 --port 19191 --db gateway.db --pq \
      --mine --reward-addr <your-pq1-address>

Miner (serves jobs; add `--mine` to also produce blocks):

    python3 miner_node.py --gateway HOST:PORT --pin KEM_PIN --keyfile mine.json --jobs 0

Desktop wallet (needs Tk): `./wallet.sh`. Web wallet + faucet + explorer
are served by the gateway itself: `/wallet`, `/`, `/blocks`.

Second operator: see [ONBOARDING.md](ONBOARDING.md) for independent KEM pins,
`--seed HOST:PORT@PIN`, firewall steps, standby sync, and the two-host
convergence command. `python demo_onboarding.py` runs its local process proof.

## Money

INFR, ticker INFR. 1 INFR = 1,000,000 watts. 40M subsidy cap over a
12-year bootstrap with halvings, then a small permanent tail; 50% of job
fees burn. See `money.py` — the schedule is code, not a promise.

## How jobs pay

Customer signs hash-locked terms per job. Gateway checks the output
(unit tests for code tasks). Valid → watts move. Garbage or timeout →
nothing moves. See `lightning.py`, `eval.py`.

## Security, honestly

- Verification is deterrence, not proof: commit-reveal, redundant
  execution, secret rotating canaries, capability profiles, stake + slash.
  See `verify.py`, `demo_verify_hard.py`.
- Chain: sha256d PoW, 6-deep reorg cap, median-time rules, operator
  checkpoints (`checkpoint_tool.py`). Shallow reorgs are still cheap —
  treat recent confirmations as pending.
- No security audit has happened. Assume bugs.

## Layout

`chain.py` blocks/consensus · `pq.py` post-quantum sigs · `ledger.py`
state index · `net.py` P2P + HTTP site · `gateway_node.py`/`miner_node.py`
runners · `wallet.py` desktop wallet · `demo_*.py` runnable proofs for
every claim above — run them before believing anything.
