import hashlib
import os
import sqlite3
import time
from pathlib import Path

import httpx

from astrbot.api import logger


class ImageCaptionCache:
    """SQLite-based cache for image captions to avoid repeating LLM calls for duplicate images."""

    def __init__(self, data_dir: Path):
        self.db_path = data_dir / "image_caption_cache.db"
        self._init_db()

    def _init_db(self):
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path))
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS caption_cache (
                    img_hash TEXT PRIMARY KEY,
                    caption TEXT,
                    created_at REAL
                )
                """
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Failed to initialize SQLite caption cache: {e}")

    async def get_hash(self, image_url: str) -> str:
        """Calculate MD5 hash of local files or remote image downloads."""
        # 1. Check if it's a local file path
        if os.path.exists(image_url):
            try:
                with open(image_url, "rb") as f:
                    data = f.read()
                    return hashlib.md5(data).hexdigest()
            except Exception:
                pass

        # 2. Check if it's a remote URL
        if image_url.startswith("http://") or image_url.startswith("https://"):
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(image_url)
                    if resp.status_code == 200:
                        return hashlib.md5(resp.content).hexdigest()
            except Exception as e:
                logger.warning(f"Failed to download remote image for hashing: {e}")

        # Fallback: MD5 hash of the URL string itself
        return hashlib.md5(image_url.encode("utf-8")).hexdigest()

    def get(self, img_hash: str) -> str | None:
        """Retrieve a cached caption from SQLite database."""
        try:
            conn = sqlite3.connect(str(self.db_path))
            cursor = conn.cursor()
            cursor.execute(
                "SELECT caption FROM caption_cache WHERE img_hash = ?",
                (img_hash,),
            )
            row = cursor.fetchone()
            conn.close()
            if row:
                return row[0]
        except Exception as e:
            logger.error(f"Failed to query caption cache: {e}")
        return None

    def set(self, img_hash: str, caption: str):
        """Insert or replace a cached caption in SQLite database."""
        try:
            conn = sqlite3.connect(str(self.db_path))
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO caption_cache (img_hash, caption, created_at) VALUES (?, ?, ?)",
                (img_hash, caption, time.time()),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Failed to write to caption cache: {e}")
