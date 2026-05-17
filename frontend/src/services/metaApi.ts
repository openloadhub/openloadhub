import request from '../utils/request'

export interface MetaItem {
  code: string
  name: string
  scope?: 'test' | 'testnet' | 'main'
}

export const metaApi = {
  getBusinessLines: (): Promise<MetaItem[]> => {
    return request.get('/business-lines')
  },
  getEnvironments: (): Promise<MetaItem[]> => {
    return request.get('/environments')
  },
}
