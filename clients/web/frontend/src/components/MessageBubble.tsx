import type { AgentEvent } from '../App'
import './MessageBubble.css'

export interface Message {
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  status?: 'pending' | 'streaming' | 'done' | 'error'
  events?: AgentEvent[]
  createdAt: string
}

interface MessageBubbleProps {
  message: Message
}

const EVENT_LABELS: Record<string, string> = {
  plan_created: '已生成计划',
  step_started: '步骤开始',
  thought: '思考',
  tool_call: '调用工具',
  tool_result: '工具返回',
  step_finished: '步骤完成',
  replanned: '重新规划',
  plan_adjusted: '调整计划',
  final_answer: '最终答案',
  error: '错误',
}

export default function MessageBubble({ message }: MessageBubbleProps) {
  const isUser = message.role === 'user'

  return (
    <div className={`message-row ${isUser ? 'user' : 'assistant'}`}>
      <div className="avatar">
        {isUser ? '我' : 'AI'}
      </div>
      <div className="message-content">
        {!isUser && message.status === 'streaming' && (
          <span className="typing-indicator">
            <span></span><span></span><span></span>
          </span>
        )}
        {message.content && (
          <div className="message-text">{message.content}</div>
        )}
        {!isUser && message.events && message.events.length > 0 && (
          <details className="message-events">
            <summary>查看执行过程 ({message.events.length})</summary>
            <div className="event-list">
              {message.events.map((ev, i) => (
                <div key={i} className={`event-line ${ev.type}`}>
                  <span className="event-label">{EVENT_LABELS[ev.type] || ev.type}</span>
                  {ev.type === 'thought' && (
                    <span className="event-text">{(ev.payload as any)?.content || ''}</span>
                  )}
                  {ev.type === 'tool_call' && (
                    <span className="event-text">
                      {(ev.payload as any)?.tool_name}({JSON.stringify((ev.payload as any)?.args)})
                    </span>
                  )}
                  {ev.type === 'final_answer' && (
                    <span className="event-text">{(ev.payload as any)?.answer}</span>
                  )}
                </div>
              ))}
            </div>
          </details>
        )}
        <div className="message-meta">
          {new Date(message.createdAt).toLocaleTimeString()}
        </div>
      </div>
    </div>
  )
}