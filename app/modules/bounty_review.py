"""
Bounty Review — human-in-the-loop replacement for auto-submission.

Philosophy
----------
This module does NOT auto-post anything. Its job is to turn the scored
bounty pipeline into a decision aid for a human:

  1. research_bounty()  -> private ROI briefing (effort, payout confidence,
                           legitimacy, files involved). Never posted.
  2. rank_by_roi()      -> your queue, sorted by expected $ / hour.
  3. prepare_draft()    -> a draft solution for YOU to read and edit.
                           Stored only. Never posted.
  4. approve()          -> the ONLY way a bounty becomes postable. This is a
                           deliberate human action, one item at a time.
  5. post_approved()    -> posts a draft, but ONLY if you approved it AND
                           pass confirm=True. Two independent gates.

The point: you engage with real work you've vetted and post as yourself,
instead of blasting unvetted AI comments at maintainers. ROI ranking means
your attention goes to the bounties actually worth your time.
"""

import json
import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.db.models import Bounty, BountyStatus
from app.llm.client import call_llm
from app.modules.submit_bounty import post_comment
from app.utils.notify import notify_alert

logger = logging.getLogger("buckgen.review")

# High-ROI threshold ($ per hour) above which we ping you proactively.
ROI_ALERT_THRESHOLD = float(80.0)


# ---------------------------------------------------------------------------
# ROI estimation + research briefing
# ---------------------------------------------------------------------------
def compute_roi(reward: float, effort_hours: float, payout_confidence: float) -> float:
    """Expected dollars per hour = reward * P(payout) / effort.

    Effort is floored at 0.5h so a tiny estimate can't produce absurd ROI.
    """
    effort = max(effort_hours, 0.5)
    conf = min(max(payout_confidence, 0.0), 1.0)
    return round((reward * conf) / effort, 2)


async def _llm_assess(bounty: Bounty) -> dict:
    """Ask the LLM for a private assessment. Returns a dict with keys:
    effort_hours, payout_confidence, briefing. Falls back to heuristics.
    """
    system_prompt = (
        "You are a senior engineer triaging paid GitHub bounties for a "
        "developer deciding what to work on. Be blunt and realistic. You are "
        "writing a PRIVATE briefing for the developer — not a public comment."
    )
    user_prompt = (
        f"BOUNTY: {bounty.title}\n"
        f"REWARD: {bounty.reward_amount} {bounty.reward_currency}\n"
        f"URL: {bounty.url}\n"
        f"DESCRIPTION:\n{(bounty.description or '')[:2500]}\n\n"
        "Assess this bounty and respond with ONLY a JSON object, no prose, "
        "no markdown fences, with these exact keys:\n"
        '  "effort_hours": number  (realistic hours for a competent dev)\n'
        '  "payout_confidence": number 0-1  (odds this is legit AND still '
        "open AND actually pays)\n"
        '  "legitimacy": short string  (real / stale / vague / likely-scam)\n'
        '  "difficulty": short string  (trivial / moderate / hard / specialist)\n'
        '  "areas": short string  (subsystems or files likely involved)\n'
        '  "verdict": one sentence  (is this worth the developer\'s time, and '
        "why or why not)"
    )
    text = await call_llm(
        "score", user_prompt, system_prompt, max_tokens=700, temperature=0.2
    )

    parsed = _safe_parse_json(text) if text else None
    if not parsed:
        # Heuristic fallback: effort scales with description length, neutral conf.
        desc_len = len(bounty.description or "")
        effort = 2.0 + min(desc_len / 800.0, 10.0)
        return {
            "effort_hours": round(effort, 1),
            "payout_confidence": 0.4,
            "briefing": (
                "[heuristic — LLM unavailable] No automated assessment. "
                "Review the issue manually before spending time on it."
            ),
        }

    effort = _as_float(parsed.get("effort_hours"), default=4.0)
    conf = _as_float(parsed.get("payout_confidence"), default=0.4)
    briefing = (
        f"Legitimacy: {parsed.get('legitimacy', '?')}\n"
        f"Difficulty: {parsed.get('difficulty', '?')}\n"
        f"Effort estimate: {effort}h\n"
        f"Payout confidence: {conf:.0%}\n"
        f"Areas involved: {parsed.get('areas', '?')}\n\n"
        f"Verdict: {parsed.get('verdict', '?')}"
    )
    return {
        "effort_hours": effort,
        "payout_confidence": conf,
        "briefing": briefing,
    }


