"""会话记录器：把一次 Agent 运行的过程持久化到 storage。"""
import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from .base import StorageBackend


class SessionRecorder:
    """封装单个 session 的落盘逻辑，路径约定为 sessions/<session_id>/*。"""

    def __init__(self, storage: StorageBackend, session_id: str):
        self.storage = storage
        self.session_id = session_id
        self._prefix = f"sessions/{session_id}"

    def record_meta(self, goal: str, status: str = "running") -> None:
        self.storage.write_json(
            f"{self._prefix}/meta.json",
            {
                "session_id": self.session_id,
                "goal": goal,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "status": status,
            },
        )

    def record_plan(self, plan: Any) -> None:
        """plan 可为 Plan 对象或已序列化的 dict。"""
        data = plan.model_dump() if hasattr(plan, "model_dump") else plan
        self.storage.write_json(f"{self._prefix}/plan.json", data)

    def record_trace(self, entry: dict) -> None:
        self.storage.append_line(
            f"{self._prefix}/trace.jsonl", json.dumps(entry, ensure_ascii=False, default=str)
        )

    def finish(self, status: str = "done") -> None:
        meta = self.storage.read_json(f"{self._prefix}/meta.json") or {}
        meta["status"] = status
        meta["finished_at"] = datetime.now().isoformat(timespec="seconds")
        self.storage.write_json(f"{self._prefix}/meta.json", meta)

    def record_workspace_write(self, path: str, op: str) -> None:
        """登记一次空间（workspace://）写入，供事后审计与 resume 追溯。

        path 为相对空间根的路径；op 为操作类型（add/update/delete/move）。
        """
        meta = self.storage.read_json(f"{self._prefix}/meta.json") or {}
        writes = meta.setdefault("workspace_writes", [])
        entry = {
            "path": path,
            "op": op,
            "ts": datetime.now().isoformat(timespec="seconds"),
        }
        writes.append(entry)
        meta["workspace_writes"] = writes
        self.storage.write_json(f"{self._prefix}/meta.json", meta)

    def record_metrics(self, turn_metrics) -> None:
        """记录单次 turn 的度量数据（追加写入 metrics.jsonl）。

        Args:
            turn_metrics: TurnMetrics 对象，包含本次 run 的完整统计
        """
        self.storage.append_line(
            f"{self._prefix}/metrics.jsonl",
            turn_metrics.model_dump_json()
        )

    # ── 会话恢复 ──────────────────────────────────────────

    def load_meta(self) -> dict:
        """读取会话元信息。"""
        return self.storage.read_json(f"{self._prefix}/meta.json") or {}

    def load_history(self) -> List[dict]:
        """从 trace.jsonl 重建 history 条目，供后续 run 注入上下文。

        trace.jsonl 每行是 ReActEngine.last_trace 的原始条目（全量保留，用于审计）：
          {thought: ...} / {action: {tool_name, args}, observation: str} / {final: str}

        重建时只提取最后一条 final 作为上轮 step 的交付结论（output）。
        ReAct 内部的所有中间过程（包括工具调用、用户反馈等）均不进会话上下文，
        这些信息保留在 trace.jsonl 中用于审计/调试。
        """
        text = self.storage.read_text(f"{self._prefix}/trace.jsonl")
        if not text:
            return []
        raw_entries: List[dict] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw_entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        if not raw_entries:
            return []

        # 提取最后一条 final 作为交付结论
        final_output = ""
        for entry in raw_entries:
            if "final" in entry:
                final_output = entry["final"]

        meta = self.load_meta()
        return [{
            "step_id": "prior",
            "description": meta.get("goal", ""),
            "success": True,
            "output": final_output,
        }]

    def load_last_answer(self) -> str:
        """从 trace.jsonl 读取最后一条 final 作为上轮答案。"""
        text = self.storage.read_text(f"{self._prefix}/trace.jsonl")
        if not text:
            return ""
        last_final = ""
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if "final" in entry:
                    last_final = entry["final"]
            except json.JSONDecodeError:
                continue
        return last_final
