import './ToolBar.css'

interface ToolBarProps {
  appName: string
  version: string
  onNewSession: () => void
  toolsOpen: boolean
  onToggleTools: () => void
}

const TOOLS = [
  { id: 'new', icon: '✚', label: '新建任务' },
  { id: 'assistant', icon: '◎', label: '助理' },
  { id: 'project', icon: '⊞', label: '项目' },
  { id: 'expert', icon: '◈', label: '专家·技能·连接器' },
  { id: 'automation', icon: '↻', label: '自动化' },
  { id: 'library', icon: '⊟', label: '资料库' },
]

export default function ToolBar({
  appName,
  version,
  onNewSession,
  toolsOpen,
}: ToolBarProps) {
  return (
    <div className="toolbar">
      <div className="toolbar-header">
        <span className="app-name">{appName}</span>
        <span className="app-version">{version}</span>
      </div>

      {toolsOpen && (
        <nav className="toolbar-nav">
          {TOOLS.map(tool => (
            <button
              key={tool.id}
              className="toolbar-item"
              onClick={tool.id === 'new' ? onNewSession : undefined}
              title={tool.label}
            >
              <span className="toolbar-icon">{tool.icon}</span>
              <span className="toolbar-label">{tool.label}</span>
            </button>
          ))}
        </nav>
      )}
    </div>
  )
}