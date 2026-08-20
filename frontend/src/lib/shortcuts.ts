import { CirclePlus, GraduationCap, Mail, MessageSquareText } from 'lucide-react'

export const SHORTCUTS = [
  {
    id: 'k12',
    label: 'K12',
    query: 'K12',
    detail: 'GPT Team 与教育版',
    icon: GraduationCap,
    color: '#7cb8ff',
  },
  {
    id: 'plus',
    label: 'ChatGPT Plus',
    query: 'ChatGPT Plus',
    detail: '独享号、订阅与成品号',
    icon: CirclePlus,
    color: '#5ecfa8',
  },
  {
    id: 'email',
    label: '邮箱',
    query: '邮箱',
    detail: 'Gmail、Outlook 等',
    icon: Mail,
    color: '#ffbe55',
  },
  {
    id: 'sms',
    label: 'OpenAI 接码',
    query: 'OpenAI 接码',
    detail: '只查 ChatGPT 验证码',
    icon: MessageSquareText,
    color: '#ff8fab',
  },
] as const

export type ShortcutId = (typeof SHORTCUTS)[number]['id']

export function shortcutId(value: string): ShortcutId | null {
  const compact = value.toLowerCase().replace(/\s+/g, '')
  if (compact.includes('k12')) return 'k12'
  if (/邮箱|email|gmail|outlook|hotmail|icloud/.test(compact)) return 'email'
  if (compact.includes('接码') || compact.includes('sms')) return 'sms'
  if (compact.includes('plus')) return 'plus'
  return null
}
