"""会话记录器：把一次 Agent 运行的过程持久化到 storage。"""
import json
from datetime import datetime
from typing import Any, List, Optional

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

    # ── 会话恢复 ──────────────────────────────────────────

    def load_meta(self) -> dict:
        """读取会话元信息。"""
        return self.storage.read_json(f"{self._prefix}/meta.json") or {}

    def load_history(self, max_obs_chars: int = 500) -> List[dict]:
        """从 trace.jsonl 重建 history 条目，供后续 run 注入上下文。

        trace 条目格式（由 AgentLoop._history_entry 产生）：
          {step_id, description, success, output, observations: [{tool, args, result}]}

        trace.jsonl 每行是 ReActEngine.last_trace 的原始条目：
          {thought: ...} / {action: Action, observation: str} / {final: str, unconverged?: bool}

        重建逻辑：按 action/final 分组，还原成 history 条目。
        """
        text = self.storage.read_text(f"{self._prefix}/trace.jsonl")
        if not text:
            return []
        # 收集原始 trace 条目
        raw_entries = []
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

        # 按最后一条 final 分段，每段构成一个 history 条目
        # 简化处理：把整个 trace 汇总为一条历史
        observations = []
        final_output = ""
        for entry in raw_entries:
            if "action" in entry:
                action = entry["action"]
                obs = entry.get("observation", "")
                if len(obs) > max_obs_chars:
                    obs = obs[:max_obs_chars] + f"…（已截断，原 {len(obs)} 字符）"
                observations.append({
                    "tool": action.get("tool_name", ""),
                    "args": action.get("args", {}),
                    "result": obs,
                })
            elif "final" in entry:
                final_output = entry["final"]

        meta = self.load_meta()
        return [{
            "step_id": "prior",
            "description": meta.get("goal", ""),
            "success": True,
            "output": final_output,
            "observations": observations,
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
