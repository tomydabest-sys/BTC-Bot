"""Generate synthetic trading data into bot.db for dashboard testing.

PATCHED: now uses Storage.save_order so the schema stays in lockstep
with the rest of the bot. Previously inserted directly with raw sqlite3,
which would silently corrupt the dashboard if Storage's schema evolved.

Usage:
    python scripts/synthetic_load.py --count 100 --db data/bot.db
    python scripts/synthetic_load.py --count 50 --strategy overshoot_reversion
"""

from __future__ import annotations

import argparse
import random
from datetime import datetime, timedelta
from pathlib import Path

from polybot.data.models import (
    Order,
    OrderStatus,
    OrderType,
    Side,
)
from polybot.data.storage import Storage


STRATEGIES = [
    "overshoot_reversion",
    "boundary_decay",
    "dual_direction_arb",
    "maker_edge",
]

OUTCOMES = ["Yes", "No"]


def _make_order(
    *,
    rng: random.Random,
    strategy: str,
    is_winner: bool,
    minutes_ago: int,
) -> Order:
    market_id = f"synth-{rng.randint(1, 9999):04d}-0x{rng.getrandbits(40):010x}"
    token_id = f"{rng.getrandbits(64):020d}"
    side = rng.choice([Side.BUY, Side.SELL])
    price = round(rng.uniform(0.20, 0.80), 4)
    size_shares = round(rng.uniform(20, 250), 2)

    # Synthesize a fill: winners go up, losers go down
    pnl_per_share = rng.uniform(0.005, 0.04) if is_winner else -rng.uniform(0.005, 0.03)
    fill_price = price
    if side == Side.BUY:
        # SELL exit price implies fill_price stays as buy fill
        pass

    created_at = datetime.utcnow() - timedelta(minutes=minutes_ago)

    return Order(
        market_id=market_id,
        token_id=token_id,
        side=side,
        price=price,
        size=size_shares,
        order_type=OrderType.GTC,
        strategy=strategy,
        signal_id=f"synth-sig-{rng.randint(0, 999_999_999):09d}",
        order_id=f"synth-ord-{rng.randint(0, 999_999_999):09d}",
        status=OrderStatus.FILLED,
        created_at=created_at,
        filled_size=size_shares,
        avg_fill_price=fill_price,
        metadata={
            "synthetic": True,
            "synthetic_pnl_per_share": pnl_per_share,
            "is_winner": is_winner,
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Synthetic trading data generator")
    parser.add_argument("--count", type=int, default=100, help="Number of orders")
    parser.add_argument("--db", default="data/bot.db", help="Storage path")
    parser.add_argument("--strategy", default=None,
                        help="Limit to one strategy (default: random mix)")
    parser.add_argument("--win-rate", type=float, default=0.55,
                        help="Fraction of orders flagged as winners (0-1)")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed")
    parser.add_argument("--span-hours", type=int, default=24,
                        help="Spread orders across the last N hours")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    storage = Storage(db_path=args.db)

    span_minutes = max(1, args.span_hours * 60)

    n_winners = int(args.count * max(0.0, min(1.0, args.win_rate)))
    win_flags = [True] * n_winners + [False] * (args.count - n_winners)
    rng.shuffle(win_flags)

    for i, is_winner in enumerate(win_flags):
        strategy = args.strategy or rng.choice(STRATEGIES)
        minutes_ago = rng.randint(0, span_minutes)
        order = _make_order(
            rng=rng,
            strategy=strategy,
            is_winner=is_winner,
            minutes_ago=minutes_ago,
        )
        try:
            storage.save_order(order)
        except Exception as e:
            print(f"  failed at i={i}: {e}")
            continue
        if (i + 1) % 25 == 0:
            print(f"  wrote {i + 1}/{args.count}")

    storage.close()
    print(f"\n[done] wrote {args.count} orders to {args.db}")
    print(f"  winners: {n_winners}  losers: {args.count - n_winners}")


if __name__ == "__main__":
    main()
