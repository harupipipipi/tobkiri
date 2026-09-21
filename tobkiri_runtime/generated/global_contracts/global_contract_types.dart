// Generated from schemas/global_contract_types.schema.json.
enum ContractStatus {
  ok,
  unknown,
  unavailable,
  notConfigured,
  denied,
  incompatible,
  missingProvider,
  staleResolution,
  invalidManifest
}

class ContractResult<T> {
  const ContractResult({
    required this.status,
    required this.contractId,
    required this.version,
    required this.providerInstanceId,
    this.diagnostics = const [],
    this.value,
  });

  final ContractStatus status;
  final String contractId;
  final String version;
  final String providerInstanceId;
  final List<String> diagnostics;
  final T? value;
}
