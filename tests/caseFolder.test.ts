import { describe, expect, it } from 'vitest'
import { selectCaseFolder } from '../src/runtime/caseFolder'

function folderFile(path: string, size = 10): File {
  const file = new File(['x'.repeat(size)], path.split('/').at(-1) ?? path)
  Object.defineProperty(file, 'webkitRelativePath', { value: path })
  return file
}

describe('selectCaseFolder', () => {
  it('selects the main CSV and keeps paper and code files out of the upload', () => {
    const result = selectCaseFolder([
      folderFile('案例1/.DS_Store'),
      folderFile('案例1/ESG-SDLA-代码.do'),
      folderFile('案例1/ESG-SDLA-数据.csv', 100),
      folderFile('案例1/ESG-SDLA-数据.dta', 200),
      folderFile('案例1/case_profile.json'),
      folderFile('案例1/原始论文1/企业ESG表现与短债长用—附录.docx'),
      folderFile('案例1/原始论文1/企业esg表现与短债长用.pdf'),
    ])

    expect(result.mainData.name).toBe('ESG-SDLA-数据.csv')
    expect(result.caseProfile?.name).toBe('case_profile.json')
    expect(result.hiddenFileCount).toBe(3)
    expect(result.excludedFileCount).toBe(2)
  })

  it('rejects a folder without visible CSV analysis data', () => {
    expect(() => selectCaseFolder([
      folderFile('案例1/02_hidden_reference/result.csv'),
      folderFile('案例1/data.dta'),
    ])).toThrow('没有可用的 CSV')
  })

  it('prefers the canonical main_data CSV over a larger dictionary CSV', () => {
    const result = selectCaseFolder([
      folderFile('case/01_model_input/data_dictionary.csv', 500),
      folderFile('case/01_model_input/main_data.csv', 100),
      folderFile('case/01_model_input/case_profile.json'),
      folderFile('case/02_hidden_reference/reference_tables.csv', 1000),
    ])

    expect(result.mainData.name).toBe('main_data.csv')
    expect(result.caseProfile?.name).toBe('case_profile.json')
    expect(result.hiddenFileCount).toBe(1)
    expect(result.excludedFileCount).toBe(1)
  })
})
