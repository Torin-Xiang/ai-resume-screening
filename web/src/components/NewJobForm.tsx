import { useState } from 'react'

interface Props {
  busy: boolean
  onCancel: () => void
  onCreate: (title: string, jdText: string) => void
}

/** 新建岗位：只有"岗位名称 + JD"两件事，剩下的交给生成标准那一步。 */
export function NewJobForm({ busy, onCancel, onCreate }: Props) {
  const [title, setTitle] = useState('')
  const [jd, setJd] = useState('')

  const valid = title.trim().length > 0

  return (
    <div className="modal-backdrop" onClick={onCancel}>
      <div className="modal" onClick={(event) => event.stopPropagation()}>
        <h2 className="modal-title">新建岗位</h2>
        <p className="modal-desc">
          一个岗位就是一个工作台。建好后可以直接由 JD 生成一份筛选标准草稿，
          确认之后就能开始传简历。
        </p>

        <label className="field">
          <span className="field-label">岗位名称</span>
          <input
            className="input"
            value={title}
            placeholder="例：AI 应用开发工程师"
            onChange={(event) => setTitle(event.target.value)}
            autoFocus
          />
        </label>

        <label className="field">
          <span className="field-label">岗位 JD</span>
          <textarea
            className="textarea"
            value={jd}
            placeholder="把岗位描述整段粘进来。写全一点，生成的标准会更贴合实际要求。"
            onChange={(event) => setJd(event.target.value)}
          />
        </label>

        <div className="modal-actions">
          <button type="button" className="btn btn-ghost" onClick={onCancel}>
            取消
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={!valid || busy}
            onClick={() => onCreate(title.trim(), jd.trim())}
          >
            {busy ? '创建中…' : '创建岗位'}
          </button>
        </div>
      </div>
    </div>
  )
}
