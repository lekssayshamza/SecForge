export interface User {
  creator_access?: boolean
  id: number
  username: string
  email: string
  role: 'learner' | 'creator' | 'admin'
  display_name?: string | null
  avatar_style?: AvatarStyle
  avatar_color?: AvatarColor
  must_change_password?: boolean
  bio?: string | null
  avatar_version?: string | null
  avatar_public?: boolean
  bio_public?: boolean
  created_at?: string | null
  password_enabled?: boolean
}

export type AvatarStyle = 'initials' | 'terminal' | 'compass' | 'code' | 'shield' | 'network'
export type AvatarColor = 'red' | 'navy' | 'green' | 'blue' | 'purple'
export interface ProfileUpdate {
  avatar_public?: boolean
  bio_public?: boolean
  bio?: string | null
  username?: string
  display_name?: string | null
  avatar_style?: AvatarStyle
  avatar_color?: AvatarColor
}

export interface LoginRequest {
  username: string
  password: string
}

export interface TokenResponse {
  access_token: string
  token_type: string
}

export interface PortRule { protocol: 'tcp' | 'udp'; from_port: number; to_port: number }

export interface Room {
  exposed_ports?: PortRule[]
  categories?: string[]
  room_type?: 'THEORY' | 'PRACTICAL'
  objectives?: string
  prerequisites?: string
  id: number
  creator_id: number
  name: string
  description: string | null
  category: string
  difficulty: string
  status: string
  created_at: string
  updated_at: string
}

export type PublicRoom = Pick<Room, 'id' | 'name' | 'description' | 'category' | 'categories' | 'difficulty' | 'room_type' | 'objectives' | 'prerequisites'> & { points_total?: number | null }
export interface PublicRoomPage {
  items: PublicRoom[]
  total: number
  page: number
  page_size: number
  categories: string[]
}

export interface RoomCreate {
  exposed_ports?: PortRule[]
  categories?: string[]
  room_type?: 'THEORY' | 'PRACTICAL'
  objectives?: string
  prerequisites?: string
  name: string
  description?: string
  category: string
  difficulty: string
}

export interface RoomUpdate {
  exposed_ports?: PortRule[]
  categories?: string[]
  objectives?: string
  prerequisites?: string
  name?: string
  description?: string
  category?: string
  difficulty?: string
}

export interface BuilderStartRequest {
  ttl_hours: number
}

export interface Builder {
  ssh_auth_mode?: 'key' | 'password'
  ssh_guest_username?: string | null
  id: number
  room_id: number
  status: string
  private_ip: string | null
  instance_id: string | null
  security_group_id: string | null
  expires_at: string
  created_at: string
  updated_at: string
  terraform_state_path: string
  error_message: string | null
  ssh_username?: string
}

export interface LabStartRequest {
  ttl_hours: number
}

export interface Lab {
  id: number
  room_id: number
  status: string
  private_ip: string | null
  instance_id: string | null
  security_group_id: string | null
  expires_at: string
  created_at: string
  updated_at: string
  stopped_at: string | null
  error_message: string | null
  session_started_at?: string | null
  extensions_used?: number
  expired_at?: string | null
  server_time?: string
  clock_received_at?: number
}

export interface MachineTemplate {
  id: number
  room_template_id: number
  role: string
  ami_id: string | null
  instance_type: string
  status: string
  error_message: string | null
  created_at: string
  updated_at: string
}

export interface RoomTemplate {
  id: number
  room_id: number
  version: number
  created_by: number
  is_published: boolean
  created_at: string
  machines: MachineTemplate[]
}
export interface Question {
  task_id?: number | null
  id: number
  question_text: string
  hint: string | null
  points: number
  display_order: number
}
export interface QuestionList { questions: Question[]; authoring_locked: boolean }
export interface QuestionWrite { task_id?: number | null; question_text: string; hint: string | null; points?: number; answer?: string }
export interface RoomProgress {
  room_id: number
  solved_question_ids: number[]
  questions_solved: number
  questions_total: number
  points_earned: number
  points_total: number
  completed: boolean
  completed_at: string | null
}
export interface SubmissionResult {
  correct: boolean
  already_solved: boolean
  points_awarded: number
  progress: RoomProgress
}

export interface AcceptedAnswer { question_id: number; answer: string | null; points_awarded: number; legacy: boolean }

export interface LearningStats { points: number; rooms_completed: number; rooms_in_progress: number; questions_solved: number }
export interface Achievement { id: string; name: string; description: string; earned: boolean; earned_at: string | null; progress: number; target: number }
export interface LearningRoom { id: number; name: string; category: string | null; difficulty: string | null; available: boolean; solved: number; questions: number; percentage: number | null; points: number; last_activity: string; completed_at: string | null }
export interface LearningActivity { id: string; kind: 'answer' | 'room_completed'; room_id: number; room_name: string; available: boolean; points: number; occurred_at: string }
export interface ActivityPage { items: LearningActivity[]; total: number; page: number; page_size: number }
export interface LearningOverview { stats: LearningStats; achievements: Achievement[]; continue_learning: LearningRoom[]; completed_rooms: LearningRoom[]; activity: LearningActivity[]; recommendations: PublicRoom[]; recommendation_label: string }

export type RankingPeriod = 'all-time' | 'weekly' | 'monthly'
export interface PublicIdentity { avatar_url?: string | null; username: string; display_name: string | null; avatar_style: AvatarStyle; avatar_color: AvatarColor }
export interface Ranking extends PublicIdentity { rank: number; points: number; rooms_completed: number }
export interface RankingPage { period: RankingPeriod; starts_at: string | null; ends_at: string | null; server_time: string; items: Ranking[]; leaders: Ranking[]; total: number; page: number; page_size: number }
export interface OwnRank { period: RankingPeriod; rank: number | null; points: number; rooms_completed: number; eligible: boolean; reason: 'ranked' | 'no-points' | 'learners-only' }
export interface PublicLearner extends PublicIdentity { bio?: string | null; points: number; rooms_completed: number; questions_solved: number; achievements: { id: string; name: string; description: string; earned_at: string }[] }

export interface RoomTask { id: number; title: string; instructions: string; display_order: number }
export interface TaskList { tasks: RoomTask[] }
