import { useState, useEffect } from 'react'
import Sidebar, { type Workspace, type Session } from './components/Sidebar'
import ChatPanel from './components/ChatPanel'
import DirectoryPicker from './components/DirectoryPicker'
import type { Message } from './components/MessageBubble'
import './App.css'

export interface AgentEvent {
  type: string
  seq: number
  ts: string
  session_id: string | null
  step_id: string | null
  payload: Record<string, unknown>
}

/** 虚拟分组 id：会话不关联任何工作空间 */
export const UNASSIGNED_ID = '__unassigned__'

const WS_STORAGE_KEY = 'cagent.workspaces.v1'
const UNASSIGNED_STORAGE_KEY = 'cagent.unassigned.v1'

function loadWorkspaces(): Workspace[] {
  try {
    const raw = localStorage.getItem(WS_STORAGE_KEY)
    if (raw) return JSON.parse(raw)
  } catch {}
  return [
    {
      id: 'cagent',
      name: 'cagent',
      path: '/Users/chongcb/Documents/git_workspace/Agent框架/cagent',
      sessions: [],
    },
    {
      id: 'cloud10086',
      name: 'cloud10086',
      path: '~/projects/cloud10086',
      sessions: [],
    },
  ]
}

function loadUnassigned(): Session[] {
  try {
    const raw = localStorage.getItem(UNASSIGNED_STORAGE_KEY)
    if (raw) return JSON.parse(raw)
  } catch {}
  return []
}

