"""
Token 计数工具
记录 astrbot_plugin_chat_echo 插件每次 LLM 调用的 token 用量，
按群按天聚合存储，自动清理超过一年的旧数据。
"""

import asyncio
import json
from datetime import date, timedelta
from pathlib import Path

from astrbot.api import logger

# 数据保留天数
DATA_RETENTION_DAYS = 365
# 后台保存间隔（秒）
SAVE_INTERVAL = 600


class TokenCounter:
    """Token 计数器
    每个群一个 JSON 文件，按天聚合存储。
    """

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir) / "token_stats"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, dict] = {}  # 修复：在 __init__ 中初始化
        self._dirty: dict[str, bool] = {}
        self._save_task: asyncio.Task | None = None
        self._running = False
        # 群名持久化
        self._group_names: dict[str, str] = {}
        self._names_file = self.data_dir / "group_names.json"
        self._load_group_names()
        self._names_dirty = False

    def _load_group_names(self):
        """从磁盘加载群名缓存"""
        if self._names_file.exists():
            try:
                with open(str(self._names_file), encoding="utf-8") as f:
                    self._group_names = json.load(f)
            except (OSError, json.JSONDecodeError):
                self._group_names = {}

    def _save_group_names(self):
        """持久化群名到磁盘"""
        try:
            with open(str(self._names_file), "w", encoding="utf-8") as f:
                json.dump(self._group_names, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.error(f"保存群名失败: {e}")

    def set_group_name(self, group_id: str, group_name: str):
        """更新群名（直接覆盖旧名）"""
        if not group_name:
            return
        old = self._group_names.get(group_id)
        if old != group_name:
            self._group_names[group_id] = group_name
            self._names_dirty = True

    def get_group_name(self, group_id: str) -> str:
        """获取缓存中的群名，若无则返回群{id}"""
        return self._group_names.get(group_id, f"群{group_id}")

    def _get_file(self, group_id: str) -> Path:
        return self.data_dir / f"{group_id}.json"

    def _load(self, group_id: str) -> dict:
        """从磁盘读取数据，优先使用缓存并回填缓存"""
        if group_id in self._cache:
            return {"days": self._cache[group_id]}
        fp = self._get_file(group_id)
        days = {}
        if fp.exists():
            try:
                with open(str(fp), encoding="utf-8") as f:
                    data = json.load(f)
                self._cleanup(group_id, data)
                days = data.get("days", {})
            except (OSError, json.JSONDecodeError):
                days = {}
        self._cache[group_id] = days
        return {"days": days}

    def _save(self, group_id: str):
        """保存指定群的数据到磁盘"""
        if group_id not in self._dirty or not self._dirty.get(group_id):
            return
        days_data = self._cache.get(group_id, {})
        # 防御：如果数据为空则不保存，避免覆盖有效文件
        if not days_data:
            return
        fp = self._get_file(group_id)
        try:
            fp.parent.mkdir(parents=True, exist_ok=True)
            tmp = fp.with_suffix(".tmp")
            with open(str(tmp), "w", encoding="utf-8") as f:
                json.dump({"days": days_data}, f, ensure_ascii=False)
            tmp.replace(fp)
            self._dirty[group_id] = False
        except OSError as e:
            logger.error(f"保存 token 数据失败 [{group_id}]: {e}")

    def _cleanup(self, group_id: str, data: dict):
        """清理超过保留天数的数据并标记 dirty 以落盘"""
        cutoff = str(date.today() - timedelta(days=DATA_RETENTION_DAYS))
        days = data.get("days", {})
        expired = [k for k in days if k < cutoff]
        if expired:
            for k in expired:
                del days[k]
            self._dirty[group_id] = True

    async def record(self, group_id: str, prompt: int, completion: int):
        """记录一次 token 用量"""
        today = str(date.today())
        if group_id not in self._cache:
            self._cache[group_id] = self._load(group_id).get("days", {})
        days = self._cache[group_id]
        if today not in days:
            days[today] = {"prompt": 0, "completion": 0, "total": 0}
        days[today]["prompt"] += prompt
        days[today]["completion"] += completion
        days[today]["total"] += prompt + completion
        self._dirty[group_id] = True

    # ======== 查询接口 ========

    def _get_period_range(self, period: str):
        today = date.today()
        if period == "day":
            return str(today), str(today)
        elif period == "yesterday":
            return str(today - timedelta(days=1)), str(today - timedelta(days=1))
        elif period == "week":
            start = today - timedelta(days=today.weekday())
            return str(start), str(today)
        elif period == "month":
            return str(today.replace(day=1)), str(today)
        elif period == "year":
            # 改为"近一年"而非"本年"
            return str(today - timedelta(days=365)), str(today)
        else:  # "all"
            return "2000-01-01", str(today)

    async def get_group_stats(self, group_id: str, period: str = "all") -> dict:
        """获取单个群统计"""
        data = self._load(group_id)
        days = data.get("days", {})
        start, end = self._get_period_range(period)
        prompt = completion = total = 0
        for ds, v in days.items():
            if start <= ds <= end:
                prompt += v.get("prompt", 0)
                completion += v.get("completion", 0)
                total += v.get("total", 0)
        return {
            "group_id": group_id,
            "prompt": prompt,
            "completion": completion,
            "total": total,
        }

    async def get_daily(self, group_id: str, days_count: int = 30) -> list[dict]:
        """获取最近 N 天逐日数据（优先从缓存读取，若无则加载）"""
        self._load(group_id)
        days = self._cache.get(group_id, {})
        today = date.today()
        result = []
        for i in range(days_count):
            d = str(today - timedelta(days=days_count - 1 - i))
            v = days.get(d, {"prompt": 0, "completion": 0, "total": 0})
            result.append(
                {
                    "date": d,
                    "prompt": v["prompt"],
                    "completion": v["completion"],
                    "total": v["total"],
                }
            )
        return result

    async def get_all_groups_daily(self, days_count: int = 30) -> list[dict]:
        """获取所有群最近 N 天的逐日数据（用于多线图）"""
        groups = []
        # 从缓存而非磁盘读取
        for gid, days in self._cache.items():
            daily = []
            today = date.today()
            for i in range(days_count):
                d = str(today - timedelta(days=days_count - 1 - i))
                v = days.get(d, {"prompt": 0, "completion": 0, "total": 0})
                daily.append(
                    {
                        "date": d,
                        "prompt": v["prompt"],
                        "completion": v["completion"],
                        "total": v["total"],
                    }
                )
            if any(d["total"] > 0 for d in daily):
                groups.append(
                    {
                        "group_id": gid,
                        "group_name": self.get_group_name(gid),
                        "daily": daily,
                    }
                )
        # 补充磁盘中有但缓存未加载的群
        for fp in self.data_dir.glob("*.json"):
            gid = fp.stem
            if gid in self._cache or gid == "group_names":
                continue
            data = self._load(gid)
            days = data.get("days", {})
            self._cache[gid] = days
            daily = []
            today = date.today()
            for i in range(days_count):
                d = str(today - timedelta(days=days_count - 1 - i))
                v = days.get(d, {"prompt": 0, "completion": 0, "total": 0})
                daily.append(
                    {
                        "date": d,
                        "prompt": v["prompt"],
                        "completion": v["completion"],
                        "total": v["total"],
                    }
                )
            if any(d["total"] > 0 for d in daily):
                groups.append(
                    {
                        "group_id": gid,
                        "group_name": self.get_group_name(gid),
                        "daily": daily,
                    }
                )
        # 按总 token 排序
        groups.sort(key=lambda g: sum(d["total"] for d in g["daily"]), reverse=True)
        return groups

    async def get_all_groups_summary(self, period: str = "all") -> list[dict]:
        """获取所有群汇总排行"""
        groups = []
        for gid in list(self._cache.keys()):
            stats = await self.get_group_stats(gid, period)
            if stats["total"] > 0:
                stats["group_name"] = self.get_group_name(gid)
                groups.append(stats)
        # 补充磁盘中有但缓存未加载的群
        for fp in self.data_dir.glob("*.json"):
            gid = fp.stem
            if gid in self._cache or gid == "group_names":
                continue
            data = self._load(gid)
            days = data.get("days", {})
            self._cache[gid] = days
            stats = await self.get_group_stats(gid, period)
            if stats["total"] > 0:
                stats["group_name"] = self.get_group_name(gid)
                groups.append(stats)
        groups.sort(key=lambda x: x["total"], reverse=True)
        return groups

    async def get_global_total(self, period: str = "all") -> dict:
        """全局总计"""
        groups = await self.get_all_groups_summary(period)
        return {
            "total": sum(g["total"] for g in groups),
            "prompt": sum(g["prompt"] for g in groups),
            "completion": sum(g["completion"] for g in groups),
            "group_count": len(groups),
        }

    # ======== 持久化 ========

    async def flush_all(self):
        """刷所有脏数据"""
        for gid in list(self._dirty.keys()):
            if self._dirty.get(gid):
                self._save(gid)
        if self._names_dirty:
            self._save_group_names()
            self._names_dirty = False

    def start(self):
        if self._save_task and not self._save_task.done():
            return
        self._running = True
        self._save_task = asyncio.create_task(self._loop())

    async def stop(self):
        self._running = False
        if self._save_task and not self._save_task.done():
            self._save_task.cancel()
            try:
                await self._save_task
            except (asyncio.CancelledError, Exception):
                pass
            self._save_task = None
        await self.flush_all()

    async def _loop(self):
        try:
            while self._running:
                await asyncio.sleep(SAVE_INTERVAL)
                await self.flush_all()
        except asyncio.CancelledError:
            await self.flush_all()
            raise
