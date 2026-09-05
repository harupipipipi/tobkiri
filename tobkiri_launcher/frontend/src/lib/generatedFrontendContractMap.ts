/* eslint-disable */
// GENERATED FILE. Do not edit by hand.
// Source: defaultspack/frontend_contract_map.v4.json
// Raw source digest: sha256:b6fba6eafe1809167a9dc7f5c88948557a46f08e3f059a46d3250fc79930841f
import type {FrontendContractMethod} from './api';

export interface GeneratedFrontendContractTarget {
  contribution_id: string;
  contract_id: string;
  operation_id: string;
  provider_id: string;
  function_id: string;
  allowed_payload_keys: string[];
}

export interface GeneratedFrontendContractRoute {
  method: FrontendContractMethod;
  path: string;
  presentation: string;
  targets: GeneratedFrontendContractTarget[];
}

export interface GeneratedFrontendContractMap {
  schema: 'io.tobkiri.frontend-contract-map.v4';
  pack_id: 'defaultspack';
  artifact_path: 'defaultspack/frontend_contract_map.v4.json';
  artifact_digest: string;
  routes: GeneratedFrontendContractRoute[];
}

export const PINNED_FRONTEND_CONTRACT_MAP_ARTIFACT_DIGEST = "sha256:b6fba6eafe1809167a9dc7f5c88948557a46f08e3f059a46d3250fc79930841f" as const;

