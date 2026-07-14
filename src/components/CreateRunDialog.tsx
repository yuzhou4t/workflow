import { X } from 'lucide-react'
import { useState } from 'react'
import { PRESET_CASES } from '../data/presets'
import type { CreateRunInput } from '../runtime/types'

interface CreateRunDialogProps {
  open: boolean
  busy: boolean
  onClose: () => void
  onCreate: (input: CreateRunInput) => Promise<void>
}

export function CreateRunDialog({ open, busy, onClose, onCreate }: CreateRunDialogProps) {
  const [presetId, setPresetId] = useState(PRESET_CASES[0].id)
  if (!open) return null

  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className="create-run-dialog" role="dialog" aria-modal="true" aria-labelledby="create-run-title">
        <button type="button" className="dialog-close" onClick={onClose} aria-label="关闭新建 Run"><X size={18} /></button>
        <header>
          <span>从标准案例包启动</span>
          <h2 id="create-run-title">新建研究 Run</h2>
          <p>第一版使用预设案例验证代码状态机和三个人类闸门。</p>
        </header>
        <div className="preset-list">
          {PRESET_CASES.map((preset) => (
            <label key={preset.id} className={presetId === preset.id ? 'is-selected' : ''}>
              <input type="radio" name="preset" value={preset.id} checked={presetId === preset.id} onChange={() => setPresetId(preset.id)} />
              <strong>{preset.title}</strong>
              <span>{preset.description}</span>
              <small>{preset.method}</small>
            </label>
          ))}
        </div>
        <div className="fixture-mode-note">
          <strong>Fixture 流程演示</strong>
          <span>只验证代码状态机、提示词流转和人工闸门，不生成或冒充真实实证结果。</span>
        </div>
        <div className="dialog-actions">
          <button type="button" onClick={onClose}>取消</button>
          <button
            type="button"
            className="gate-primary"
            disabled={busy}
            onClick={() => onCreate({ presetId, mode: 'fixture' })}
          >
            {busy ? '正在创建…' : '创建 Run'}
          </button>
        </div>
      </section>
    </div>
  )
}
