import { useState } from 'react'
import ToolBar from './ToolBar'
import WorkspaceTree from './WorkspaceTree'
import './Sidebar.css'

export interface Session {
  id: string
  title: string
  status: 'running' | 'completed' | 'failed'
  updatedAt: string
  active?: boolean
}

export interface Workspace {
  id: string
  name: string
  path: string
  sessions: Session[]
  /** 内置分组（如"未关联空间"）：不显示三点菜单、不可删除 */
  builtin?: boolean
}

interface SidebarProps {
  workspaces: Workspace[]
  activeWorkspaceId: string
  selectedSessionId: string | null
  onSelectWorkspace: (workspaceId: string) => void
  onSelectSession: (sessionId: string) => void
  onNewSession: () => void
  onNewWorkspace: () => void
  onOpenFolder: (ws: Workspace) => void
  onDeleteWorkspace: (ws: Workspace) => void
}

export default function Sidebar({
  workspaces,
  activeWorkspaceId,
  selectedSessionId,
  onSelectWorkspace,
  onSelectSession,
  onNewSession,
  onNewWorkspace,
  onOpenFolder,
  onDeleteWorkspace,
}: SidebarProps) {
  const [toolsOpen, setToolsOpen] = useState(true)

  return (
    <aside className="sidebar">
      {/* 顶部工具栏 */}
      <ToolBar
        appName="cagent"
        version="v0.1.0"
        onNewSession={onNewSession}
        toolsOpen={toolsOpen}
        onToggleTools={() => setToolsOpen(!toolsOpen)}
      />

      {/* 空间-会话树形列表 */}
      <WorkspaceTree
        workspaces={workspaces}
        activeWorkspaceId={activeWorkspaceId}
        selectedSessionId={selectedSessionId}
        onSelectWorkspace={onSelectWorkspace}
        onSelectSession={onSelectSession}
        onOpenFolder={onOpenFolder}
        onDeleteWorkspace={onDeleteWorkspace}
        onNewWorkspace={onNewWorkspace}
      />
    </aside>
  )
}