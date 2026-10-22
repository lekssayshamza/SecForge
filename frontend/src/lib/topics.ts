import type { IconName } from '../components/Icon'
export function roomTopic(category: string): 'web' | 'network' | 'code' | 'crypto' | 'system' {
  const topic = category.toLowerCase()
  if (/web|http/.test(topic)) return 'web'
  if (/network|packet/.test(topic)) return 'network'
  if (/crypt|password/.test(topic)) return 'crypto'
  if (/code|revers|program/.test(topic)) return 'code'
  return 'system'
}
export function topicIcon(category: string): IconName {
  return ({ web: 'web', network: 'network', code: 'code', crypto: 'key', system: 'terminal' } as const)[roomTopic(category)]
}
