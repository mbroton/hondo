import { useId, useState } from "react"
import { Link, useNavigate } from "react-router"
import { RiArrowLeftLine } from "@remixicon/react"
import { toast } from "sonner"
import { PageHeading } from "@/components/page-heading"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { FieldGroup } from "@/components/ui/field"
import { useSkillAction } from "@/lib/api/skills"
import { skillsPath, useSkillsPage } from "./context"
import { skillErrorMessage } from "./helpers"
import { SkillFile } from "./skill-file"

export default function NewSkillPage() {
  const { workspace } = useSkillsPage()
  const navigate = useNavigate()
  const id = useId()
  const [content, setContent] = useState("")
  const action = useSkillAction(workspace.id)
  return (
    <>
      <Link
        to={skillsPath(workspace.id)}
        className="flex w-fit items-center gap-2 text-sm text-muted-foreground hover:text-foreground"
      >
        <RiArrowLeftLine className="size-4" />
        Skills
      </Link>
      <PageHeading
        title="Add skill"
        description={`Paste a SKILL.md file with the steps for a task. Hondo uses it in ${workspace.name} after you publish it.`}
      />
      <Card className="max-w-3xl">
        <CardContent>
          <form
            className="flex flex-col gap-6"
            onSubmit={async (event) => {
              event.preventDefault()
              try {
                const result = await action.mutateAsync({
                  type: "create",
                  body: { content },
                })
                if (!result) return
                navigate(skillsPath(workspace.id, result.id), {
                  replace: true,
                })
                toast.success("Skill added. Select channels, then publish it.")
              } catch {
                return
              }
            }}
          >
            {action.isError && (
              <Alert variant="destructive">
                <AlertDescription>
                  {skillErrorMessage(action.error)}
                </AlertDescription>
              </Alert>
            )}
            <FieldGroup>
              <SkillFile
                id={`${id}-content`}
                value={content}
                onChange={setContent}
                disabled={action.isPending}
                autoFocus
              />
            </FieldGroup>
            <div className="flex flex-wrap justify-end gap-2">
              <Button
                type="button"
                variant="outline"
                onClick={() => navigate(skillsPath(workspace.id))}
                disabled={action.isPending}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                disabled={action.isPending || !content.trim()}
              >
                {action.isPending ? "Adding…" : "Add skill"}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </>
  )
}
