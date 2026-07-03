"""
LLM bounty scorer.
Tries local Ollama first; falls back to heuristic scoring if unavailable.
"""

import logging
import re
from typing import Any

from app.config import settings

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
    """Call local Ollama for LLM-based scoring.  Returns None if unavailable."""
    try:
        import httpx

        prompt = _build_prompt(bounty)
        payload = {
            "model": settings.OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 32},
        }
        async with httpx.AsyncClient(
            timeout=30.0,
            headers=settings.http_headers(),
            proxy=settings.proxy_config(),
        ) as client:
            resp = await client.post(
                f"{settings.OLLAMA_BASE_URL}/api/generate",
                json=payload,
            )
            resp.raise_for_status()
            text = resp.json().get("response", "").strip()

        score = _parse_llm_score(text)
        if score is not None:
            logger.debug("LLM scored '%s' = %.2f", bounty.get("title", "")[:40], score)
            return score

        logger.warning("LLM returned unparseable score: %s", text[:80])
        return None

    except httpx.ConnectError:
        logger.debug(
            "Ollama not available at %s — using heuristic", settings.OLLAMA_BASE_URL
        )
        return None
    except Exception as exc:
        logger.debug("LLM scoring failed: %s — using heuristic", exc)
        return None


def _build_prompt(bounty: dict[str, Any]) -> str:
    """Build a compact prompt for the LLM."""
    title = bounty.get("title", "")
    desc = bounty.get("description", "")[:300]
    reward = bounty.get("reward_amount", 0)
    currency = bounty.get("reward_currency", "ETH")
    level = bounty.get("experience_level", "")
    btype = bounty.get("bounty_type", "")
    keywords = ", ".join(bounty.get("keywords", []) or [])
    issue_kw = ", ".join(bounty.get("issue_keywords", []) or [])

    return f"""Rate this bounty's value-to-effort ratio from 0.0 to 1.0.
Only return a single float number, nothing else.

Title: {title}
Description: {desc[:200]}
Reward: {reward} {currency}
Level: {level}
Type: {btype}
Keywords: {keywords} {issue_kw}

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
    score = 0.5  # neutral baseline

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
    score += min(keyword_hits / 5, 1.0) * 0.15

    # Keywords from metadata
    keywords = bounty.get("keywords", []) or []
    issue_kw = bounty.get("issue_keywords", []) or []
    meta_hits = len(keywords) + len(issue_kw)
    score += min(meta_hits / 3, 1.0) * 0.1

    # Reward bonus
    reward = bounty.get("reward_amount", 0)
    if reward > 100:
        score += 0.1
    elif reward > 10:
        score += 0.05

    return max(0.0, min(1.0, score))
