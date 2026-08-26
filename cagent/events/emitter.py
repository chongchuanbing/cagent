"""事件总线：订阅/发布 + SSE 序列化投递。"""
import queue
import threading
from typing import Any, Callable, Dict, Generator, List, Optional

from .schema import AgentEvent, EventType

# 订阅者签名：接收一条 AgentEvent
EventHandler = Callable[[AgentEvent], None]


def to_sse(event: AgentEvent) -> str:
    """将事件序列化为标准 SSE 消息。

    ```
    event: tool_call
    data: {"type":"tool_call","seq":3,...}

    ```
    Web 端可直接挂在 HTTP 响应流上；CLI 端也可按此协议自消费。
    """
    return f"event: {event.type.value}\ndata: {event.model_dump_json()}\n\n"


class EventEmitter:
    """进程内事件发布/订阅中心。

    - 任意接入层可 `subscribe(handler)` 实时消费事件；
    - `iter_sse()` 为 Web 端提供 SSE 生成器（生成器存活期间发布的事件会被 yield）；
    - seq 自动递增，事件顺序可重放。
    """

    def __init__(self):
        self._handlers: List[EventHandler] = []
        self._lock = threading.Lock()
        self._seq = 0
        # 默认会话上下文：设置后 emit 未显式传 session_id 时自动附带
        self.default_session_id: Optional[str] = None

    # ---------- 订阅 ----------
    def subscribe(self, handler: EventHandler) -> None:
        """注册事件订阅者。handler 会被同步调用。"""
        with self._lock:
            self._handlers.append(handler)

    def unsubscribe(self, handler: EventHandler) -> None:
        with self._lock:
            if handler in self._handlers:
                self._handlers.remove(handler)

    # ---------- 发布 ----------
    def emit(
        self,
        type: EventType,
        payload: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
        step_id: Optional[str] = None,
    ) -> AgentEvent:
        """发布一条事件并返回该事件对象。"""
        with self._lock:
            self._seq += 1
            event = AgentEvent(
                type=type,
                seq=self._seq,
                session_id=session_id or self.default_session_id,
                step_id=step_id,
                payload=payload or {},
            )
            handlers = list(self._handlers)
        for h in handlers:
            try:
                h(event)
            except Exception:  # noqa: BLE001 —— 订阅者异常不影响主流程
                pass
        return event

    # ---------- SSE 流式投递 ----------
    def iter_sse(self) -> Generator[str, None, None]:
        """阻塞式 SSE 生成器：在该生成器被迭代期间，所有事件实时 yield。

        用于 Web 端流式响应（如 FastAPI StreamingResponse）。
        """
        q: "queue.Queue[AgentEvent]" = queue.Queue()
        with self._lock:
            self._handlers.append(q.put)
        try:
            while True:
                yield to_sse(q.get())
        finally:
            with self._lock:
                if q.put in self._handlers:
                    self._handlers.remove(q.put)
