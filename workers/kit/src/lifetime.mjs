export const CHECK_MS = 1_000

function alive(pid) {
  try {
    process.kill(pid, 0)
    return true
  } catch (error) {
    return error.code === 'EPERM'
  }
}

export function watchOwner({
  ownerPid = Number(process.env.EHX_OWNER_PID || 0),
  idleMs = Number(process.env.EHX_IDLE_EXIT_MS || 0),
  watchStdin = Boolean(process.env.EHX_OWNER_PIPE),
  exit = (reason) => {
    console.error(`[worker] exiting: ${reason}`)
    process.exit(0)
  },
  checkMs = CHECK_MS,
  ppid = () => process.ppid,
  isAlive = alive,
  now = () => Date.now(),
} = {}) {
  let last = now()
  let stopped = false
  const stop = (reason) => {
    if (stopped) return
    stopped = true
    clearInterval(timer)
    exit(reason)
  }
  const timer = setInterval(() => {
    if (!ownerPid && ppid() === 1) return stop('the owner process is gone (re-parented to init)')
    if (ownerPid && !isAlive(ownerPid)) return stop(`the owner process ${ownerPid} is gone`)
    if (idleMs && now() - last > idleMs) return stop(`idle for more than ${idleMs} ms`)
  }, checkMs)
  timer.unref?.()
  if (watchStdin && process.stdin) {
    process.stdin.on('end', () => stop('the owner closed its pipe'))
    process.stdin.on('error', () => stop('the owner pipe failed'))
    process.stdin.resume()
  }
  return {
    touch: () => {
      last = now()
    },
    stop: () => {
      stopped = true
      clearInterval(timer)
    },
  }
}
