#!/usr/bin/env python3
"""Deterministic routing + evaluator rotation. No single router or scorer.

Rule (public, auditable): weight = quality_ema * stake / price.
Winner = highest weight, ties broken by H(job_id | miner_id).
Anyone replays the ledger and checks each route.
Evaluators: commit hash(eval_set) before epoch, assigned per job by
H(epoch | job_seq) % evaluators. Reveal after epoch for audit.
"""
import hashlib
import json


def h(*parts) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()


def select_miner(job_id: str, miners: list[dict]) -> tuple[str, str]:
    """miners: [{id, ema (0..1), stake, price}]. Returns (winner, rule_hash)."""
    scored = []
    for m in miners:
        w = (m["ema"] * max(1, m["stake"])) / max(m["price"], 1e-9)
        scored.append((w, h(job_id, m["id"]), m["id"]))
    scored.sort(reverse=True)
    rule = h(json.dumps({"job": job_id, "order": scored}, sort_keys=True))
    return scored[0][2], rule


def assign_evaluator(epoch: int, job_seq: int, evaluators: list[str]) -> str:
    return evaluators[int(h(epoch, job_seq), 16) % len(evaluators)]


def commit_eval_set(eval_set: dict) -> str:
    return h(json.dumps(eval_set, sort_keys=True))
