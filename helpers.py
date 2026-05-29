import random

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image as ImageComponent


async def extract_image_urls(event: AstrMessageEvent) -> list:
    """Extract list of image URLs or local file paths from message event asynchronously."""
    urls = []
    try:
        for comp in event.get_messages():
            if isinstance(comp, ImageComponent):
                if comp.url:
                    urls.append(comp.url)
                elif comp.file and (
                    comp.file.startswith("http://") or comp.file.startswith("https://")
                ):
                    urls.append(comp.file)
                else:
                    path = await comp.convert_to_file_path()
                    if path:
                        urls.append(path)
    except Exception:
        pass
    return urls


def is_group_event(event: AstrMessageEvent) -> bool:
    """Check if the event belongs to a group."""
    try:
        return bool(event.get_group_id())
    except (AttributeError, TypeError):
        return False


def extract_bot_text(response) -> str:
    """Extract text content from LLM response object or string."""
    if hasattr(response, "completion_text"):
        return response.completion_text or ""
    if hasattr(response, "text"):
        return response.text or ""
    if isinstance(response, str):
        return response
    return ""


def extract_sent_text(event: AstrMessageEvent) -> str:
    """Extract sent text from a sent message event result."""
    bot_text = ""
    try:
        result = event.get_result()
        if result and hasattr(result, "chain") and result.chain:
            for comp in result.chain:
                if hasattr(comp, "text"):
                    bot_text += comp.text or ""
                elif hasattr(comp, "content"):
                    bot_text += comp.content or ""
    except Exception:
        pass
    return bot_text


def is_probability_hit(prob: int) -> bool:
    """Determine if a random check falls within the specified probability."""
    return prob >= 100 or random.randint(1, 100) <= prob


async def compress_image_if_needed(image_url: str) -> str:
    """Downloads image if it is a remote URL, resizes and compresses it if it's too large,
    and returns a local path to the compressed image.
    If no compression is needed or it fails, returns the original image_url (or downloaded path).
    """
    import os
    import base64
    import uuid
    from io import BytesIO
    from pathlib import Path
    from PIL import Image as PILImage
    from astrbot import logger
    from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
    from astrbot.core.utils.io import download_image_by_url

    temp_dir = Path(get_astrbot_temp_path())
    temp_dir.mkdir(parents=True, exist_ok=True)

    is_downloaded = False
    local_path = None

    try:
        if image_url.startswith("http://") or image_url.startswith("https://"):
            local_path = await download_image_by_url(image_url)
            is_downloaded = True
        elif image_url.startswith("file://"):
            path_str = image_url.replace("file://localhost", "").replace("file://", "")
            local_path = path_str
        else:
            local_path = image_url

        if not local_path or not os.path.exists(local_path):
            return image_url

        file_size = os.path.getsize(local_path)

        with PILImage.open(local_path) as img:
            width, height = img.size

        MAX_DIMENSION = 1536
        MAX_FILE_SIZE = 1 * 1024 * 1024  # 1MB

        if file_size <= MAX_FILE_SIZE and max(width, height) <= MAX_DIMENSION:
            if is_downloaded:
                return local_path
            return image_url

        with PILImage.open(local_path) as img:
            if img.mode != "RGB":
                img = img.convert("RGB")
            if max(width, height) > MAX_DIMENSION:
                img.thumbnail(
                    (MAX_DIMENSION, MAX_DIMENSION), PILImage.Resampling.LANCZOS
                )

            compressed_path = temp_dir / f"chat_echo_compressed_{uuid.uuid4().hex}.jpg"
            img.save(compressed_path, format="JPEG", quality=85, optimize=True)

            if is_downloaded and local_path != str(compressed_path):
                try:
                    os.unlink(local_path)
                except Exception:
                    pass

            return str(compressed_path)
    except Exception as e:
        logger.warning(f"[ChatEcho] Failed to check or compress image {image_url}: {e}")
        return local_path if (local_path and os.path.exists(local_path)) else image_url
