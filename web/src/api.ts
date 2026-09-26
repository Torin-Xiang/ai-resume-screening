/** 后端接口封装。所有请求集中在 this file，组件不直接拼 URL。 */

import type {
  Job,
  Ranking,
  ResumeList,
  Rubric,
  RubricGenerateResult,
  RubricVersion,
  UploadResult,
} from './types'

async function handle<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    let detail = `${resp.status} ${resp.statusText}`
    try {
      const body = await resp.json()
      if (body?.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch {
      /* 非 JSON 响应，保留状态码文案 */
    }
    // 失败留在控制台一份，便于对着 DevTools 定位；同时抛出去让界面显示错误横幅
    console.error('[api] 请求失败', resp.status, resp.url, detail)
    throw new Error(detail)
  }
  if (resp.status === 204) return undefined as T
  return (await resp.json()) as T
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  return handle<T>(await fetch(path, init))
}

function jsonInit(method: string, body: unknown): RequestInit {
  return {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }
}

export const api = {
  listJobs: () => request<Job[]>('/api/jobs'),

  createJob: (payload: { title: string; jd_text: string; rubric?: Rubric | null }) =>
    request<Job>('/api/jobs', jsonInit('POST', payload)),

  getJob: (jobId: string) => request<Job>(`/api/jobs/${jobId}`),

  archiveJob: (jobId: string) => request<Job>(`/api/jobs/${jobId}/archive`, { method: 'POST' }),

  deleteJob: (jobId: string) => request<void>(`/api/jobs/${jobId}`, { method: 'DELETE' }),

  getRubric: async (jobId: string): Promise<RubricVersion | null> => {
    const resp = await fetch(`/api/jobs/${jobId}/rubric`)
    if (resp.status === 404) return null
    return handle<RubricVersion>(resp)
  },

  listRubricVersions: (jobId: string) =>
    request<RubricVersion[]>(`/api/jobs/${jobId}/rubric/versions`),

  setRubric: (jobId: string, rubric: Rubric, note = '') =>
    request<RubricVersion>(`/api/jobs/${jobId}/rubric`, jsonInit('PUT', { rubric, note, rescore: true })),

  generateRubric: (jobId: string, jdText = '', save = false) =>
    request<RubricGenerateResult>(
      `/api/jobs/${jobId}/rubric/generate`,
      jsonInit('POST', { jd_text: jdText, save }),
    ),

  rebuildRanking: (jobId: string) =>
    request<{ rescored: number }>(`/api/jobs/${jobId}/ranking/rebuild`, { method: 'POST' }),

  getRanking: (jobId: string) => request<Ranking>(`/api/jobs/${jobId}/ranking`),

  listResumes: (jobId: string) => request<ResumeList>(`/api/jobs/${jobId}/resumes`),

  removeResume: (jobId: string, resumeId: string) =>
    request<void>(`/api/jobs/${jobId}/resumes/${resumeId}`, { method: 'DELETE' }),

  upload: async (jobId: string, files: File[]): Promise<UploadResult> => {
    const form = new FormData()
    for (const file of files) form.append('files', file)
    // 注意：不要手动设置 Content-Type，浏览器需要自己补 boundary
    return handle<UploadResult>(
      await fetch(`/api/jobs/${jobId}/resumes`, { method: 'POST', body: form }),
    )
  },

  review: (jobId: string, resumeId: string, decision: 'agree' | 'disagree' | 'clear') =>
    request<{ resume_id: string; hr_decision: string | null }>(
      `/api/jobs/${jobId}/review`,
      jsonInit('POST', { resume_id: resumeId, decision }),
    ),
}
