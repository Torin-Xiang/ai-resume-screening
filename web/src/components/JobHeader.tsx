import type { Job, RubricVersion } from '../types'

interface Props {
  job: Job
  rubric: RubricVersion | null
  connected: boolean
  onEditRubric: () => void
  onArchive: () => void
}

function summarize(rubric: RubricVersion | null): string {
  if (!rubric) return '尚未设置筛选标准'
  const { hard_filters, bonus, red_flags } = rubric.payload
  return `必须项 ${hard_filters?.length ?? 0} · 加分项 ${bonus?.length ?? 0} · 减分项 ${red_flags?.length ?? 0}`
}

/** 右栏顶部：当前岗位的 JD 与标准摘要。 */
export function JobHeader({ job, rubric, connected, onEditRubric, onArchive }: Props) {
  return (
    <header className="job-header">
      <div className="job-header-main">
        <h1 className="job-header-title">{job.title}</h1>
        <p className="job-header-jd">{job.jd_text || '未填写 JD'}</p>
        <div className="job-header-meta">
          <span className={`chip${rubric ? '' : ' chip-warn'}`}>
            {rubric ? `标准 ${rubric.version} · ${summarize(rubric)}` : '尚未设置筛选标准 —— 上传后无法评估'}
          </span>
          <span className={`chip chip-dot${connected ? '' : ' chip-off'}`}>
            {connected ? '实时更新中' : '连接已断开'}
          </span>
        </div>
      </div>
      <div className="job-header-actions">
        <button type="button" className="btn" onClick={onEditRubric}>
          查看 / 调整标准
        </button>
        <button type="button" className="btn btn-ghost" onClick={onArchive}>
          归档岗位
        </button>
      </div>
    </header>
  )
}
