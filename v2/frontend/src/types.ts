export interface Character {
  id: number
  slug: string
  name: string
  display_name: string
  version: string
  avatar: string
  greeting: string
}

export interface Session {
  id: string
  title: string
  has_user_message: boolean
  created_at: string
  updated_at: string
}

export interface Message {
  id: number
  role: 'user' | 'assistant'
  content: string
  created_at: string
}
