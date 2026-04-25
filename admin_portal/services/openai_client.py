"""Thin wrapper around the OpenAI Python SDK.

The key lives in settings.OPENAI_API_KEY (loaded from .env). Until a real key is
provided the wrapper returns a deterministic "pending_human_review" verdict so
the rest of the pipeline keeps working in dev.
"""
import json
import logging
from typing import Any, Dict

from django.conf import settings

logger = logging.getLogger(__name__)

_PLACEHOLDER_KEY_PREFIXES = ("sk-REPLACE", "sk-replace", "", None)


def _is_placeholder(key: str) -> bool:
    if not key:
        return True
    for prefix in _PLACEHOLDER_KEY_PREFIXES:
        if prefix and key.startswith(prefix):
            return True
    return False


SYSTEM_PROMPT = (
    "You are the Aqua AI account-review intelligence. Your job is to decide whether "
    "a newly created breeder or consultant profile on the AquaAI marketplace should "
    "be auto-approved, auto-rejected, or flagged for the human super-admins "
    "(Steven@humara.io and Ben@humara.io). \n\n"
    "Use only verifiable, policy-grounded reasons. Be conservative: if anything is "
    "ambiguous, missing, suspicious or impersonating, FLAG instead of approving.\n\n"
    "Return STRICT JSON of the form:\n"
    "{\n"
    '  "decision": "approved" | "rejected" | "flagged",\n'
    '  "confidence": 0.0-1.0,\n'
    '  "rationale": "2-4 sentence summary explaining the decision",\n'
    '  "red_flags": ["...", "..."],\n'
    '  "recommended_actions": [\n'
    '    {"action": "approve|reject|request_more_info|deactivate|notify_admins",\n'
    '     "reason": "why",\n'
    '     "details": {"any": "structured data"}}\n'
    "  ]\n"
    "}"
)


def classify_profile(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Send the profile to GPT-4 and return its decision dict.

    Falls back to a 'flagged / human review' shape when no API key is configured
    so deployments without the key still produce auditable rows.
    """
    key = getattr(settings, "OPENAI_API_KEY", "")
    model = getattr(settings, "OPENAI_MODEL", "gpt-4o")

    if _is_placeholder(key):
        logger.warning("OPENAI_API_KEY is not configured; emitting placeholder verdict.")
        return {
            "decision": "flagged",
            "confidence": 0.0,
            "rationale": (
                "OpenAI key not configured yet. Holding the profile for human review "
                "until the API key is set."
            ),
            "red_flags": ["openai_key_missing"],
            "recommended_actions": [
                {
                    "action": "notify_admins",
                    "reason": "AI key missing",
                    "details": {"hint": "Set OPENAI_API_KEY in the environment."},
                }
            ],
            "_raw": {"placeholder": True},
            "_model": "placeholder",
        }

    try:
        from openai import OpenAI
    except ImportError:
        logger.exception("openai package not installed; skipping AI review.")
        return {
            "decision": "flagged",
            "confidence": 0.0,
            "rationale": "openai SDK is not installed in this environment.",
            "red_flags": ["openai_sdk_missing"],
            "recommended_actions": [],
            "_raw": {},
            "_model": "unavailable",
        }

    client = OpenAI(api_key=key)
    user_message = (
        "Review this AquaAI profile and return strict JSON per the system instructions.\n\n"
        f"PROFILE:\n{json.dumps(payload, default=str, indent=2)}"
    )
    try:
        completion = client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            temperature=0.1,
        )
        raw = completion.choices[0].message.content or "{}"
        parsed = json.loads(raw)
    except Exception as exc:  # pragma: no cover - network failures
        logger.exception("OpenAI classification failed")
        return {
            "decision": "flagged",
            "confidence": 0.0,
            "rationale": f"GPT call failed: {exc}",
            "red_flags": ["openai_error"],
            "recommended_actions": [],
            "_raw": {"error": str(exc)},
            "_model": model,
        }

    parsed.setdefault("decision", "flagged")
    parsed.setdefault("confidence", 0.0)
    parsed.setdefault("rationale", "")
    parsed.setdefault("red_flags", [])
    parsed.setdefault("recommended_actions", [])
    parsed["_raw"] = parsed.copy()
    parsed["_model"] = model
    return parsed


def summarise_day(stats: Dict[str, Any], decisions: list) -> str:
    """Ask the model to summarise the day's decisions for the email digest."""
    key = getattr(settings, "OPENAI_API_KEY", "")
    if _is_placeholder(key):
        return (
            f"{stats.get('total_reviewed', 0)} profiles reviewed today: "
            f"{stats.get('approved_count', 0)} approved, "
            f"{stats.get('rejected_count', 0)} rejected, "
            f"{stats.get('flagged_count', 0)} flagged for human follow-up. "
            "(Set OPENAI_API_KEY for richer narrative summaries.)"
        )
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key)
        completion = client.chat.completions.create(
            model=getattr(settings, "OPENAI_MODEL", "gpt-4o"),
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Write a concise (max 6 sentences) executive summary of today's "
                        "AquaAI breeder/consultant approval activity for the super-admins."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps({"stats": stats, "decisions": decisions}, default=str),
                },
            ],
            temperature=0.2,
        )
        return (completion.choices[0].message.content or "").strip()
    except Exception as exc:  # pragma: no cover
        logger.exception("daily summary generation failed")
        return f"Could not generate AI summary: {exc}"
