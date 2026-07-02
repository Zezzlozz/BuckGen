"""
Quick integration test: fetch live bounties from GitHub Issues, score them.
Run from project root:  python -m tests.test_gitcoin
"""

import asyncio
import sys
from pathlib import Path

# Add project root to path so 'app' is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main():
    from app.modules.gitcoin import fetch_open_bounties, normalize_bounty
    from app.llm.scorer import score_bounty

    print("Fetching bounty-labelled GitHub issues...")
    bounties = await fetch_open_bounties(max_bounties=5)
    print(f"Got {len(bounties)} issues")

    if not bounties:
        print("No bounties returned — API may be rate-limited")
        return

    for raw in bounties[:3]:
        norm = normalize_bounty(raw)
        score = await score_bounty(norm)
        title = norm["title"][:60]
        reward = norm["reward_amount"]
        currency = norm["reward_currency"]
        level = norm["experience_level"] or "unspecified"
        repo = norm["repo"]
        print(f"  [{score:.2f}] {title}")
        print(f"       {reward} {currency} | {level} | {repo}")


if __name__ == "__main__":
    asyncio.run(main())
