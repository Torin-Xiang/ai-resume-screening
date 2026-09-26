import type { Job } from '../types'

interface Props {
  jobs: Job[]
  activeJobId: string | null
  loading: boolean
  onSelect: (jobId: string) => void
  onNew: () => void
}

/** 左栏：一个岗位就是一个工作台入口（产品方案 §2：产品的基本单位是岗位）。 */
export function JobList({ jobs, activeJobId, loading, onSelect, onNew }: Props) {
  return (
    <aside className="job-list">
      <div className="job-list-head">
        <span className="brand">简历初筛</span>
        <button type="button" className="btn btn-primary btn-sm" onClick={onNew}>
          新建岗位
        </button>
      </div>

      {loading ? (
        <p className="muted pad">加载中…</p>
      ) : jobs.length === 0 ? (
        <p className="muted pad">还没有岗位。点「新建岗位」，把 JD 粘进去就能开始。</p>
      ) : (
        <ul className="job-items">
          {jobs.map((job) => {
            const active = job.job_id === activeJobId
            return (
              <li key={job.job_id}>
                <button
                  type="button"
                  className={`job-item${active ? ' active' : ''}`}
                  onClick={() => onSelect(job.job_id)}
                >
                  <span className="job-item-title">{job.title}</span>
                  <span className="job-item-meta">
                    {job.resume_count} 份简历
                    {job.pending_count > 0 ? (
                      <em className="pending"> · {job.pending_count} 处理中</em>
                    ) : null}
                  </span>
                  <span className={`job-item-rubric${job.rubric_version ? '' : ' warn'}`}>
                    {job.rubric_version ? `标准 ${job.rubric_version}` : '尚未设置标准'}
                  </span>
                </button>
              </li>
            )
          })}
        </ul>
      )}
    </aside>
  )
}
