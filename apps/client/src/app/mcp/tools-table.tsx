import { Fragment, useId, useState } from "react"
import { Link } from "react-router"
import {
  RiAddLine,
  RiArrowDownSLine,
  RiArrowRightSLine,
  RiPencilLine,
} from "@remixicon/react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { Switch } from "@/components/ui/switch"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import type { MCPConnection } from "@/lib/api/types"
import { cn } from "@/lib/utils"

export function ToolsTable({
  tools,
  selected,
  ask,
  pending,
  onChange,
  toolLink,
}: {
  tools: MCPConnection["tools"]
  selected: string[]
  ask: string[]
  pending: boolean
  onChange: (tools: string[], ask: string[]) => void
  toolLink?: (name: string) => string
}) {
  const id = useId()
  function select(names: string[]) {
    const added = tools.filter(
      (tool) =>
        names.includes(tool.name) &&
        !selected.includes(tool.name) &&
        !tool.read_only
    )
    onChange(names, [
      ...ask.filter((name) => names.includes(name)),
      ...added.map((tool) => tool.name),
    ])
  }
  const [search, setSearch] = useState("")
  const [expanded, setExpanded] = useState<string[]>([])
  const query = search.trim().toLowerCase()
  const visible = tools.filter((tool) =>
    `${tool.name} ${tool.description}`.toLowerCase().includes(query)
  )
  const allSelected = visible.every((tool) => selected.includes(tool.name))
  const anySelected = visible.some((tool) => selected.includes(tool.name))

  return (
    <div className="flex min-w-0 flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Input
          className="max-w-sm"
          aria-label="Search tools"
          placeholder="Search tools…"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <div className="flex gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={pending || allSelected}
            onClick={() =>
              select([
                ...new Set([...selected, ...visible.map((tool) => tool.name)]),
              ])
            }
          >
            {query ? "Select matching" : "Select all"}
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={pending || !anySelected}
            onClick={() => {
              const visibleNames = new Set(visible.map((tool) => tool.name))
              select(selected.filter((name) => !visibleNames.has(name)))
            }}
          >
            {query ? "Deselect matching" : "Deselect all"}
          </Button>
        </div>
      </div>
      <div className="overflow-hidden rounded-lg border">
        <Table aria-label="Connector tools" className="table-fixed">
          <TableHeader>
            <TableRow>
              <TableHead className="w-11 px-4">
                <span className="sr-only">Allowed</span>
              </TableHead>
              <TableHead className="px-3 sm:w-1/3">Tool</TableHead>
              <TableHead className="hidden px-3 sm:table-cell">
                Description
              </TableHead>
              <TableHead className="w-24 px-3">Ask first</TableHead>
              <TableHead className={cn("px-3", toolLink ? "w-20" : "w-12")}>
                <span className="sr-only">Details</span>
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {visible.map((tool, index) => {
              const open = expanded.includes(tool.name)
              return (
                <Fragment key={tool.name}>
                  <TableRow>
                    <TableCell className="px-4 py-2.5">
                      <Checkbox
                        id={`${id}-tool-${index}`}
                        aria-label={tool.name}
                        checked={selected.includes(tool.name)}
                        onCheckedChange={(checked) =>
                          select(
                            checked
                              ? [...selected, tool.name]
                              : selected.filter((name) => name !== tool.name)
                          )
                        }
                        disabled={pending}
                      />
                    </TableCell>
                    <TableCell className="px-3 py-2.5 whitespace-normal">
                      <label
                        htmlFor={`${id}-tool-${index}`}
                        className="cursor-pointer font-medium wrap-anywhere"
                      >
                        {tool.name}
                      </label>
                      {tool.read_only && (
                        <Badge variant="outline" className="ml-2 align-middle">
                          Read-only
                        </Badge>
                      )}
                    </TableCell>
                    <TableCell className="hidden px-3 py-2.5 sm:table-cell">
                      <p className="truncate text-muted-foreground">
                        {tool.description || "No description supplied."}
                      </p>
                    </TableCell>
                    <TableCell className="px-3 py-2.5">
                      {selected.includes(tool.name) && (
                        <Switch
                          size="sm"
                          aria-label={`Ask before running ${tool.name}`}
                          checked={ask.includes(tool.name)}
                          onCheckedChange={(checked) =>
                            onChange(
                              selected,
                              checked
                                ? [...ask, tool.name]
                                : ask.filter((name) => name !== tool.name)
                            )
                          }
                          disabled={pending}
                        />
                      )}
                    </TableCell>
                    <TableCell className="px-3 py-2.5">
                      <div className="flex gap-1">
                        {toolLink && (
                          <Button
                            variant="ghost"
                            size="icon-sm"
                            aria-label={`Edit ${tool.name}`}
                            nativeButton={false}
                            render={<Link to={toolLink(tool.name)} />}
                          >
                            <RiPencilLine />
                          </Button>
                        )}
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon-sm"
                          aria-label={`${open ? "Hide" : "Show"} details for ${tool.name}`}
                          aria-expanded={open}
                          aria-controls={
                            open ? `${id}-details-${index}` : undefined
                          }
                          onClick={() =>
                            setExpanded(
                              open
                                ? expanded.filter((name) => name !== tool.name)
                                : [...expanded, tool.name]
                            )
                          }
                        >
                          {open ? <RiArrowDownSLine /> : <RiArrowRightSLine />}
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                  {open && (
                    <TableRow id={`${id}-details-${index}`}>
                      <TableCell
                        colSpan={5}
                        className="px-4 py-4 whitespace-normal"
                      >
                        <div className="flex min-w-0 flex-col gap-3 sm:pl-10">
                          <p className="text-sm wrap-anywhere whitespace-pre-wrap">
                            {tool.description || "No description supplied."}
                          </p>
                          {tool.request && (
                            <>
                              <p className="text-xs font-medium">Request</p>
                              <pre className="max-h-64 overflow-auto rounded-md bg-muted p-3 text-xs wrap-anywhere whitespace-pre-wrap">
                                {`${tool.request.method} ${tool.request.url}${tool.request.body ? `\n\n${tool.request.body}` : ""}`}
                              </pre>
                            </>
                          )}
                          <p className="text-xs font-medium">Inputs</p>
                          <pre className="max-h-64 overflow-auto rounded-md bg-muted p-3 text-xs">
                            {JSON.stringify(tool.input_schema, null, 2)}
                          </pre>
                        </div>
                      </TableCell>
                    </TableRow>
                  )}
                </Fragment>
              )
            })}
            {visible.length === 0 && (
              <TableRow>
                <TableCell
                  colSpan={5}
                  className="py-10 text-center text-muted-foreground"
                >
                  No tools match your search.
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </div>
    </div>
  )
}

export function AddToolButton({
  to,
  disabled,
}: {
  to: string
  disabled: boolean
}) {
  return disabled ? (
    <Button type="button" variant="outline" size="sm" disabled>
      <RiAddLine data-icon="inline-start" />
      Add tool
    </Button>
  ) : (
    <Button
      variant="outline"
      size="sm"
      nativeButton={false}
      render={<Link to={to} />}
    >
      <RiAddLine data-icon="inline-start" />
      Add tool
    </Button>
  )
}
