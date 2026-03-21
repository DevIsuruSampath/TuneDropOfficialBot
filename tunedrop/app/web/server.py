from __future__ import annotations

from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from tunedrop.app.core.config import settings
from tunedrop.app.services.link_generator import link_store
from tunedrop.app.utils.time_utils import estimate_download_time, format_bytes, format_seconds


BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def create_web_app() -> FastAPI:
    app = FastAPI(title="Telegram Music Downloader")
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/download/{token}", response_class=HTMLResponse)
    async def download_page(request: Request, token: str):
        item = await link_store.get(token)
        if not item:
            raise HTTPException(status_code=404, detail="File not found")

        size_bytes = int(item["file_size"])
        context = {
            "request": request,
            "file_name": item["file_name"],
            "size_text": format_bytes(size_bytes),
            "speed_kbps": f"{settings.download_speed_kbps:.0f}",
            "estimated_time": format_seconds(estimate_download_time(size_bytes, settings.download_speed_kbps)),
            "direct_link": f"/file/{token}",
        }
        return templates.TemplateResponse("download.html", context)

    @app.get("/file/{token}")
    async def direct_file(token: str):
        item = await link_store.get(token)
        if not item:
            raise HTTPException(status_code=404, detail="File not found")

        file_id = item["file_id"]
        file_name = item.get("file_name", "download.zip")

        file_bytes = await fetch_telegram_file(file_id)
        if file_bytes is None:
            raise HTTPException(status_code=502, detail="Failed to fetch file from Telegram")

        return Response(
            content=file_bytes,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
        )

    return app


async def fetch_telegram_file(file_id: str) -> bytes | None:
    api_url = f"https://api.telegram.org/bot{settings.bot_token}/getFile"
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(api_url, params={"file_id": file_id})
        response.raise_for_status()
        payload = response.json()
    if not payload.get("ok"):
        return None
    file_path = payload["result"]["file_path"]
    file_url = f"https://api.telegram.org/file/bot{settings.bot_token}/{file_path}"
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.get(file_url)
        response.raise_for_status()
        return response.content
