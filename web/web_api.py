from quart import jsonify
from quart import request as qreq

PLUGIN_NAME = "astrbot_plugin_chat_echo"


class EchoWebApi:
    def __init__(self, plugin):
        self.plugin = plugin
        self.context = plugin.context
        self.logger = plugin.logger
        self.token_counter = plugin.token_counter
        self.caption_cache = plugin.caption_cache
        self.config_helper = plugin.config_helper

    def register_routes(self):
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/token_stats",
            self.page_token_stats,
            ["GET"],
            "Token 统计数据",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/token_history",
            self.page_token_history,
            ["GET"],
            "历史趋势数据（多群多线）",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/caption_cache",
            self.api_caption_cache_list,
            ["GET"],
            "图片转述缓存列表",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/caption_cache/stats",
            self.api_caption_cache_stats,
            ["GET"],
            "图片转述缓存统计",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/caption_cache/delete",
            self.api_caption_cache_delete,
            ["POST"],
            "删除单条转述缓存",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/caption_cache/clear",
            self.api_caption_cache_clear,
            ["POST"],
            "清空全部转述缓存",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/caption_cache/clear_before",
            self.api_caption_cache_clear_before,
            ["POST"],
            "按时间清理转述缓存",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/caption_cache/update",
            self.api_caption_cache_update,
            ["POST"],
            "更新转述内容",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/personas",
            self.api_personas_list,
            ["GET"],
            "获取系统人格列表",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/persona_prompts",
            self.api_persona_prompts_get,
            ["GET"],
            "获取自定义人格提示词配置",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/persona_prompts",
            self.api_persona_prompts_set,
            ["POST"],
            "更新自定义人格提示词配置",
        )

    async def page_token_stats(self):
        try:
            await self.token_counter.flush_all()
            period = qreq.args.get("period", "all") if qreq else "all"
            global_total = await self.token_counter.get_global_total(period)
            groups = await self.token_counter.get_all_groups_summary(period)
            return jsonify(
                {"status": "ok", "data": {"global": global_total, "groups": groups}}
            )
        except Exception as e:
            self.logger.exception(f"Failed to get token stats: {e}")
            return jsonify({"status": "error", "message": str(e)})

    async def page_token_history(self):
        try:
            await self.token_counter.flush_all()
            days = int(qreq.args.get("days", 30)) if qreq else 30
            groups_data = await self.token_counter.get_all_groups_daily(min(days, 365))
            return jsonify({"status": "ok", "data": {"groups": groups_data}})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def api_caption_cache_list(self):
        """GET handler: paginated caption cache list with optional search."""
        try:
            offset = int(qreq.args.get("offset", 0)) if qreq else 0
            limit = int(qreq.args.get("limit", 20)) if qreq else 20
            search = qreq.args.get("search", "").strip() if qreq else ""
            limit = min(limit, 100)
            items = self.caption_cache.get_all(offset, limit, search=search)
            total = self.caption_cache.get_count(search=search)
            return jsonify(
                {
                    "status": "ok",
                    "data": {
                        "items": items,
                        "total": total,
                        "offset": offset,
                        "limit": limit,
                        "search": search,
                    },
                }
            )
        except Exception as e:
            self.logger.exception(f"Failed to list caption cache: {e}")
            return jsonify({"status": "error", "message": str(e)})

    async def api_caption_cache_stats(self):
        """GET handler: caption cache statistics."""
        try:
            count = self.caption_cache.get_count()
            db_size = self.caption_cache.get_db_size()
            return jsonify(
                {"status": "ok", "data": {"count": count, "db_size": db_size}}
            )
        except Exception as e:
            self.logger.exception(f"Failed to get caption cache stats: {e}")
            return jsonify({"status": "error", "message": str(e)})

    async def api_caption_cache_delete(self):
        """POST handler: delete a single cache entry."""
        try:
            body = await qreq.get_json()
            img_hash = body.get("img_hash", "") if body else ""
            if not img_hash:
                return jsonify({"status": "error", "message": "img_hash is required"})
            ok = self.caption_cache.delete(img_hash)
            return jsonify({"status": "ok", "deleted": ok})
        except Exception as e:
            self.logger.exception(f"Failed to delete caption cache entry: {e}")
            return jsonify({"status": "error", "message": str(e)})

    async def api_caption_cache_clear(self):
        """POST handler: clear all cache entries."""
        try:
            deleted = self.caption_cache.clear()
            self.logger.info(
                f"[CaptionCache] Cleared all entries, deleted {deleted} items."
            )
            return jsonify({"status": "ok", "deleted": deleted})
        except Exception as e:
            self.logger.exception(f"Failed to clear caption cache: {e}")
            return jsonify({"status": "error", "message": str(e)})

    async def api_caption_cache_clear_before(self):
        """POST handler: clear cache entries before a given timestamp."""
        try:
            body = await qreq.get_json()
            before = float(body.get("before", 0)) if body else 0
            if before <= 0:
                return jsonify(
                    {
                        "status": "error",
                        "message": "valid 'before' timestamp is required",
                    }
                )
            deleted = self.caption_cache.delete_before(before)
            self.logger.info(
                f"[CaptionCache] Cleared entries before {before}, deleted {deleted} items."
            )
            return jsonify({"status": "ok", "deleted": deleted})
        except Exception as e:
            self.logger.exception(f"Failed to clear old caption cache: {e}")
            return jsonify({"status": "error", "message": str(e)})

    async def api_caption_cache_update(self):
        """POST handler: update caption text for a cache entry."""
        try:
            body = await qreq.get_json()
            img_hash = body.get("img_hash", "") if body else ""
            caption = body.get("caption", "") if body else ""
            if not img_hash:
                return jsonify({"status": "error", "message": "img_hash is required"})
            ok = self.caption_cache.update_caption(img_hash, caption)
            return jsonify({"status": "ok", "updated": ok})
        except Exception as e:
            self.logger.exception(f"Failed to update caption cache: {e}")
            return jsonify({"status": "error", "message": str(e)})

    async def api_personas_list(self):
        """GET handler: return list of all system personas."""
        try:
            personas_list = []
            raw_personas = []
            if hasattr(self.context, "provider_manager") and hasattr(
                self.context.provider_manager, "personas"
            ):
                raw_personas = self.context.provider_manager.personas
            elif hasattr(self.context, "persona_manager") and hasattr(
                self.context.persona_manager, "personas_v3"
            ):
                raw_personas = self.context.persona_manager.personas_v3

            for p in raw_personas:
                if isinstance(p, dict):
                    name = p.get("name", "")
                    prompt = p.get("prompt", "")
                else:
                    name = getattr(p, "name", "")
                    prompt = getattr(p, "prompt", "")

                if name:
                    personas_list.append({"name": name, "id": name, "prompt": prompt})

            return jsonify(personas_list)
        except Exception as e:
            self.logger.exception(f"Failed to get personas list: {e}")
            return jsonify([])

    async def api_persona_prompts_get(self):
        """GET handler: return current custom persona prompt list."""
        try:
            persona_replies = self.config_helper.persona_replies() or []
            return jsonify(persona_replies)
        except Exception as e:
            self.logger.exception(f"Failed to get persona prompts: {e}")
            return jsonify([])

    async def api_persona_prompts_set(self):
        """POST handler: update or delete a custom persona prompt configuration."""
        try:
            body = await qreq.get_json()
            if not body:
                return jsonify({"status": "error", "message": "Request body is empty"})

            persona_name = body.get("persona_name", "").strip()
            custom_persona_prompt = body.get("custom_persona_prompt", "")

            if not persona_name:
                return jsonify(
                    {"status": "error", "message": "persona_name is required"}
                )

            # Get current persona prompts list
            persona_replies = self.config_helper.persona_replies() or []
            # Make sure it's a list we can mutate
            persona_replies = list(persona_replies)

            # Find if this persona already has an entry
            found_idx = -1
            for idx, entry in enumerate(persona_replies):
                if (
                    isinstance(entry, dict)
                    and entry.get("persona_name", "").lower() == persona_name.lower()
                ):
                    found_idx = idx
                    break

            if custom_persona_prompt.strip() == "":
                # Delete entry if prompt is empty
                if found_idx != -1:
                    persona_replies.pop(found_idx)
            else:
                # Add or update entry
                new_entry = {
                    "persona_name": persona_name,
                    "custom_persona_prompt": custom_persona_prompt,
                    "__template_key": "persona_reply_template",
                }
                if found_idx != -1:
                    persona_replies[found_idx] = new_entry
                else:
                    persona_replies.append(new_entry)

            # Save configuration
            if isinstance(self.plugin.config, dict):
                self.plugin.config["persona_replies"] = persona_replies
            else:
                setattr(self.plugin.config, "persona_replies", persona_replies)

            if hasattr(self.plugin.config, "save_config"):
                self.plugin.config.save_config()

            # Refresh config helper cache
            self.config_helper.refresh()

            return jsonify({"status": "ok"})
        except Exception as e:
            self.logger.exception(f"Failed to save persona prompt: {e}")
            return jsonify({"status": "error", "message": str(e)})
