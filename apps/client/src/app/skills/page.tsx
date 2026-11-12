import { useState } from "react"
import { Link } from "react-router"
import {
  RiAddLine,
  RiBookOpenLine,
  RiCheckboxCircleFill,
  RiDraftLine,
} from "@remixicon/react"
import { ErrorCard } from "@/components/error-card"
import { PageHeading } from "@/components/page-heading"
import { Button } from "@/components/ui/button"
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { useSkills } from "@/lib/api/skills"
import type { SkillSummary } from "@/lib/api/types"
import { skillsPath, useSkillsPage } from "./context"

function SkillState({ published }: { published: boolean }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-sm">
      {published ? (
        <RiCheckboxCircleFill
          className="size-4 shrink-0 text-success"
          aria-hidden="true"
        />
      ) : (
        <RiDraftLine
          className="size-4 shrink-0 text-muted-foreground"
          aria-hidden="true"
        />
      )}
      {published ? "Published" : "Draft"}
    </span>
  )
}

function channelSummary(skill: SkillSummary) {
  if (skill.all_channels) return "All channels"
  const count = skill.channels.length
  if (count === 0) return "No channels"
  return count === 1 ? "1 channel" : `${count} channels`
}

function SkillsTable({
  workspaceId,
  skills,
}: {
  workspaceId: string
  skills: SkillSummary[]
}) {
  const [search, setSearch] = useState("")
  const visible = skills.filter((skill) =>
    `${skill.name} ${skill.description}`
      .toLowerCase()
      .includes(search.toLowerCase())
  )
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <Input
        className="max-w-sm"
        aria-label="Search skills"
        placeholder="Search skills…"
        value={search}
        onChange={(event) => setSearch(event.target.value)}
      />
      <div className="overflow-hidden rounded-lg border">
        <Table aria-label="Skills">
          <TableHeader>
            <TableRow>
              <TableHead className="w-full px-3 sm:px-4">Skill</TableHead>
              <TableHead className="px-3 sm:px-4">State</TableHead>
              <TableHead className="hidden px-4 md:table-cell">
                Channels
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {visible.map((skill) => (
              <TableRow key={skill.id}>
                <TableCell className="px-3 py-3 whitespace-normal sm:px-4">
                  <Link
                    to={skillsPath(workspaceId, skill.id)}
                    className="flex max-w-xl flex-col gap-0.5 rounded-sm outline-none hover:underline focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <span className="font-medium wrap-anywhere">
                      {skill.name}
                    </span>
                    <span className="line-clamp-2 text-xs wrap-anywhere text-muted-foreground">
                      {skill.description}
                    </span>
                  </Link>
                </TableCell>
                <TableCell className="px-3 py-3 sm:px-4">
                  <SkillState published={skill.published} />
                </TableCell>
                <TableCell className="hidden px-4 py-3 md:table-cell">
                  {channelSummary(skill)}
                </TableCell>
              </TableRow>
            ))}
            {visible.length === 0 && (
              <TableRow>
                <TableCell
                  colSpan={3}
                  className="py-10 text-center text-muted-foreground"
                >
                  No skills match your search.
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </div>
    </div>
  )
}

export default function SkillsListPage() {
  const { workspace } = useSkillsPage()
  const query = useSkills(workspace.id)
  return (
    <>
      <PageHeading
        title="Skills"
        description={`Instructions that Hondo can follow in ${workspace.name}.`}
      >
        <Button
          nativeButton={false}
          render={<Link to={skillsPath(workspace.id, "new")} />}
        >
          <RiAddLine data-icon="inline-start" />
          Add skill
        </Button>
      </PageHeading>
      {query.isPending ? (
        <Skeleton className="h-64 w-full" />
      ) : !query.data ? (
        <ErrorCard
          title="Could not load skills"
          onRetry={() => void query.refetch()}
          retrying={query.isFetching}
        />
      ) : query.data.skills.length === 0 ? (
        <Empty className="min-h-64 border border-solid">
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <RiBookOpenLine />
            </EmptyMedia>
            <EmptyTitle>No skills yet</EmptyTitle>
            <EmptyDescription>
              Add a skill to give Hondo the steps for a task. You can also ask
              Hondo to draft one from a Slack thread.
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : (
        <SkillsTable workspaceId={workspace.id} skills={query.data.skills} />
      )}
    </>
  )
}
