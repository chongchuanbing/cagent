"""AgentService：Web 层的 Agent 封装，管理运行中的任务与事件流。"""
import asyncio
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from cagent.core import Agent
from cagent.config.provider import ConfigProvider
from cagent.tools.registry import ToolRegistry
from cagent.events import EventEmitter, EventType


@dataclass
class TaskState:
    """运行中的任务状态。"""
    id: str
    goal: str
    status: str = "running"  # running | completed | failed
    result: Optional[str] = None
    events: List[Dict[str, Any]] = field(default_factory=list)
    subscribers: List[asyncio.Queue] = field(default_factory=list)


class AgentService:
    """Web 层的 Agent 服务封装。

    - 管理多个并发任务（按 task_id 隔离）
    - 每个任务有独立的 EventEmitter 与事件队列
    - 提供订阅接口供 SSE 流式推送
    """

    def __init__(self):
        self._tasks: Dict[str, TaskState] = {}
        self._lock = threading.Lock()
        # 默认 Agent 实例（可复用配置）
        self._agent: Optional[Agent] = None

    def _get_agent(self) -> Agent:
        """懒加载 Agent 实例（使用默认配置）。"""
        if self._agent is None:
            config = ConfigProvider()  # 从 config/agent.yaml 加载
            tools = ToolRegistry()
            # TODO: 注册内置工具（如 calculator、ask_user 等）
            self._agent = Agent(tools=tools, config=config)
        return self._agent

    def create_task(self, goal: str) -> str:
        """创建新任务，返回 task_id。"""
        task_id = uuid.uuid4().hex[:8]
        with self._lock:
            self._tasks[task_id] = TaskState(id=task_id, goal=goal)
        return task_id

    def run_task_async(self, task_id: str) -> None:
        """在后台线程运行任务（非阻塞）。"""
        task = self._tasks.get(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")

        def _run():
            agent = self._get_agent()
            emitter = EventEmitter()

            # 订阅事件并转发给任务订阅者
            def _event_handler(event):
                event_dict = event.model_dump()
                task.events.append(event_dict)
                # 通知所有异步订阅者
                for q in task.subscribers:
                    try:
                        asyncio.run_coroutine_threadsafe(q.put(event_dict), q._loop)
                    except Exception:
                        pass

            emitter.subscribe(_event_handler)
            agent.emitter = emitter

            try:
                result = agent.run(goal=task.goal, session_id=task_id)
                task.result = result
                task.status = "completed"
            except Exception as e:
                task.status = "failed"
                task.result = f"Error: {e}"

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

    def get_task(self, task_id: str) -> Optional[TaskState]:
        """获取任务状态。"""
        return self._tasks.get(task_id)

    def list_tasks(self) -> List[TaskState]:
        """列出所有任务。"""
        return list(self._tasks.values())

    async def subscribe_events(self, task_id: str) -> asyncio.Queue:
        """订阅任务的事件流（返回异步队列，SSE 消费）。"""
        task = self._tasks.get(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")

        queue: asyncio.Queue = asyncio.Queue()
        queue._loop = asyncio.get_running_loop()
        task.subscribers.append(queue)
        return queue
