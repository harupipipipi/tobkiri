import type {DefaultsConfirmation} from '@/src/lib/defaultsSetup';

export function DefaultsConfirmationDetails({confirmation}: {readonly confirmation: DefaultsConfirmation}) {
  return <details className="rounded-lg border border-border bg-bg-main p-4">
    <summary className="cursor-pointer font-medium text-text-main">Exact activation details ({confirmation.bindings.length} operation bindings)</summary>
    <p className="mt-3 text-xs text-text-muted">These are the Host-issued identities submitted by this confirmation. Digests identify exact revisions; they are not descriptions of what an operation is allowed to do.</p>
    <dl className="mt-3 space-y-2 text-xs">
      <Field label="Profile" value={confirmation.profile_id} />
      <Field label="Profile revision" value={confirmation.profile_revision} />
      <Field label="Catalog revision" value={confirmation.catalog_revision} />
      <Field label="Plan digest" value={confirmation.plan_digest} />
      <Field label="Authority snapshot" value={confirmation.authority_snapshot_digest} />
      <Field label="SecurityEpoch" value={String(confirmation.security_epoch)} />
      <Field label="Confirmation digest" value={confirmation.confirmation_digest} />
    </dl>
    <ul aria-label="Confirmed operation bindings" className="mt-4 space-y-2">
      {confirmation.bindings.map((binding, index) => <li key={`${binding.caller_function_id}:${binding.operation_id}:${index}`}>
        <details className="rounded border border-border p-3">
          <summary className="cursor-pointer break-all text-xs text-text-main">{binding.operation_id} — {binding.pack_id}</summary>
          <dl className="mt-3 space-y-2 text-xs">
            <Field label="Caller" value={binding.caller_function_id} />
            <Field label="Provider Function" value={binding.function_principal.function_id} />
            <Field label="Contract" value={binding.contract_id} />
            <Field label="Contract revision" value={binding.function_principal.contract_revision_digest} />
            <Field label="Requested scope digest" value={binding.requested_scope_digest} />
            <Field label="Authority mode" value={binding.authority_mode ?? 'Not supplied'} />
            <Field label="Execution" value={`${binding.execution_kind} / ${binding.backend} / ${binding.domain_kind}`} />
            <Field label="Artifact digest" value={binding.artifact_digest} />
          </dl>
        </details>
      </li>)}
    </ul>
  </details>;
}

function Field({label, value}: {readonly label: string; readonly value: string}) {
  return <div><dt className="text-text-muted">{label}</dt><dd className="break-all font-mono text-text-main">{value}</dd></div>;
}
