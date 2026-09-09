"""会话记录器：把一次 Agent 运行的过程持久化到 storage。"""
import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .base import StorageBackend


class SessionRecorder:
    """封装单个 session 的落盘逻辑，路径约定为 sessions/<session_id>/*。

    持久化文件：
    - meta.json: 会话元信息（goal/status/created_at/finished_at）
    - plan.json: 当前 plan 快照（每步完成后实时更新，用于中断恢复）
    - trace.jsonl: 每步的 ReAct 原始 trace（全量，用于审计/调试）
    - history.jsonl: 每步压缩后的 history 条目（跨 step 共享上下文）
    - metrics.jsonl: 度量数据
    """

    def __init__(self, storage: StorageBackend, session_id: str):
        self.storage = storage
        self.session_id = session_id
        self._prefix = f"sessions/{session_id}"

    def record_meta(self, goal: str, status: str = "running") -> None:
        # 保留既有字段（如 workspace_writes），避免覆盖
        meta = self.storage.read_json(f"{self._prefix}/meta.json") or {}
        meta.update({
            "session_id": self.session_id,
            "goal": goal,
            "created_at": meta.get("created_at") or datetime.now().isoformat(timespec="seconds"),
            "status": status,
        })
        self.storage.write_json(f"{self._prefix}/meta.json", meta)

    def record_plan(self, plan: Any) -> None:
        """plan 可为 Plan 对象或已序列化的 dict。每步状态变化后应调用以实时更新。"""
        data = plan.model_dump() if hasattr(plan, "model_dump") else plan
        self.storage.write_json(f"{self._prefix}/plan.json", data)

    def record_trace(self, entry: dict) -> None:
        self.storage.append_line(
            f"{self._prefix}/trace.jsonl", json.dumps(entry, ensure_ascii=False, default=str)
        )

    def record_history_entry(self, entry: dict) -> None:
        """追加一条压缩后的 history 条目（每步完成后调用）。

        与 trace.jsonl 的分工：
        - trace.jsonl: ReAct 原始全量轨迹（thought/action/observation/final），用于审计
        - history.jsonl: 每步压缩后的跨 step 共享上下文（step_id/description/success/output），
          用于中断恢复时重建完整的 step 历史，避免"恢复后历史 step 全丢弃"
        """
        self.storage.append_line(
            f"{self._prefix}/history.jsonl",
            json.dumps(entry, ensure_ascii=False, default=str),
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

    def is_interrupted(self) -> bool:
        """判断上次 run 是否被中断（status == 'interrupted'）。"""
        return self.load_meta().get("status") == "interrupted"

    def load_plan(self) -> Optional[dict]:
        """加载上次持久化的 plan 快照（用于中断恢复）。

        返回原始 dict（Plan.model_dump() 格式），由调用方反序列化；
        无 plan.json 时返回 None。
        """
        return self.storage.read_json(f"{self._prefix}/plan.json")

    def load_history_entries(self) -> List[dict]:
        """从 history.jsonl 加载每步压缩后的 history 条目（中断恢复用）。

        与 load_history() 的区别：
        - load_history_entries(): 读 history.jsonl，返回每个已完成 step 的真实历史记录
          （含真实 step_id / description / success / output），用于中断恢复时
          重建完整的多步上下文；
        - load_history(): 从 trace.jsonl 只提取最后一条 final，生成单条"prior"摘要，
          用于正常多轮对话的轻量上下文注入。
        """
        text = self.storage.read_text(f"{self._prefix}/history.jsonl")
        if not text:
            return []
        entries: List[dict] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries

    def load_history(self) -> List[dict]:
        """从 trace.jsonl 重建 history 条目，供后续 run 注入上下文（正常多轮对话用）。

        trace.jsonl 每行是 ReActEngine.last_trace 的原始条目（全量保留，用于审计）：
          {thought: ...} / {action: {tool_name, args}, observation: str} / {final: str}

        重建时只提取最后一条 final 作为上轮 step 的交付结论（output）。
        ReAct 内部的所有中间过程（包括工具调用、用户反馈等）均不进会话上下文，
        这些信息保留在 trace.jsonl 中用于审计/调试。

        注意：本方法用于正常多轮对话场景，只注入"上轮最后一句"作为轻量上下文。
        中断恢复场景应使用 load_history_entries() 获取完整的多步历史。
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

    # ── 中断恢复 ──────────────────────────────────────────

    def load_interrupted_plan(self) -> Optional[dict]:
        """加载中断的 plan（用于恢复执行）。

        返回 Plan 的原始字典（Plan.model_dump() 格式），
        由 AgentLoop 反序列化为 Plan 对象。
        """
        return self.load_plan()

    def load_history_from_file(self) -> List[dict]:
        """从 history.jsonl 加载完整的历史记录（用于中断恢复）。

        与 load_history() 的区别：
        - load_history(): 从 trace.jsonl 提取最后一条 final，生成单条摘要（用于多轮对话）
        - load_history_from_file(): 从 history.jsonl 加载所有已完成 step 的完整历史（用于中断恢复）
        """
        text = self.storage.read_text(f"{self._prefix}/history.jsonl")
        if not text:
            return []
        entries = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                entries.append(entry)
            except json.JSONDecodeError:
                continue
        return entries
