import { useState, useRef, useEffect } from 'react'
import type { Workspace, Session } from './Sidebar'
import './WorkspaceTree.css'

interface WorkspaceTreeProps {
  workspaces: Workspace[]
  activeWorkspaceId: string
  selectedSessionId: string | null
  onSelectWorkspace: (id: string) => void
  onSelectSession: (sessionId: string) => void
  onOpenFolder: (ws: Workspace) => void
  onDeleteWorkspace: (ws: Workspace) => void
  onNewWorkspace: () => void
}

const STATUS_ICON: Record<Session['status'], string> = {
  running: '●',
  completed: '✓',
  failed: '×',
}

export default function WorkspaceTree({
  workspaces,
  activeWorkspaceId,
  selectedSessionId,
  onSelectWorkspace,
  onSelectSession,
  onOpenFolder,
  onDeleteWorkspace,
  onNewWorkspace,
}: WorkspaceTreeProps) {
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set([activeWorkspaceId]))
  const [menuFor, setMenuFor] = useState<string | null>(null)
  const menuRef = useRef<HTMLDivElement>(null)

  // 切换活跃空间时自动展开其节点
  useEffect(() => {
    setExpanded(prev => {
      if (prev.has(activeWorkspaceId)) return prev
      const next = new Set(prev)
      next.add(activeWorkspaceId)
      return next
    })
  }, [activeWorkspaceId])

  // 点击外部关闭菜单
  useEffect(() => {
    if (!menuFor) return
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuFor(null)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [menuFor])

  const toggle = (id: string) => {
    setExpanded(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  return (
    <div className="ws-tree">
      <div className="tree-scroll">
        {workspaces.map(ws => {
          const isExpanded = expanded.has(ws.id)
          const isActive = ws.id === activeWorkspaceId
          return (
            <div key={ws.id} className="tree-group">
              {/* 空间节点 */}
              <div
                className={`tree-node ${isActive ? 'active' : ''}`}
                onClick={() => onSelectWorkspace(ws.id)}
              >
                <button
                  className="twisty"
                  onClick={e => { e.stopPropagation(); toggle(ws.id) }}
                  title={isExpanded ? '折叠' : '展开'}
                >
                  {isExpanded ? '▾' : '▸'}
                </button>
                <span className="node-icon">{ws.builtin ? '◇' : '▤'}</span>
                <span className="node-name" title={ws.path || '不关联任何目录'}>{ws.name}</span>
                {ws.sessions.length > 0 && (
                  <span className="node-count">{ws.sessions.length}</span>
                )}
                {/* 内置分组（未关联空间）无目录路径，不提供菜单 */}
                {!ws.builtin && (
                  <button
                    className="more-btn"
                    onClick={e => {
                      e.stopPropagation()
                      setMenuFor(menuFor === ws.id ? null : ws.id)
                    }}
                    title="更多操作"
                  >
                    ⋯
                  </button>
                )}

                {!ws.builtin && menuFor === ws.id && (
                  <div className="node-menu" ref={menuRef} onClick={e => e.stopPropagation()}>
                    <div
                      className="menu-item"
                      onClick={() => { onOpenFolder(ws); setMenuFor(null) }}
                    >
                      <span className="menu-icon">📂</span>
                      打开文件夹
                    </div>
                    <div className="menu-divider" />
                    <div
                      className="menu-item danger"
                      onClick={() => { onDeleteWorkspace(ws); setMenuFor(null) }}
                    >
                      <span className="menu-icon">🗑</span>
                      从列表删除
                    </div>
                  </div>
                )}
              </div>

              {/* 会话（任务）子节点 */}
              {isExpanded && (
                <div className="tree-children">
                  {ws.sessions.length === 0 ? (
                    <div className="node-empty">暂无任务</div>
                  ) : (
                    ws.sessions.map(s => (
                      <div
                        key={s.id}
                        className={`tree-leaf ${s.id === selectedSessionId ? 'selected' : ''}`}
                        onClick={() => onSelectSession(s.id)}
                        title={s.title}
                      >
                        <span className={`leaf-status ${s.status}`}>{STATUS_ICON[s.status]}</span>
                        <span className="leaf-title">{s.title}</span>
                        <span className="leaf-time">{s.updatedAt}</span>
                      </div>
                    ))
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>

      <button className="tree-add" onClick={onNewWorkspace}>
        <span>+</span>
        <span>添加空间</span>
      </button>
    </div>
  )
}