async def research_bounty(db: Session, bounty_id: int) -> dict:
    """Generate a private ROI briefing for one bounty. Posts nothing."""
    bounty = db.query(Bounty).filter(Bounty.id == bounty_id).first()
    if not bounty:
        return {"success": False, "error": "Bounty not found"}

    assessment = await _llm_assess(bounty)
    bounty.effort_hours = assessment["effort_hours"]
    bounty.payout_confidence = assessment["payout_confidence"]
    bounty.briefing = assessment["briefing"]
    bounty.roi_score = compute_roi(
        bounty.reward_amount, bounty.effort_hours, bounty.payout_confidence
    )
    # Only advance status if we haven't already moved further along.
    if bounty.status == BountyStatus.OPEN:
        bounty.status = BountyStatus.RESEARCHED
    bounty.updated_at = datetime.now(UTC)
    db.commit()

    if bounty.roi_score >= ROI_ALERT_THRESHOLD:
        await notify_alert(
            f"High-ROI bounty: {bounty.title[:50]}",
            f"~${bounty.roi_score}/hr · {bounty.reward_amount} "
            f"{bounty.reward_currency}\n{bounty.url}",
        )

    return {
        "success": True,
        "bounty_id": bounty.id,
        "roi_score": bounty.roi_score,
        "effort_hours": bounty.effort_hours,
        "payout_confidence": bounty.payout_confidence,
        "briefing": bounty.briefing,
    }


async def research_unassessed(db: Session, limit: int = 15) -> list[dict]:
    """Research the top-scored OPEN bounties that lack an ROI briefing yet."""
    candidates = (
        db.query(Bounty)
        .filter(Bounty.status == BountyStatus.OPEN, Bounty.roi_score == 0.0)
        .order_by(Bounty.score.desc())
        .limit(limit)
        .all()
    )
    results = []
    for b in candidates:
        results.append(await research_bounty(db, b.id))
    return results


# ---------------------------------------------------------------------------
# Ranking + digest (the "convenient output")
# ---------------------------------------------------------------------------
def rank_by_roi(db: Session, limit: int = 20, min_roi: float = 0.0) -> list[dict]:
    """Your review queue, sorted by expected $ / hour (best first)."""
    rows = (
        db.query(Bounty)
        .filter(
            Bounty.status.in_(
                [
                    BountyStatus.RESEARCHED,
                    BountyStatus.DRAFTED,
                    BountyStatus.APPROVED,
                ]
            ),
            Bounty.roi_score >= min_roi,
        )
        .order_by(Bounty.roi_score.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": b.id,
            "title": b.title,
            "reward": f"{b.reward_amount} {b.reward_currency}",
            "roi_per_hour": b.roi_score,
            "effort_hours": b.effort_hours,
            "payout_confidence": b.payout_confidence,
            "status": b.status.value,
            "url": b.url,
            "briefing": b.briefing,
            "has_draft": bool(b.draft_solution),
        }
        for b in rows
    ]


def build_digest(db: Session, limit: int = 15) -> str:
    """Markdown digest of the ROI-ranked queue — easy to export or paste."""
    queue = rank_by_roi(db, limit=limit)
    if not queue:
        return "# Bounty Review Queue\n\n_No researched bounties yet._\n"

    lines = [
        "# Bounty Review Queue",
        f"_Generated {datetime.now(UTC):%Y-%m-%d %H:%M UTC} · ranked by expected $/hr_",
        "",
    ]
    for i, item in enumerate(queue, 1):
        lines.append(
            f"## {i}. {item['title']}  —  ~${item['roi_per_hour']}/hr"
        )
        lines.append(
            f"- **Reward:** {item['reward']}  ·  **Effort:** "
            f"{item['effort_hours']}h  ·  **Payout confidence:** "
            f"{item['payout_confidence']:.0%}  ·  **Status:** {item['status']}"
        )
        lines.append(f"- **Link:** {item['url']}")
        if item["briefing"]:
            indented = "\n".join("  > " + ln for ln in item["briefing"].splitlines())
            lines.append(indented)
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Draft (for your eyes) — never posts
# ---------------------------------------------------------------------------
async def prepare_draft(db: Session, bounty_id: int) -> dict:
    """Generate a draft solution for YOUR review. Stores it; posts nothing."""
    bounty = db.query(Bounty).filter(Bounty.id == bounty_id).first()
    if not bounty:
        return {"success": False, "error": "Bounty not found"}

    system_prompt = (
        "You are drafting a technical response to a GitHub bounty issue. This "
        "draft is for the developer to review and edit before they decide "
        "whether to post it themselves. Be accurate; do not invent APIs. If "
        "the issue is underspecified, say what you'd need to know."
    )
    user_prompt = (
        f"ISSUE: {bounty.title}\n"
        f"DESCRIPTION:\n{(bounty.description or '')[:2500]}\n\n"
        "Write a concrete, technically accurate draft response with a clear "
        "implementation approach and code where warranted. Flag any "
        "assumptions explicitly."
    )
    text = await call_llm(
        "submit", user_prompt, system_prompt, max_tokens=2000, temperature=0.5
    )
    if not text:
        return {"success": False, "error": "LLM unavailable — no draft generated"}

    bounty.draft_solution = text
    if bounty.status in (BountyStatus.OPEN, BountyStatus.RESEARCHED):
        bounty.status = BountyStatus.DRAFTED
    bounty.updated_at = datetime.now(UTC)
    db.commit()
    return {
        "success": True,
        "bounty_id": bounty.id,
        "draft": text,
        "note": "Draft stored for your review. Nothing has been posted.",
    }


