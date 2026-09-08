import { useState, useEffect, useCallback } from 'react'
import './DirectoryPicker.css'

export interface DirEntry {
  name: string
  path: string
}

interface BrowseResult {
  current: string
  parent: string | null
  crumbs: { name: string; path: string; index: number }[]
  children: DirEntry[]
}

interface DirectoryPickerProps {
  open: boolean
  onClose: () => void
  onSelect: (path: string) => void
}

export default function DirectoryPicker({ open, onClose, onSelect }: DirectoryPickerProps) {
  const [roots, setRoots] = useState<DirEntry[]>([])
  const [current, setCurrent] = useState<string>('')
  const [parent, setParent] = useState<string | null>(null)
  const [crumbs, setCrumbs] = useState<BrowseResult['crumbs']>([])
  const [children, setChildren] = useState<DirEntry[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const browse = useCallback(async (path: string) => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(`/api/filesystem/browse?path=${encodeURIComponent(path)}`)
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}))
        throw new Error(detail.detail || `浏览失败 (${res.status})`)
      }
      const data: BrowseResult = await res.json()
      setCurrent(data.current)
      setParent(data.parent)
      setCrumbs(data.crumbs)
      setChildren(data.children)
    } catch (e) {
      setError(e instanceof Error ? e.message : '浏览失败')
    } finally {
      setLoading(false)
    }
  }, [])

  // 打开时加载根目录并进入主目录
  useEffect(() => {
    if (!open) return
    fetch('/api/filesystem/roots')
      .then(r => r.json())
      .then((data: DirEntry[]) => {
        setRoots(data)
        if (data.length > 0) browse(data[0].path)
      })
      .catch(() => setError('加载根目录失败'))
  }, [open, browse])

  if (!open) return null

  const handleSelect = () => {
    if (current) onSelect(current)
    onClose()
  }

  return (
    <div className="picker-overlay" onClick={onClose}>
      <div className="picker-modal" onClick={e => e.stopPropagation()}>
        <header className="picker-header">
          <h3>选择工作目录</h3>
          <button className="picker-close" onClick={onClose} title="关闭">×</button>
        </header>

        {/* 快捷根目录 */}
        <div className="picker-roots">
          {roots.map(r => (
            <button
              key={r.path}
              className={`root-chip ${current === r.path ? 'active' : ''}`}
              onClick={() => browse(r.path)}
              title={r.path}
            >
              {r.name}
            </button>
          ))}
        </div>

        {/* 面包屑导航 */}
        <nav className="picker-crumbs">
          <button
            className="crumb home"
            onClick={() => roots[0] && browse(roots[0].path)}
            title="回到根目录"
          >
            🏠
          </button>
          {parent && (
            <button className="crumb up" onClick={() => browse(parent)} title="上级目录">
              ↑
            </button>
          )}
          {crumbs.map((c, i) => (
            <span key={c.path} className="crumb-item">
              {i > 0 && <span className="crumb-sep">/</span>}
              <button
                className={`crumb ${i === crumbs.length - 1 ? 'current' : ''}`}
                onClick={() => browse(c.path)}
              >
                {c.name}
              </button>
            </span>
          ))}
        </nav>

        {/* 目录列表 */}
        <div className="picker-body">
          {loading && <div className="picker-hint">加载中...</div>}
          {error && <div className="picker-error">{error}</div>}
          {!loading && !error && children.length === 0 && (
            <div className="picker-hint">该目录下没有子目录</div>
          )}
          {!loading && !error && children.map(dir => (
            <div
              key={dir.path}
              className="dir-row"
              onDoubleClick={() => browse(dir.path)}
              onClick={() => browse(dir.path)}
              title={dir.path}
            >
              <span className="dir-icon">📁</span>
              <span className="dir-name">{dir.name}</span>
              <span className="dir-enter">›</span>
            </div>
          ))}
        </div>

        {/* 当前路径 + 操作 */}
        <footer className="picker-footer">
          <div className="picker-current" title={current}>{current || '—'}</div>
          <div className="picker-actions">
            <button className="btn-cancel" onClick={onClose}>取消</button>
            <button className="btn-confirm" onClick={handleSelect} disabled={!current}>
              选择此目录
            </button>
          </div>
        </footer>
      </div>
    </div>
  )
}