"""评论冷却记录：同视频冷却 + 全局间隔。持久化 JSON。"""

from __future__ import annotations

import json
import time
from pathlib import Path


class CooldownStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._data: dict = {"per_video": {}, "last_global": 0.0}
        self._load()

    def _load(self):
        try:
            if self.path.exists():
                d = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    self._data = d
        except Exception:
            self._data = {"per_video": {}, "last_global": 0.0}

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    def in_video_cooldown(self, bvid: str, hours: float) -> float:
        """返回剩余秒数，0 表示可评。"""
        if hours <= 0:
            return 0.0
        ts = self._data.get("per_video", {}).get(str(bvid), 0)
        left = ts + hours * 3600 - time.time()
        return max(0.0, left)

    def in_global_cooldown(self, minutes: float) -> float:
        if minutes <= 0:
            return 0.0
        left = self._data.get("last_global", 0.0) + minutes * 60 - time.time()
        return max(0.0, left)

    def mark(self, bvid: str):
        self._data.setdefault("per_video", {})[str(bvid)] = time.time()
        self._data["last_global"] = time.time()
        # 简单清理：只留最近 200 条
        pv = self._data["per_video"]
        if len(pv) > 200:
            for k in sorted(pv, key=lambda x: pv[x])[: len(pv) - 200]:
                pv.pop(k, None)
        self._save()
