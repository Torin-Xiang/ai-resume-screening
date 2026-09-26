import { useCallback, useEffect, useRef, useState } from 'react'

import { api } from './api'
import { JobHeader } from './components/JobHeader'
import { JobList } from './components/JobList'
import { NewJobForm } from './components/NewJobForm'
import { RankingBoard } from './components/RankingBoard'
import { ResumeStatusPanel } from './components/ResumeStatusPanel'
import { RubricPanel } from './components/RubricPanel'
import type { ReviewDecision } from './components/CandidateRow'
import { Uploader } from './components/Uploader'
import { useJobEvents } from './hooks/useJobEvents'
import type { Job, Ranking, ResumeList, RubricVersion, UploadRejected } from './types'

const EMPTY_RESUMES: ResumeList = { items: [], pending: 0 }
const REFRESH_DEBOUNCE_MS = 150

export default function App() {
  const [jobs, setJobs] = useState<Job[]>([])
  const [jobsLoading, setJobsLoading] = useState(true)
  const [activeJobId, setActiveJobId] = useState<string | null>(null)

  const [ranking, setRanking] = useState<Ranking | null>(null)
  const [resumes, setResumes] = useState<ResumeList>(EMPTY_RESUMES)
  const [rubric, setRubric] = useState<RubricVersion | null>(null)

  const [error, setError] = useState<string | null>(null)
  const [showNewJob, setShowNewJob] = useState(false)
  const [showRubric, setShowRubric] = useState(false)
  const [creating, setCreating] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [rejected, setRejected] = useState<UploadRejected[]>([])

  const activeJob = jobs.find((job) => job.job_id === activeJobId) ?? null

  // 在定时器与事件回调里读最新值，避免闭包拿到过期的岗位 id
  const activeJobIdRef = useRef<string | null>(null)
  useEffect(() => {
    activeJobIdRef.current = activeJobId
  }, [activeJobId])

  const reportError = useCallback((cause: unknown) => {
    setError(cause instanceof Error ? cause.message : String(cause))
  }, [])

  const loadJobs = useCallback(async (): Promise<Job[]> => {
    try {
      const list = await api.listJobs()
      setJobs(list)
      return list
    } catch (cause) {
      reportError(cause)
      return []
    } finally {
      setJobsLoading(false)
    }
  }, [reportError])

  // —— 首屏：拉岗位列表并默认选中第一个 ——
  useEffect(() => {
    void (async () => {
      const list = await loadJobs()
      if (list.length > 0) {
        setActiveJobId((current) => current ?? list[0].job_id)
      }
    })()
  }, [loadJobs])

  // —— 切换岗位：重新拉取该岗位的排名、简历与标准 ——
  // 三个请求各自独立处理：任何一个失败都不该让另外两个的数据也显示不出来
  useEffect(() => {
    if (!activeJobId) {
      setRanking(null)
      setResumes(EMPTY_RESUMES)
      setRubric(null)
      return
    }
    const jobId = activeJobId
    let cancelled = false

    const load = async <T,>(promise: Promise<T>, apply: (value: T) => void) => {
      try {
        const value = await promise
        if (!cancelled) apply(value)
      } catch (cause) {
        // 不静默吞错：加载失败必须让用户看见，否则界面会呈现出"没有数据"的假象
        if (!cancelled) reportError(cause)
      }
    }

    void load(api.getRanking(jobId), setRanking)
    void load(api.listResumes(jobId), setResumes)
    void load(api.getRubric(jobId), setRubric)

    return () => {
      cancelled = true
    }
  }, [activeJobId, reportError])

  // —— 事件驱动的刷新（后端已对排名推送做过合并，这里再做一层防抖）——
  const refreshTimer = useRef<number | null>(null)
  const scheduleRefresh = useCallback(() => {
    if (refreshTimer.current !== null) return
    refreshTimer.current = window.setTimeout(() => {
      refreshTimer.current = null
      const jobId = activeJobIdRef.current
      if (jobId) {
        void api.getRanking(jobId).then(setRanking).catch(() => undefined)
        void api.listResumes(jobId).then(setResumes).catch(() => undefined)
      }
      void loadJobs()
    }, REFRESH_DEBOUNCE_MS)
  }, [loadJobs])

  const connected = useJobEvents(activeJobId, () => scheduleRefresh())

  useEffect(
    () => () => {
      if (refreshTimer.current !== null) window.clearTimeout(refreshTimer.current)
    },
    [],
  )

  // —— 动作 ——

  const handleCreate = async (title: string, jdText: string) => {
    setCreating(true)
    try {
      const job = await api.createJob({ title, jd_text: jdText })
      await loadJobs()
      setActiveJobId(job.job_id)
      setShowNewJob(false)
      // 新岗位还没有标准，直接引导去生成——没有标准，传了简历也评不了
      setShowRubric(true)
    } catch (cause) {
      reportError(cause)
    } finally {
      setCreating(false)
    }
  }

  const handleFiles = async (files: File[]) => {
    const jobId = activeJobIdRef.current
    if (!jobId) return
    setUploading(true)
    setRejected([])
    try {
      const result = await api.upload(jobId, files)
      setRejected(result.rejected)
      scheduleRefresh()
    } catch (cause) {
      reportError(cause)
    } finally {
      setUploading(false)
    }
  }

  const handleReview = async (resumeId: string, decision: ReviewDecision) => {
    const jobId = activeJobIdRef.current
    if (!jobId) return
    try {
      await api.review(jobId, resumeId, decision)
      scheduleRefresh()
    } catch (cause) {
      reportError(cause)
    }
  }

  const handleRemoveResume = async (resumeId: string) => {
    const jobId = activeJobIdRef.current
    if (!jobId) return
    try {
      await api.removeResume(jobId, resumeId)
      scheduleRefresh()
    } catch (cause) {
      reportError(cause)
    }
  }

  const handleArchive = async () => {
    if (!activeJob) return
    const confirmed = window.confirm(
      `归档岗位「${activeJob.title}」？\n\n归档后不再出现在列表里，已有的简历和结论会保留。`,
    )
    if (!confirmed) return
    try {
      await api.archiveJob(activeJob.job_id)
      const list = await loadJobs()
      setActiveJobId(list[0]?.job_id ?? null)
    } catch (cause) {
      reportError(cause)
    }
  }

  return (
    <div className="app">
      <JobList
        jobs={jobs}
        activeJobId={activeJobId}
        loading={jobsLoading}
        onSelect={setActiveJobId}
        onNew={() => setShowNewJob(true)}
      />

      <main className="workspace">
        {error && (
          <button type="button" className="banner banner-error" onClick={() => setError(null)}>
            {error}
            <span className="banner-hint">点击关闭</span>
          </button>
        )}

        {!activeJob ? (
          <div className="empty">
            <p className="empty-title">左边选一个岗位，或者新建一个</p>
            <p className="muted">
              每个岗位是一个独立的工作台：自己的 JD、自己的筛选标准、自己的简历池和排名。
            </p>
          </div>
        ) : (
          <>
            <JobHeader
              job={activeJob}
              rubric={rubric}
              connected={connected}
              onEditRubric={() => setShowRubric(true)}
              onArchive={handleArchive}
            />
            <Uploader
              disabled={uploading}
              uploading={uploading}
              rejected={rejected}
              onFiles={handleFiles}
            />
            <ResumeStatusPanel resumes={resumes} onRemove={handleRemoveResume} />
            <RankingBoard ranking={ranking} onReview={handleReview} />
          </>
        )}
      </main>

      {showNewJob && (
        <NewJobForm busy={creating} onCancel={() => setShowNewJob(false)} onCreate={handleCreate} />
      )}

      {showRubric && activeJob && (
        <RubricPanel
          job={activeJob}
          rubric={rubric}
          onClose={() => setShowRubric(false)}
          onChanged={scheduleRefresh}
          onError={reportError}
        />
      )}
    </div>
  )
}
