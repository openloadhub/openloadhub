import type { Task, TaskStatus } from '@/services/taskApi'
import type { TaskAsset } from '@/services/taskAssetApi'

type LatestRunStatus = NonNullable<Task['last_run_status']>
export type TaskStartGuardStatus = TaskStatus | 'preparing'

export const LARGE_DATA_ASSET_ALL_DISTRIBUTION_LIMIT_BYTES = 200 * 1024 * 1024

const ACTIVE_LATEST_RUN_STATUSES = new Set<LatestRunStatus>(['preparing', 'running'])

const TASK_STATUS_LABELS: Record<TaskStatus, string> = {
  draft: '草稿',
  pending_submit_approval: '待处理',
  ready: '可执行',
  script_missing: '脚本缺失',
  awaiting_approval: '待处理',
  approval_rejected: '待处理',
  running: '执行中',
}

const LATEST_RUN_STATUS_LABELS: Record<LatestRunStatus, string> = {
  preparing: '准备中',
  running: '运行中',
  succeeded: '成功',
  failed: '失败',
  stopped: '已停止',
}

type TaskStartGuardInput =
  | Pick<Task, 'status' | 'last_run_status' | 'last_run_status_label'>
  | null
  | undefined
type MixedRunTaskCandidateInput =
  | Pick<
      Task,
      'status' | 'last_run_status' | 'last_run_status_label' | 'task_pattern' | 'script_id'
    >
  | null
  | undefined
type TaskAssetDistributionGuardInput =
  | Pick<Task, 'data_distribution' | 'properties' | 'data_assets'>
  | null
  | undefined

export interface DataAssetAllDistributionGuardResult {
  blocked: boolean
  reason?: 'large_file' | 'shard_required'
  limitBytes: number
  asset?: TaskAsset
  shardCount?: number | null
  distribution?: string | null
  message?: string
  description?: string
}

const PREPARABLE_TASK_STATUSES = new Set<TaskStatus>([
  'draft',
  'pending_submit_approval',
  'approval_rejected',
  'script_missing',
  'ready',
])

export const getTaskEffectiveStartStatus = (
  task: TaskStartGuardInput
): TaskStartGuardStatus | undefined => {
  if (task?.last_run_status && ACTIVE_LATEST_RUN_STATUSES.has(task.last_run_status)) {
    return task.last_run_status as TaskStartGuardStatus
  }
  return task?.status
}

export const getTaskEffectiveStatusLabel = (task: TaskStartGuardInput): string => {
  if (task?.last_run_status && ACTIVE_LATEST_RUN_STATUSES.has(task.last_run_status)) {
    return task.last_run_status_label || LATEST_RUN_STATUS_LABELS[task.last_run_status]
  }
  if (!task?.status) {
    return '-'
  }
  return TASK_STATUS_LABELS[task.status] || task.status
}

export const canStartTask = (task: TaskStartGuardInput): boolean => {
  if (!task) {
    return false
  }
  return (
    task.status === 'ready' &&
    task.last_run_status !== 'preparing' &&
    task.last_run_status !== 'running'
  )
}

export const canPrepareTaskForRun = (task: TaskStartGuardInput): boolean => {
  if (!task) {
    return false
  }
  if (task.last_run_status === 'preparing' || task.last_run_status === 'running') {
    return false
  }
  return PREPARABLE_TASK_STATUSES.has(task.status)
}

export const canEnterStartFlow = (task: TaskStartGuardInput): boolean => {
  return canStartTask(task) || canPrepareTaskForRun(task)
}

export const canJoinMixedRunTask = (task: MixedRunTaskCandidateInput): boolean => {
  if (!task) {
    return false
  }
  if (task.last_run_status === 'preparing' || task.last_run_status === 'running') {
    return false
  }
  if (task.status === 'running' || task.status === 'script_missing') {
    return false
  }
  if (task.task_pattern !== 'script') {
    return false
  }
  return Number(task.script_id) > 0
}

export const getMixedRunCandidateStatusLabel = (task: MixedRunTaskCandidateInput): string => {
  if (!task) {
    return '-'
  }
  if (task.last_run_status) {
    return (
      task.last_run_status_label ||
      LATEST_RUN_STATUS_LABELS[task.last_run_status] ||
      task.last_run_status
    )
  }
  if (canJoinMixedRunTask(task) && task.status !== 'ready') {
    return '可加入'
  }
  return getTaskEffectiveStatusLabel(task)
}

