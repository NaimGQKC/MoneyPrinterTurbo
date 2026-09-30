"""Pedra listing-video controller.

Exposes two endpoints:
  POST /api/v1/pedra/videos   – start a video generation task (returns task_id immediately)
  GET  /api/v1/pedra/tasks/{task_id} – poll status / get video_url when done

The Cloud Run container downloads listing photos server-side (bypassing the IP
block that prevents Pedra from fetching fotos15.apinmo.com directly), converts
them to base64 data-URIs, and calls the Pedra synchronous API in a background
thread.  Pedra can take up to 10 minutes; callers should poll every 30-60 s.
"""

import base64
import os
import threading
from typing import List, Optional

import requests as req_lib
from fastapi import Depends, Path, Request
from loguru import logger
from pydantic import BaseModel

from app.controllers import base
from app.controllers.v1.base import new_router
from app.models import const
from app.models.exception import HttpException
from app.services import state as sm
from app.utils import utils

router = new_router(dependencies=[Depends(base.verify_token)])

PEDRA_API_KEY = os.environ.get("PEDRA_API_KEY", "3bA5tnOAdXPEPbwy6dJUHeKwTXfSGP38")
PEDRA_BASE_URL = "https://app.pedra.ai/api"
PEDRA_TIMEOUT_S = 660  # 11 min — Pedra says up to 10 min

INMOVILLA_BASE = "https://fotos15.apinmo.com"
INMOVILLA_AGENCY = int(os.environ.get("INMOVILLA_AGENCY", "12930"))

_EFFECTS = ["zoom-in", "zoom-out", "pan-right", "pan-left"]
_PHOTO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.remaxconfort.es/",
    "Accept": "image/webp,image/jpeg,*/*",
}


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class PedraImageInput(BaseModel):
    imageUrl: str
    effect: Optional[str] = "zoom-in"
    title: Optional[str] = None


class PedraVideoRequest(BaseModel):
    # Option A: caller supplies explicit photo URLs (fotos15 or any public URL)
    images: Optional[List[PedraImageInput]] = None
    # Option B: supply property_id and we discover / download photos automatically
    property_id: Optional[str] = None
    photo_numbers: Optional[List[int]] = None   # override auto-discovery
    # Branding
    ending_title: Optional[str] = "Nadia Rouchdi \u00b7 RE/MAX Confort"
    ending_subtitle: Optional[str] = "+34 649 605 404"
    property_characteristics: Optional[List[dict]] = None
    is_vertical: Optional[bool] = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_base64(url: str) -> str:
    """Download an image URL and return a base64 data-URI."""
    resp = req_lib.get(url, headers=_PHOTO_HEADERS, timeout=30, allow_redirects=True)
    resp.raise_for_status()
    ct = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
    b64 = base64.b64encode(resp.content).decode()
    return f"data:{ct};base64,{b64}"


def _discover_photos(property_id: str) -> List[int]:
    """Probe fotos15.apinmo.com HEAD requests to find valid photo numbers.

    Returns up to 10 photo numbers.  Stops early after 12 consecutive misses
    once at least 2 photos have been found.
    """
    base_url = f"{INMOVILLA_BASE}/{INMOVILLA_AGENCY}/{property_id}"
    found: List[int] = []
    consec = 0
    for n in range(1, 150):
        url = f"{base_url}/{n}-1.jpg"
        try:
            r = req_lib.head(url, headers=_PHOTO_HEADERS, timeout=4, allow_redirects=True)
            if r.status_code == 200:
                found.append(n)
                consec = 0
                if len(found) >= 10:
                    break
            else:
                consec += 1
        except Exception:
            consec += 1
        if consec >= 12 and len(found) >= 2:
            break
    return found


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

def _run_pedra_task(task_id: str, req: PedraVideoRequest) -> None:
    try:
        sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=5)

        images: List[dict] = []

        if req.images:
            for i, img in enumerate(req.images):
                try:
                    entry: dict = {
                        "imageUrl": _to_base64(img.imageUrl),
                        "effect": img.effect or _EFFECTS[i % 4],
                    }
                    if img.title:
                        entry["title"] = img.title
                    images.append(entry)
                except Exception as exc:
                    logger.warning(f"[pedra:{task_id}] skip explicit image #{i}: {exc}")

        elif req.property_id:
            sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=10)
            nums = req.photo_numbers if req.photo_numbers else _discover_photos(req.property_id)
            logger.info(f"[pedra:{task_id}] discovered photo numbers: {nums}")
            for i, n in enumerate(nums):
                url = f"{INMOVILLA_BASE}/{INMOVILLA_AGENCY}/{req.property_id}/{n}-1.jpg"
                try:
                    images.append({
                        "imageUrl": _to_base64(url),
                        "effect": _EFFECTS[i % 4],
                    })
                except Exception as exc:
                    logger.warning(f"[pedra:{task_id}] skip photo {n}: {exc}")

        if len(images) < 2:
            raise ValueError(f"Only {len(images)} valid image(s) collected, need \u22652")

        logger.info(f"[pedra:{task_id}] sending {len(images)} images to Pedra")
        sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=35)

        body: dict = {
            "apiKey": PEDRA_API_KEY,
            "images": images,
            "is_vertical": req.is_vertical,
            "ending_title": req.ending_title,
            "ending_subtitle": req.ending_subtitle,
        }
        if req.property_characteristics:
            body["property_characteristics"] = req.property_characteristics

        sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=40)

        resp = req_lib.post(
            f"{PEDRA_BASE_URL}/create_video",
            json=body,
            timeout=PEDRA_TIMEOUT_S,
        )

        if resp.status_code != 200:
            raise ValueError(f"Pedra HTTP {resp.status_code}: {resp.text[:400]}")

        data = resp.json()
        video_url: Optional[str] = None
        out = data.get("output")
        if isinstance(out, str):
            video_url = out
        elif isinstance(out, list) and out:
            video_url = out[0]
        if not video_url:
            video_url = data.get("video_url") or data.get("url")
        if not video_url:
            raise ValueError(f"No video URL in Pedra response: {str(data)[:400]}")

        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            video_url=video_url,
        )
        logger.success(f"[pedra:{task_id}] done \u2192 {video_url}")

    except Exception as exc:
        logger.error(f"[pedra:{task_id}] failed: {exc}")
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_FAILED,
            progress=0,
            message=str(exc),
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/pedra/videos", summary="Start Pedra listing-video generation")
def create_pedra_video(request: Request, body: PedraVideoRequest):
    request_id = base.get_task_id(request)
    task_id = utils.get_uuid()

    if not body.images and not body.property_id:
        raise HttpException(
            task_id=task_id,
            status_code=400,
            message=f"{request_id}: provide 'images' list or 'property_id'",
        )

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=0)
    thread = threading.Thread(target=_run_pedra_task, args=(task_id, body), daemon=True)
    thread.start()

    task = {
        "task_id": task_id,
        "request_id": request_id,
        "state": const.TASK_STATE_PROCESSING,
        "progress": 0,
    }
    logger.success(f"[pedra] task queued: {task_id}")
    return utils.get_response(200, task)


@router.get("/pedra/tasks/{task_id}", summary="Poll Pedra video task status")
def get_pedra_task(
    request: Request,
    task_id: str = Path(..., description="task_id from POST /pedra/videos"),
):
    request_id = base.get_task_id(request)
    task = sm.state.get_task(task_id)
    if task:
        return utils.get_response(200, task)
    raise HttpException(
        task_id=task_id,
        status_code=404,
        message=f"{request_id}: pedra task not found",
    )
