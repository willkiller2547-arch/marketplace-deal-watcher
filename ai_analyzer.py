from __future__ import annotations

import json
import os
from typing import Any

import keyring
import requests

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
KEYRING_SERVICE = "MarketplaceDealWatcher"
KEYRING_USERNAME = "openai_api_key"

AI_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "eligible": {"type": "boolean"},
        "deal_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        "verdict": {
            "type": "string",
            "enum": ["skip", "poor", "fair", "good", "very_good", "exceptional"],
        },
        "detected_item": {"type": "string"},
        "estimated_value_low": {"type": "number", "minimum": 0},
        "estimated_value_high": {"type": "number", "minimum": 0},
        "summary": {"type": "string"},
        "positives": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 5,
        },
        "red_flags": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 5,
        },
    },
    "required": [
        "eligible",
        "deal_score",
        "confidence",
        "verdict",
        "detected_item",
        "estimated_value_low",
        "estimated_value_high",
        "summary",
        "positives",
        "red_flags",
    ],
}


def get_api_key() -> str:
    env_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if env_key:
        return env_key
    try:
        return (keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME) or "").strip()
    except Exception:
        return ""


def has_api_key() -> bool:
    return bool(get_api_key())


def save_api_key(api_key: str) -> None:
    key = api_key.strip()
    if not key:
        raise ValueError("API key is empty.")
    keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, key)


def clear_api_key() -> None:
    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USERNAME)
    except keyring.errors.PasswordDeleteError:
        pass


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _extract_output_text(payload: dict[str, Any]) -> str:
    for output in payload.get("output", []):
        if output.get("type") != "message":
            continue
        for content in output.get("content", []):
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                return content["text"]
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    raise RuntimeError("OpenAI response did not contain output text.")


def test_api_key(model: str = "gpt-5-mini") -> None:
    api_key = get_api_key()
    if not api_key:
        raise ValueError("No OpenAI API key is saved.")

    body = {
        "model": model.strip() or "gpt-5-mini",
        "store": False,
        "input": "Reply with exactly: OK",
        "max_output_tokens": 40,
    }
    response = requests.post(
        OPENAI_RESPONSES_URL,
        headers=_headers(api_key),
        json=body,
        timeout=45,
    )
    if response.status_code >= 400:
        try:
            detail = response.json().get("error", {}).get("message", response.text)
        except Exception:
            detail = response.text
        raise RuntimeError(f"OpenAI API error {response.status_code}: {detail[:500]}")


def analyze_listing(
    *,
    item: dict[str, Any],
    search_name: str,
    baseline: float | None,
    baseline_source: str,
    rule_score: int,
    rule_reasons: list[str],
    model: str = "gpt-5-mini",
    use_image: bool = True,
) -> dict[str, Any]:
    api_key = get_api_key()
    if not api_key:
        raise ValueError("No OpenAI API key is saved.")

    price = item.get("price")
    baseline_text = (
        f"${baseline:,.2f} ({baseline_source})" if baseline else "No reliable baseline available"
    )

    prompt = f"""You are evaluating a Facebook Marketplace listing for a buyer.

Your job is to decide whether this is a genuinely good consumer purchase, not merely whether the number is low.
Use the supplied Marketplace baseline as your strongest pricing evidence. Consider the exact item/specs, condition, completeness, likely missing parts, suspicious wording, and whether the listing appears materially better or worse than the baseline group.

Important rules:
- Do not invent exact market facts that are not supported by the listing or supplied baseline.
- If evidence is weak, lower confidence and keep the estimated value range conservative.
- estimated_value_low/high should be in the same currency as the listing. Use 0 for both if you cannot responsibly estimate a range.
- A deal score near 50 is ordinary/fair; 75+ is clearly attractive; 90+ should be rare.
- If the listing appears to involve age-restricted, dangerous, weapon-related, intoxicating, gambling-related, or otherwise unsuitable goods, set eligible=false, verdict=skip, deal_score=0. Do not provide purchasing advice for it.
- Focus on purchase value and obvious red flags; do not claim a seller is fraudulent without evidence.

Search: {search_name}
Listing title: {item.get('title', '')}
Asking price: {price if price is not None else 'Unknown'}
Marketplace baseline: {baseline_text}
Rule score: {rule_score}/100
Rule reasons: {', '.join(rule_reasons) if rule_reasons else 'None'}
Listing text:
{str(item.get('text', ''))[:2200]}
"""

    content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    image_url = str(item.get("image_url") or "").strip()
    if use_image and image_url.startswith(("http://", "https://")):
        content.append({"type": "input_image", "image_url": image_url, "detail": "low"})

    body = {
        "model": model.strip() or "gpt-5-mini",
        "store": False,
        "input": [{"role": "user", "content": content}],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "marketplace_deal_analysis",
                "description": "A structured assessment of whether a Marketplace listing is a good value.",
                "schema": AI_SCHEMA,
                "strict": True,
            }
        },
        "max_output_tokens": 1200,
    }

    response = requests.post(
        OPENAI_RESPONSES_URL,
        headers=_headers(api_key),
        json=body,
        timeout=90,
    )
    if response.status_code >= 400:
        try:
            detail = response.json().get("error", {}).get("message", response.text)
        except Exception:
            detail = response.text
        raise RuntimeError(f"OpenAI API error {response.status_code}: {detail[:700]}")

    payload = response.json()
    text = _extract_output_text(payload)
    analysis = json.loads(text)

    analysis["deal_score"] = max(0, min(100, int(analysis.get("deal_score", 0))))
    analysis["confidence"] = max(0, min(100, int(analysis.get("confidence", 0))))
    analysis["positives"] = [str(x)[:220] for x in analysis.get("positives", [])[:5]]
    analysis["red_flags"] = [str(x)[:220] for x in analysis.get("red_flags", [])[:5]]
    analysis["summary"] = str(analysis.get("summary", ""))[:700]
    analysis["detected_item"] = str(analysis.get("detected_item", ""))[:250]
    return analysis
