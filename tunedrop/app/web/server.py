from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import logging

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from tunedrop.app.core.config import settings
from tunedrop.app.services.link_generator import link_store
from tunedrop.app.utils.time_utils import estimate_download_time, format_bytes, format_seconds

logger = logging.getLogger(__name__)


BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

_http_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global _http_client
    _http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=15, read=120, write=30, pool=10),
        follow_redirects=True,
    )
    try:
        yield
    finally:
        await _http_client.aclose()
        _http_client = None


def _get_client() -> httpx.AsyncClient:
    if _http_client is None:
        raise RuntimeError("HTTP client is not initialized.")
    return _http_client


def create_web_app() -> FastAPI:
    app = FastAPI(title="Telegram Music Downloader", lifespan=_lifespan)

    # Security headers middleware
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return response

    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/generate/{ref}")
    async def generate_download_link(ref: str):
        if not ref or len(ref) > 64:
            raise HTTPException(status_code=400, detail="Invalid reference")
        link = await link_store.resolve_ref(ref)
        if not link:
            raise HTTPException(status_code=404, detail="Download reference not found")
        return RedirectResponse(url=link, status_code=307)

    @app.get("/download/{token}", response_class=HTMLResponse)
    async def download_page(request: Request, token: str):
        if not token or len(token) > 64:
            raise HTTPException(status_code=400, detail="Invalid token")
        item = await link_store.get(token)
        if not item:
            raise HTTPException(status_code=404, detail="File not found")

        if item.get("expired"):
            context = {
                "request": request,
                "file_name": item.get("file_name", "Unknown"),
                "expired": True,
                "ads_enabled": settings.ads_enabled,
                "ads_desktop_top_banner": settings.ads_desktop_top_banner,
                "ads_desktop_inline_banner": settings.ads_desktop_inline_banner,
                "ads_mobile_top_banner": settings.ads_mobile_top_banner,
                "ads_mobile_bottom_banner": settings.ads_mobile_bottom_banner,
            }
            return templates.TemplateResponse("download.html", context)

        size_bytes = int(item.get("file_size", 0))
        context = {
            "request": request,
            "file_name": item.get("file_name", "Unknown"),
            "size_text": format_bytes(size_bytes),
            "speed_kbps": f"{settings.download_speed_kbps:.0f}",
            "estimated_time": format_seconds(estimate_download_time(size_bytes, settings.download_speed_kbps)),
            "direct_link": f"/file/{token}",
            "expires_at": item.get("expires_at"),
            "ads_enabled": settings.ads_enabled,
            "ads_desktop_top_banner": settings.ads_desktop_top_banner,
            "ads_desktop_inline_banner": settings.ads_desktop_inline_banner,
            "ads_mobile_top_banner": settings.ads_mobile_top_banner,
            "ads_mobile_bottom_banner": settings.ads_mobile_bottom_banner,
        }
        return templates.TemplateResponse("download.html", context)

    @app.get("/file/{token}")
    async def direct_file(token: str):
        if not token or len(token) > 64:
            raise HTTPException(status_code=400, detail="Invalid token")
        item = await link_store.get(token)
        if not item:
            raise HTTPException(status_code=404, detail="File not found")
        if item.get("expired"):
            raise HTTPException(status_code=410, detail="Download link has expired")

        file_id = item.get("file_id")
        if not file_id:
            raise HTTPException(status_code=404, detail="File data incomplete")
        file_name = item.get("file_name", "download.zip")
        file_size = item.get("file_size")

        file_url = await resolve_telegram_file_url(file_id)
        if file_url is None:
            raise HTTPException(status_code=502, detail="Failed to fetch file from Telegram")

        headers = {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(file_name, safe='')}
        if file_size is not None:
            headers["Content-Length"] = str(int(file_size))

        return StreamingResponse(
            stream_telegram_file(file_url),
            media_type="application/octet-stream",
            headers=headers,
        )

    return app


async def resolve_telegram_file_url(file_id: str) -> str | None:
    client = _get_client()
    try:
        api_url = f"https://api.telegram.org/bot{settings.bot_token}/getFile"
        response = await client.get(api_url, params={"file_id": file_id})
        response.raise_for_status()
    except httpx.HTTPStatusError:
        logger.warning("Telegram getFile API error for file_id=%s", file_id)
        return None
    except httpx.RequestError:
        logger.warning("Network error fetching Telegram file URL for file_id=%s", file_id)
        return None
    try:
        payload = response.json()
    except ValueError:
        logger.warning("Invalid JSON from Telegram getFile API for file_id=%s", file_id)
        return None
    if not payload.get("ok"):
        return None
    result = payload.get("result")
    if not result or not isinstance(result, dict):
        return None
    file_path = result.get("file_path")
    if not file_path:
        return None
    if ".." in file_path or file_path.startswith("/"):
        logger.warning("Suspicious file_path from Telegram API: %s", file_path[:80])
        return None
    return f"https://api.telegram.org/file/bot{settings.bot_token}/{file_path}"


async def stream_telegram_file(url: str):
    client = _get_client()
    try:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes(chunk_size=65536):
                yield chunk
    except (httpx.HTTPStatusError, httpx.ReadError):
        logger.warning("Telegram file stream error for %s", url[:80])
    except httpx.RequestError:
        logger.warning("Network error while streaming Telegram file")
