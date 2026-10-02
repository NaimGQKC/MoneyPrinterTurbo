"""Content Plan execution controller.

Endpoint
--------
POST /api/v1/content-plan
    Receives the weekly JSON content plan from the Content Strategist
    (via GHL workflow AI Agent step) and distributes to each stream:
      - MPT  : submits faceless reel scripts to the local /api/v1/videos endpoint
      - HeyGen: returns the formatted avatar script so the GHL workflow can email it
      - Pedra : returns the chosen listing_reference so the GHL workflow can tag the contact
      - Blog  : placeholder (GHL blog agent called from workflow side)

The endpoint returns a flat JSON object so GHL workflow steps can
reference individual fields directly (e.g. {{response.heygen_script}}).
"""

import json
import logging
import os

import httpx
from fastapi import APIRouter, HTTPException, Request

router = APIRouter()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_SELF_BASE = "http://localhost:8080"          # internal self-call for MPT
_MPT_API_KEY = os.environ.get("MPT_API_KEY", "nadia-mpt-2026")
_MPT_VOICE = "es-ES-ElviraNeural"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_plan(body: dict) -> dict:
    """Accept plan as nested dict or as a JSON string under 'plan' key."""
    raw = body.get("plan", body)
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"Cannot parse plan JSON: {exc}")
    return raw


async def _submit_mpt(script: str, title: str) -> dict:
    """Fire a reel task to the MPT endpoint and return task_id or error."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{_SELF_BASE}/api/v1/videos",
                headers={"x-api-key": _MPT_API_KEY, "Content-Type": "application/json"},
                json={
                    "video_subject": title,
                    "video_script": script,
                    "voice_name": _MPT_VOICE,
                },
            )
        data = resp.json()
        return {"status": "submitted", "task_id": data.get("task_id", ""), "title": title}
    except Exception as exc:  # noqa: BLE001
        logger.exception("MPT submit failed for '%s'", title)
        return {"status": "error", "detail": str(exc), "title": title}


def _format_heygen_email(item: dict) -> str:
    """Return a plain-text HeyGen script block ready to paste into an email body."""
    brief = item.get("creative_brief", {})
    prod = item.get("production_instructions", {})
    dist = item.get("distribution_notes", {})

    lines = [
        f"SCRIPT HEYGEN -- {item.get('title', '')}",
        "",
        f"PILAR: {item.get('pillar', '')} | AUDIENCIA: {item.get('audience_segment', '')}",
        f"CANAL PRINCIPAL: {dist.get('primary_channel', '')} | DIA SUGERIDO: {dist.get('suggested_posting_day', '')}",
        "",
        "-- HOOK (primeros 3 segundos) --",
        brief.get("key_message", ""),
        "",
        "-- GUION COMPLETO --",
        prod.get("script_direction", ""),
        "",
        "-- CTA --",
        brief.get("cta", ""),
        "",
        "-- INSTRUCCIONES HEYGEN --",
        prod.get("visual_direction", ""),
        f"Duracion: {prod.get('length_or_word_count', '60-90 seg')}",
        "",
        "-- CAPTION (redes) --",
        dist.get("caption_angle", ""),
        "",
        "RE/MAX Confort Zaragoza -- Plan semanal automatico",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main endpoint
# ---------------------------------------------------------------------------

@router.post("/api/v1/content-plan")
async def execute_content_plan(request: Request):
    """
    Receive the Content Strategist JSON plan and distribute to each stream.

    Returns a flat dict so GHL workflow output-variable mapping works:
      - heygen_script   : formatted script text (for email action in workflow)
      - heygen_title    : short title
      - pedra_listing_ref : listing reference number (for GHL tag action)
      - mpt_tasks       : list of {task_id, title} for submitted reels
      - blog_title      : blog post title
      - blog_brief      : full blog production instructions
      - week            : ISO week start date
      - data_gaps       : comma-separated data gaps from planner
    """
    body = await request.json()
    plan = _extract_plan(body)

    result: dict = {
        "heygen_script": "",
        "heygen_title": "",
        "pedra_listing_ref": "",
        "mpt_tasks": [],
        "blog_title": "",
        "blog_brief": "",
        "week": plan.get("week", {}).get("start_date", ""),
        "data_gaps": ", ".join(plan.get("data_gaps", [])),
    }

    mpt_tasks = []

    for item in plan.get("content_plan", []):
        fmt = item.get("format", "")

        if fmt == "nadia_avatar_video_heygen":
            result["heygen_title"] = item.get("title", "")
            result["heygen_script"] = _format_heygen_email(item)

        elif fmt == "faceless_educational_reel_mpt":
            script = item.get("production_instructions", {}).get("script_direction", "")
            title = item.get("title", "Reel")
            task = await _submit_mpt(script, title)
            mpt_tasks.append(task)

        elif fmt == "property_listing_video_pedra":
            listings = (
                plan.get("inputs_reviewed", {})
                .get("available_listings", {})
                .get("priority_listings", [])
            )
            if listings:
                result["pedra_listing_ref"] = listings[0].get("listing_reference", "")
            if not result["pedra_listing_ref"]:
                result["pedra_listing_ref"] = item.get("local_zaragoza_hook", "")

        elif fmt == "blog_post":
            prod = item.get("production_instructions", {})
            result["blog_title"] = item.get("title", "")
            result["blog_brief"] = (
                f"Titulo: {item.get('title', '')}\n"
                f"Angulo: {item.get('core_angle', '')}\n"
                f"Problema del vendedor: {item.get('seller_problem_addressed', '')}\n"
                f"Hook local: {item.get('local_zaragoza_hook', '')}\n"
                f"Instrucciones: {prod.get('script_direction', '')}\n"
                f"CTA: {item.get('creative_brief', {}).get('cta', '')}\n"
                f"Palabras: {prod.get('length_or_word_count', '800-1200')}"
            )

    result["mpt_tasks"] = mpt_tasks
    return result
