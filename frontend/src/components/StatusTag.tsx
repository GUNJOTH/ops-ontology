import { Tag } from 'antd'
import type { CandidateReviewState, CandidateValidatorStatus } from '../api/types'

const labels: Record<CandidateReviewState | CandidateValidatorStatus, string> = {
  pending: '待审核', approved: '已通过', modified: '已修改通过', rejected: '已拒绝', deferred: '待补证据',
  candidate: '候选', needs_review: '需复核', blocked: '已阻断',
}

const colors: Record<CandidateReviewState | CandidateValidatorStatus, string> = {
  pending: 'purple', approved: 'green', modified: 'green', rejected: 'red', deferred: 'orange',
  candidate: 'blue', needs_review: 'orange', blocked: 'red',
}

export function StatusTag({ value }: { value: CandidateReviewState | CandidateValidatorStatus }) {
  return <Tag color={colors[value]}>{labels[value]}</Tag>
}
