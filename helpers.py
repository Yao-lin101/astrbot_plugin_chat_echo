import random

from astrbot.api.event import AstrMessageEvent
from astrbot.core.message.components import Image as ImageComponent


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
