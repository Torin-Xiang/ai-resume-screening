/** 与后端接口对齐的类型定义（见 src/resume_screening/web/schemas.py）。 */

export interface Job {
  job_id: string
  title: string
  jd_text: string
  status: string
  created_at: string
  updated_at: string
  resume_count: number
  pending_count: number
  rubric_version: string | null
}

export interface Evidence {
  quote: string
  source: string
  page: number | null
}

export type Judgement = 'satisfied' | 'not_satisfied' | 'not_mentioned' | 'unknown'
export type DimensionKind = 'hard_filter' | 'bonus' | 'red_flag'

export interface Dimension {
  id: string
  name: string
  kind: DimensionKind
  judgement: Judgement
  score: number
  reason: string
  judged_by: 'rule' | 'llm'
  evidence: Evidence[]
}

export interface RankingItem {
  resume_id: string
  file_name: string
  candidate_name: string
  candidate_contact: string
  verdict: string
  group: string
  score: number
  max_score: number
  reason: string
  hard_filter_blocked: boolean
  parse_status: string
  notes: string[]
  dimensions: Dimension[]
  hr_decision: string | null
}

export interface RankingGroup {
  key: string
  label: string
  count: number
  items: RankingItem[]
}

export interface Ranking {
  job_id: string
  rubric_id: string | null
  rubric_version: string | null
  groups: RankingGroup[]
  counts: Record<string, number>
  total: number
  pending: number
}

/** 处理状态机（与后端 store.entities.ResumeStatus 对齐）。 */
export type ResumeStatus =
  | 'queued'
  | 'parsing'
  | 'extracting'
  | 'scoring'
  | 'done'
  | 'needs_manual'
  | 'failed'

export interface ResumeItem {
  resume_id: string
  file_name: string
  status: ResumeStatus
  error: string
  origin: string
  created_at: string
  updated_at: string
  verdict: string | null
  group: string | null
  reason: string | null
  candidate_name: string
  candidate_contact: string
}

export interface ResumeList {
  items: ResumeItem[]
  pending: number
}

export interface UploadItem {
  resume_id: string
  file_name: string
  status: string
  is_new: boolean
  origin: string
}

export interface UploadRejected {
  file_name: string
  reason: string
}

export interface UploadResult {
  accepted: number
  items: UploadItem[]
  rejected: UploadRejected[]
}

export interface RubricDimension {
  id: string
  name: string
  definition: string
  positive_examples: string[]
  negative_examples: string[]
  rule?: string | null
  params?: Record<string, unknown>
  weight?: number
  penalty?: number
}

export interface Rubric {
  schema_version?: string
  version: string
  job_title?: string
  jd_summary?: string
  hard_filters: RubricDimension[]
  bonus: RubricDimension[]
  red_flags: RubricDimension[]
  notes?: string
}

export interface RubricVersion {
  rubric_id: string
  job_id: string
  version: string
  payload: Rubric
  note: string
  created_at: string
}

export interface RubricGenerateResult {
  rubric: Rubric
  dropped: string[]
  saved: boolean
  rubric_id: string | null
  version: string | null
}

/** 处理中 = 还没出结论，界面上**不显示分数**（产品方案 §6.1）。 */
export const PENDING_STATUSES: ResumeStatus[] = [
  'queued',
  'parsing',
  'extracting',
  'scoring',
]

export function isPending(status: ResumeStatus): boolean {
  return PENDING_STATUSES.includes(status)
}

export const STATUS_LABELS: Record<ResumeStatus, string> = {
  queued: '排队中',
  parsing: '解析中',
  extracting: '提取中',
  scoring: '评估中',
  done: '已完成',
  needs_manual: '需人工',
  failed: '失败',
}

export const GROUP_LABELS: Record<string, string> = {
  recommend: '推荐',
  needs_review: '待确认',
  not_recommend: '不推荐',
}

export const JUDGEMENT_LABELS: Record<Judgement, string> = {
  satisfied: '满足',
  not_satisfied: '不满足',
  not_mentioned: '未提及',
  unknown: '无法判定',
}

export const KIND_LABELS: Record<DimensionKind, string> = {
  hard_filter: '必须项',
  bonus: '加分项',
  red_flag: '减分项',
}
