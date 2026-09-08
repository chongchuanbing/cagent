import { useState, useRef, useEffect } from 'react'
import WorkspaceSelector from './WorkspaceSelector'
import type { Workspace } from './Sidebar'
import './InputBox.css'

interface InputBoxProps {
  onSend: (text: string) => void
  disabled?: boolean
  placeholder?: string
  workspaces: Workspace[]
  selectedWorkspaceId: string
  onSelectWorkspace: (id: string) => void
  onBrowseLocal: () => void
}

export default function InputBox({
  onSend,
  disabled,
  placeholder = '今天帮你做些什么？@ 引用对话文件、/ 调用技能与指令',
  workspaces,
  selectedWorkspaceId,
  onSelectWorkspace,
  onBrowseLocal,
}: InputBoxProps) {
  const [text, setText] = useState('')
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto'
      textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, 160)}px`
    }
  }, [text])

  const handleSend = () => {
    const trimmed = text.trim()
    if (!trimmed || disabled) return
    onSend(trimmed)
    setText('')
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  return (
    <div className="input-box">
      <textarea
        ref={textareaRef}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        rows={1}
        disabled={disabled}
      />
      <div className="input-actions">
        <div className="input-left">
          <button className="input-btn" title="附件">＋</button>
        </div>
        <div className="input-right">
          <button className="model-btn">Hy4 preview ▾</button>
          <button
            className="send-btn"
            onClick={handleSend}
            disabled={!text.trim() || disabled}
            title="发送"
          >
            ▲
          </button>
        </div>
      </div>
      <div className="input-footbar">
        <WorkspaceSelector
          workspaces={workspaces}
          selectedId={selectedWorkspaceId}
          onSelect={onSelectWorkspace}
          onBrowseLocal={onBrowseLocal}
        />
        <button className="input-btn" title="默认权限">
          <span className="perm-icon">🛡</span>
          <span>默认权限</span>
          <span className="caret">▾</span>
        </button>
      </div>
    </div>
  )
}