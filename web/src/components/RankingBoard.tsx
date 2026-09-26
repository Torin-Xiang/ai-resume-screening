import type { Ranking } from '../types'
import { CandidateRow, type ReviewDecision } from './CandidateRow'

interface Props {
  ranking: Ranking | null
  onReview: (resumeId: string, decision: ReviewDecision) => void
  onRemove: (resumeId: string) => void
}

/** 每个分组的说明——让 HR 明白"待确认"不是系统偷懒，而是一份待办清单。 */
const GROUP_NOTES: Record<string, string> = {
  recommend: '必须项全部满足',
  needs_review: '有必须项简历里没写，需要你判断',
  not_recommend: '有必须项明确不满足',
}

/**
 * 排名视图：三分组，**不是一条纯排序**（产品方案 §6.3）。
 *
 * 「待确认」必须和「不推荐」分开：简历里没写到的人如果被混进不推荐，
 * 就成了最危险的误伤——那正是「未提及不得淘汰」要防的事。
 */
export function RankingBoard({ ranking, onReview, onRemove }: Props) {
  if (!ranking || ranking.total === 0) {
    return (
      <div className="empty">
        <p className="empty-title">还没有可看的排名</p>
        <p className="muted">
          上传简历后，这里会按「推荐 / 待确认 / 不推荐」分组显示排序结果，每条都带一句理由。
        </p>
      </div>
    )
  }

  return (
    <div className="ranking">
      {ranking.groups.map((group) => (
        <section key={group.key} className={`group group-${group.key}`}>
          <header className="group-head">
            <span className="group-label">{group.label}</span>
            <span className="group-count">{group.count}</span>
            <span className="group-note">{GROUP_NOTES[group.key] ?? ''}</span>
          </header>

          {group.items.length === 0 ? (
            <p className="muted group-empty">暂无</p>
          ) : (
            <ul className="rows">
              {group.items.map((item) => (
                <CandidateRow
                  key={item.resume_id}
                  item={item}
                  onReview={onReview}
                  onRemove={onRemove}
                />
              ))}
            </ul>
          )}
        </section>
      ))}
    </div>
  )
}
