import type { LlmSettings } from '../types'

interface Props {
  settings: LlmSettings | null
  switching: boolean
  onSelect: (provider: string, model: string) => void
}

/**
 * 模型通道切换（全局，不是岗位级）。
 *
 * 切换只影响**之后**处理的简历——正在评的不打断，已有分数不重算；
 * 想用新通道重评旧简历，用"查看 / 调整标准"里的重新评分。
 */
export function LlmSwitcher({ settings, switching, onSelect }: Props) {
  if (!settings) {
    return <span className="llm-switcher muted">模型通道加载中…</span>
  }

  const current = settings.choices.find(
    (choice) => choice.provider === settings.provider && choice.model === settings.model,
  )

  return (
    <label className="llm-switcher" title="切换只影响之后处理的简历；已有分数不重算">
      <span className="llm-switcher-label">判定模型</span>
      <select
        value={`${settings.provider}::${settings.model}`}
        disabled={switching}
        onChange={(event) => {
          const [provider, model] = event.target.value.split('::')
          onSelect(provider, model)
        }}
      >
        {!current && settings.model && (
          <option value={`${settings.provider}::${settings.model}`}>
            {settings.provider} / {settings.model}（当前配置）
          </option>
        )}
        {settings.choices.map((choice) => (
          <option
            key={`${choice.provider}::${choice.model}`}
            value={`${choice.provider}::${choice.model}`}
            disabled={!choice.available}
          >
            {choice.label}
            {choice.available ? '' : `（缺 ${choice.needs_key}）`}
          </option>
        ))}
      </select>
      {switching && <span className="muted">切换中…</span>}
    </label>
  )
}
