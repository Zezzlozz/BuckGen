"""
GitHub Issues bounty scanner.
Searches GitHub Issues API using multiple label-based queries to find
bounty-labelled and reward-bearing issues across all of GitHub (not
just known web3 repos — label search covers the entire ecosystem).
"""

import asyncio
import logging
import re
import time
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger("buckgen.bounties")

GITHUB_API = "https://api.github.com"

# GitHub rate limit tracking — shared across all queries
_rate_limit_remaining: int = 5000  # optimistic default with token
_rate_limit_reset: float = 0.0  # Unix timestamp when bucket refills


def _update_rate_limit(resp: httpx.Response) -> None:
    """Parse rate limit headers from a GitHub API response."""
    global _rate_limit_remaining, _rate_limit_reset

    remaining = resp.headers.get("X-RateLimit-Remaining")
    reset = resp.headers.get("X-RateLimit-Reset")

    if remaining is not None:
        _rate_limit_remaining = int(remaining)
    if reset is not None:
        _rate_limit_reset = float(reset)

    if _rate_limit_remaining < 50:
        wait = max(0, _rate_limit_reset - time.time())
        logger.warning(
            "GitHub rate limit running low: %d remaining, resets in %.0fs",
            _rate_limit_remaining,
            wait,
        )


async def _wait_if_needed() -> None:
    """Block until rate limit resets if remaining is too low."""
    if _rate_limit_remaining < 10:
        wait = max(0, _rate_limit_reset - time.time()) + 1.0  # +1s safety margin
        if wait > 0:
            logger.warning(
                "GitHub rate limit exhausted (%d remaining). Waiting %.0fs...",
                _rate_limit_remaining,
                wait,
            )
            await asyncio.sleep(min(wait, 120.0))  # cap at 2 min


# Multiple search queries to discover bounty-style issues across GitHub.
# GitHub search does NOT support OR between label: qualifiers in a single query,
# so we run these in parallel and merge results.
SEARCH_QUERIES = [
    "label:bounty is:issue is:open",
    'label:"bug bounty" is:issue is:open',
    "label:paid is:issue is:open",
    "label:reward is:issue is:open",
    "label:💰 is:issue is:open",
]

# Repos known to use GitHub issue labels for bounties (used as a secondary
# filter — search queries above handle the primary discovery)
BOUNTY_REPOS = [
    "gitcoinco/web",
    "keep3r-network/keep3r.network",
    "yearn/yearn-pm",
    "code-423n4/2024-*",  # C4 contests (wildcard — not directly queryable)
]

REWARD_REGEXES = [
    re.compile(
        r"(?:[\$€£])?\s*([\d,]+(?:\.\d+)?)\s*(USD|USDC|USDT|ETH|BTC|SOL|MATIC)?", re.I
    ),
    re.compile(r"reward\s*:?\s*([\d,]+(?:\.\d+)?)", re.I),
    re.compile(r"bounty\s*:?\s*([\d,]+(?:\.\d+)?)", re.I),
]


