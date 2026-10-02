from fastapi import APIRouter

from app.controllers.v1 import content_plan, pedra, video

root_api_router = APIRouter()
root_api_router.include_router(video.router)
root_api_router.include_router(pedra.router)
root_api_router.include_router(content_plan.router)
