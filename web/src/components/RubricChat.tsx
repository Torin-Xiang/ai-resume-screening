import { useState } from 'react'

import { api } from '../api'
import { OP_LABELS, VERDICT_LABELS, type Impact, type RubricProposal } from '../types'

interface Props {
  jobId: string
  hasRubric: boolean
  /** 生效后通知上层刷新排名、简历列表与标准（改标准只重跑评分，不等解析）。 */
  onApplied: () => void
  onError: (message: string) => void
}

const EXAMPLES = [
  '把「有 AI 项目经验」改成必须项',
  '跳槽频繁这条不扣分了，改成提醒',
  '加分项里加一条「有招聘系统搭建经验」',
]

const GROUP_ORDER = ['pass', 'needs_review', 'reject']

/**
 * 用一句话改筛选标准（产品方案 §5 P0#5、§6.4；技术方案 §7 三段式）。
 *
 * 为什么是"提清单 → 看影响 → 再确认"而不是直接改：
 * HR 授权的是「帮我改」，不是「随便改」。标准是这套系统的判断依据，
 * 静默改掉等于静默换掉评价尺度，之前发出去的排名就没法解释了。
 *
 * 同理，`exact === false` 时这里**不显示改动后的分组数字**——新增维度或口径
 * 变化无法从旧结论推算，宁可说"算不准"，也不给一个会被当真的假数字。
 */
export function RubricChat({ jobId, hasRubric, onApplied, onError }: Props) {
  const [utterance, setUtterance] = useState('')
  const [phase, setPhase] = useState<'idle' | 'proposing' | 'applying'>('idle')
  const [proposal, setProposal] = useState<RubricProposal | null>(null)
  const [accepted, setAccepted] = useState<number[]>([])
  const [notice, setNotice] = useState('')

  const busy = phase !== 'idle'

  const reset = () => {
    setProposal(null)
    setAccepted([])
    setNotice('')
  }

  const propose = async () => {
    const text = utterance.trim()
    if (!text) return
    setPhase('proposing')
    reset()
    try {
      const result = await api.proposeRubric(jobId, text)
      setProposal(result)
      // 默认全选：多数情况 HR 是整体认可的，要减再减（减少点击，不是减少知情）
      setAccepted(result.patches.map((patch) => patch.index))
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error))
    } finally {
      setPhase('idle')
    }
  }

  const apply = async (indices?: number[]) => {
    if (!proposal) return
    setPhase('applying')
    try {
      const result = await api.applyRubric(jobId, proposal.proposal_id, indices)
      setNotice(
        `已生效：采纳 ${result.applied} 项改动，标准更新为 ${result.version}，` +
          `按新标准重排了 ${result.rescored} 份简历（未重新解析）。`,
      )
      setProposal(null)
      setAccepted([])
      setUtterance('')
      onApplied()
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error))
    } finally {
      setPhase('idle')
    }
  }

  const toggle = (index: number) => {
    setAccepted((current) =>
      current.includes(index) ? current.filter((item) => item !== index) : [...current, index],
    )
  }

  return (
    <section className="chat">
      <div className="chat-bar">
        <input
          className="chat-input"
          value={utterance}
          disabled={busy}
          placeholder={
            hasRubric
              ? '用一句话改筛选标准，例如「AI 项目经验改成必须项」'
              : '这个岗位还没有标准，先在「查看 / 调整标准」里生成一份'
          }
          onChange={(event) => setUtterance(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !busy) void propose()
          }}
        />
        <button
          type="button"
          className="btn btn-primary"
          disabled={busy || !utterance.trim()}
          onClick={() => void propose()}
        >
          {phase === 'proposing' ? '理解中…' : '改标准'}
        </button>
      </div>

      {!proposal && !notice && (
        <p className="chat-hint">
          改动会先给出<b>清单</b>和<b>对现有排名的影响</b>，你确认后才生效。例如：
          {EXAMPLES.map((example) => (
            <button
              key={example}
              type="button"
              className="chat-example"
              disabled={busy}
              onClick={() => setUtterance(example)}
            >
              {example}
            </button>
          ))}
        </p>
      )}

      {notice && (
        <p className="chat-notice">
          {notice}
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => setNotice('')}>
            知道了
          </button>
        </p>
      )}

      {proposal?.refused && (
        <div className="chat-card chat-refused">
          {/* 理由本身已经把"为什么不能做 + 可以怎么做"说全了，这里不再叠加标题和
              说明段——同一段话出现两遍会让 HR 以为系统在重复报错。 */}
          <p>{proposal.refused_reason}</p>
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => {
              setUtterance('')
              reset()
            }}
          >
            换个说法
          </button>
        </div>
      )}

      {proposal && !proposal.refused && (
        <div className="chat-card">
          <div className="chat-card-head">
            <strong>{proposal.summary || '识别到以下改动'}</strong>
            <span className="muted">
              {proposal.patches.length} 项，当前标准<b>尚未改变</b>
            </span>
          </div>

          <ul className="patch-list">
            {proposal.patches.map((patch) => (
              <li key={patch.index}>
                <label>
                  <input
                    type="checkbox"
                    checked={accepted.includes(patch.index)}
                    disabled={busy}
                    onChange={() => toggle(patch.index)}
                  />
                  <span className={`patch-op patch-op-${patch.op}`}>
                    {OP_LABELS[patch.op] ?? patch.op}
                  </span>
                  <span className="patch-desc">{patch.description}</span>
                  {/* 后端的 description 是"一句话说清改了什么"，本身已经带上理由；
                      只在它没带的时候补一行，避免同一句话在界面上出现两遍。 */}
                  {patch.reason && !patch.description.includes(patch.reason) && (
                    <span className="patch-reason">理由：{patch.reason}</span>
                  )}
                </label>
              </li>
            ))}
          </ul>

          <ImpactView impact={proposal.impact} />

          <div className="chat-actions">
            <button
              type="button"
              className="btn btn-primary"
              disabled={busy}
              onClick={() => void apply()}
            >
              {phase === 'applying' ? '生效中…' : '确认生效（全部采纳）'}
            </button>
            <button
              type="button"
              className="btn"
              disabled={busy || accepted.length === 0}
              title={accepted.length === 0 ? '至少选一项' : ''}
              onClick={() => void apply([...accepted].sort((a, b) => a - b))}
            >
              只采纳选中的 {accepted.length} 项
            </button>
            <button type="button" className="btn btn-ghost" disabled={busy} onClick={reset}>
              取消
            </button>
          </div>
        </div>
      )}
    </section>
  )
}

