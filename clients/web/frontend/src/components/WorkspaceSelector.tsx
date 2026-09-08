import { useState, useRef, useEffect } from 'react'
import type { Workspace } from './Sidebar'
import './WorkspaceSelector.css'

interface WorkspaceSelectorProps {
  workspaces: Workspace[]
  selectedId: string
  onSelect: (id: string) => void
  onBrowseLocal: () => void
}

export default function WorkspaceSelector({
  workspaces,
  selectedId,
  onSelect,
  onBrowseLocal,
}: WorkspaceSelectorProps) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const selected = workspaces.find(w => w.id === selectedId)

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  return (
    <div className="workspace-selector" ref={ref}>
      <button className="ws-trigger" onClick={() => setOpen(!open)}>
        <span className="ws-icon">{selected?.builtin ? '◇' : '📁'}</span>
        <span className="ws-label">{selected?.name || '选择工作空间'}</span>
        <span className="ws-caret">▾</span>
      </button>
      {open && (
        <div className="ws-dropdown">
          <div className="ws-section-title">已有空间</div>
          {workspaces.map((ws, i) => (
            <div key={ws.id}>
              {/* 内置分组与用户空间之间加分隔线 */}
              {i > 0 && ws.builtin !== workspaces[i - 1].builtin && (
                <div className="ws-divider" />
              )}
              <div
                className={`ws-item ${ws.id === selectedId ? 'active' : ''}`}
                onClick={() => { onSelect(ws.id); setOpen(false) }}
              >
                <span className="ws-item-icon">{ws.builtin ? '◇' : '▤'}</span>
                <div className="ws-item-info">
                  <div className="ws-item-name">{ws.name}</div>
                  <div className="ws-item-path">
                    {ws.path || '不关联任何目录'}
                  </div>
                </div>
                {ws.id === selectedId && <span className="ws-check">✓</span>}
              </div>
            </div>
          ))}
          <div className="ws-divider" />
          <div className="ws-item ws-browse" onClick={() => { onBrowseLocal(); setOpen(false) }}>
            <span className="ws-item-icon">📂</span>
            <div className="ws-item-info">
              <div className="ws-item-name">选择本地目录...</div>
              <div className="ws-item-path">从文件系统添加</div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}