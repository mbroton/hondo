import type { ReactNode } from "react"

export function PageHeading({
  title,
  description,
  children,
}: {
  title: string
  description: string
  children?: ReactNode
}) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-4">
      <title>{`${title} · Hondo`}</title>
      <div className="flex min-w-0 flex-col gap-2">
        <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">
          {title}
        </h1>
        <p className="max-w-2xl text-sm leading-6 text-muted-foreground">
          {description}
        </p>
      </div>
      {children && (
        <div className="flex shrink-0 items-center gap-2">{children}</div>
      )}
    </div>
  )
}
