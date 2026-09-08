import { useEffect, useRef } from 'react'
import MessageBubble from './MessageBubble'
import InputBox from './InputBox'
import type { Message } from './MessageBubble'
import type { Workspace } from './Sidebar'
import './ChatPanel.css'

interface ChatPanelProps {
  title: string
  messages: Message[]
  isStreaming: boolean
  onSend: (text: string) => void
  onStop?: () => void
  workspaces: Workspace[]
  selectedWorkspaceId: string
  onSelectWorkspace: (id: string) => void
  onBrowseLocal: () => void
}

export default function ChatPanel({
  title,
  messages,
  isStreaming,
  onSend,
  workspaces,
  selectedWorkspaceId,
  onSelectWorkspace,
  onBrowseLocal,
}: ChatPanelProps) {
  const messagesRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (messagesRef.current) {
      messagesRef.current.scrollTop = messagesRef.current.scrollHeight
    }
  }, [messages])

  return (
    <main className="chat-panel">
      <header className="chat-header">
        <h2 className="chat-title">{title}</h2>
        <div className="chat-actions">
          <button className="header-btn" title="搜索">⌕</button>
          <button className="header-btn" title="展开">↗</button>
          <button className="header-btn" title="历史">⏱</button>
        </div>
      </header>

      <div className="chat-messages" ref={messagesRef}>
        {messages.length === 0 ? (
          <div className="chat-welcome">
            <div className="welcome-logo">cagent</div>
            <div className="welcome-hint">内容由 AI 生成，请核实重要信息</div>
          </div>
        ) : (
          messages.map(msg => <MessageBubble key={msg.id} message={msg} />)
        )}
      </div>

      <footer className="chat-footer">
        <InputBox
          onSend={onSend}
          disabled={isStreaming}
          workspaces={workspaces}
          selectedWorkspaceId={selectedWorkspaceId}
          onSelectWorkspace={onSelectWorkspace}
          onBrowseLocal={onBrowseLocal}
        />
      </footer>
    </main>
  )
}