import request from '../utils/request'

export type TaskAssetCategory = 'proto' | 'data'

const DIRECT_UPLOAD_MIN_BYTES = 50 * 1024 * 1024

export interface TaskAssetShard {
  shard_index?: number | null
  index?: number | null
  file_name?: string | null
  storage_key?: string | null
  file_size?: number | null
  line_count?: number | null
  content_hash?: string | null
  [key: string]: unknown
}

export interface TaskAssetShardManifest {
  shard_count?: number | null
  shards?: TaskAssetShard[] | null
  [key: string]: unknown
}

export interface TaskAssetMetadata {
  shard_count?: number | null
  shardCount?: number | null
  shards?: TaskAssetShard[] | null
  shard_manifest?: TaskAssetShardManifest | TaskAssetShard[] | null
  shardManifest?: TaskAssetShardManifest | TaskAssetShard[] | null
  requires_shard?: boolean | null
  requiresShard?: boolean | null
  shard_required?: boolean | null
  shardRequired?: boolean | null
  [key: string]: unknown
}

export interface TaskAsset {
  id: number
  task_id?: number | null
  category: TaskAssetCategory
  file_name: string
  file_path: string
  file_size: number
  content_hash?: string | null
  storage_type?: 'local' | 's3' | string
  compression_type?: string | null
  compressed_file_size?: number | null
  line_count?: number | null
  shard_count?: number | null
  shard_manifest?: TaskAssetShardManifest | TaskAssetShard[] | null
  ingest_status?: string | null
  ingest_error?: string | null
  metadata?: TaskAssetMetadata | null
  metadata_json?: TaskAssetMetadata | null
  created_by?: number | null
  created_at: string
  updated_at?: string | null
}

export interface TaskAssetDirectUploadCreateRequest {
  category: TaskAssetCategory
  file_name: string
  file_size: number
  content_hash_sha256: string
  content_type?: string | null
  task_id?: number | null
}

export interface TaskAssetDirectUploadSession {
  session_id: string
  upload_method: 'PUT'
  upload_url: string
  upload_headers: Record<string, string>
  bucket: string
  object_key: string
  object_uri: string
  expires_in_seconds: number
  expires_at: number
  finalize_token: string
}

const toSha256Hex = async (file: File): Promise<string> => {
  const subtle = globalThis.crypto?.subtle
  if (!subtle) {
    throw new Error('当前浏览器不支持直传文件校验')
  }
  const digest = await subtle.digest('SHA-256', await file.arrayBuffer())
  return Array.from(new Uint8Array(digest))
    .map(byte => byte.toString(16).padStart(2, '0'))
    .join('')
}

const shouldUseDirectUpload = (category: TaskAssetCategory, file: File): boolean => {
  if (category !== 'data') {
    return false
  }
  return file.size > DIRECT_UPLOAD_MIN_BYTES || file.name.toLowerCase().endsWith('.zip')
}

const isS3UnavailableError = (error: unknown): boolean => {
  const message = error instanceof Error ? error.message : String(error ?? '')
  return /direct task asset upload requires minio\/s3|requires minio\/s3/i.test(message)
}

export const taskAssetApi = {
  uploadAsset: (category: TaskAssetCategory, file: File, taskId?: number): Promise<TaskAsset> => {
    const formData = new FormData()
    formData.append('file', file)
    return request.post(
      `/task-assets/upload?category=${category}${taskId ? `&task_id=${taskId}` : ''}`,
      formData,
      {
        headers: { 'Content-Type': 'multipart/form-data' },
      }
    )
  },

  uploadAssetForTask: async (
    category: TaskAssetCategory,
    file: File,
    taskId?: number
  ): Promise<TaskAsset> => {
    if (!shouldUseDirectUpload(category, file)) {
      return taskAssetApi.uploadAsset(category, file, taskId)
    }

    let session: TaskAssetDirectUploadSession
    try {
      session = await taskAssetApi.createDirectUploadSession({
        category,
        file_name: file.name,
        file_size: file.size,
        content_hash_sha256: await toSha256Hex(file),
        content_type: file.type || null,
        task_id: taskId ?? null,
      })
    } catch (error) {
      if (file.size <= DIRECT_UPLOAD_MIN_BYTES && isS3UnavailableError(error)) {
        return taskAssetApi.uploadAsset(category, file, taskId)
      }
      throw error
    }

    await taskAssetApi.uploadDirectObject(session, file)
    return taskAssetApi.finalizeDirectUploadSession(session)
  },

  createDirectUploadSession: (
    payload: TaskAssetDirectUploadCreateRequest
  ): Promise<TaskAssetDirectUploadSession> => {
    return request.post('/task-assets/direct-upload-sessions', payload)
  },

  uploadDirectObject: async (session: TaskAssetDirectUploadSession, file: File): Promise<void> => {
    const response = await fetch(session.upload_url, {
      method: session.upload_method,
      headers: session.upload_headers,
      body: file,
    })
    if (!response.ok) {
      throw new Error(`Direct asset upload failed: ${response.status}`)
    }
  },

  finalizeDirectUploadSession: (session: TaskAssetDirectUploadSession): Promise<TaskAsset> => {
    return request.post(`/task-assets/direct-upload-sessions/${session.session_id}/finalize`, {
      finalize_token: session.finalize_token,
    })
  },

  listAssets: (taskId: number, category?: TaskAssetCategory): Promise<TaskAsset[]> => {
    return request.get('/task-assets', {
      params: {
        task_id: taskId,
        category,
      },
    })
  },

  bindAssets: (taskId: number, assetIds: number[]): Promise<TaskAsset[]> => {
    return request.post('/task-assets/bind', {
      task_id: taskId,
      asset_ids: assetIds,
    })
  },

  downloadAsset: (assetId: number): Promise<Blob> => {
    return request.get(`/task-assets/${assetId}/download`, {
      responseType: 'blob',
      skipErrorHandler: true,
    })
  },

  deleteAsset: (assetId: number): Promise<null> => {
    return request.delete(`/task-assets/${assetId}`)
  },
}