async def fetch_open_bounties(
    client: httpx.AsyncClient | None = None,
    max_bounties: int = 200,
) -> list[dict[str, Any]]:
    """
    Search GitHub Issues for open bounty-labelled issues.
    Supports pagination to fetch up to `max_bounties` results.
    Uses unauthenticated API (60 req/hr).  Add GITHUB_TOKEN to env for 5000/hr.
    """
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(
            timeout=30.0,
            headers=settings.http_headers(),
            proxy=settings.proxy_config(),
        )
    else:
        # Merge UA into existing client headers
        client = client

    headers = {"Accept": "application/vnd.github.v3+json"}

    # Pull token from config if available
    if settings.GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {settings.GITHUB_TOKEN}"

    # Run all label queries in parallel for maximum coverage.
    # GitHub search does not support OR between label: qualifiers in a single
    # query, so we issue separate requests per label.
    # Per-page rate limit checks prevent burst-consumption of the quota.
    tasks = [
        _search_single_query(client, headers, q, max_bounties) for q in SEARCH_QUERIES
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Merge and deduplicate by issue URL
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for result in results:
        if isinstance(result, Exception):
            continue  # individual query failures are already logged
        for item in result:
            url = item.get("html_url", "")
            if url not in seen:
                seen.add(url)
                merged.append(item)

    logger.info(
        "GitHub search complete: %d unique bounties from %d queries",
        len(merged),
        len(SEARCH_QUERIES),
    )

    if own_client:
        await client.aclose()

    return merged[:max_bounties]


async def _search_single_query(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    query: str,
    max_bounties: int,
) -> list[dict[str, Any]]:
    """Fetch all pages for a single search query."""
    items: list[dict[str, Any]] = []
    per_page = min(max_bounties, 100)
    page = 1

    try:
        while len(items) < max_bounties:
            # Check rate limit before each API call
            await _wait_if_needed()
            params = {
                "q": query,
                "sort": "updated",
                "order": "desc",
                "per_page": per_page,
                "page": page,
            }

            resp = await client.get(
                f"{GITHUB_API}/search/issues",
                params=params,
                headers=headers,
            )

            # Track remaining rate limit
            _update_rate_limit(resp)

            if resp.status_code == 403 and "rate limit" in resp.text.lower():
                logger.warning(
                    "GitHub API rate limited on query %r — try setting GITHUB_TOKEN",
                    query,
                )
                break

            resp.raise_for_status()
            data = resp.json()
            batch = data.get("items", [])
            items.extend(batch)

            logger.debug(
                "GitHub search %r page %d: %d items (total: %d / %s)",
                query,
                page,
                len(batch),
                len(items),
                data.get("total_count", "?"),
            )

            if len(batch) < per_page:
                break

            total_count = data.get("total_count", 0)
            if total_count and len(items) >= total_count:
                break

            page += 1

        return items

    except httpx.HTTPStatusError as exc:
        logger.error(
            "GitHub API HTTP %d on query %r: %s",
            exc.response.status_code,
            query,
            exc.response.text[:200],
        )
        return items
    except httpx.RequestError as exc:
        logger.error("GitHub request failed on query %r: %s", query, exc)
        return items


def normalize_bounty(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Extract bounty fields from a GitHub API issue item.
    """
    labels = [label.get("name", "") for label in raw.get("labels", [])]
    repo_full = raw.get("repository_url", "").replace(
        "https://api.github.com/repos/", ""
    )

    return {
        "external_id": str(raw.get("id", raw.get("number", ""))),
        "title": raw.get("title", "").strip(),
        "description": (raw.get("body") or "")[:4000],
        "reward_amount": _parse_reward_from_issue(raw),
        "reward_currency": _parse_currency_from_issue(raw),
        "experience_level": _detect_level(
            labels + [raw.get("title", ""), raw.get("body") or ""]
        ),
        "labels": labels,
        "repo": repo_full,
        "url": raw.get("html_url", ""),
        "created": raw.get("created_at", ""),
        "updated": raw.get("updated_at", ""),
        "state": raw.get("state", ""),
    }


# ---------------------------------------------------------------------------
# Reward extraction from issue body / title
# ---------------------------------------------------------------------------
def _parse_reward_from_issue(issue: dict[str, Any]) -> float:
    """Try to extract a numeric reward amount from the issue."""
    text = f"{issue.get('title', '')} {issue.get('body', '')}"

    # First: look for price labels like "price: 500" or "reward: 1000 USDC"
    labels = [label.get("name", "") for label in issue.get("labels", [])]
    for label in labels:
        m = re.search(r"price[:\s]*\$?(\d+[\d,.]*)", label, re.I)
        if m:
            return _clean_number(m.group(1))

    # Second: check body/title for dollar amounts
    for pattern in REWARD_REGEXES:
        for m in pattern.finditer(text):
            val = _clean_number(m.group(1))
            if val > 0:
                return val

    return 0.0


def _parse_currency_from_issue(issue: dict[str, Any]) -> str:
    """Try to detect the reward currency."""
    text = f"{issue.get('title', '')} {issue.get('body', '')}"
    for currency in ["ETH", "USDC", "USDT", "BTC", "SOL", "MATIC", "USD", "EUR"]:
        if currency in text.upper():
            return currency
    return "USD"


def _detect_level(texts: list[str]) -> str:
    """Detect experience level from combined text."""
    combined = " ".join(texts).lower()
    if any(w in combined for w in ["beginner", "good first issue", "easy", "low"]):
        return "beginner"
    if any(w in combined for w in ["intermediate", "medium", "moderate"]):
        return "intermediate"
    if any(
        w in combined for w in ["advanced", "expert", "hard", "difficult", "critical"]
    ):
        return "advanced"
    return ""


def _clean_number(s: str) -> float:
    """Convert a string like '1,500' or '1500.50' to float."""
    s = s.replace(",", "").replace(" ", "")
    try:
        return float(s)
    except ValueError:
        return 0.0
