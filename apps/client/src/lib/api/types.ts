import type { components } from "./schema"

type Schemas = components["schemas"]

export type User = Schemas["UserOut"]
export type Team = Schemas["TeamOut"]
export type Me = Schemas["MeResponse"]

export type Workspace = Schemas["WorkspaceOut"]
export type WorkspacesResponse = Schemas["WorkspacesResponse"]
export type Setup = Schemas["SetupResponse"]

export type Admin = Schemas["AdminOut"]
export type AdminsResponse = Schemas["AdminsResponse"]
export type SlackMember = Schemas["SlackMemberOut"]
export type SlackMembersResponse = Schemas["SlackMembersResponse"]

export type UsageSummary = Schemas["UsageResponse"]
export type ActivityResponse = Schemas["ActivityResponse"]

export type Provider = Schemas["ProviderOut"]
export type ProviderConnection = Schemas["ProviderConnectionOut"]
export type ProvidersResponse = Schemas["ProvidersResponse"]
export type ProviderModel = Schemas["ProviderModelOut"]
export type ProviderModelsResponse = Schemas["ProviderModelsResponse"]

export type AgentModel = Schemas["AgentModelResponse"]
export type AgentModelSelection = Schemas["AgentModelRequest"]
export type MentionOnlyChannels = Schemas["MentionOnlyChannels"]
export type ListeningChannels = Schemas["ListeningChannels"]

export type MCPConnection = Schemas["MCPConnectionOut"]
export type MCPConnections = Schemas["MCPConnectionsOut"]
export type MCPKeyHeader = Schemas["MCPKeyHeader"]
export type MCPPolicy = Schemas["MCPPolicyUpdate"]
export type MCPChannelsResponse = Schemas["MCPChannelsOut"]
export type MCPOAuthRedirect = Schemas["MCPOAuthRedirect"]
export type BuiltinConnector = Schemas["BuiltinConnectorOut"]
export type BuiltinConnectors = Schemas["BuiltinConnectorsOut"]
export type HTTPConnectionCreate = Schemas["HTTPConnectionCreate"]
export type HTTPTool = Schemas["HTTPToolSave"]
export type HTTPTestResult = Schemas["HTTPTestOut"]

export type SkillSummary = Schemas["SkillSummaryOut"]
export type Skill = Schemas["SkillOut"]
export type Skills = Schemas["SkillsOut"]
export type SkillUpdate = Schemas["SkillUpdate"]

export type ThreadSummary = Schemas["ThreadSummaryOut"]
export type ThreadSummaries = Schemas["ThreadSummariesOut"]