export const GENERATED_FRONTEND_CONTRACT_MAP: GeneratedFrontendContractMap = {
  "schema": "io.tobkiri.frontend-contract-map.v4",
  "pack_id": "defaultspack",
  "artifact_path": "defaultspack/frontend_contract_map.v4.json",
  "artifact_digest": "sha256:b6fba6eafe1809167a9dc7f5c88948557a46f08e3f059a46d3250fc79930841f",
  "routes": [
    {
      "method": "GET",
      "path": "/api/home/dashboard",
      "presentation": "broker_result",
      "targets": [
        {
          "contribution_id": "defaults.home.dashboard",
          "contract_id": "tobkiri.host.pack-control.v4",
          "operation_id": "dashboard.read",
          "provider_id": "tobkiri.host.pack-control",
          "function_id": "tobkiri.host.pack-control",
          "allowed_payload_keys": []
        }
      ]
    },
    {
      "method": "GET",
      "path": "/api/pack-control/catalog",
      "presentation": "broker_result",
      "targets": [
        {
          "contribution_id": "defaults.pack.catalog",
          "contract_id": "tobkiri.host.pack-control.v4",
          "operation_id": "catalog.read",
          "provider_id": "tobkiri.host.pack-control",
          "function_id": "tobkiri.host.pack-control",
          "allowed_payload_keys": []
        }
      ]
    },
    {
      "method": "POST",
      "path": "/api/pack-control/install",
      "presentation": "broker_result",
      "targets": [
        {
          "contribution_id": "defaults.pack.install",
          "contract_id": "tobkiri.host.pack-control.v4",
          "operation_id": "pack.install",
          "provider_id": "tobkiri.host.pack-control",
          "function_id": "tobkiri.host.pack-control",
          "allowed_payload_keys": [
            "pack_id"
          ]
        }
      ]
    },
    {
      "method": "POST",
      "path": "/api/pack-control/approval-candidate",
      "presentation": "broker_result",
      "targets": [
        {
          "contribution_id": "defaults.pack.approval-candidate",
          "contract_id": "tobkiri.host.pack-control.v4",
          "operation_id": "approval.candidate",
          "provider_id": "tobkiri.host.pack-control",
          "function_id": "tobkiri.host.pack-control",
          "allowed_payload_keys": [
            "pack_id"
          ]
        }
      ]
    },
    {
      "method": "POST",
      "path": "/api/pack-control/approval-approve",
      "presentation": "broker_result",
      "targets": [
        {
          "contribution_id": "defaults.pack.approval-approve",
          "contract_id": "tobkiri.host.pack-control.v4",
          "operation_id": "approval.approve",
          "provider_id": "tobkiri.host.pack-control",
          "function_id": "tobkiri.host.pack-control",
          "allowed_payload_keys": [
            "candidate_id",
            "pack_id"
          ]
        }
      ]
    },
    {
      "method": "POST",
      "path": "/api/pack-control/approval-revoke",
      "presentation": "broker_result",
      "targets": [
        {
          "contribution_id": "defaults.pack.approval-revoke",
          "contract_id": "tobkiri.host.pack-control.v4",
          "operation_id": "approval.revoke",
          "provider_id": "tobkiri.host.pack-control",
          "function_id": "tobkiri.host.pack-control",
          "allowed_payload_keys": [
            "pack_id"
          ]
        }
      ]
    },
    {
      "method": "POST",
      "path": "/api/pack-control/enable",
      "presentation": "broker_result",
      "targets": [
        {
          "contribution_id": "defaults.pack.enable",
          "contract_id": "tobkiri.host.pack-control.v4",
          "operation_id": "pack.enable",
          "provider_id": "tobkiri.host.pack-control",
          "function_id": "tobkiri.host.pack-control",
          "allowed_payload_keys": [
            "pack_id"
          ]
        }
      ]
    },
    {
      "method": "POST",
      "path": "/api/pack-control/disable",
      "presentation": "broker_result",
      "targets": [
        {
          "contribution_id": "defaults.pack.disable",
          "contract_id": "tobkiri.host.pack-control.v4",
          "operation_id": "pack.disable",
          "provider_id": "tobkiri.host.pack-control",
          "function_id": "tobkiri.host.pack-control",
          "allowed_payload_keys": [
            "pack_id"
          ]
        }
      ]
    },
    {
      "method": "POST",
      "path": "/api/pack-control/restart",
      "presentation": "broker_result",
      "targets": [
        {
          "contribution_id": "defaults.runtime.restart",
          "contract_id": "tobkiri.host.pack-control.v4",
          "operation_id": "runtime.restart",
          "provider_id": "tobkiri.host.pack-control",
          "function_id": "tobkiri.host.pack-control",
          "allowed_payload_keys": []
        }
      ]
    },
    {
      "method": "GET",
      "path": "/api/runtime-surface/profile",
      "presentation": "broker_result",
      "targets": [
        {
          "contribution_id": "defaults.runtime-surface.profile",
          "contract_id": "tobkiri.host.control-presentation.v4",
          "operation_id": "profile.read",
          "provider_id": "tobkiri.host.control-presentation",
          "function_id": "tobkiri.host.control-presentation",
          "allowed_payload_keys": [
            "expected_profile_revision",
            "expected_plan_digest"
          ]
        }
      ]
    },
    {
      "method": "GET",
      "path": "/api/runtime-surface/profiles",
      "presentation": "broker_result",
      "targets": [
        {
          "contribution_id": "defaults.runtime-surface.profile-catalog",
          "contract_id": "tobkiri.host.control-presentation.v4",
          "operation_id": "profile.catalog.read",
          "provider_id": "tobkiri.host.control-presentation",
          "function_id": "tobkiri.host.control-presentation",
          "allowed_payload_keys": []
        }
      ]
    },
    {
      "method": "GET",
      "path": "/api/runtime-surface/operation-status",
      "presentation": "broker_result",
      "targets": [
        {
          "contribution_id": "defaults.runtime-surface.operation-status",
          "contract_id": "tobkiri.host.control-presentation.v4",
          "operation_id": "operation.status.read",
          "provider_id": "tobkiri.host.control-presentation",
          "function_id": "tobkiri.host.control-presentation",
          "allowed_payload_keys": [
            "request_id"
          ]
        }
      ]
    },
    {
      "method": "GET",
      "path": "/api/runtime-surface/settings",
      "presentation": "broker_result",
      "targets": [
        {
          "contribution_id": "defaults.runtime-surface.settings",
          "contract_id": "tobkiri.host.control-presentation.v4",
          "operation_id": "settings.read",
          "provider_id": "tobkiri.host.control-presentation",
          "function_id": "tobkiri.host.control-presentation",
          "allowed_payload_keys": []
        }
      ]
    },
    {
      "method": "GET",
      "path": "/api/runtime-surface/topology/packs",
      "presentation": "broker_result",
      "targets": [
        {
          "contribution_id": "defaults.runtime-surface.packs",
          "contract_id": "tobkiri.host.control-presentation.v4",
          "operation_id": "topology.packs.read",
          "provider_id": "tobkiri.host.control-presentation",
          "function_id": "tobkiri.host.control-presentation",
          "allowed_payload_keys": [
            "expected_profile_revision",
            "expected_plan_digest"
          ]
        }
      ]
    },
    {
      "method": "GET",
      "path": "/api/runtime-surface/topology/contracts",
      "presentation": "broker_result",
      "targets": [
        {
          "contribution_id": "defaults.runtime-surface.contracts",
          "contract_id": "tobkiri.host.control-presentation.v4",
          "operation_id": "topology.contracts.read",
          "provider_id": "tobkiri.host.control-presentation",
          "function_id": "tobkiri.host.control-presentation",
          "allowed_payload_keys": [
            "expected_profile_revision",
            "expected_plan_digest"
          ]
        }
      ]
    },
    {
      "method": "GET",
      "path": "/api/runtime-surface/topology/operations",
      "presentation": "broker_result",
      "targets": [
        {
          "contribution_id": "defaults.runtime-surface.operations",
          "contract_id": "tobkiri.host.control-presentation.v4",
          "operation_id": "topology.operations.read",
          "provider_id": "tobkiri.host.control-presentation",
          "function_id": "tobkiri.host.control-presentation",
          "allowed_payload_keys": [
            "expected_profile_revision",
            "expected_plan_digest"
          ]
        }
      ]
    },
    {
      "method": "GET",
      "path": "/api/runtime-surface/topology/principals",
      "presentation": "broker_result",
      "targets": [
        {
          "contribution_id": "defaults.runtime-surface.principals",
          "contract_id": "tobkiri.host.control-presentation.v4",
          "operation_id": "topology.principals.read",
          "provider_id": "tobkiri.host.control-presentation",
          "function_id": "tobkiri.host.control-presentation",
          "allowed_payload_keys": [
            "expected_profile_revision",
            "expected_plan_digest"
          ]
        }
      ]
    },
    {
      "method": "POST",
      "path": "/api/runtime-surface/profile-change/resolve",
      "presentation": "broker_result",
      "targets": [
        {
          "contribution_id": "defaults.runtime-surface.profile-change.resolve",
          "contract_id": "tobkiri.host.control-presentation.v4",
          "operation_id": "profile.change.resolve",
          "provider_id": "tobkiri.host.control-presentation",
          "function_id": "tobkiri.host.control-presentation",
          "allowed_payload_keys": [
            "profile_id",
            "expected_profile_revision",
            "expected_plan_digest",
            "desired_pack_ids",
            "profile_definition_digest",
            "profile_catalog_digest",
            "bundle_lock_digest"
          ]
        }
      ]
    },
    {
      "method": "POST",
      "path": "/api/runtime-surface/profile-change/review",
      "presentation": "broker_result",
      "targets": [
        {
          "contribution_id": "defaults.runtime-surface.profile-change.review",
          "contract_id": "tobkiri.host.control-presentation.v4",
          "operation_id": "profile.change.review",
          "provider_id": "tobkiri.host.control-presentation",
          "function_id": "tobkiri.host.control-presentation",
          "allowed_payload_keys": [
            "candidate_id",
            "candidate_digest"
          ]
        }
      ]
    },
    {
      "method": "POST",
      "path": "/api/runtime-surface/profile-change/approve",
      "presentation": "broker_result",
      "targets": [
        {
          "contribution_id": "defaults.runtime-surface.profile-change.approve",
          "contract_id": "tobkiri.host.control-presentation.v4",
          "operation_id": "profile.change.approve",
          "provider_id": "tobkiri.host.control-presentation",
          "function_id": "tobkiri.host.control-presentation",
          "allowed_payload_keys": [
            "candidate_id",
            "candidate_digest"
          ]
        }
      ]
    },
    {
      "method": "POST",
      "path": "/api/runtime-surface/profile-change/activate",
      "presentation": "broker_result",
      "targets": [
        {
          "contribution_id": "defaults.runtime-surface.profile-change.activate",
          "contract_id": "tobkiri.host.control-presentation.v4",
          "operation_id": "profile.change.activate",
          "provider_id": "tobkiri.host.control-presentation",
          "function_id": "tobkiri.host.control-presentation",
          "allowed_payload_keys": [
            "approval_id",
            "approval_digest"
          ]
        }
      ]
    },
    {
      "method": "GET",
      "path": "/api/ui/catalog",
      "presentation": "dynamic_pack_catalog",
      "targets": [
        {
          "contribution_id": "defaults.pack.catalog",
          "contract_id": "tobkiri.host.pack-control.v4",
          "operation_id": "catalog.read",
          "provider_id": "tobkiri.host.pack-control",
          "function_id": "tobkiri.host.pack-control",
          "allowed_payload_keys": []
        }
      ]
    },
    {
      "method": "GET",
      "path": "/api/interactive-approval/v1/list",
      "presentation": "broker_result",
      "targets": [
        {
          "contribution_id": "defaults.interactive-approval.list",
          "contract_id": "tobkiri.service.interactive-approval.v1",
          "operation_id": "interactive_approval.list",
          "provider_id": "rumi_host_authority_bridge_pack.host-authority.interactive-approval",
          "function_id": "rumi_host_authority_bridge_pack.host-authority.interactive-approval",
          "allowed_payload_keys": []
        }
      ]
    },
    {
      "method": "POST",
      "path": "/api/interactive-approval/v1/get",
      "presentation": "broker_result",
      "targets": [
        {
          "contribution_id": "defaults.interactive-approval.get",
          "contract_id": "tobkiri.service.interactive-approval.v1",
          "operation_id": "interactive_approval.get",
          "provider_id": "rumi_host_authority_bridge_pack.host-authority.interactive-approval",
          "function_id": "rumi_host_authority_bridge_pack.host-authority.interactive-approval",
          "allowed_payload_keys": [
            "request_id"
          ]
        }
      ]
    },
    {
      "method": "POST",
      "path": "/api/interactive-approval/v1/approve",
      "presentation": "broker_result",
      "targets": [
        {
          "contribution_id": "defaults.interactive-approval.approve",
          "contract_id": "tobkiri.service.interactive-approval.v1",
          "operation_id": "interactive_approval.approve",
          "provider_id": "rumi_host_authority_bridge_pack.host-authority.interactive-approval",
          "function_id": "rumi_host_authority_bridge_pack.host-authority.interactive-approval",
          "allowed_payload_keys": [
            "request_id",
            "confirmation_text",
            "ui_operator"
          ]
        }
      ]
    },
    {
      "method": "POST",
      "path": "/api/interactive-approval/v1/deny",
      "presentation": "broker_result",
      "targets": [
        {
          "contribution_id": "defaults.interactive-approval.deny",
          "contract_id": "tobkiri.service.interactive-approval.v1",
          "operation_id": "interactive_approval.deny",
          "provider_id": "rumi_host_authority_bridge_pack.host-authority.interactive-approval",
          "function_id": "rumi_host_authority_bridge_pack.host-authority.interactive-approval",
          "allowed_payload_keys": [
            "request_id",
            "ui_operator"
          ]
        }
      ]
    },
    {
      "method": "POST",
      "path": "/api/command-protocol/v1/high-risk",
      "presentation": "broker_result",
      "targets": [
        {
          "contribution_id": "defaults.command-protocol.high-risk",
          "contract_id": "tobkiri.service.command.high-risk.v1",
          "operation_id": "high_risk_command.manage",
          "provider_id": "rumi_command_protocol_pack.high-risk-command.service",
          "function_id": "rumi_command_protocol_pack.high-risk-command.service",
          "allowed_payload_keys": [
            "phase",
            "invocation_id",
            "command_ref",
            "arguments",
            "presentation"
          ]
        }
      ]
    },
    {
      "method": "POST",
      "path": "/api/ui/capability/invoke",
      "presentation": "capability_result",
      "targets": [
        {
          "contribution_id": "defaults.conversation.complete",
          "contract_id": "conversation.turn.v1",
          "operation_id": "complete",
          "provider_id": "defaultspack.conversation",
          "function_id": "defaultspack.conversation",
          "allowed_payload_keys": [
            "messages"
          ]
        },
        {
          "contribution_id": "defaults.pack.install",
          "contract_id": "tobkiri.host.pack-control.v4",
          "operation_id": "pack.install",
          "provider_id": "tobkiri.host.pack-control",
          "function_id": "tobkiri.host.pack-control",
          "allowed_payload_keys": [
            "pack_id"
          ]
        },
        {
          "contribution_id": "defaults.pack.approval-candidate",
          "contract_id": "tobkiri.host.pack-control.v4",
          "operation_id": "approval.candidate",
          "provider_id": "tobkiri.host.pack-control",
          "function_id": "tobkiri.host.pack-control",
          "allowed_payload_keys": [
            "pack_id"
          ]
        },
        {
          "contribution_id": "defaults.pack.approval-approve",
          "contract_id": "tobkiri.host.pack-control.v4",
          "operation_id": "approval.approve",
          "provider_id": "tobkiri.host.pack-control",
          "function_id": "tobkiri.host.pack-control",
          "allowed_payload_keys": [
            "candidate_id",
            "pack_id"
          ]
        },
        {
          "contribution_id": "defaults.pack.approval-revoke",
          "contract_id": "tobkiri.host.pack-control.v4",
          "operation_id": "approval.revoke",
          "provider_id": "tobkiri.host.pack-control",
          "function_id": "tobkiri.host.pack-control",
          "allowed_payload_keys": [
            "pack_id"
          ]
        },
        {
          "contribution_id": "defaults.pack.enable",
          "contract_id": "tobkiri.host.pack-control.v4",
          "operation_id": "pack.enable",
          "provider_id": "tobkiri.host.pack-control",
          "function_id": "tobkiri.host.pack-control",
          "allowed_payload_keys": [
            "pack_id"
          ]
        },
        {
          "contribution_id": "defaults.pack.status",
          "contract_id": "tobkiri.host.pack-control.v4",
          "operation_id": "pack.status",
          "provider_id": "tobkiri.host.pack-control",
          "function_id": "tobkiri.host.pack-control",
          "allowed_payload_keys": [
            "pack_id"
          ]
        },
        {
          "contribution_id": "defaults.pack.disable",
          "contract_id": "tobkiri.host.pack-control.v4",
          "operation_id": "pack.disable",
          "provider_id": "tobkiri.host.pack-control",
          "function_id": "tobkiri.host.pack-control",
          "allowed_payload_keys": [
            "pack_id"
          ]
        },
        {
          "contribution_id": "defaults.profile.reload",
          "contract_id": "tobkiri.host.pack-control.v4",
          "operation_id": "profile.reload",
          "provider_id": "tobkiri.host.pack-control",
          "function_id": "tobkiri.host.pack-control",
          "allowed_payload_keys": []
        },
        {
          "contribution_id": "defaults.runtime.restart",
          "contract_id": "tobkiri.host.pack-control.v4",
          "operation_id": "runtime.restart",
          "provider_id": "tobkiri.host.pack-control",
          "function_id": "tobkiri.host.pack-control",
          "allowed_payload_keys": []
        }
      ]
    }
  ]
};

