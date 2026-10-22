import api from './axios'
import type { RankingPage, RankingPeriod, OwnRank, PublicLearner } from '../types/api'
export const getLeaderboard = async (period: RankingPeriod, page: number): Promise<RankingPage> => (await api.get<RankingPage>('/public/leaderboard', { params: { period, page, page_size: 20 } })).data
export const getMyRank = async (period: RankingPeriod): Promise<OwnRank> => (await api.get<OwnRank>('/leaderboard/me', { params: { period } })).data
export const getPublicLearner = async (username: string): Promise<PublicLearner> => (await api.get<PublicLearner>('/public/learners/' + encodeURIComponent(username))).data