# ---------------------------------------------------------------------------
# Approval gate — the ONLY path to posting
# ---------------------------------------------------------------------------
def approve(db: Session, bounty_id: int) -> dict:
    """Mark a bounty as approved-to-post. Deliberate, per-item human action.

    Requires an existing draft so approval is a decision about real content,
    not a blank cheque. Still posts nothing by itself.
    """
    bounty = db.query(Bounty).filter(Bounty.id == bounty_id).first()
    if not bounty:
        return {"success": False, "error": "Bounty not found"}
    if not bounty.draft_solution:
        return {
            "success": False,
            "error": "No draft to approve — run prepare_draft and review it first.",
        }
    if bounty.status not in (
        BountyStatus.OPEN,
        BountyStatus.RESEARCHED,
        BountyStatus.DRAFTED,
    ):
        return {
            "success": False,
            "error": f"Cannot approve from status '{bounty.status.value}'.",
        }
    bounty.status = BountyStatus.APPROVED
    bounty.approved_at = datetime.now(UTC)
    bounty.updated_at = datetime.now(UTC)
    db.commit()
    return {
        "success": True,
        "bounty_id": bounty.id,
        "status": bounty.status.value,
        "note": "Approved. Posting still requires an explicit post call with confirm=true.",
    }


async def post_approved(db: Session, bounty_id: int, confirm: bool = False) -> dict:
    """Post the reviewed draft — ONLY if approved AND confirm=True.

    Two independent gates: the bounty must be in APPROVED status (set by a
    human via approve()), and this call must pass confirm=True. Either one
    missing => nothing is posted.
    """
    bounty = db.query(Bounty).filter(Bounty.id == bounty_id).first()
    if not bounty:
        return {"success": False, "error": "Bounty not found"}

    if bounty.status != BountyStatus.APPROVED:
        return {
            "success": False,
            "error": (
                f"Not approved (status '{bounty.status.value}'). Approve the "
                "reviewed draft first — posting is gated on explicit approval."
            ),
        }
    if not confirm:
        return {
            "success": False,
            "error": "confirm=false — not posted. Pass confirm=true to post.",
            "would_post_to": bounty.url,
        }
    if not bounty.draft_solution:
        return {"success": False, "error": "No draft content to post."}

    # Parse owner/repo/issue from the URL.
    parts = bounty.url.rstrip("/").split("/")
    if "issues" not in parts:
        return {"success": False, "error": f"Cannot parse issue URL: {bounty.url}"}
    try:
        idx = parts.index("issues")
        repo = "/".join(parts[3:idx])
        issue_number = int(parts[idx + 1])
    except (ValueError, IndexError):
        return {"success": False, "error": f"Cannot parse issue number: {bounty.url}"}

    posted = await post_comment(repo, issue_number, bounty.draft_solution)
    if not posted:
        return {"success": False, "error": "GitHub post failed"}

    bounty.status = BountyStatus.APPLIED
    bounty.updated_at = datetime.now(UTC)
    db.commit()
    await notify_alert(
        f"Posted (approved): {bounty.title[:40]}",
        f"{repo}#{issue_number}\n{bounty.url}",
    )
    return {
        "success": True,
        "bounty_id": bounty.id,
        "repo": repo,
        "issue_number": issue_number,
    }


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _as_float(v, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _safe_parse_json(text: str) -> dict | None:
    """Parse a JSON object from an LLM response, tolerating stray fences."""
    if not text:
        return None
    cleaned = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    # Grab the outermost brace pair if there's surrounding prose.
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        cleaned = cleaned[start : end + 1]
    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None