const EXPECTED_ROUTES = {
  "GET /api/home/dashboard": {
    "presentation": "broker_result",
    "targets": [
      {
        "contribution_id": "defaults.home.dashboard",
        "contract_id": "tobkiri.host.pack-control.v4",
        "operation_id": "dashboard.read",
        "provider_id": "tobkiri.host.pack-control",
        "function_id": "tobkiri.host.pack-control",
        "allowed_payload_keys": []
      }
    ]
  },
  "GET /api/pack-control/catalog": {
    "presentation": "broker_result",
    "targets": [
      {
        "contribution_id": "defaults.pack.catalog",
        "contract_id": "tobkiri.host.pack-control.v4",
        "operation_id": "catalog.read",
        "provider_id": "tobkiri.host.pack-control",
        "function_id": "tobkiri.host.pack-control",
        "allowed_payload_keys": []
      }
    ]
  },
  "POST /api/pack-control/install": {
    "presentation": "broker_result",
    "targets": [
      {
        "contribution_id": "defaults.pack.install",
        "contract_id": "tobkiri.host.pack-control.v4",
        "operation_id": "pack.install",
        "provider_id": "tobkiri.host.pack-control",
        "function_id": "tobkiri.host.pack-control",
        "allowed_payload_keys": [
          "pack_id"
        ]
      }
    ]
  },
  "POST /api/pack-control/approval-candidate": {
    "presentation": "broker_result",
    "targets": [
      {
        "contribution_id": "defaults.pack.approval-candidate",
        "contract_id": "tobkiri.host.pack-control.v4",
        "operation_id": "approval.candidate",
        "provider_id": "tobkiri.host.pack-control",
        "function_id": "tobkiri.host.pack-control",
        "allowed_payload_keys": [
          "pack_id"
        ]
      }
    ]
  },
  "POST /api/pack-control/approval-approve": {
    "presentation": "broker_result",
    "targets": [
      {
        "contribution_id": "defaults.pack.approval-approve",
        "contract_id": "tobkiri.host.pack-control.v4",
        "operation_id": "approval.approve",
        "provider_id": "tobkiri.host.pack-control",
        "function_id": "tobkiri.host.pack-control",
        "allowed_payload_keys": [
          "candidate_id",
          "pack_id"
        ]
      }
    ]
  },
  "POST /api/pack-control/approval-revoke": {
    "presentation": "broker_result",
    "targets": [
      {
        "contribution_id": "defaults.pack.approval-revoke",
        "contract_id": "tobkiri.host.pack-control.v4",
        "operation_id": "approval.revoke",
        "provider_id": "tobkiri.host.pack-control",
        "function_id": "tobkiri.host.pack-control",
        "allowed_payload_keys": [
          "pack_id"
        ]
      }
    ]
  },
  "POST /api/pack-control/enable": {
    "presentation": "broker_result",
    "targets": [
      {
        "contribution_id": "defaults.pack.enable",
        "contract_id": "tobkiri.host.pack-control.v4",
        "operation_id": "pack.enable",
        "provider_id": "tobkiri.host.pack-control",
        "function_id": "tobkiri.host.pack-control",
        "allowed_payload_keys": [
          "pack_id"
        ]
      }
    ]
  },
  "POST /api/pack-control/disable": {
    "presentation": "broker_result",
    "targets": [
      {
        "contribution_id": "defaults.pack.disable",
        "contract_id": "tobkiri.host.pack-control.v4",
        "operation_id": "pack.disable",
        "provider_id": "tobkiri.host.pack-control",
        "function_id": "tobkiri.host.pack-control",
        "allowed_payload_keys": [
          "pack_id"
        ]
      }
    ]
  },
  "POST /api/pack-control/restart": {
    "presentation": "broker_result",
    "targets": [
      {
        "contribution_id": "defaults.runtime.restart",
        "contract_id": "tobkiri.host.pack-control.v4",
        "operation_id": "runtime.restart",
        "provider_id": "tobkiri.host.pack-control",
        "function_id": "tobkiri.host.pack-control",
        "allowed_payload_keys": []
      }
    ]
  },
  "GET /api/runtime-surface/profile": {
    "presentation": "broker_result",
    "targets": [
      {
        "contribution_id": "defaults.runtime-surface.profile",
        "contract_id": "tobkiri.host.control-presentation.v4",
        "operation_id": "profile.read",
        "provider_id": "tobkiri.host.control-presentation",
        "function_id": "tobkiri.host.control-presentation",
        "allowed_payload_keys": [
          "expected_profile_revision",
          "expected_plan_digest"
        ]
      }
    ]
  },
  "GET /api/runtime-surface/profiles": {
    "presentation": "broker_result",
    "targets": [
      {
        "contribution_id": "defaults.runtime-surface.profile-catalog",
        "contract_id": "tobkiri.host.control-presentation.v4",
        "operation_id": "profile.catalog.read",
        "provider_id": "tobkiri.host.control-presentation",
        "function_id": "tobkiri.host.control-presentation",
        "allowed_payload_keys": []
      }
    ]
  },
  "GET /api/runtime-surface/operation-status": {
    "presentation": "broker_result",
    "targets": [
      {
        "contribution_id": "defaults.runtime-surface.operation-status",
        "contract_id": "tobkiri.host.control-presentation.v4",
        "operation_id": "operation.status.read",
        "provider_id": "tobkiri.host.control-presentation",
        "function_id": "tobkiri.host.control-presentation",
        "allowed_payload_keys": [
          "request_id"
        ]
      }
    ]
  },
  "GET /api/runtime-surface/settings": {
    "presentation": "broker_result",
    "targets": [
      {
        "contribution_id": "defaults.runtime-surface.settings",
        "contract_id": "tobkiri.host.control-presentation.v4",
        "operation_id": "settings.read",
        "provider_id": "tobkiri.host.control-presentation",
        "function_id": "tobkiri.host.control-presentation",
        "allowed_payload_keys": []
      }
    ]
  },
  "GET /api/runtime-surface/topology/packs": {
    "presentation": "broker_result",
    "targets": [
      {
        "contribution_id": "defaults.runtime-surface.packs",
        "contract_id": "tobkiri.host.control-presentation.v4",
        "operation_id": "topology.packs.read",
        "provider_id": "tobkiri.host.control-presentation",
        "function_id": "tobkiri.host.control-presentation",
        "allowed_payload_keys": [
          "expected_profile_revision",
          "expected_plan_digest"
        ]
      }
    ]
  },
  "GET /api/runtime-surface/topology/contracts": {
    "presentation": "broker_result",
    "targets": [
      {
        "contribution_id": "defaults.runtime-surface.contracts",
        "contract_id": "tobkiri.host.control-presentation.v4",
        "operation_id": "topology.contracts.read",
        "provider_id": "tobkiri.host.control-presentation",
        "function_id": "tobkiri.host.control-presentation",
        "allowed_payload_keys": [
          "expected_profile_revision",
          "expected_plan_digest"
        ]
      }
    ]
  },
  "GET /api/runtime-surface/topology/operations": {
    "presentation": "broker_result",
    "targets": [
      {
        "contribution_id": "defaults.runtime-surface.operations",
        "contract_id": "tobkiri.host.control-presentation.v4",
        "operation_id": "topology.operations.read",
        "provider_id": "tobkiri.host.control-presentation",
        "function_id": "tobkiri.host.control-presentation",
        "allowed_payload_keys": [
          "expected_profile_revision",
          "expected_plan_digest"
        ]
      }
    ]
  },
  "GET /api/runtime-surface/topology/principals": {
    "presentation": "broker_result",
    "targets": [
      {
        "contribution_id": "defaults.runtime-surface.principals",
        "contract_id": "tobkiri.host.control-presentation.v4",
        "operation_id": "topology.principals.read",
        "provider_id": "tobkiri.host.control-presentation",
        "function_id": "tobkiri.host.control-presentation",
        "allowed_payload_keys": [
          "expected_profile_revision",
          "expected_plan_digest"
        ]
      }
    ]
  },
  "POST /api/runtime-surface/profile-change/resolve": {
    "presentation": "broker_result",
    "targets": [
      {
        "contribution_id": "defaults.runtime-surface.profile-change.resolve",
        "contract_id": "tobkiri.host.control-presentation.v4",
        "operation_id": "profile.change.resolve",
        "provider_id": "tobkiri.host.control-presentation",
        "function_id": "tobkiri.host.control-presentation",
        "allowed_payload_keys": [
          "profile_id",
          "expected_profile_revision",
          "expected_plan_digest",
          "desired_pack_ids",
          "profile_definition_digest",
          "profile_catalog_digest",
          "bundle_lock_digest"
        ]
      }
    ]
  },
  "POST /api/runtime-surface/profile-change/review": {
    "presentation": "broker_result",
    "targets": [
      {
        "contribution_id": "defaults.runtime-surface.profile-change.review",
        "contract_id": "tobkiri.host.control-presentation.v4",
        "operation_id": "profile.change.review",
        "provider_id": "tobkiri.host.control-presentation",
        "function_id": "tobkiri.host.control-presentation",
        "allowed_payload_keys": [
          "candidate_id",
          "candidate_digest"
        ]
      }
    ]
  },
  "POST /api/runtime-surface/profile-change/approve": {
    "presentation": "broker_result",
    "targets": [
      {
        "contribution_id": "defaults.runtime-surface.profile-change.approve",
        "contract_id": "tobkiri.host.control-presentation.v4",
        "operation_id": "profile.change.approve",
        "provider_id": "tobkiri.host.control-presentation",
        "function_id": "tobkiri.host.control-presentation",
        "allowed_payload_keys": [
          "candidate_id",
          "candidate_digest"
        ]
      }
    ]
  },
  "POST /api/runtime-surface/profile-change/activate": {
    "presentation": "broker_result",
    "targets": [
      {
        "contribution_id": "defaults.runtime-surface.profile-change.activate",
        "contract_id": "tobkiri.host.control-presentation.v4",
        "operation_id": "profile.change.activate",
        "provider_id": "tobkiri.host.control-presentation",
        "function_id": "tobkiri.host.control-presentation",
        "allowed_payload_keys": [
          "approval_id",
          "approval_digest"
        ]
      }
    ]
  },
  "GET /api/ui/catalog": {
    "presentation": "dynamic_pack_catalog",
    "targets": [
      {
        "contribution_id": "defaults.pack.catalog",
        "contract_id": "tobkiri.host.pack-control.v4",
        "operation_id": "catalog.read",
        "provider_id": "tobkiri.host.pack-control",
        "function_id": "tobkiri.host.pack-control",
        "allowed_payload_keys": []
      }
    ]
  },
  "GET /api/interactive-approval/v1/list": {
    "presentation": "broker_result",
    "targets": [
      {
        "contribution_id": "defaults.interactive-approval.list",
        "contract_id": "tobkiri.service.interactive-approval.v1",
        "operation_id": "interactive_approval.list",
        "provider_id": "rumi_host_authority_bridge_pack.host-authority.interactive-approval",
        "function_id": "rumi_host_authority_bridge_pack.host-authority.interactive-approval",
        "allowed_payload_keys": []
      }
    ]
  },
  "POST /api/interactive-approval/v1/get": {
    "presentation": "broker_result",
    "targets": [
      {
        "contribution_id": "defaults.interactive-approval.get",
        "contract_id": "tobkiri.service.interactive-approval.v1",
        "operation_id": "interactive_approval.get",
        "provider_id": "rumi_host_authority_bridge_pack.host-authority.interactive-approval",
        "function_id": "rumi_host_authority_bridge_pack.host-authority.interactive-approval",
        "allowed_payload_keys": [
          "request_id"
        ]
      }
    ]
  },
  "POST /api/interactive-approval/v1/approve": {
    "presentation": "broker_result",
    "targets": [
      {
        "contribution_id": "defaults.interactive-approval.approve",
        "contract_id": "tobkiri.service.interactive-approval.v1",
        "operation_id": "interactive_approval.approve",
        "provider_id": "rumi_host_authority_bridge_pack.host-authority.interactive-approval",
        "function_id": "rumi_host_authority_bridge_pack.host-authority.interactive-approval",
        "allowed_payload_keys": [
          "request_id",
          "confirmation_text",
          "ui_operator"
        ]
      }
    ]
  },
  "POST /api/interactive-approval/v1/deny": {
    "presentation": "broker_result",
    "targets": [
      {
        "contribution_id": "defaults.interactive-approval.deny",
        "contract_id": "tobkiri.service.interactive-approval.v1",
        "operation_id": "interactive_approval.deny",
        "provider_id": "rumi_host_authority_bridge_pack.host-authority.interactive-approval",
        "function_id": "rumi_host_authority_bridge_pack.host-authority.interactive-approval",
        "allowed_payload_keys": [
          "request_id",
          "ui_operator"
        ]
      }
    ]
  },
  "POST /api/command-protocol/v1/high-risk": {
    "presentation": "broker_result",
    "targets": [
      {
        "contribution_id": "defaults.command-protocol.high-risk",
        "contract_id": "tobkiri.service.command.high-risk.v1",
        "operation_id": "high_risk_command.manage",
        "provider_id": "rumi_command_protocol_pack.high-risk-command.service",
        "function_id": "rumi_command_protocol_pack.high-risk-command.service",
        "allowed_payload_keys": [
          "phase",
          "invocation_id",
          "command_ref",
          "arguments",
          "presentation"
        ]
      }
    ]
  },
  "POST /api/ui/capability/invoke": {
    "presentation": "capability_result",
    "targets": [
      {
        "contribution_id": "defaults.conversation.complete",
        "contract_id": "conversation.turn.v1",
        "operation_id": "complete",
        "provider_id": "defaultspack.conversation",
        "function_id": "defaultspack.conversation",
        "allowed_payload_keys": [
          "messages"
        ]
      },
      {
        "contribution_id": "defaults.pack.install",
        "contract_id": "tobkiri.host.pack-control.v4",
        "operation_id": "pack.install",
        "provider_id": "tobkiri.host.pack-control",
        "function_id": "tobkiri.host.pack-control",
        "allowed_payload_keys": [
          "pack_id"
        ]
      },
      {
        "contribution_id": "defaults.pack.approval-candidate",
        "contract_id": "tobkiri.host.pack-control.v4",
        "operation_id": "approval.candidate",
        "provider_id": "tobkiri.host.pack-control",
        "function_id": "tobkiri.host.pack-control",
        "allowed_payload_keys": [
          "pack_id"
        ]
      },
      {
        "contribution_id": "defaults.pack.approval-approve",
        "contract_id": "tobkiri.host.pack-control.v4",
        "operation_id": "approval.approve",
        "provider_id": "tobkiri.host.pack-control",
        "function_id": "tobkiri.host.pack-control",
        "allowed_payload_keys": [
          "candidate_id",
          "pack_id"
        ]
      },
      {
        "contribution_id": "defaults.pack.approval-revoke",
        "contract_id": "tobkiri.host.pack-control.v4",
        "operation_id": "approval.revoke",
        "provider_id": "tobkiri.host.pack-control",
        "function_id": "tobkiri.host.pack-control",
        "allowed_payload_keys": [
          "pack_id"
        ]
      },
      {
        "contribution_id": "defaults.pack.enable",
        "contract_id": "tobkiri.host.pack-control.v4",
        "operation_id": "pack.enable",
        "provider_id": "tobkiri.host.pack-control",
        "function_id": "tobkiri.host.pack-control",
        "allowed_payload_keys": [
          "pack_id"
        ]
      },
      {
        "contribution_id": "defaults.pack.status",
        "contract_id": "tobkiri.host.pack-control.v4",
        "operation_id": "pack.status",
        "provider_id": "tobkiri.host.pack-control",
        "function_id": "tobkiri.host.pack-control",
        "allowed_payload_keys": [
          "pack_id"
        ]
      },
      {
        "contribution_id": "defaults.pack.disable",
        "contract_id": "tobkiri.host.pack-control.v4",
        "operation_id": "pack.disable",
        "provider_id": "tobkiri.host.pack-control",
        "function_id": "tobkiri.host.pack-control",
        "allowed_payload_keys": [
          "pack_id"
        ]
      },
      {
        "contribution_id": "defaults.profile.reload",
        "contract_id": "tobkiri.host.pack-control.v4",
        "operation_id": "profile.reload",
        "provider_id": "tobkiri.host.pack-control",
        "function_id": "tobkiri.host.pack-control",
        "allowed_payload_keys": []
      },
      {
        "contribution_id": "defaults.runtime.restart",
        "contract_id": "tobkiri.host.pack-control.v4",
        "operation_id": "runtime.restart",
        "provider_id": "tobkiri.host.pack-control",
        "function_id": "tobkiri.host.pack-control",
        "allowed_payload_keys": []
      }
    ]
  }
} as const;