function App() {
  const [workspaces, setWorkspaces] = useState<Workspace[]>(loadWorkspaces)
  // 不关联空间的会话（自由提问产生）
  const [unassignedSessions, setUnassignedSessions] = useState<Session[]>(loadUnassigned)
  // 新建任务默认不关联空间
  const [activeWorkspaceId, setActiveWorkspaceId] = useState<string>(UNASSIGNED_ID)
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [isStreaming, setIsStreaming] = useState(false)
  const [pickerOpen, setPickerOpen] = useState(false)

  useEffect(() => {
    localStorage.setItem(WS_STORAGE_KEY, JSON.stringify(workspaces))
  }, [workspaces])

  useEffect(() => {
    localStorage.setItem(UNASSIGNED_STORAGE_KEY, JSON.stringify(unassignedSessions))
  }, [unassignedSessions])

  // 侧边栏树：内置「未关联空间」置顶 + 用户空间
  const treeWorkspaces: Workspace[] = [
    {
      id: UNASSIGNED_ID,
      name: '未关联空间',
      path: '',
      sessions: unassignedSessions,
      builtin: true,
    },
    ...workspaces,
  ]

  const activeWorkspace = treeWorkspaces.find(w => w.id === activeWorkspaceId)
  const selectedSession = activeWorkspace?.sessions.find(s => s.id === selectedSessionId)

  useEffect(() => {
    if (!selectedSessionId) {
      setMessages([])
      return
    }
    fetch(`/api/sessions/${selectedSessionId}/messages`)
      .then(r => r.json())
      .then(setMessages)
      .catch(() => setMessages([]))
  }, [selectedSessionId])

  // 更新会话：未关联分组和用户空间都要找
  const updateSession = (sessionId: string, updater: (s: Session) => Session) => {
    setUnassignedSessions(prev =>
      prev.map(s => (s.id === sessionId ? updater(s) : s)),
    )
    setWorkspaces(prev =>
      prev.map(ws => ({
        ...ws,
        sessions: ws.sessions.map(s => (s.id === sessionId ? updater(s) : s)),
      })),
    )
  }

  // 切换工作空间：仅清空当前会话视图，不创建任何会话
  const handleSelectWorkspace = (id: string) => {
    setActiveWorkspaceId(id)
    setSelectedSessionId(null)
    setMessages([])
  }

  // 新建任务：只进入空白状态，等用户真正提问后才创建会话
  const handleNewSession = () => {
    setSelectedSessionId(null)
    setMessages([])
    setActiveWorkspaceId(UNASSIGNED_ID)
  }

  // 打开目录选择器（后端浏览真实文件系统，Web / 桌面端行为一致）
  const handleBrowseLocal = () => {
    setPickerOpen(true)
  }

  // 用户在目录选择器中选定目录后，创建（或复用）并切换到该工作空间
  const handleDirectorySelect = (path: string) => {
    const existing = workspaces.find(w => w.path === path)
    if (existing) {
      handleSelectWorkspace(existing.id)
      return
    }
    const segments = path.replace(/[\\/]+$/, '').split(/[\\/]/)
    const name = segments[segments.length - 1] || 'workspace'
    const id = `ws-${Date.now()}`
    setWorkspaces(prev => [...prev, { id, name, path, sessions: [] }])
    handleSelectWorkspace(id)
  }

  // 在系统文件管理器中打开空间目录
  const handleOpenFolder = async (ws: Workspace) => {
    try {
      const res = await fetch('/api/filesystem/open', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: ws.path }),
      })
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}))
        alert(`打开失败：${detail.detail || res.status}`)
      }
    } catch (e) {
      alert(`打开失败：${e instanceof Error ? e.message : '未知错误'}`)
    }
  }

  // 从列表删除空间（仅移除条目，不删磁盘文件）
  const handleDeleteWorkspace = (ws: Workspace) => {
    const sessionCount = ws.sessions.length
    const tip = sessionCount > 0
      ? `确定从列表移除空间「${ws.name}」吗？其下 ${sessionCount} 个会话将一并移除（不会删除磁盘文件）。`
      : `确定从列表移除空间「${ws.name}」吗？（不会删除磁盘文件）`
    if (!confirm(tip)) return

    setWorkspaces(prev => {
      const next = prev.filter(w => w.id !== ws.id)
      // 删掉的是当前活跃空间时，回到「未关联空间」
      if (ws.id === activeWorkspaceId) {
        setSelectedSessionId(null)
        setMessages([])
        setActiveWorkspaceId(UNASSIGNED_ID)
      }
      return next
    })
  }

  // 发送提问：此时才真正创建会话
  const handleSendMessage = async (text: string) => {
    if (isStreaming || !text.trim()) return

    const userMsg: Message = {
      id: `user-${Date.now()}`,
      role: 'user',
      content: text,
      createdAt: new Date().toISOString(),
    }
    setMessages(prev => [...prev, userMsg])

    const assistantId = `assistant-${Date.now()}`
    const assistantMsg: Message = {
      id: assistantId,
      role: 'assistant',
      content: '',
      status: 'streaming',
      events: [],
      createdAt: new Date().toISOString(),
    }
    setMessages(prev => [...prev, assistantMsg])
    setIsStreaming(true)

    const isUnassigned = activeWorkspaceId === UNASSIGNED_ID
    const url = isUnassigned
      ? '/api/sessions'
      : `/api/workspaces/${activeWorkspaceId}/sessions`

    try {
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ goal: text }),
      })
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}))
        throw new Error(detail.detail || `创建会话失败 (${res.status})`)
      }
      const session = await res.json()

      const newSession: Session = {
        id: session.id,
        title: text.slice(0, 30),
        status: 'running',
        updatedAt: '刚刚',
      }
      if (isUnassigned) {
        setUnassignedSessions(prev => [newSession, ...prev])
      } else {
        setWorkspaces(prev =>
          prev.map(ws =>
            ws.id === activeWorkspaceId
              ? { ...ws, sessions: [newSession, ...ws.sessions] }
              : ws,
          ),
        )
      }
      setSelectedSessionId(session.id)

      const es = new EventSource(`/api/sessions/${session.id}/events`)

      es.addEventListener('final_answer', (event: MessageEvent) => {
        const data = JSON.parse(event.data)
        setMessages(prev =>
          prev.map(m =>
            m.id === assistantId
              ? {
                  ...m,
                  content: data.payload?.answer || '',
                  status: 'done',
                  events: [...(m.events || []), data],
                }
              : m,
          ),
        )
        updateSession(session.id, s => ({
          ...s,
          status: 'completed',
          updatedAt: '刚刚',
        }))
        es.close()
        setIsStreaming(false)
      })

      es.addEventListener('error', (event: MessageEvent) => {
        try {
          const data = JSON.parse(event.data)
          setMessages(prev =>
            prev.map(m =>
              m.id === assistantId
                ? { ...m, content: data.payload?.message || '执行出错', status: 'error' }
                : m,
            ),
          )
        } catch {}
        updateSession(session.id, s => ({ ...s, status: 'failed' }))
        es.close()
        setIsStreaming(false)
      })

      es.onmessage = (event: MessageEvent) => {
        try {
          const data = JSON.parse(event.data)
          setMessages(prev =>
            prev.map(m =>
              m.id === assistantId
                ? { ...m, events: [...(m.events || []), data] }
                : m,
            ),
          )
        } catch {}
      }
    } catch (err) {
      console.error('发送失败:', err)
      setMessages(prev =>
        prev.map(m =>
          m.id === assistantId
            ? {
                ...m,
                content: `创建会话失败：${err instanceof Error ? err.message : '未知错误'}`,
                status: 'error',
              }
            : m,
        ),
      )
      setIsStreaming(false)
    }
  }

  const handleNewWorkspace = () => {
    handleBrowseLocal()
  }

  return (
    <div className="app">
      <Sidebar
        workspaces={treeWorkspaces}
        activeWorkspaceId={activeWorkspaceId}
        selectedSessionId={selectedSessionId}
        onSelectWorkspace={handleSelectWorkspace}
        onSelectSession={setSelectedSessionId}
        onNewSession={handleNewSession}
        onNewWorkspace={handleNewWorkspace}
        onOpenFolder={handleOpenFolder}
        onDeleteWorkspace={handleDeleteWorkspace}
      />
      <ChatPanel
        title={selectedSession?.title || activeWorkspace?.name || '新会话'}
        messages={messages}
        isStreaming={isStreaming}
        onSend={handleSendMessage}
        workspaces={treeWorkspaces}
        selectedWorkspaceId={activeWorkspaceId}
        onSelectWorkspace={handleSelectWorkspace}
        onBrowseLocal={handleBrowseLocal}
      />
      <DirectoryPicker
        open={pickerOpen}
        onClose={() => setPickerOpen(false)}
        onSelect={handleDirectorySelect}
      />
    </div>
  )
}

export default App