/**
 * 影响预估。
 *
 * 三件事必须同时说清，少一件 HR 就会误判：
 * 1. 有几个人换分组（分组分布的前后对比）
 * 2. **有几份简历没算进来**（没出结论的不在预估里，不能装作覆盖了全部）
 * 3. 算不准的部分要显式标出来，而不是省略或给个近似值
 */
function ImpactView({ impact }: { impact: Impact }) {
  const changed = GROUP_ORDER.filter(
    (key) => (impact.counts_before[key] ?? 0) > 0 || (impact.counts_after[key] ?? 0) > 0,
  )

  return (
    <div className="impact">
      <div className="impact-head">
        <strong>对现有排名的影响</strong>
        {impact.exact ? (
          <span className="muted">
            涉及 {impact.total} 份简历
            {impact.changed > 0 ? `，预计 ${impact.changed} 份会换分组` : '，预计不会有人换分组'}
          </span>
        ) : (
          <span className="chip chip-warn">无法精确预估</span>
        )}
      </div>

      <ul className="impact-counts">
        {changed.map((key) => (
          <li key={key}>
            <span className="impact-group">{VERDICT_LABELS[key] ?? key}</span>
            <span className="impact-num">{impact.counts_before[key] ?? 0}</span>
            {impact.exact && (
              <>
                <span className="impact-arrow">→</span>
                <span
                  className={`impact-num${
                    (impact.counts_after[key] ?? 0) !== (impact.counts_before[key] ?? 0)
                      ? ' impact-num-changed'
                      : ''
                  }`}
                >
                  {impact.counts_after[key] ?? 0}
                </span>
              </>
            )}
          </li>
        ))}
      </ul>

      {impact.moves.length > 0 && (
        <ul className="impact-moves">
          {impact.moves.slice(0, 12).map((move) => (
            <li key={move.resume_id}>
              <span className="move-name">{move.file_name}</span>
              <span className="move-change">
                {move.to_verdict
                  ? `${VERDICT_LABELS[move.from_verdict] ?? move.from_verdict} → ${
                      VERDICT_LABELS[move.to_verdict] ?? move.to_verdict
                    }`
                  : '无法预估'}
              </span>
              {move.note && <span className="muted move-note">{move.note}</span>}
            </li>
          ))}
          {impact.moves.length > 12 && (
            <li className="muted">另有 {impact.moves.length - 12} 份，确认后看新排名</li>
          )}
        </ul>
      )}

      {impact.not_scored > 0 && (
        <p className="impact-flag">
          另有 <b>{impact.not_scored}</b> 份简历尚无结论（仍在处理中或需人工处理），
          未计入这次预估。
        </p>
      )}

      {impact.notes.map((note) => (
        <p key={note} className="impact-note">
          {note}
        </p>
      ))}
    </div>
  )
}
