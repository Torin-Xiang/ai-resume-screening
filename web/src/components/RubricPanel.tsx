import { useEffect, useState } from 'react'

import { api } from '../api'
import type { Job, RubricVersion } from '../types'

interface Props {
  job: Job
  rubric: RubricVersion | null
  onClose: () => void
  onChanged: () => void
  onError: (message: string) => void
}

/**
 * 标准面板：查看 / 由 JD 生成草稿 / 直接编辑 JSON / 保存 / 回滚到历史版本。
 *
 * 保存后**立刻按新标准重排**（后端只重跑评分层，不重新解析），
 * 所以改口径不需要重新上传任何简历。
 *
 * 用一句话改标准走的是工作区里的对话条（RubricChat），不在这里——
 * 那是日常动作，不该藏在抽屉里点两层才够得着。
 */
export function RubricPanel({ job, rubric, onClose, onChanged, onError }: Props) {
  const [text, setText] = useState('')
  const [versions, setVersions] = useState<RubricVersion[]>([])
  const [busy, setBusy] = useState<'idle' | 'generating' | 'saving' | 'rolling'>('idle')
  const [dropped, setDropped] = useState<string[]>([])
  const [notice, setNotice] = useState('')

  useEffect(() => {
    setText(rubric ? JSON.stringify(rubric.payload, null, 2) : '')
    setDropped([])
    setNotice('')
  }, [rubric])

  useEffect(() => {
    api
      .listRubricVersions(job.job_id)
      .then(setVersions)
      .catch(() => setVersions([]))
  }, [job.job_id, rubric])

  const generate = async () => {
    setBusy('generating')
    setNotice('')
    try {
      const result = await api.generateRubric(job.job_id)
      setText(JSON.stringify(result.rubric, null, 2))
      setDropped(result.dropped)
      setNotice('草稿已生成。请检查每个维度的口径与正反例，确认后再保存。')
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error))
    } finally {
      setBusy('idle')
    }
  }

  const save = async () => {
    let parsed: unknown
    try {
      parsed = JSON.parse(text)
    } catch {
      onError('标准不是合法的 JSON，请检查格式')
      return
    }
    setBusy('saving')
    try {
      const saved = await api.setRubric(
        job.job_id,
        parsed as RubricVersion['payload'],
        '界面调整',
      )
      setNotice(`已保存为 ${saved.version}，并按新标准重排（未重新解析简历）。`)
      onChanged()
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error))
    } finally {
      setBusy('idle')
    }
  }

  const rollback = async (target: RubricVersion) => {
    const confirmed = window.confirm(
      `回滚到 ${target.version}？\n\n` +
        '回滚是把旧版本重新设为当前版本，并立刻按它重排排名。' +
        '历史版本不会被删除，「改了又改回来」这件事也留在版本记录里。',
    )
    if (!confirmed) return
    setBusy('rolling')
    setNotice('')
    try {
      const saved = await api.rollbackRubric(job.job_id, target.rubric_id)
      setText(JSON.stringify(saved.payload, null, 2))
      setNotice(`已回滚到 ${target.version}，当前版本为 ${saved.version}，排名已按它重排。`)
      onChanged()
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error))
    } finally {
      setBusy('idle')
    }
  }

  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside className="drawer" onClick={(event) => event.stopPropagation()}>
        <header className="drawer-head">
          <span className="drawer-title">筛选标准 · {job.title}</span>
          <button type="button" className="btn btn-ghost btn-sm" onClick={onClose}>
            关闭
          </button>
        </header>

        <p className="drawer-desc">
          判断口径写在这里：每个维度的 <code>definition</code> 与正反例决定模型怎么判。
          「有 AI 项目经验」这类条件真正的难点是口径不清——微调算不算、调接口算不算，只有你能定。
        </p>

        <div className="drawer-actions">
          <button
            type="button"
            className="btn"
            onClick={generate}
            disabled={busy !== 'idle' || !job.jd_text}
            title={job.jd_text ? '' : '该岗位没有 JD，无法生成'}
          >
            {busy === 'generating' ? '生成中…' : '由 JD 生成草稿'}
          </button>
          <button
            type="button"
            className="btn btn-primary"
            onClick={save}
            disabled={busy !== 'idle' || !text.trim()}
          >
            {busy === 'saving' ? '保存中…' : '保存并按新标准重排'}
          </button>
        </div>

        {notice && <p className="drawer-notice">{notice}</p>}

        {dropped.length > 0 && (
          <div className="drawer-dropped">
            <strong>以下维度因触碰合规红线已被自动剔除：</strong>
            <ul>
              {dropped.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </div>
        )}

        <textarea
          className="drawer-editor"
          value={text}
          spellCheck={false}
          placeholder='点击「由 JD 生成草稿」，或直接粘贴一份标准 JSON'
          onChange={(event) => setText(event.target.value)}
        />

        {versions.length > 0 && (
          <div className="drawer-versions">
            <span className="muted">历史版本（改标准不会覆盖旧版本，可回溯、可回滚、可对比）</span>
            <ul>
              {versions.map((version) => {
                const current = version.rubric_id === rubric?.rubric_id
                return (
                  <li key={version.rubric_id}>
                    <span className={`version-tag${current ? ' version-tag-current' : ''}`}>
                      {version.version}
                    </span>
                    <span className="muted">{version.note || '—'}</span>
                    <span className="muted version-time">
                      {version.created_at.replace('T', ' ')}
                    </span>
                    {current ? (
                      <span className="muted version-current">当前</span>
                    ) : (
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm"
                        disabled={busy !== 'idle'}
                        onClick={() => void rollback(version)}
                      >
                        {busy === 'rolling' ? '回滚中…' : '回滚到此版本'}
                      </button>
                    )}
                  </li>
                )
              })}
            </ul>
          </div>
        )}
      </aside>
    </div>
  )
}
