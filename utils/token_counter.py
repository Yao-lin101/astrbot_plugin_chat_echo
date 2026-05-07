"""
Token 计数工具
记录 astrbot_plugin_chat_echo 插件每次 LLM 调用的 token 用量，
按群按天聚合存储，自动清理超过一年的旧数据。
"""
import json
import asyncio
from pathlib import Path
from datetime import date, timedelta
from typing import Dict, List, Optional

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
        self._dirty: Dict[str, bool] = {}
        self._save_task: Optional[asyncio.Task] = None
        self._running = False
    
    def _get_file(self, group_id: str) -> Path:
        return self.data_dir / f"{group_id}.json"
    
    def _load(self, group_id: str) -> dict:
        fp = self._get_file(group_id)
        if fp.exists():
            try:
                with open(str(fp), 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self._cleanup(data)
                return data
            except (json.JSONDecodeError, IOError):
                return {"days": {}}
        return {"days": {}}
    
    def _save(self, group_id: str):
        if group_id not in self._dirty:
            return
        fp = self._get_file(group_id)
        try:
            fp.parent.mkdir(parents=True, exist_ok=True)
            tmp = fp.with_suffix('.tmp')
            with open(str(tmp), 'w', encoding='utf-8') as f:
                json.dump({"days": self._cache.get(group_id, {})}, f, ensure_ascii=False)
            tmp.replace(fp)
            self._dirty[group_id] = False
        except IOError as e:
            logger.error(f"保存 token 数据失败 [{group_id}]: {e}")
    
    def _cleanup(self, data: dict):
        """清理超过保留天数的数据"""
        cutoff = str(date.today() - timedelta(days=DATA_RETENTION_DAYS))
        days = data.get("days", {})
        expired = [k for k in days if k < cutoff]
        for k in expired:
            del days[k]
    
    async def record(self, group_id: str, prompt: int, completion: int):
        """记录一次 token 用量"""
        today = str(date.today())
        if not hasattr(self, '_cache'):
            self._cache: Dict[str, dict] = {}
        if group_id not in self._cache:
            self._cache[group_id] = self._load(group_id).get("days", {})
        days = self._cache[group_id]
        if today not in days:
            days[today] = {"prompt": 0, "completion": 0, "total": 0}
        days[today]["prompt"] += prompt
        days[today]["completion"] += completion
        days[today]["total"] += (prompt + completion)
        self._dirty[group_id] = True
    
    # ======== 查询接口 ========
    
    def _get_period_range(self, period: str):
        today = date.today()
        if period == "day":
            return str(today), str(today)
        elif period == "week":
            start = today - timedelta(days=today.weekday())
            return str(start), str(today)
        elif period == "month":
            return str(today.replace(day=1)), str(today)
        elif period == "year":
            return str(today - timedelta(days=365)), str(today)
        else:  # "all"
            return "2000-01-01", str(today)
    
    async def get_group_stats(self, group_id: str, period: str = "all") -> dict:
        """获取单个群统计"""
        # 如果脏了先刷盘确保一致性
        if self._dirty.get(group_id):
            self._save(group_id)
        data = self._load(group_id)
        days = data.get("days", {})
        start, end = self._get_period_range(period)
        prompt = completion = total = 0
        for ds, v in days.items():
            if start <= ds <= end:
                prompt += v.get("prompt", 0)
                completion += v.get("completion", 0)
                total += v.get("total", 0)
        return {"group_id": group_id, "prompt": prompt, "completion": completion, "total": total}
    
    async def get_daily(self, group_id: str, days_count: int = 30) -> List[dict]:
        """获取最近 N 天逐日数据"""
        if self._dirty.get(group_id):
            self._save(group_id)
        data = self._load(group_id)
        days = data.get("days", {})
        today = date.today()
        result = []
        for i in range(days_count):
            d = str(today - timedelta(days=days_count - 1 - i))
            v = days.get(d, {"prompt": 0, "completion": 0, "total": 0})
            result.append({"date": d, "prompt": v["prompt"], "completion": v["completion"], "total": v["total"]})
        return result
    
    async def get_all_groups_daily(self, days_count: int = 30) -> List[dict]:
        """获取所有群最近 N 天的逐日数据（用于多线图）"""
        groups = []
        for fp in self.data_dir.glob("*.json"):
            gid = fp.stem
            if self._dirty.get(gid):
                self._save(gid)
            daily = await self.get_daily(gid, days_count)
            if any(d["total"] > 0 for d in daily):
                groups.append({"group_id": gid, "daily": daily})
        # 按总 token 排序
        groups.sort(key=lambda g: sum(d["total"] for d in g["daily"]), reverse=True)
        return groups
    
    async def get_all_groups_summary(self, period: str = "all") -> List[dict]:
        """获取所有群汇总排行"""
        groups = []
        for fp in self.data_dir.glob("*.json"):
            gid = fp.stem
            stats = await self.get_group_stats(gid, period)
            if stats["total"] > 0:
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
            "group_count": len(groups)
        }
    
    # ======== 持久化 ========
    
    async def flush_all(self):
        """刷所有脏数据"""
        for gid in list(self._dirty.keys()):
            if self._dirty.get(gid):
                self._save(gid)
    
    def start(self):
        if self._save_task and not self._save_task.done():
            return
        self._running = True
        self._cache: Dict[str, dict] = {}
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
