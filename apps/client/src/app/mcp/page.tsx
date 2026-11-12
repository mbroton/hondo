import { useState } from "react"
import { Link } from "react-router"
import { RiAddLine, RiPlugLine } from "@remixicon/react"
import { PageHeading } from "@/components/page-heading"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { connectorsPath, useMCPPage } from "./context"
import { serverHost } from "./helpers"
import { AccessStatus, ConnectionStatus } from "./status"

export default function MCPListPage() {
  const { workspace, connections } = useMCPPage()
  const [search, setSearch] = useState("")
  const visible = connections.filter((connection) =>
    `${connection.name} ${connection.url}`
      .toLowerCase()
      .includes(search.toLowerCase())
  )
  return (
    <>
      <PageHeading
        title="Connectors"
        description={`External tools for ${workspace.name}.`}
      >
        <Button
          nativeButton={false}
          render={<Link to={connectorsPath(workspace.id, "new")} />}
        >
          <RiAddLine data-icon="inline-start" />
          Add connector
        </Button>
      </PageHeading>
      {connections.length === 0 ? (
        <Empty className="min-h-64 border border-solid">
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <RiPlugLine />
            </EmptyMedia>
            <EmptyTitle>No connectors yet</EmptyTitle>
            <EmptyDescription>
              Add a connector to give Hondo access to external tools.
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : (
        <div className="flex min-w-0 flex-col gap-4">
          <Input
            className="max-w-sm"
            aria-label="Search connectors"
            placeholder="Search connectors…"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
          <div className="overflow-hidden rounded-lg border">
            <Table aria-label="Connectors">
              <TableHeader>
                <TableRow>
                  <TableHead className="w-full px-3 sm:px-4">
                    Connector
                  </TableHead>
                  <TableHead className="px-3 sm:px-4">Connection</TableHead>
                  <TableHead className="px-3 sm:px-4">Agent access</TableHead>
                  <TableHead className="hidden px-4 md:table-cell">
                    Tools
                  </TableHead>
                  <TableHead className="hidden px-4 md:table-cell">
                    Channels
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {visible.map((connection) => (
                  <TableRow key={connection.id}>
                    <TableCell className="px-3 py-3 sm:px-4">
                      <Link
                        to={connectorsPath(workspace.id, connection.id)}
                        className="flex max-w-80 flex-col gap-0.5 rounded-sm outline-none hover:underline focus-visible:ring-2 focus-visible:ring-ring"
                      >
                        <span className="font-medium wrap-anywhere whitespace-normal">
                          {connection.name}
                        </span>
                        <span className="hidden truncate text-xs text-muted-foreground sm:block">
                          {serverHost(connection.url)}
                        </span>
                      </Link>
                    </TableCell>
                    <TableCell className="px-3 py-3 whitespace-normal sm:px-4">
                      <ConnectionStatus connection={connection} />
                    </TableCell>
                    <TableCell className="px-3 py-3 sm:px-4">
                      <AccessStatus enabled={connection.enabled} />
                    </TableCell>
                    <TableCell className="hidden px-4 py-3 md:table-cell">
                      <Link
                        to={`${connectorsPath(workspace.id, connection.id)}?tab=tools`}
                        className="underline-offset-4 hover:underline"
                      >
                        {connection.tools.filter((tool) => tool.enabled).length}{" "}
                        / {connection.tools.length}
                        <span className="sr-only"> tools allowed</span>
                        {connection.review_required && (
                          <span className="ml-2 text-warning">Review</span>
                        )}
                      </Link>
                    </TableCell>
                    <TableCell className="hidden px-4 py-3 md:table-cell">
                      <Link
                        to={`${connectorsPath(workspace.id, connection.id)}?tab=access`}
                        className="underline-offset-4 hover:underline"
                      >
                        {connection.all_channels
                          ? "All channels"
                          : connection.channels.length}
                        {!connection.all_channels && (
                          <span className="sr-only"> channels</span>
                        )}
                      </Link>
                    </TableCell>
                  </TableRow>
                ))}
                {visible.length === 0 && (
                  <TableRow>
                    <TableCell
                      colSpan={5}
                      className="py-10 text-center text-muted-foreground"
                    >
                      No connectors match your search.
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          </div>
        </div>
      )}
    </>
  )
}
