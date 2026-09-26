import { useState } from 'react'

import type { Dimension, Judgement, RankingItem } from '../types'
import { JUDGEMENT_LABELS, KIND_LABELS } from '../types'

export type ReviewDecision = 'agree' | 'disagree' | 'clear'

interface Props {
  item: RankingItem
  onReview: (resumeId: string, decision: ReviewDecision) => void
  onRemove: (resumeId: string) => void
}

function judgementClass(judgement: Judgement): string {
  if (judgement === 'satisfied') return 'ok'
  if (judgement === 'not_satisfied') return 'bad'
  return 'warn'
}

function DimensionRow({ dimension }: { dimension: Dimension }) {
  return (
    <li className="dim">
      <div className="dim-head">
        <span className="dim-name">{dimension.name}</span>
        <span className="dim-kind">{KIND_LABELS[dimension.kind]}</span>
        <span className={`dim-judge ${judgementClass(dimension.judgement)}`}>
          {JUDGEMENT_LABELS[dimension.judgement]}
        </span>
        <span className="dim-by">{dimension.judged_by === 'rule' ? '规则判定' : '模型判定'}</span>
      </div>
      {dimension.reason && <p className="dim-reason">{dimension.reason}</p>}
      {dimension.evidence.length > 0 ? (
        <ul className="quotes">
          {dimension.evidence.map((evidence) => (
            <li key={evidence.quote}>
              <q>{evidence.quote}</q>
              {evidence.page ? <span className="quote-src">第 {evidence.page} 页</span> : null}
            </li>
          ))}
        </ul>
      ) : (
        <p className="dim-noevidence">无原文引用</p>
      )}
    </li>
  )
}

/**
 * 一条候选人。
 *
 * 折叠时只给一句话理由（产品方案 §6.2：≤30 字、指向具体标准）；
 * 展开后能看到每条维度的判定与**原文引用**——这是"结论可核对"的落点，
 * 也是 HR 敢不敢信系统的分界线。
 */
export function CandidateRow({ item, onReview, onRemove }: Props) {
  const [open, setOpen] = useState(false)
  const decided = item.hr_decision

  const handleRemove = () => {
    const name = item.candidate_name || item.file_name
    // 删除前必须确认：这一步把简历连同结论一起移出岗位，不可撤销
    if (!window.confirm(`删除「${name}」？\n\n会把这份简历和它的评分结论一起移出当前岗位，不可恢复。`)) {
      return
    }
    onRemove(item.resume_id)
  }

  return (
    <li className={`row${open ? ' open' : ''}`}>
      <div className="row-main">
        <button
          type="button"
          className="row-toggle"
          onClick={() => setOpen((value) => !value)}
          aria-expanded={open}
        >
          <span className="row-caret">{open ? '▾' : '▸'}</span>
          <span className="row-name">{item.candidate_name || item.file_name}</span>
          <span className="row-reason">{item.reason}</span>
          <span className="row-score" title={`${item.score} / ${item.max_score}`}>
            {item.score}
          </span>
        </button>
        <div className="row-actions">
          <button
            type="button"
            className={`btn btn-xs${decided === 'agree' ? ' btn-on' : ''}`}
            onClick={() => onReview(item.resume_id, decided === 'agree' ? 'clear' : 'agree')}
          >
            同意
          </button>
          <button
            type="button"
            className={`btn btn-xs${decided === 'disagree' ? ' btn-off' : ''}`}
            onClick={() => onReview(item.resume_id, decided === 'disagree' ? 'clear' : 'disagree')}
          >
            不同意
          </button>
          <button type="button" className="btn btn-xs btn-ghost" onClick={handleRemove} title="把这份简历移出当前岗位">
            删除
          </button>
        </div>
      </div>

      {open && (
        <div className="row-detail">
          <div className="row-detail-meta">
            <span>{item.candidate_contact || '联系方式未提取到'}</span>
            <span className="row-file">{item.file_name}</span>
          </div>

          {item.notes.length > 0 && (
            <ul className="notes">
              {item.notes.map((note) => (
                <li key={note}>{note}</li>
              ))}
            </ul>
          )}

          <ul className="dims">
            {item.dimensions.map((dimension) => (
              <DimensionRow key={dimension.id} dimension={dimension} />
            ))}
          </ul>
        </div>
      )}
    </li>
  )
}
