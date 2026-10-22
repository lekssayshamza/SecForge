import type { PortRule } from '../types/api'

export interface PortInput { protocol: 'tcp' | 'udp'; start: string; end: string }
export const defaultPorts: PortRule[] = [22, 80].map(port => ({ protocol: 'tcp', from_port: port, to_port: port }))

export function parsePortInputs(rows: PortInput[]): PortRule[] {
  if (rows.length > 20) throw new Error('Use at most 20 port rules.')
  const rules = rows.map(row => {
    const end = row.end || row.start
    if (!['tcp', 'udp'].includes(row.protocol) || !/^\d+$/.test(row.start) || !/^\d+$/.test(end)) {
      throw new Error('Choose TCP or UDP and enter whole port numbers from 1 to 65535.')
    }
    const from_port = Number(row.start), to_port = Number(end)
    if (from_port < 1 || to_port > 65535 || to_port < from_port) {
      throw new Error('Use ports from 1 to 65535, with the range end at least the start.')
    }
    return { protocol: row.protocol, from_port, to_port }
  }).sort((a, b) => a.protocol.localeCompare(b.protocol) || a.from_port - b.from_port)
  const merged: PortRule[] = []
  for (const rule of rules) {
    const previous = merged[merged.length - 1]
    if (previous?.protocol === rule.protocol && rule.from_port <= previous.to_port + 1) previous.to_port = Math.max(previous.to_port, rule.to_port)
    else merged.push({ ...rule })
  }
  if (merged.reduce((total, rule) => total + rule.to_port - rule.from_port + 1, 0) > 4096) throw new Error('Expose at most 4096 TCP/UDP ports per room.')
  return merged
}
