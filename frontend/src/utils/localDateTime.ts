import dayjs from 'dayjs'
import timezone from 'dayjs/plugin/timezone'
import utc from 'dayjs/plugin/utc'

const DATE_TIME_FORMAT = 'YYYY-MM-DD HH:mm:ss'
const DISPLAY_TIMEZONE = 'Asia/Shanghai'

dayjs.extend(utc)
dayjs.extend(timezone)

export const formatLocalDateTime = (value?: string | null): string => {
  if (!value) {
    return '-'
  }
  const parsed = dayjs.utc(value)
  return parsed.isValid() ? parsed.tz(DISPLAY_TIMEZONE).format(DATE_TIME_FORMAT) : '-'
}
