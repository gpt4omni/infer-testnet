#!/usr/bin/env python3
"""Infer verification layers (v0). No single trick is unbypassable —
each layer raises attack cost above honest serving on a 1080 Ti.

L1 commit-reveal  (anti-copycat): commit output hash before seeing
   anyone else's reveal. Replayed/stolen outputs can't match a fresh
   commitment, and commitments bind job_id so old outputs don't replay.
L2 redundancy     (anti-garbage): sample of paid jobs re-executed by an
   independent miner. Mismatch -> dispute decided by ground truth
   (unit tests). Gensyn-Verde-style, minus bitwise referee: our tasks
   are objectively checkable, so the referee is cheap.
L3 fingerprint    (anti-substitution): at registration, record outputs
   on fixed canary probes (temp 0 => deterministic). Spot-checks compare
   hashes; a swapped-in tiny model fails the fingerprint. No TEE needed,
   so consumer GPUs stay viable. TEE later = bonus weight, not gate.
L4 indistinguishability (anti-eval-detection): evals ride the identical
   gateway path/payment flow (existing dispatch); eval sets committed
   up front (routing.commit_eval_set). See demo_collusion.py.
L5 stake+slash    (economics): slash >> gain, challenger earns a cut.
   Uses ledger.slash. Demand-weighted pay (paid jobs > eval-only) lives
   in gateway/eval scoring.

Known limits (don't oversell): all-dishonest redundancy panels still
lie (Verde paper limitation); subjective quality is never fully
cryptographic; fingerprints are an arms race. Deterrence, not proof.
"""
import hashlib
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))
import pq

from eval import TASKS, check
from miner import sha
from canary import gen_tasks, run_tests, epoch_commit, TIERS

CANARY_PROBES = ["add", "fib", "reverse"]  # fixed, temp-0 deterministic


def commit(miner_id: str, job_id: str, output: str) -> str:
    return sha(f"{miner_id}|{job_id}|{output}")


class CommitStore:
    """Enforces commit-before-reveal ordering per job."""

    def __init__(self):
        self.commits = {}  # (job_id, miner_id) -> hash
        self.revealed = {}  # job_id -> {miner_id: output}

    def submit_commit(self, miner_id, job_id, h):
        self.commits[(job_id, miner_id)] = h

    def reveal(self, miner_id, job_id, output) -> tuple[bool, str]:
        """A reveal is valid only with a prior matching commit. Late
        copiers who saw someone else's output can't fabricate history."""
        want = self.commits.get((job_id, miner_id))
        if want is None:
            return False, "no prior commit (copycat or replay)"
        if commit(miner_id, job_id, output) != want:
            return False, "commit mismatch (output swapped after commit)"
        self.revealed.setdefault(job_id, {})[miner_id] = output
        return True, "ok"


def redundant_check(task_name: str, out_a: str, out_b: str) -> tuple[str, str]:
    """Decide a redundancy dispute by ground truth. Returns
    (winner, reason). Both-passing divergence is fine (diversity, no fault)."""
    tests = TASKS[task_name]["tests"]
    pa, pb = check(out_a, tests), check(out_b, tests)
    if pa and not pb:
        return "A", "A passes ground truth, B fails"
    if pb and not pa:
        return "B", "B passes ground truth, A fails"
    if pa and pb:
        return "tie", "both pass (legitimate diversity)"
    return "neither", "both fail (bad task or both cheating)"


class FingerprintDB:
    """Model-identity without TEE: registered canary outputs."""

    def __init__(self):
        self.reg = {}  # miner_id -> {probe: output_hash}

    def register(self, miner_id, backend) -> dict:
        fp = {p: sha(backend.generate(TASKS[p]["prompt"])[0]) for p in CANARY_PROBES}
        self.reg[miner_id] = fp
        return fp

    def spot_check(self, miner_id, backend, threshold=1.0) -> tuple[bool, float]:
        """Serve the same probes fresh; fraction of matching hashes."""
        ref = self.reg[miner_id]
        hits = sum(sha(backend.generate(TASKS[p]["prompt"])[0]) == ref[p]
                   for p in CANARY_PROBES)
        frac = hits / len(CANARY_PROBES)
        return frac >= threshold, frac


def make_watermark_key() -> dict:
    """Hash-based output signer (Shor-proof). One OTS index per signature;
    256 per key, then rotate. Demo-grade rotation: make a new key."""
    seed = __import__("secrets").token_bytes(32)
    root = pq.root_of(seed)
    return {"seed": seed.hex(), "root": root.hex(),
            "addr": pq.address_of(root), "next": 0}


def _watermark_msg(job_id: str, output: str) -> tuple[str, int]:
    body = f"{job_id}|{output}"
    return body, max(1, len(body.encode()))


