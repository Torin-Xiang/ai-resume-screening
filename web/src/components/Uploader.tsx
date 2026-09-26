import { useRef, useState } from 'react'

import type { UploadRejected } from '../types'

interface Props {
  disabled: boolean
  uploading: boolean
  rejected: UploadRejected[]
  onFiles: (files: File[]) => void
  /** 返回 true 表示已受理（清空输入框），false 表示失败（保留内容让 HR 可以重试）。 */
  onPaste: (text: string, fileName: string) => Promise<boolean>
}

/**
 * 上传区：拖拽 / 选择文件，外加「粘贴文本」兜底。
 *
 * 刻意的设计：这里**没有"开始筛选"按钮**。上传即入队处理（产品方案 §6.1），
 * HR 不需要再确认一次，也不需要等一批凑齐。
 *
 * 粘贴入口不做成主按钮，是因为它是**兜底**不是常态；但要一直可见——
 * 扫描件解析不出来时，HR 必须一眼看到出路，而不是以为"这份系统放过了"。
 */
export function Uploader({ disabled, uploading, rejected, onFiles, onPaste }: Props) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [dragging, setDragging] = useState(false)
  const [pasting, setPasting] = useState(false)
  const [text, setText] = useState('')
  const [fileName, setFileName] = useState('')
  const [sending, setSending] = useState(false)

  const pick = (list: FileList | null) => {
    if (!list || list.length === 0) return
    onFiles(Array.from(list))
  }

  const submitPaste = async () => {
    if (!text.trim() || sending) return
    setSending(true)
    try {
      const ok = await onPaste(text, fileName.trim() || '粘贴的简历')
      if (ok) {
        setText('')
        setFileName('')
        setPasting(false)
      }
    } finally {
      setSending(false)
    }
  }

  const busy = disabled || sending

  return (
    <section className="uploader-wrap">
      <div
        className={`uploader${dragging ? ' dragging' : ''}${disabled ? ' disabled' : ''}`}
        onDragOver={(event) => {
          event.preventDefault()
          if (!disabled) setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault()
          setDragging(false)
          if (!disabled) pick(event.dataTransfer.files)
        }}
        onClick={() => {
          if (!disabled) inputRef.current?.click()
        }}
        role="button"
        tabIndex={0}
        onKeyDown={(event) => {
          if (event.key === 'Enter' && !disabled) inputRef.current?.click()
        }}
      >
        <input
          ref={inputRef}
          type="file"
          accept=".pdf"
          multiple
          hidden
          onChange={(event) => {
            pick(event.target.files)
            event.target.value = ''
          }}
        />
        <span className="uploader-title">
          {uploading ? '正在上传…' : '把简历拖到这里，或点击选择文件'}
        </span>
        <span className="uploader-hint">
          支持 PDF，可一次多份。上传后自动开始处理，不需要再点任何按钮。
        </span>
      </div>

      <div className="uploader-fallback">
        <button
          type="button"
          className="link-btn"
          onClick={() => setPasting((current) => !current)}
        >
          {pasting ? '收起' : '扫描件识别不出来？粘贴简历文本'}
        </button>
      </div>

      {pasting && (
        <div className="paste-box">
          <p className="muted paste-tip">
            扫描件没有文本层，解析不出内容时请把简历正文粘到这里。
            粘贴的内容会走完整的评估链路，和其他简历一起排序——不做 OCR 是刻意的选择：
            粘进来的是你亲自确认过的内容，不存在"认错了还查不出来"。
          </p>
          <input
            className="paste-name"
            value={fileName}
            disabled={busy}
            placeholder="这份简历叫什么（建议填候选人姓名，便于在列表里认出来）"
            onChange={(event) => setFileName(event.target.value)}
          />
          <textarea
            className="paste-text"
            value={text}
            disabled={busy}
            spellCheck={false}
            placeholder="把简历正文粘贴到这里，例如：&#10;姓名：张三&#10;学历：本科&#10;工作经验：3 年&#10;..."
            onChange={(event) => setText(event.target.value)}
          />
          <div className="paste-actions">
            <span className="muted">{text.trim().length} 字</span>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              disabled={busy || !text.trim()}
              onClick={() => void submitPaste()}
            >
              {sending ? '提交中…' : '加入筛选'}
            </button>
          </div>
        </div>
      )}

      {rejected.length > 0 && (
        <ul className="rejected">
          {rejected.map((item) => (
            <li key={`${item.file_name}-${item.reason}`}>
              <strong>{item.file_name}</strong>：{item.reason}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
