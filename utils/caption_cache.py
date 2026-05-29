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
            # Auto-migrate: add image_url column if it doesn't exist
            cursor.execute("PRAGMA table_info(caption_cache)")
            columns = {row[1] for row in cursor.fetchall()}
            if "image_url" not in columns:
                cursor.execute(
                    "ALTER TABLE caption_cache ADD COLUMN image_url TEXT DEFAULT ''"
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

    def set(self, img_hash: str, caption: str, image_url: str = ""):
        """Insert or replace a cached caption in SQLite database."""
        try:
            conn = sqlite3.connect(str(self.db_path))
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO caption_cache (img_hash, caption, created_at, image_url) VALUES (?, ?, ?, ?)",
                (img_hash, caption, time.time(), image_url),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Failed to write to caption cache: {e}")

    def get_all(self, offset: int = 0, limit: int = 20, search: str = "") -> list[dict]:
        """Retrieve paginated cache entries, ordered by creation time descending.

        Args:
            offset: Number of entries to skip.
            limit: Maximum number of entries to return.
            search: Optional keyword to filter by caption content (case-insensitive LIKE match).
        """
        try:
            conn = sqlite3.connect(str(self.db_path))
            cursor = conn.cursor()
            if search:
                cursor.execute(
                    "SELECT img_hash, caption, created_at, image_url FROM caption_cache WHERE caption LIKE ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (f"%{search}%", limit, offset),
                )
            else:
                cursor.execute(
                    "SELECT img_hash, caption, created_at, image_url FROM caption_cache ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                )
            rows = cursor.fetchall()
            conn.close()
            return [
                {
                    "img_hash": row[0],
                    "caption": row[1],
                    "created_at": row[2],
                    "image_url": row[3] or "",
                }
                for row in rows
            ]
        except Exception as e:
            logger.error(f"Failed to query caption cache list: {e}")
            return []

    def get_count(self, search: str = "") -> int:
        """Return total number of cached entries, optionally filtered by search keyword."""
        try:
            conn = sqlite3.connect(str(self.db_path))
            cursor = conn.cursor()
            if search:
                cursor.execute(
                    "SELECT COUNT(*) FROM caption_cache WHERE caption LIKE ?",
                    (f"%{search}%",),
                )
            else:
                cursor.execute("SELECT COUNT(*) FROM caption_cache")
            count = cursor.fetchone()[0]
            conn.close()
            return count
        except Exception as e:
            logger.error(f"Failed to get caption cache count: {e}")
            return 0

    def get_db_size(self) -> int:
        """Return database file size in bytes."""
        try:
            if self.db_path.exists():
                return self.db_path.stat().st_size
        except Exception as e:
            logger.error(f"Failed to get caption cache DB size: {e}")
        return 0

    def delete(self, img_hash: str) -> bool:
        """Delete a single cache entry by hash."""
        try:
            conn = sqlite3.connect(str(self.db_path))
            cursor = conn.cursor()
            cursor.execute("DELETE FROM caption_cache WHERE img_hash = ?", (img_hash,))
            deleted = cursor.rowcount > 0
            conn.commit()
            conn.close()
            return deleted
        except Exception as e:
            logger.error(f"Failed to delete caption cache entry: {e}")
            return False

    def delete_before(self, timestamp: float) -> int:
        """Delete all cache entries created before the given timestamp. Returns the number of deleted entries."""
        try:
            conn = sqlite3.connect(str(self.db_path))
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM caption_cache WHERE created_at < ?", (timestamp,)
            )
            deleted = cursor.rowcount
            conn.commit()
            conn.close()
            return deleted
        except Exception as e:
            logger.error(f"Failed to delete old caption cache entries: {e}")
            return 0

    def clear(self) -> int:
        """Clear all cache entries. Returns the number of deleted entries."""
        try:
            conn = sqlite3.connect(str(self.db_path))
            cursor = conn.cursor()
            cursor.execute("DELETE FROM caption_cache")
            deleted = cursor.rowcount
            conn.commit()
            conn.close()
            return deleted
        except Exception as e:
            logger.error(f"Failed to clear caption cache: {e}")
            return 0

    def update_caption(self, img_hash: str, new_caption: str) -> bool:
        """Update the caption text for a given image hash."""
        try:
            conn = sqlite3.connect(str(self.db_path))
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE caption_cache SET caption = ? WHERE img_hash = ?",
                (new_caption, img_hash),
            )
            updated = cursor.rowcount > 0
            conn.commit()
            conn.close()
            return updated
        except Exception as e:
            logger.error(f"Failed to update caption cache: {e}")
            return False
