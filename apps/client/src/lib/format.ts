const tokenFormat = new Intl.NumberFormat()

export function formatTokens(value: number): string {
  return tokenFormat.format(value)
}

// Spend on a single model can be fractions of a cent, so every amount gets up
// to four decimals — the same precision everywhere, so the rows on screen add
// up to the total on screen. Null means unpriced: the tokens are real, the
// price isn't known.
const usdFormat = new Intl.NumberFormat(undefined, {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 4,
})

export function formatUsd(value: string | null): string {
  if (value === null) {
    return "—"
  }
  return usdFormat.format(Number(value))
}

const relativeFormat = new Intl.RelativeTimeFormat(undefined, {
  numeric: "auto",
})

const relativeDivisions: {
  amount: number
  unit: Intl.RelativeTimeFormatUnit
}[] = [
  { amount: 60, unit: "second" },
  { amount: 60, unit: "minute" },
  { amount: 24, unit: "hour" },
  { amount: 7, unit: "day" },
  { amount: 4.34524, unit: "week" },
  { amount: 12, unit: "month" },
  { amount: Number.POSITIVE_INFINITY, unit: "year" },
]

// `now` is a parameter so a polling list can pass the moment its data was
// fetched: React Query hands back an identical object when nothing changed, and
// a Date.now() read during render would then never re-run.
export function formatRelativeTime(
  iso: string,
  now: number = Date.now()
): string {
  let delta = (new Date(iso).getTime() - now) / 1000
  for (const { amount, unit } of relativeDivisions) {
    if (Math.abs(delta) < amount) {
      return relativeFormat.format(Math.round(delta), unit)
    }
    delta /= amount
  }
  return iso
}

const monthLabelFormat = new Intl.DateTimeFormat(undefined, {
  month: "long",
  year: "numeric",
  timeZone: "UTC",
})

export type MonthOption = {
  value: string
  label: string
}

function monthValue(date: Date): string {
  const month = String(date.getUTCMonth() + 1).padStart(2, "0")
  return `${date.getUTCFullYear()}-${month}`
}

// Months are UTC to match the server, which windows usage on UTC month
// boundaries — a browser in UTC+13 must not ask for a month that isn't there.
export function currentMonth(): string {
  return monthValue(new Date())
}

export function recentMonths(count = 12): MonthOption[] {
  const now = new Date()
  return Array.from({ length: count }, (_, index) => {
    const date = new Date(
      Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - index, 1)
    )
    return { value: monthValue(date), label: monthLabelFormat.format(date) }
  })
}

export function initials(...parts: string[]): string {
  const letters = parts
    .map((part) => part.trim().charAt(0))
    .filter(Boolean)
    .join("")
  return letters.toUpperCase()
}
