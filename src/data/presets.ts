import type { PresetCase } from '../runtime/types'

export const PRESET_CASES: PresetCase[] = [
  {
    id: 'green-finance-did',
    title: '绿色金融试验区政策评估',
    description: '企业—年度面板；检验政策是否促进企业绿色创新。',
    method: 'DID / 多期 DID',
  },
  {
    id: 'esg-panel',
    title: 'ESG 与企业融资成本',
    description: '企业—年度面板；检验 ESG 表现与融资环境的关系。',
    method: '双向固定效应',
  },
]
