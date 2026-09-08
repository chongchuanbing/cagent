"""FastAPI 路由：REST API + SSE 事件流。

数据模型：
- Workspace：项目空间（对应本地一个目录）
- Session：会话（一次任务执行）
"""
import asyncio
import os
from pathlib import Path
from typing import List, Optional
from datetime import datetime

from fastapi import APIRouter, Request, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .agent_service import AgentService

router = APIRouter()


# ---------- 文件系统浏览（供目录选择器使用）----------

def _dir_entry(p: Path) -> dict:
    return {"name": p.name or str(p), "path": str(p)}


@router.get("/filesystem/roots")
async def list_roots():
    """列出可选的根目录（按平台返回，避免重复）。"""
    home = Path.home()
    roots: List[dict] = [{"name": "主目录", "path": str(home)}]

    if os.name == "nt":
        # Windows
        drive = os.path.splitdrive(str(home))[0] or "C:"
        roots.insert(0, {"name": f"本地磁盘 ({drive})", "path": drive + os.sep})
        for extra in ("D:\\", "E:\\"):
            if Path(extra).exists():
                roots.append({"name": f"磁盘 ({extra[:2]})", "path": extra})
    elif Path("/Users").exists() and Path("/System").exists():
        # macOS
        roots.insert(0, {"name": "Macintosh HD", "path": "/"})
        roots.append({"name": "Users", "path": "/Users"})
        if Path("/Volumes").exists():
            roots.append({"name": "Volumes", "path": "/Volumes"})
    else:
        # Linux / 其他 POSIX
        roots.insert(0, {"name": "根目录", "path": "/"})
        if Path("/home").exists() and str(home) != "/home":
            roots.append({"name": "home", "path": "/home"})
        if Path("/mnt").exists():
            roots.append({"name": "mnt", "path": "/mnt"})

    # 去重（保留首次出现）
    seen, uniq = set(), []
    for r in roots:
        if r["path"] not in seen:
            seen.add(r["path"])
            uniq.append(r)
    return uniq


@router.get("/filesystem/browse")
async def browse_directory(path: str = Query(default="")):
    """浏览指定目录下的子目录（只返回目录，不返回文件）。

    返回：当前路径、父路径、面包屑、子目录列表。
    """
    target = Path(path) if path else Path.home()
    target = target.expanduser()
    if not target.is_absolute():
        target = (Path.home() / target).resolve()

    if not target.exists():
        raise HTTPException(status_code=404, detail=f"路径不存在: {target}")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail=f"不是目录: {target}")

    # 子目录（跳过隐藏目录与常见噪音目录）
    SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv",
            ".idea", ".vscode", "Library", ".Trash", ".cache"}
    children = []
    try:
        for entry in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            if not entry.is_dir():
                continue
            if entry.name.startswith(".") or entry.name in SKIP:
                continue
            children.append(_dir_entry(entry))
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"无权限访问: {target}")

    # 面包屑（相对于根的分段）
    parts = [p for p in target.parts if p not in ("/", "")]
    crumbs, acc = [], ""
    for i, seg in enumerate(parts):
        acc = acc + "/" + seg if acc else "/" + seg
        crumbs.append({"name": seg, "path": acc, "index": i})

    parent = str(target.parent) if target.parent != target else None

    return {
        "current": str(target),
        "parent": parent,
        "crumbs": crumbs,
        "children": children,
    }


class OpenFolderRequest(BaseModel):
    path: str


@router.post("/filesystem/open")
async def open_folder(req: OpenFolderRequest):
    """在系统文件管理器中打开指定目录（跨平台）。"""
    import platform
    import subprocess

    target = Path(req.path).expanduser()
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"路径不存在: {target}")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail=f"不是目录: {target}")

    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.Popen(["open", str(target)])
        elif system == "Windows":
            os.startfile(str(target))  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", str(target)])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"打开失败: {e}")

    return {"ok": True, "path": str(target)}


# ---------- 请求/响应模型 ----------

class SessionCreateRequest(BaseModel):
    goal: str


class MessageResponse(BaseModel):
    id: str
    role: str
    content: str
    status: Optional[str] = None
    createdAt: str


class SessionResponse(BaseModel):
    id: str
    title: str
    status: str
    goal: str
    updatedAt: str
    workspaceId: Optional[str] = None  # None 表示未关联任何空间


# ---------- 工作空间 ----------

