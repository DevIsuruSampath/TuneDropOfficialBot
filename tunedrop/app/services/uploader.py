from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pyrogram import Client
from pyrogram.types import Message

from tunedrop.app.core.config import settings


@dataclass(slots=True)
class UploadedFile:
    message_id: int
    file_id: str
    file_name: str
    file_size: int


async def upload_zip_to_storage(app: Client, zip_path: Path, caption: str, chat_id: int | None = None) -> UploadedFile:
    target_chat_id = chat_id or settings.private_channel_id
    if not target_chat_id:
        raise RuntimeError("No storage channel configured. Set PRIVATE_CHANNEL_ID or pass chat_id.")
    if not zip_path.exists():
        raise FileNotFoundError(f"ZIP file not found: {zip_path}")
    message: Message = await app.send_document(
        chat_id=target_chat_id,
        document=str(zip_path),
        file_name=zip_path.name,
        caption=caption,
    )
    document = message.document
    if document is None:
        raise RuntimeError("Telegram did not return document metadata.")
    return UploadedFile(
        message_id=message.id,
        file_id=document.file_id,
        file_name=document.file_name or zip_path.name,
        file_size=document.file_size or zip_path.stat().st_size,
    )
