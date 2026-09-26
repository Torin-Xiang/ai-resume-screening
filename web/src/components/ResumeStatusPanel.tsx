import type { ResumeItem, ResumeList } from '../types'
import { STATUS_LABELS, isPending } from '../types'

interface Props {
  resumes: ResumeList
  onRemove: (resumeId: string) => void
}

function Row({ item, onRemove }: { item: ResumeItem; onRemove: (id: string) => void }) {
  return (
    <li className={`status-row status-${item.status}`}>
      <span className="status-name">{item.file_name}</span>
      <span className="status-tag">{STATUS_LABELS[item.status]}</span>
      {item.error && <span className="status-error">{item.error}</span>}
      <button type="button" className="btn btn-xs btn-ghost" onClick={() => onRemove(item.resume_id)}>
        移除
      </button>
    </li>
  )
}

/**
 * 处理状态面板。
 *
 * 存在的理由是产品方案 §6.1 的硬约束：**绝不允许静默跳过**。
 * 处理中的要显示在进展，解析不达标和失败的必须让 HR 一眼看到，
 * 否则 HR 会以为系统看过并放过了这个人。
 */
export function ResumeStatusPanel({ resumes, onRemove }: Props) {
  const pending = resumes.items.filter((item) => isPending(item.status))
  const attention = resumes.items.filter(
    (item) => item.status === 'needs_manual' || item.status === 'failed',
  )

  if (pending.length === 0 && attention.length === 0) return null

  return (
    <section className="status-panel">
      {pending.length > 0 && (
        <div className="status-block">
          <div className="status-head">
            <span className="spinner" />
            <span>正在处理 {pending.length} 份简历…</span>
            <span className="muted">处理完成会自动出现在下面的排名里</span>
          </div>
          <ul>
            {pending.map((item) => (
              <Row key={item.resume_id} item={item} onRemove={onRemove} />
            ))}
          </ul>
        </div>
      )}

      {attention.length > 0 && (
        <div className="status-block status-block-attention">
          <div className="status-head">
            <span>需要你处理 {attention.length} 份</span>
            <span className="muted">这些没有进入排名，系统没有跳过它们</span>
          </div>
          <ul>
            {attention.map((item) => (
              <Row key={item.resume_id} item={item} onRemove={onRemove} />
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}
