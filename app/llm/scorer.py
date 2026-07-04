"""
LLM bounty scorer.
Tries OpenCode Zen → local Ollama → heuristic scoring.
"""

import logging
import re
from typing import Any

from app.config import settings
from app.llm.client import call_llm

logger = logging.getLogger("buckgen.llm")

# -- Scoring rubric ---------------------------------------------------------
# Weights for heuristic scoring (used when LLM is unavailable)
EXPERIENCE_WEIGHTS = {
    "beginner": 1.0,
    "intermediate": 0.7,
    "advanced": 0.3,
}
TYPE_WEIGHTS = {
    "bug": 0.5,
    "feature": 0.8,
    "security": 1.0,
    "improvement": 0.6,
}
LENGTH_WEIGHTS = {
    "hours": 1.0,
    "days": 0.8,
    "weeks": 0.4,
    "months": 0.2,
}
DESIRABLE_KEYWORDS = [
    "python",
    "typescript",
    "javascript",
    "react",
    "node",
    "api",
    "web3",
    "ethereum",
    "blockchain",
    "solidity",
    "smart contract",
    "automation",
    "bot",
    "scraping",
    "data",
    "testing",
    "defi",
    "nft",
    "bridge",
    "wallet",
    "oracle",
    "governance",
    "staking",
    "liquidity",
    "yield",
    "cross-chain",
    "layer2",
    "rollup",
    "zero-knowledge",
    "zk",
    "dex",
    "amm",
    "slippage",
    "mev",
    "frontend",
    "backend",
    "database",
    "trading",
    "exchange",
    "indexer",
    "graphql",
    "docker",
    "kubernetes",
    "ci/cd",
    "devops",
]


async def score_bounty(bounty: dict[str, Any]) -> float:
    """
    Score a bounty by its expected value-to-effort ratio.
    Returns a float 0.0 (bad) to 1.0 (excellent).
    """
    # First try LLM
    score = await _score_with_llm(bounty)
    if score is not None:
        return score

    # Fallback to heuristic
    return _score_heuristic(bounty)


# ---------------------------------------------------------------------------
# LLM scorer (Ollama)
# ---------------------------------------------------------------------------
async def _score_with_llm(bounty: dict[str, Any]) -> float | None:
    """Score a bounty via OpenCode Zen → Ollama → heuristic."""
    try:
        prompt = _build_prompt(bounty)
        text = await call_llm("score", prompt, max_tokens=settings.LLM_SCORE_MAX_TOKENS)
        if text is None:
            return None

        score = _parse_llm_score(text)
        if score is not None:
            logger.debug("LLM scored '%s' = %.2f", bounty.get("title", "")[:40], score)
            return score

        logger.warning("LLM returned unparseable score: %s", text[:80])
        return None
    except Exception as exc:
        logger.debug("LLM scoring failed: %s — using heuristic", exc)
        return None


def _build_prompt(bounty: dict[str, Any]) -> str:
    """Build a prompt for the LLM with full context and explicit rubric."""
    title = bounty.get("title", "")
    desc = bounty.get("description", "")[:2000]
    reward = bounty.get("reward_amount", 0)
    currency = bounty.get("reward_currency", "ETH")
    level = bounty.get("experience_level", "")
    btype = bounty.get("bounty_type", "")
    keywords = ", ".join(bounty.get("keywords", []) or [])
    issue_kw = ", ".join(bounty.get("issue_keywords", []) or [])
    comments = bounty.get("comments_count", 0)
    reactions = bounty.get("reactions_count", 0)
    repo_stars = bounty.get("repo_stars", 0)
    created = bounty.get("created_at", "")
    updated = bounty.get("updated_at", "")

    return f"""Rate this bounty's value-to-effort ratio from 0.0 to 1.0.
Only return a single float number, nothing else.

Title: {title}
Description: {desc[:1500]}
Reward: {reward} {currency}
Level: {level}
Type: {btype}
Keywords: {keywords} {issue_kw}
Comments: {comments}
Reactions: {reactions}
Repo Stars: {repo_stars}
Created: {created}
Updated: {updated}

Scoring Rubric:
- 0.9-1.0: High reward for quick task (beginner-friendly, small scope, $500+)
- 0.7-0.9: Good value, reasonable effort (intermediate, days, $100+)
- 0.5-0.7: Average value-to-effort ratio
- 0.3-0.5: Low reward for significant effort (advanced, weeks, under $50)
- 0.0-0.3: Poor value (unpaid, extremely complex, or niche expertise required)

Score:"""


def _parse_llm_score(text: str) -> float | None:
    """Extract a float 0.0–1.0 from LLM response text."""
    # Find first floating-point number
    match = re.search(r"(\d+\.\d+)", text)
    if match:
        try:
            val = float(match.group(1))
            return max(0.0, min(1.0, val))
        except ValueError:
            return None
    # Also try integer 0 or 1
    match = re.search(r"\b([01])\b", text)
    if match:
        try:
            val = float(match.group(1))
            return max(0.0, min(1.0, val))
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Heuristic fallback
# ---------------------------------------------------------------------------
def _score_heuristic(bounty: dict[str, Any]) -> float:
    """Score a bounty without an LLM, using keyword + metadata weights."""
    score = settings.HEURISTIC_BASELINE_SCORE

    # Experience level
    level = bounty.get("experience_level", "").lower()
    score += EXPERIENCE_WEIGHTS.get(level, 0.0) * 0.15

    # Bounty type
    btype = bounty.get("bounty_type", "").lower()
    score += TYPE_WEIGHTS.get(btype, 0.5) * 0.1

    # Project length
    length = bounty.get("project_length", "").lower()
    score += LENGTH_WEIGHTS.get(length, 0.5) * 0.1

    # Keywords in title/description
    title_desc = (bounty.get("title", "") + " " + bounty.get("description", "")).lower()
    keyword_hits = sum(1 for kw in DESIRABLE_KEYWORDS if kw in title_desc)
    score += min(keyword_hits / 5, 1.0) * settings.KEYWORD_BONUS_WEIGHT

    # Keywords from metadata
    keywords = bounty.get("keywords", []) or []
    issue_kw = bounty.get("issue_keywords", []) or []
    meta_hits = len(keywords) + len(issue_kw)
    score += min(meta_hits / 3, 1.0) * settings.META_BONUS_WEIGHT

    # Reward bonus
    reward = bounty.get("reward_amount", 0)
    if reward > 100:
        score += settings.LARGE_REWARD_BONUS
    elif reward > 10:
        score += settings.SMALL_REWARD_BONUS

    return max(0.0, min(1.0, score))