const isRecord = (value: unknown): value is Record<string, unknown> => {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

const toPositiveNumber = (value: unknown): number | null => {
  const numeric = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(numeric) && numeric > 0 ? numeric : null
}

export const normalizeDataDistribution = (value: unknown): string | null => {
  if (typeof value !== 'string') {
    return null
  }
  const normalized = value.trim().toLowerCase()
  if (['all', 'full', 'full_data'].includes(normalized)) {
    return 'all'
  }
  if (['avg', 'average', 'split', 'avg_split_data'].includes(normalized)) {
    return 'avg'
  }
  return normalized || null
}

const getManifestShardCount = (manifest: unknown): number | null => {
  if (Array.isArray(manifest)) {
    return manifest.length > 0 ? manifest.length : null
  }
  if (!isRecord(manifest)) {
    return null
  }
  const explicitCount = toPositiveNumber(manifest.shard_count ?? manifest.shardCount)
  if (explicitCount !== null) {
    return explicitCount
  }
  return Array.isArray(manifest.shards) && manifest.shards.length > 0
    ? manifest.shards.length
    : null
}

export const getTaskAssetShardCount = (asset: TaskAsset): number | null => {
  const directCount = toPositiveNumber(asset.shard_count)
  if (directCount !== null) {
    return directCount
  }
  const directManifestCount = getManifestShardCount(asset.shard_manifest)
  if (directManifestCount !== null) {
    return directManifestCount
  }
  for (const metadata of [asset.metadata_json, asset.metadata]) {
    if (!isRecord(metadata)) {
      continue
    }
    const metadataCount = toPositiveNumber(metadata.shard_count ?? metadata.shardCount)
    if (metadataCount !== null) {
      return metadataCount
    }
    if (Array.isArray(metadata.shards) && metadata.shards.length > 0) {
      return metadata.shards.length
    }
    const metadataManifestCount = getManifestShardCount(
      metadata.shard_manifest ?? metadata.shardManifest
    )
    if (metadataManifestCount !== null) {
      return metadataManifestCount
    }
  }
  return null
}

const hasShardRequiredMarker = (asset: TaskAsset): boolean => {
  for (const metadata of [asset.metadata_json, asset.metadata]) {
    if (!isRecord(metadata)) {
      continue
    }
    const marker =
      metadata.requires_shard ??
      metadata.requiresShard ??
      metadata.shard_required ??
      metadata.shardRequired ??
      metadata.large_file_requires_shard ??
      metadata.largeFileRequiresShard
    if (marker === true) {
      return true
    }
  }
  const shardCount = getTaskAssetShardCount(asset)
  return typeof shardCount === 'number' && shardCount > 1
}

const formatBytesForGuard = (bytes: number): string => {
  if (bytes < 1024 * 1024) {
    return `${Math.round(bytes / 1024)} KB`
  }
  return `${(bytes / (1024 * 1024)).toFixed(0)} MB`
}

export const evaluateDataAssetAllDistributionGuard = (
  task: TaskAssetDistributionGuardInput,
  limitBytes = LARGE_DATA_ASSET_ALL_DISTRIBUTION_LIMIT_BYTES
): DataAssetAllDistributionGuardResult => {
  const propertiesDistribution = isRecord(task?.properties)
    ? normalizeDataDistribution(task.properties.data_distribution)
    : null
  const distribution = normalizeDataDistribution(task?.data_distribution) ?? propertiesDistribution
  if (distribution !== 'all') {
    return { blocked: false, limitBytes, distribution }
  }

  const dataAssets = (task?.data_assets ?? []).filter(asset => asset.category === 'data')
  const oversizedAsset = dataAssets.find(asset => Number(asset.file_size) > limitBytes)
  if (oversizedAsset) {
    return {
      blocked: true,
      reason: 'large_file',
      limitBytes,
      distribution,
      asset: oversizedAsset,
      shardCount: getTaskAssetShardCount(oversizedAsset),
      message: '大数据文件禁止 all 分发',
      description: `${oversizedAsset.file_name} 大于 ${formatBytesForGuard(limitBytes)}，all 模式会让每个执行节点完整下载同一份数据。请切换为平均分割后再启动。`,
    }
  }

  const shardRequiredAsset = dataAssets.find(hasShardRequiredMarker)
  if (shardRequiredAsset) {
    const shardCount = getTaskAssetShardCount(shardRequiredAsset)
    return {
      blocked: true,
      reason: 'shard_required',
      limitBytes,
      distribution,
      asset: shardRequiredAsset,
      shardCount,
      message: '分片数据禁止 all 分发',
      description: `${shardRequiredAsset.file_name} 已带分片元数据${shardCount ? `（${shardCount} 片）` : ''}，all 模式会绕过分片消费并放大下载压力。请切换为平均分割后再启动。`,
    }
  }

  return { blocked: false, limitBytes, distribution }
}
