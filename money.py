#!/usr/bin/env python3
"""Infer money: denominations + emission schedule.

Coin:   Infer (INFR) — pays for useful inference, not hashes.
Micro:  watt — 1 INFR = 1,000,000 watts. Inference costs energy;
        pricing work in watts keeps cost-per-useful-result legible
        (and stops calling everything sats).

Supply (protocol constants, v0):
  SUBSIDY_CAP  40M INFR over a 12-year bootstrap, 4-year halvings:
    era0  15,500 INFR/day  -> ~22.65M
    era1   7,750 INFR/day  -> ~11.32M
    era2   3,875 INFR/day  -> ~5.66M   (total ~39.6M ~= cap)
  TAIL  720 INFR/day forever after (~0.66%/yr, declining in %).
    Pays evaluators/gateways when subsidies end. Flat fees alone
    don't fund public goods (scoring, audits) — Monero lesson.
  BURN  50% of customer fees destroyed. Links INFR value to real
    paid demand; pure emissions without demand just tax holders.

Why not 21M: identity (not a BTC clone) + a longer bootstrap —
an inference market needs years of subsidized supply before fee
revenue can carry small operators.
"""
from __future__ import annotations

COIN_NAME = "Infer"
TICKER = "INFR"
MICRO_NAME = "watt"
WATTS_PER_INFR = 1_000_000

SUBSIDY_CAP_INFR = 40_000_000
GENESIS_DAILY_INFR = 15_500
HALVING_YEARS = 4
BOOTSTRAP_YEARS = 12
DAYS_PER_YEAR = 365.25
TAIL_DAILY_INFR = 720
FEE_BURN_FRAC = 0.5


def to_watts(infr: float) -> int:
    return int(round(infr * WATTS_PER_INFR))


def to_infr(watts: int) -> float:
    return watts / WATTS_PER_INFR


def fmt_watts(watts: int) -> str:
    if watts >= WATTS_PER_INFR:
        return f"{to_infr(watts):,.3f} {TICKER}"
    return f"{watts:,} {MICRO_NAME}s"


def era_of_day(day: int) -> int:
    return int(day // (DAYS_PER_YEAR * HALVING_YEARS))


def daily_subsidy_infr(day: int) -> float:
    """Subsidy on day since genesis. 0 once the cap is hit (tail is separate)."""
    if day < 0:
        raise ValueError("negative day")
    if day >= BOOTSTRAP_YEARS * DAYS_PER_YEAR:
        return 0.0
    return GENESIS_DAILY_INFR / (2 ** era_of_day(day))


def subsidy_total_infr() -> float:
    total, day = 0.0, 0
    end = int(BOOTSTRAP_YEARS * DAYS_PER_YEAR)
    while day < end:
        total += daily_subsidy_infr(day)
        day += 1
    return total


def tail_yearly_pct() -> float:
    return 100.0 * TAIL_DAILY_INFR * DAYS_PER_YEAR / SUBSIDY_CAP_INFR


def split_customer_fee(fee_watts: int) -> tuple[int, int]:
    """(miner_watts, burned_watts)."""
    burned = int(fee_watts * FEE_BURN_FRAC)
    return fee_watts - burned, burned
