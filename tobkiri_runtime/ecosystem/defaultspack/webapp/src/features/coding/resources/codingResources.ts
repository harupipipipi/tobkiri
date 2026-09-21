import { api } from "../../../lib/api";

export const codingResources = {
  listCodingApprovals: api.listCodingApprovals,
  approveCodingApproval: api.approveCodingApproval,
  denyCodingApproval: api.denyCodingApproval,
  listCodingCheckpoints: api.listCodingCheckpoints,
  createCodingCheckpoint: api.createCodingCheckpoint,
  restoreCodingSnapshot: api.restoreCodingSnapshot,
  listRumiLogs: api.listRumiLogs,
  appendRumiLog: api.appendRumiLog,
  seedRumiLogPlan: api.seedRumiLogPlan,
  getGitStatus: api.getGitStatus,
  getGitDiff: api.getGitDiff,
  runTerminalCommand: api.runTerminalCommand,
  listMcpServers: api.listMcpServers,
  registerMcpServer: api.registerMcpServer,
  connectMcpServer: api.connectMcpServer,
  manageMcpServer: api.manageMcpServer,
  listBrowserArtifacts: api.listBrowserArtifacts,
  createCodingAgentSession: api.createCodingAgentSession,
  getCodingAgentSessionStatus: api.getCodingAgentSessionStatus,
};
