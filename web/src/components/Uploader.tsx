import { useRef, useState } from 'react'

import type { UploadRejected } from '../types'

interface Props {
  disabled: boolean
  uploading: boolean
  rejected: UploadRejected[]
  onFiles: (files: File[]) => void
}

/**
 * 上传区。
 *
 * 刻意的设计：这里**没有"开始筛选"按钮**。上传即入队处理（产品方案 §6.1），
 * HR 不需要再确认一次，也不需要等一批凑齐。
 */
export function Uploader({ disabled, uploading, rejected, onFiles }: Props) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [dragging, setDragging] = useState(false)

  const pick = (list: FileList | null) => {
    if (!list || list.length === 0) return
    onFiles(Array.from(list))
  }

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
