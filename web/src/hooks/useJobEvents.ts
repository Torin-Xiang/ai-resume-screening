import { useEffect, useRef, useState } from 'react'

export type StreamHandler = (event: string, data: unknown) => void

/**
 * 订阅岗位事件流。
 *
 * 存量和刷新都靠这个：上传后 HR 盯着的就是这张排名表，
 * 让"处理中 → 完成 → 排到正确位置"就地发生，而不是让用户手动刷新页面。
 */
export function useJobEvents(jobId: string | null, onEvent: StreamHandler): boolean {
  const handlerRef = useRef(onEvent)
  useEffect(() => {
    handlerRef.current = onEvent
  })

  const [connected, setConnected] = useState(false)

  useEffect(() => {
    if (!jobId) {
      setConnected(false)
      return
    }

    const source = new EventSource(`/api/jobs/${jobId}/events`)
    const names = ['connected', 'resume.status', 'ranking.updated', 'job.updated']

    const unbind = names.map((name) => {
      const listener = (event: Event) => {
        if (name === 'connected') {
          setConnected(true)
          return
        }
        const message = event as MessageEvent
        let data: unknown = null
        try {
          data = JSON.parse(message.data)
        } catch {
          data = message.data
        }
        handlerRef.current(name, data)
      }
      source.addEventListener(name, listener)
      return () => source.removeEventListener(name, listener)
    })

    source.onerror = () => setConnected(false)

    return () => {
      unbind.forEach((off) => off())
      source.close()
    }
  }, [jobId])

  return connected
}
