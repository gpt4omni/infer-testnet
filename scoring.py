#!/usr/bin/env python3
"""Trustless jury for answer quality. No appointed judges — only bonds.

1. MEDIAN OF STRANGERS: each delivery is scored by k randomly-assigned
   STAKED scorers (deterministic draw, no one chooses). Commit-reveal so
   scorers can't copy each other. Median wins; no single taste decides pay.
2. CHALLENGE MARKET: anyone posts a bond beside a dispute. Fresh scorers
   (excluding the originals) re-run hidden probes; loser pays winner.
   The only profitable challenge is a true one.
3. ROTATING RUBRICS: probe sets committed by hash up front, revealed after
   the epoch. Hill-climbing last month's judge trains for a dead test.
4. EARNED REPUTATION: routing weight from scored-work history bound to the
   key. Can't buy it, can't transfer it; every new sockpuppet pays full
   tuition again (starts at zero, earns nothing until measured).
5. OFF-CHAIN: judgments never enter consensus. The chain settles payments
   and slashings only — outcomes anyone can verify, taste never Protocolized.
"""
import hashlib
import statistics as _st

SCORERS_PER_JOB = 3
CHALLENGE_BOND_WATTS = 500_000  # 0.5 INFR: spam costs, truth pays
CHALLENGE_TOL = 0.25  # median disagreement beyond this = someone lied
SCORER_MIN_STAKE_WATTS = 1_000_000  # same Sybil toll as serving


def _h(*parts) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()


def draw_scorers(job_id: str, scorer_ids: list[str], k: int = SCORERS_PER_JOB) -> list[str]:
    """Deterministic draw — verifiable by anyone, choosable by no one."""
    return sorted(scorer_ids, key=lambda m: _h(job_id, m))[:k]


def commit_score(job_id: str, score: float, salt: str) -> str:
    return _h(job_id, f"{score:.3f}", salt)


class Jury:
    def __init__(self):
        self.commits = {}  # (job_id, scorer) -> hash
        self.reveals = {}  # job_id -> {scorer: score}

    def commit(self, job_id, scorer, commit_hash):
        self.commits[(job_id, scorer)] = commit_hash

    def reveal(self, job_id, scorer, score: float, salt: str) -> tuple[bool, str]:
        want = self.commits.get((job_id, scorer))
        if want is None:
            return False, "no commit (late or copied)"
        if commit_score(job_id, score, salt) != want:
            return False, "commit mismatch (score changed after seeing others)"
        self.reveals.setdefault(job_id, {})[scorer] = score
        return True, "ok"

    def median(self, job_id: str):
        vals = sorted(self.reveals.get(job_id, {}).values())
        if not vals:
            return None
        return vals[len(vals) // 2]


class Rubric:
    """Rotating probe set: commit hash now, seed (hence tasks) after epoch."""

    def __init__(self):
        self.published = {}  # epoch -> commit hash
        self.revealed = {}  # epoch -> seed

    def publish(self, epoch: int, seed: str) -> str:
        from canary import epoch_commit
        self.published[epoch] = epoch_commit(seed)
        self.revealed[epoch] = seed  # held; real flow reveals post-epoch
        return self.published[epoch]

    def verify(self, epoch: int, seed: str) -> bool:
        from canary import epoch_commit
        return self.published.get(epoch) == epoch_commit(seed)

    def probes(self, epoch: int, n_per_tier: int = 4):
        from canary import gen_tasks
        seed = self.revealed.get(epoch)
        if seed is None:
            raise ValueError("rubric not yet revealed for epoch")
        return gen_tasks(seed, n_per_tier)


def reputation(conn, miner_id: str) -> dict:
    """Earned, non-transferable: bound to key, starts at zero."""
    n = conn.execute("SELECT COUNT(*), COALESCE(AVG(score),0) FROM jobs WHERE miner_id=?",
                     (miner_id,)).fetchone()
    return {"jobs": n[0], "mean_score": n[1] or 0.0}


def open_challenge(conn, challenger_key: dict, job_id: str, claim: str):
    """Lock challenger bond in a sink. Returns challenge id."""
    import time as _t
    from ledger import transfer, register_pq, next_ots
    import pq as _pq
    cid = f"challenge:{job_id}:{int(_t.time())}"
    n = next_ots(conn, challenger_key["addr"])
    cbor = _pq.sign(challenger_key["seed"], n, challenger_key["addr"], cid, CHALLENGE_BOND_WATTS)
    transfer(conn, challenger_key["addr"], cid, CHALLENGE_BOND_WATTS, cbor)
    return cid


def settle_challenge(conn, cid: str, fresh_scores: dict, original_median: float,
                     challenger: str, accused: str):
    """Fresh panel (excluding originals) rescored hidden probes. Median vs
    original beyond tolerance: accused slashed, challenger paid. Else the
    bond burns — challenges are for truth, not lottery tickets."""
    import time as _t
    from ledger import slash
    vals = sorted(fresh_scores.values())
    if not vals:
        return False, "no fresh scores"
    med = vals[len(vals) // 2]
    if abs(med - original_median) > CHALLENGE_TOL:
        slash(conn, accused, "miner", CHALLENGE_BOND_WATTS // 2, f"lost challenge {cid}")
        c = conn.execute("SELECT COALESCE(SUM(amount_watts),0) FROM transfers WHERE to_id=?", (cid,)).fetchone()[0]
        conn.execute("INSERT INTO transfers(from_id,to_id,amount_watts,ts) VALUES(?,?,?,?)",
                     (cid, challenger, c, _t.time()))
        conn.commit()
        return True, f"CHEAT PROVEN (panel {med:.2f} vs {original_median:.2f}); challenger paid {c} watts"
    c = conn.execute("SELECT COALESCE(SUM(amount_watts),0) FROM transfers WHERE to_id=?", (cid,)).fetchone()[0]
    conn.execute("INSERT INTO transfers(from_id,to_id,amount_watts,ts) VALUES(?,?,?,?)",
                 (cid, "burn", c, _t.time()))
    conn.commit()
    return False, "challenge failed — bond burned"


def route_weight(rep: dict, d1_avg) -> float:
    """Routing preference: measured quality times earned history.
    Zero history or zero quality = zero weight. Nothing to buy."""
    import math
    q = d1_avg if d1_avg is not None else 0.0
    return q * math.log1p(rep.get("jobs", 0))
