#!/usr/bin/env python3
"""Infer money demo: emission schedule, cap check, fee split, samples."""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))
from money import (COIN_NAME, TICKER, MICRO_NAME, WATTS_PER_INFR,
                   SUBSIDY_CAP_INFR, GENESIS_DAILY_INFR, HALVING_YEARS,
                   BOOTSTRAP_YEARS, TAIL_DAILY_INFR, FEE_BURN_FRAC,
                   daily_subsidy_infr, subsidy_total_infr, tail_yearly_pct,
                   split_customer_fee, to_watts, to_infr, fmt_watts)


def main():
    print(f"{COIN_NAME} ({TICKER}) — 1 {TICKER} = {WATTS_PER_INFR:,} {MICRO_NAME}s\n")
    print("--- subsidy schedule ---")
    for era in range(3):
        d = GENESIS_DAILY_INFR / 2 ** era
        tot = d * 365.25 * HALVING_YEARS
        print(f"era{era} years {era*4}-{era*4+4}: {d:>9,.0f} {TICKER}/day -> ~{tot:,.1f} {TICKER}")
    total = subsidy_total_infr()
    print(f"total subsidy: ~{total:,.0f} {TICKER} (cap {SUBSIDY_CAP_INFR:,}; "
          f"{'OK' if total <= SUBSIDY_CAP_INFR else 'OVER CAP'})")
    print(f"tail after year {BOOTSTRAP_YEARS}: {TAIL_DAILY_INFR} {TICKER}/day "
          f"(~{tail_yearly_pct():.2f}%/yr, declining in %)")
    print(f"fee split: {(1-FEE_BURN_FRAC)*100:.0f}% miner / {FEE_BURN_FRAC*100:.0f}% burned")

    print("\n--- samples ---")
    assert to_watts(1.5) == 1_500_000 and abs(to_infr(2500) - 0.0025) < 1e-12
    m, b = split_customer_fee(to_watts(0.04))
    print(f"customer pays 0.040 {TICKER}: miner {fmt_watts(m)}, burned {fmt_watts(b)}")
    print(f"typical code job (8 toks @ 0.02 {TICKER}/1k): {fmt_watts(int(8*0.02*1000))}")
    print(f"1080 Ti stake: {fmt_watts(to_watts(500))}")
    print("\nmoney math OK")


if __name__ == "__main__":
    main()
