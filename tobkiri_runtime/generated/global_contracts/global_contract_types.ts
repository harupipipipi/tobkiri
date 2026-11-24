// Generated from schemas/global_contract_types.schema.json.
export type ContractStatus = 'ok' | 'unknown' | 'unavailable' | 'not_configured' | 'denied' | 'incompatible' | 'missing_provider' | 'stale_resolution' | 'invalid_manifest';

export interface ContractResult<T = unknown> {
  status: ContractStatus;
  contract_id: string;
  version: string;
  provider_instance_id: string;
  diagnostics?: string[];
  value?: T;
}
