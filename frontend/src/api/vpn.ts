import api from './axios'

export interface VpnProfileDownload {
  blob: Blob
  contentDisposition: string | null
}

export const downloadVpnProfile = async (): Promise<VpnProfileDownload> => {
  const response = await api.get<Blob>('/vpn/profile', { responseType: 'blob' })
  return {
    blob: response.data,
    contentDisposition: response.headers['content-disposition'] ?? null,
  }
}
