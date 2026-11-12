import { ApiError } from "@/lib/api/client"

const messages: Record<string, string> = {
  skill_not_found:
    "This skill no longer exists. Go back to Skills to see the current list.",
  skill_changed:
    "This skill changed after you opened it. Your changes are still here. Copy them, then discard them to load the latest version.",
  skill_name_taken:
    "A skill with this name already exists in this workspace. Change the name in the frontmatter.",
  skill_not_ready: "Select at least one channel before you publish this skill.",
  skill_limit:
    "This workspace has the maximum of 100 skills. Delete a skill before you add a new one.",
  invalid_skill_frontmatter:
    "Start the file with YAML frontmatter between two --- lines. Write each field on its own line as key: value.",
  invalid_skill_name:
    "Add a name to the frontmatter. Use only lowercase letters, numbers and single hyphens, with no hyphen at the start or end. Use at most 64 characters.",
  invalid_skill_description:
    "Add a description to the frontmatter. Use at most 1,024 characters.",
  invalid_request:
    "Make sure that the file is not empty and has at most 20,000 characters.",
}

export function skillErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return "Your session expired. Sign in again."
    if (error.detail && messages[error.detail]) return messages[error.detail]
  }
  return "The skill could not be updated. Try again."
}