function isDigest(value: unknown): value is string {
  return typeof value === 'string' && /^sha256:[0-9a-f]{64}$/.test(value);
}

function exactStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === 'string');
}

function exactKeys(value: unknown, keys: string[]): value is Record<string, unknown> {
  return typeof value === 'object'
    && value !== null
    && !Array.isArray(value)
    && Object.keys(value).length === keys.length
    && keys.every((key) => Object.prototype.hasOwnProperty.call(value, key));
}

/** Validate the generated binding against its pinned canonical artifact. */
export function validateGeneratedFrontendContractMap(
  value: unknown,
  expectedArtifactDigest = PINNED_FRONTEND_CONTRACT_MAP_ARTIFACT_DIGEST,
): GeneratedFrontendContractMap {
  if (!exactKeys(value, ['schema', 'pack_id', 'artifact_path', 'artifact_digest', 'routes'])) {
    throw new Error('Generated frontend Contract Map artifact is stale or tampered.');
  }
  const map = value as Partial<GeneratedFrontendContractMap>;
  if (
    map.schema !== 'io.tobkiri.frontend-contract-map.v4'
    || map.pack_id !== 'defaultspack'
    || map.artifact_path !== 'defaultspack/frontend_contract_map.v4.json'
    || !isDigest(map.artifact_digest)
    || map.artifact_digest !== expectedArtifactDigest
    || !Array.isArray(map.routes)
    || map.routes.length !== Object.keys(EXPECTED_ROUTES).length
  ) {
    throw new Error('Generated frontend Contract Map artifact is stale or tampered.');
  }
  const seen = new Set<string>();
  for (const route of map.routes) {
    if (!exactKeys(route, ['method', 'path', 'presentation', 'targets'])) {
      throw new Error('Generated frontend Contract Map route is invalid.');
    }
    if (
      (route.method !== 'GET' && route.method !== 'POST')
      || typeof route.path !== 'string'
      || typeof route.presentation !== 'string'
      || !Array.isArray(route.targets)
      || route.targets.length === 0
    ) {
      throw new Error('Generated frontend Contract Map route is invalid.');
    }
    const key = `${route.method} ${route.path}`;
    const expected = EXPECTED_ROUTES[key];
    if (
      !expected
      || seen.has(key)
      || route.presentation !== expected.presentation
      || route.targets.length !== expected.targets.length
    ) {
      throw new Error('Generated frontend Contract Map target set is invalid.');
    }
    route.targets.forEach((target, index) => {
      const expectedTarget = expected.targets[index];
      if (!exactKeys(target, [
        'contribution_id',
        'contract_id',
        'operation_id',
        'provider_id',
        'function_id',
        'allowed_payload_keys',
      ]) || (
        target.contribution_id !== expectedTarget.contribution_id
        || target.contract_id !== expectedTarget.contract_id
        || target.operation_id !== expectedTarget.operation_id
        || target.provider_id !== expectedTarget.provider_id
        || target.function_id !== expectedTarget.function_id
        || !exactStringArray(target.allowed_payload_keys)
        || target.allowed_payload_keys.length !== expectedTarget.allowed_payload_keys.length
        || target.allowed_payload_keys.some((item, itemIndex) => item !== expectedTarget.allowed_payload_keys[itemIndex])
      )) {
        throw new Error('Generated frontend Contract Map target metadata is invalid.');
      }
    });
    seen.add(key);
  }
  return map as GeneratedFrontendContractMap;
}

