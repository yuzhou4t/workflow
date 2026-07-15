const HIDDEN_SUFFIXES = new Set(['.pdf', '.doc', '.docx', '.do', '.r', '.rmd', '.py', '.ipynb', '.log'])
const HIDDEN_PATH_MARKERS = ['hidden', 'reference', 'references', 'gold', '原始论文']

export interface CaseFolderSelection {
  mainData: File
  caseProfile?: File
  hiddenFileCount: number
  excludedFileCount: number
}

function relativePath(file: File): string {
  return file.webkitRelativePath || file.name
}

function suffix(path: string): string {
  const filename = path.split('/').at(-1) ?? path
  const dot = filename.lastIndexOf('.')
  return dot >= 0 ? filename.slice(dot).toLocaleLowerCase() : ''
}

function isHiddenReference(file: File): boolean {
  const path = relativePath(file)
  if (HIDDEN_SUFFIXES.has(suffix(path))) return true
  const folders = path.split('/').slice(0, -1)
  return folders.some((folder) => HIDDEN_PATH_MARKERS.some((marker) => folder.toLocaleLowerCase().includes(marker)))
}

function mainCsvOrder(left: File, right: File): number {
  const leftIsCanonical = /^main_data\.csv$/i.test(left.name)
  const rightIsCanonical = /^main_data\.csv$/i.test(right.name)
  if (leftIsCanonical !== rightIsCanonical) return leftIsCanonical ? -1 : 1
  const leftLooksLikeData = /数据|data/i.test(left.name)
  const rightLooksLikeData = /数据|data/i.test(right.name)
  if (leftLooksLikeData !== rightLooksLikeData) return leftLooksLikeData ? -1 : 1
  if (left.size !== right.size) return right.size - left.size
  return left.name.localeCompare(right.name, 'zh-CN')
}

export function selectCaseFolder(files: Iterable<File>): CaseFolderSelection {
  const allFiles = Array.from(files)
  if (!allFiles.length) throw new Error('所选案例文件夹为空。')

  const hiddenFiles = allFiles.filter(isHiddenReference)
  const visibleFiles = allFiles.filter((file) => !isHiddenReference(file))
  const csvFiles = visibleFiles.filter((file) => suffix(relativePath(file)) === '.csv').sort(mainCsvOrder)
  if (!csvFiles.length) throw new Error('所选案例文件夹中没有可用的 CSV 主分析数据。')
  const caseProfile = visibleFiles.find((file) => /^(case_profile|case-profile)\.json$/i.test(file.name))

  return {
    mainData: csvFiles[0],
    caseProfile,
    hiddenFileCount: hiddenFiles.length,
    excludedFileCount: visibleFiles.length - 1 - (caseProfile ? 1 : 0),
  }
}
