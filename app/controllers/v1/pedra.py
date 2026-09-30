"""Pedra listing-video controller.

Endpoints
---------
POST /api/v1/pedra/videos
    Start a listing-video generation task. Returns {task_id} immediately.
    Optional ``callback_url``: when the video is ready (or failed), Cloud Run
    POSTs {task_id, state, video_url} to that URL — so GHL workflows don't
    need to poll; they just wait for the inbound webhook.

GET /api/v1/pedra/tasks/{task_id}
    Poll task status. Returns {state, progress, video_url} when done.
    state: 4=processing, 1=complete, -1=failed

Design notes
------------
- Photos are downloaded server-side (base64 data-URI) to bypass the IP block
  that prevents Pedra from fetching fotos15.apinmo.com directly.
- Auto-discovery probes HEAD requests up to photo #150 and returns up to 10
  valid photo numbers.
- The Pedra API is synchronous and can take up to 10 minutes; it runs in a
  daemon thread.
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
    # Option B: property_id — we auto-discover and download photos
    property_id: Optional[str] = None
    photo_numbers: Optional[List[int]] = None  # override auto-discovery
    # Branding
    ending_title: Optional[str] = "Nadia Rouchdi \u00b7 RE/MAX Confort"
    ending_subtitle: Optional[str] = "+34 649 605 404"
    property_characteristics: Optional[List[dict]] = None
    is_vertical: Optional[bool] = True
    # Callback: if set, we POST {task_id, state, video_url, message} here when done
    callback_url: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_base64(url: str) -> str:
    """Download an image and return a base64 data-URI string."""
    resp = req_lib.get(url, headers=_PHOTO_HEADERS, timeout=30, allow_redirects=True)
    resp.raise_for_status()
    ct = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
    b64 = base64.b64encode(resp.content).decode()
    return f"data:{ct};base64,{b64}"


def _discover_photos(property_id: str) -> List[int]:
    """Probe fotos15.apinmo.com HEAD requests to find valid photo numbers.

    Returns up to 10 photo numbers. Stops after 12 consecutive 404s once at
    least 2 photos have been found.
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


def _fire_callback(callback_url: str, payload: dict) -> None:
    """POST result payload to callback_url; swallow errors (best-effort)."""
    try:
        resp = req_lib.post(callback_url, json=payload, timeout=15)
        logger.info(f"[pedra] callback {callback_url} → {resp.status_code}")
    except Exception as exc:
        logger.warning(f"[pedra] callback failed: {exc}")


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

def _run_pedra_task(task_id: str, req: PedraVideoRequest) -> None:  # noqa: C901
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

        logger.info(f"[pedra:{task_id}] sending {len(images)} images to Pedra API")
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

        if req.callback_url:
            _fire_callback(req.callback_url, {
                "task_id": task_id,
                "state": const.TASK_STATE_COMPLETE,
                "video_url": video_url,
            })

    except Exception as exc:
        err = str(exc)
        logger.error(f"[pedra:{task_id}] failed: {err}")
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_FAILED,
            progress=0,
            message=err,
        )
        if req.callback_url:
            _fire_callback(req.callback_url, {
                "task_id": task_id,
                "state": const.TASK_STATE_FAILED,
                "message": err,
            })


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/pedra/videos", summary="Start Pedra listing-video generation")
def create_pedra_video(request: Request, body: PedraVideoRequest):
    """Kick off a Pedra video task. Returns task_id immediately.

    Supply ``callback_url`` and Cloud Run will POST the finished video_url
    back to that URL — no polling required from GHL workflows.
    """
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
    """Returns {state, progress, video_url} for a Pedra task.

    state: 4=processing  1=complete  -1=failed
    """
    request_id = base.get_task_id(request)
    task = sm.state.get_task(task_id)
    if task:
        return utils.get_response(200, task)
    raise HttpException(
        task_id=task_id,
        status_code=404,
        message=f"{request_id}: pedra task not found",
    )
