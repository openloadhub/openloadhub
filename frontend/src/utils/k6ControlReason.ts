export const formatK6ControlReason = (reason?: string | null): string => {
  const normalized = String(reason || '').trim()
  if (!normalized) {
    return '当前运行暂不可控制能力'
  }
  if (normalized.includes('scenario_direct_upshift_blocked_recent_runtime_backpressure')) {
    return '检测到 recent VU buffer/backpressure，且当前观测吞吐未贴近当前档位目标，已阻止继续上调 TPS；VU 数本身不是固定 TPS 上限，建议先看当前吞吐、分段上调，或增加执行节点后重跑。'
  }
  if (normalized.includes('scenario_direct_runtime_not_applied')) {
    return 'K6 scenario direct 调整已进入后台处理，但短窗口内 TPS 仍在收敛；请观察控制能力状态和监控 TPS。'
  }
  if (normalized.includes('scenario_direct_downshift_temporarily_disabled')) {
    return '当前标准 K6 脚本暂不支持运行中下调 TPS；请停止后按新的目标 TPS 重跑。'
  }
  if (normalized.includes('k6_control_unreachable')) {
    return 'agent 的 k6 控制端口不可达，暂时无法下发控制能力；请检查 agent/runtime 状态。'
  }
  if (normalized === 'curl_runtime_control_temporarily_disabled') {
    return '当前 curl 生成脚本暂不支持运行中调 TPS；请在启动前设置目标 TPS。'
  }
  if (normalized === 'no_controllable_agents') {
    return '当前没有可控 agent，暂时无法下发控制能力。'
  }
  if (normalized === 'not_all_agents_support_k6_control') {
    return '当前有 agent 不支持 K6 控制能力，暂时无法对全部运行下发调整。'
  }
  if (normalized === 'target_tps_required') {
    return '请填写有效的目标 TPS 后再下发控制能力。'
  }
  if (normalized === 'no_running_k6_runs_with_base_target_tps') {
    return '当前没有带基线 target_tps 的运行中 K6 Run，暂时无法广播控制能力。'
  }
  if (normalized === 'invalid_base_total_tps_for_broadcast') {
    return '当前批次基线总 TPS 无效，无法计算广播比例；请确认运行参数中的 target_tps。'
  }
  if (normalized === 'invalid_broadcast_ratio') {
    return '本次广播倍率无效；请填写大于 0 的倍率或有效总 TPS。'
  }
  if (normalized.startsWith('invalid_')) {
    return '控制能力参数无效；请检查目标 TPS、倍率和运行状态后重试。'
  }
  return normalized
}

export const formatK6ControlActionError = (message?: string | null): string => {
  const normalized = String(message || '').trim()
  if (!normalized) {
    return '下发 K6 控制能力失败'
  }
  return formatK6ControlReason(normalized)
}

export const formatK6ControlReasonList = (items?: Array<string | null | undefined> | null): string[] =>
  (items ?? [])
    .filter(item => String(item || '').trim().length > 0)
    .map(item => formatK6ControlReason(item))
    .filter(item => item.length > 0)