def sign_output(key: dict, job_id: str, output: str) -> dict:
    body, amt = _watermark_msg(job_id, output)
    if key["next"] >= pq.NKEYS:
        raise ValueError("watermark key exhausted — rotate")
    pkg = pq.sign(key["seed"], key["next"], key["addr"], body, amt)
    pkg["root"] = key["root"]
    pkg["addr"] = key["addr"]
    key["next"] += 1
    return pkg


def verify_output(pkg: dict, job_id: str, output: str) -> bool:
    try:
        body, amt = _watermark_msg(job_id, output)
        return pq.verify(pkg["addr"], pkg["root"], pkg, body, amt)
    except (ValueError, KeyError, TypeError):
        return False


def open_challenge(conn, accuser: str, accused_miner: str, task_name: str,
                   accused_output: str, trusted_backend, slash_amount: int,
                   challenger_cut: float = 0.25):
    """Anyone restakes a dispute: re-execute via trusted backend, decide by
    ground truth, slash liar, pay challenger a cut. Returns (cheating, detail)."""
    from ledger import slash  # local import: ledger is sibling, avoids cycle
    fresh, _ = trusted_backend.generate(TASKS[task_name]["prompt"])
    winner, reason = redundant_check(task_name, accused_output, fresh)
    if winner == "B":  # trusted output passes, accused fails
        slash(conn, accused_miner, "miner", slash_amount,
              f"challenge by {accuser}: failed ground truth on {task_name}")
        return True, f"CHEAT PROVEN ({reason}); challenger earns {challenger_cut*100:.0f}% of slash"
    return False, f"challenge failed ({reason})"


# ---------------------------------------------------------------------------
# Hard layer: secret canaries + capability profiles + latency tripwire.
# Replaces static exact-hash fingerprinting (public probes => hardcodable).
# ---------------------------------------------------------------------------

# Minimum physically plausible latency per claimed model class, ms.
# A 12B model cannot answer in 5ms; instant answers mean cached/hardcoded
# output. Floors below are illustrative (mocks); production values come
# from benchmarked tokens/sec per model class. Gateway-measured only —
# miners never self-report timing.
MODEL_LATENCY_FLOOR_MS = {
    "tiny-0.5b": 0.0,
    "qwen2.5-7b-Q4": 20.0,
    "gemma-4-12B-Q4": 1000.0,
}

# Max L1 distance between registered and spot-checked capability vectors
# before flagging substitution. One full tier collapsing (1.0->0.0 over
# 3 tiers = 0.33) always trips it; noise stays far below.
PROFILE_DISTANCE_THRESHOLD = 0.25


def latency_plausible(model: str, latency_ms: float) -> tuple[bool, str]:
    floor = MODEL_LATENCY_FLOOR_MS.get(model, 0.0)
    if latency_ms < floor:
        return False, (f"{latency_ms:.1f}ms below {model} floor {floor:.0f}ms "
                       f"— cached/hardcoded output suspected, mandatory spot-check")
    return True, "ok"


def _profile_vector(results: dict[int, list[bool]]) -> tuple[float, ...]:
    return tuple(sum(1 for x in results.get(t, [False]) if x) / max(1, len(results.get(t, [1])))
                 for t in TIERS)


class CapabilityProfile:
    """Model identity as a capability DISTRIBUTION, not exact hashes.

    Register: run calibration tasks from a seed, record per-tier pass
    rates -> vector like (1.0, 1.0, 0.9). Spot-check: fresh tasks from a
    NEW seed, compare L1 distance. A substituted tiny model keeps easy
    tiers but collapses hard tiers — exact-hash matching would miss a
    clever swap that reproduces easy probes; the profile doesn't, because
    fresh tasks can't be memorized and capability gaps show by tier.

    Live finding (GTX 1080 Ti, Gemma-12B): single-shot profiles are stable
    ONLY with complete signature prompts — an omitted signature made the
    model burn its token budget deliberating and emit empty content
    (measured drift 1.0 on honest hardware). Next rung: best-of-N majority
    vote per probe + Beta-Binomial per-tier confidence instead of a fixed
    0.25 threshold.
    """

    def __init__(self):
        self.reg = {}  # miner_id -> vector

    def register(self, miner_id, backend, seed, n_per_tier=6) -> tuple[float, ...]:
        self.reg[miner_id] = self._measure(backend, seed, n_per_tier)
        return self.reg[miner_id]

    def spot_check(self, miner_id, backend, seed, n_per_tier=6) -> tuple[bool, float, tuple]:
        vec = self._measure(backend, seed, n_per_tier)
        dist = sum(abs(a - b) for a, b in zip(vec, self.reg[miner_id]))
        return dist < PROFILE_DISTANCE_THRESHOLD, dist, vec

    @staticmethod
    def _measure(backend, seed, n_per_tier) -> tuple[float, ...]:
        results: dict[int, list[bool]] = {}
        for t in gen_tasks(seed, n_per_tier):
            code, _ = backend.generate(t["prompt"])
            results.setdefault(t["tier"], []).append(run_tests(code, t["tests"]))
        return _profile_vector(results)