WORKSPACES = {
    "cagent": {
        "id": "cagent",
        "name": "cagent",
        "path": "/Users/chongcb/Documents/git_workspace/Agent框架/cagent",
    },
    "cloud10086": {
        "id": "cloud10086",
        "name": "cloud10086",
        "path": "~/projects/cloud10086",
    },
}


@router.get("/workspaces")
async def list_workspaces():
    """列出所有工作空间。"""
    return list(WORKSPACES.values())


def _create_session(goal: str, workspace_id: Optional[str], service: AgentService) -> SessionResponse:
    """内部：创建会话并启动 Agent 执行。"""
    session_id = service.create_task(goal)
    service.run_task_async(session_id)
    task = service.get_task(session_id)
    return SessionResponse(
        id=task.id,
        title=goal[:30],
        status=task.status,
        goal=goal,
        updatedAt="刚刚",
        workspaceId=workspace_id,
    )


@router.post("/sessions", response_model=SessionResponse)
async def create_session_unassigned(req: SessionCreateRequest, request: Request):
    """创建不关联任何工作空间的会话（自由提问）。"""
    service: AgentService = request.app.state.agent_service
    return _create_session(req.goal, None, service)


@router.post("/workspaces/{workspace_id}/sessions", response_model=SessionResponse)
async def create_session(workspace_id: str, req: SessionCreateRequest, request: Request):
    """在工作空间中创建新会话并启动执行。

    空间由前端（localStorage）管理，后端不校验其是否存在，
    仅把 workspace_id 作为归属标记记录在会话上。
    """
    service: AgentService = request.app.state.agent_service
    return _create_session(req.goal, workspace_id, service)


@router.get("/workspaces/{workspace_id}/sessions", response_model=List[SessionResponse])
async def list_sessions(workspace_id: str):
    """列出工作空间的所有会话（当前实现是内存中的活跃会话）。"""
    if workspace_id not in WORKSPACES:
        raise HTTPException(status_code=404, detail="Workspace not found")
    # TODO: 从存储中读取历史会话
    return []


# ---------- 会话 ----------

@router.get("/sessions/{session_id}/messages", response_model=List[MessageResponse])
async def get_session_messages(session_id: str, request: Request):
    """获取会话消息列表。"""
    service: AgentService = request.app.state.agent_service
    task = service.get_task(session_id)
    if not task:
        raise HTTPException(status_code=404, detail="Session not found")

    messages = [
        MessageResponse(
            id=f"user-{session_id}",
            role="user",
            content=task.goal,
            createdAt=datetime.now().isoformat(),
        )
    ]
    if task.result:
        messages.append(MessageResponse(
            id=f"assistant-{session_id}",
            role="assistant",
            content=task.result,
            status=task.status,
            createdAt=datetime.now().isoformat(),
        ))
    return messages


@router.get("/sessions/{session_id}/events")
async def stream_events(session_id: str, request: Request):
    """SSE 事件流：实时推送会话执行过程中的事件。"""
    service: AgentService = request.app.state.agent_service
    task = service.get_task(session_id)
    if not task:
        raise HTTPException(status_code=404, detail="Session not found")

    queue = await service.subscribe_events(session_id)

    async def event_generator():
        try:
            while True:
                event = await queue.get()
                event_type = event.get("type", "message")
                yield f"event: {event_type}\ndata: {event}\n\n"
                if event_type in ("final_answer", "error"):
                    break
        except asyncio.CancelledError:
            pass
        finally:
            if queue in task.subscribers:
                task.subscribers.remove(queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ---------- 兼容旧接口（Tasks） ----------

class TaskCreateRequest(BaseModel):
    goal: str


class TaskResponse(BaseModel):
    id: str
    goal: str
    status: str
    result: Optional[str] = None


@router.post("/tasks", response_model=TaskResponse)
async def create_task(req: TaskCreateRequest, request: Request):
    service: AgentService = request.app.state.agent_service
    task_id = service.create_task(req.goal)
    service.run_task_async(task_id)
    task = service.get_task(task_id)
    return TaskResponse(id=task.id, goal=task.goal, status=task.status, result=task.result)


@router.get("/tasks", response_model=List[TaskResponse])
async def list_tasks(request: Request):
    service: AgentService = request.app.state.agent_service
    return [
        TaskResponse(id=t.id, goal=t.goal, status=t.status, result=t.result)
        for t in service.list_tasks()
    ]


@router.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str, request: Request):
    service: AgentService = request.app.state.agent_service
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return TaskResponse(id=task.id, goal=task.goal, status=task.status, result=task.result)