export interface VerifiedGeneratedTarget {
  method: FrontendContractMethod;
  logical_target: string;
  contract_id: string;
  operation_id: string;
  contribution_id: string;
  provider_id: string;
  function_id: string;
  allowed_payload_keys: string[];
  map_artifact_digest: string;
  source_ref: string;
}

export function generatedRouteFor(
  map: GeneratedFrontendContractMap,
  method: FrontendContractMethod,
  logicalTarget: string,
): GeneratedFrontendContractRoute {
  const verified = validateGeneratedFrontendContractMap(map);
  const route = verified.routes.find((candidate) => candidate.method === method && candidate.path === logicalTarget);
  if (!route) {
    throw new Error(`Generated frontend Contract Map has no exact route for ${method} ${logicalTarget}.`);
  }
  return route;
}

export function generatedTargetFor(
  map: GeneratedFrontendContractMap,
  method: FrontendContractMethod,
  logicalTarget: string,
): VerifiedGeneratedTarget {
  const route = generatedRouteFor(map, method, logicalTarget);
  if (!route || route.targets.length !== 1) {
    throw new Error(`Generated frontend Contract Map has no exact target for ${method} ${logicalTarget}.`);
  }
  const target = route.targets[0];
  return {
    method: route.method,
    logical_target: route.path,
    contract_id: target.contract_id,
    operation_id: target.operation_id,
    contribution_id: target.contribution_id,
    provider_id: target.provider_id,
    function_id: target.function_id,
    allowed_payload_keys: [...target.allowed_payload_keys],
    map_artifact_digest: map.artifact_digest,
    source_ref: `pack-artifact://${map.pack_id}/${map.artifact_path}`,
  };
}

export const VERIFIED_GENERATED_FRONTEND_CONTRACT_MAP = validateGeneratedFrontendContractMap(
  GENERATED_FRONTEND_CONTRACT_MAP,
);
export const VERIFIED_GENERATED_RUNTIME_TARGETS = VERIFIED_GENERATED_FRONTEND_CONTRACT_MAP;
