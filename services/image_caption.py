import asyncio
import os

from ..helpers import compress_image_if_needed, is_probability_hit


async def get_image_caption(
    plugin, image_url: str, umo: str, force: bool = False
) -> str:
    """Call LLM provider to get description/caption for a given image URL."""
    # Query cache first
    img_hash = await plugin.caption_cache.get_hash(image_url)
    cached = plugin.caption_cache.get(img_hash)
    if cached:
        plugin.logger.info(
            f"[ImageCache] Hit cache for image {image_url[:60]}... -> {cached[:30]}"
        )
        return cached

    # Check probability for new image captioning
    if not force and not is_probability_hit(
        plugin.config_helper.image_caption_probability()
    ):
        plugin.logger.info(
            f"[ImageCache] Cache miss for image {image_url[:60]}..., but skipped captioning due to probability constraint ({plugin.config_helper.image_caption_probability()}%)."
        )
        return ""

    provider_id = plugin.config_helper.image_caption_provider()
    global_cfg = plugin.context.get_config(umo=umo)

    # Fallback to global default image caption provider if not set in plugin
    if not provider_id:
        provider_id = global_cfg.get("provider_settings", {}).get(
            "default_image_caption_provider_id", ""
        )

    if not provider_id:
        plugin.logger.warning(
            "No image caption provider configured in plugin or global settings."
        )
        return ""

    prov = plugin.context.get_provider_by_id(provider_id)
    if prov is None:
        plugin.logger.error(f"Image caption provider '{provider_id}' not found.")
        return ""

    prompt = global_cfg.get("provider_settings", {}).get(
        "image_caption_prompt", "Please describe the image using Chinese."
    )

    compressed_url = image_url
    is_temp_file = False
    try:
        compressed_url = await compress_image_if_needed(image_url)
        if image_url.startswith("http://") or image_url.startswith("https://"):
            is_temp_file = True
        elif compressed_url != image_url:
            is_temp_file = True

        plugin.logger.debug(
            f"Requesting image caption from provider {provider_id} for URL {compressed_url}"
        )
        resp = await prov.text_chat(prompt=prompt, image_urls=[compressed_url])
        if resp and resp.completion_text:
            caption = resp.completion_text.strip()
            plugin.caption_cache.set(img_hash, caption, image_url=image_url)
            return caption
    except Exception as e:
        plugin.logger.exception(f"Failed to get image caption: {e}")
    finally:
        if is_temp_file and compressed_url and os.path.exists(compressed_url):
            try:
                os.unlink(compressed_url)
            except Exception:
                pass

    return ""


async def ensure_context_captions(
    plugin, messages: list[dict], umo: str
) -> list[asyncio.Task]:
    """Lazily caption any uncaptioned images in the message list in-place.
    Returns list of background caption tasks (fire-and-forget).
    """
    tasks = []
    if not plugin.config_helper.enable_image_caption():
        return tasks
    for msg in messages:
        image_urls = msg.get("image_urls")
        if image_urls and "[图片描述:" not in msg.get("content", ""):
            captions = []
            for url in image_urls:
                caption = await get_image_caption(plugin, url, umo, force=True)
                if caption:
                    captions.append(caption)
            if captions:
                msg["content"] += " " + " ".join(
                    f"[图片描述: {cap}]" for cap in captions
                )
    return tasks


async def prewarm_captions(plugin, msg: dict, umo: str) -> list[asyncio.Task]:
    """Start background image caption tasks for a single message.
    Returns list of tasks that will save captions into the cache.
    """
    tasks = []
    if not plugin.config_helper.enable_image_caption():
        return tasks
    image_urls = msg.get("image_urls", [])
    if not image_urls:
        return tasks
    for url in image_urls:
        # Fire-and-forget: start captioning in background, result goes to cache
        task = asyncio.create_task(get_image_caption(plugin, url, umo, force=True))
        tasks.append(task)
    return tasks
