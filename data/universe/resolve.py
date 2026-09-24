"""Synchronize universe identities and Yahoo listings into PostgreSQL."""

from __future__ import annotations

import asyncio

from loguru import logger

from data.db.pool import close_pool, open_pool
from data.db.universe import sync_universe


async def resolve_universe() -> dict[str, int]:
    """Synchronize CSV universe members and provider listings into PostgreSQL."""
    pool = await open_pool()
    try:
        async with pool.connection() as conn:
            return await sync_universe(conn)
    finally:
        await close_pool(pool)


def main() -> None:
    """Run the PostgreSQL universe/listing synchronization job."""
    stats = asyncio.run(resolve_universe())
    logger.info(
        "universe sync members={} valid={} review={} unresolved={} conflict={}",
        stats["members"],
        stats["valid"],
        stats["review"],
        stats["unresolved"],
        stats["conflict"],
    )


if __name__ == "__main__":
    main()
