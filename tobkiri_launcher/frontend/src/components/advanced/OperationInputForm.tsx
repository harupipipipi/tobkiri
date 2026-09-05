import {useEffect, useRef, useState, type FormEvent} from 'react';
import {AlertCircle} from 'lucide-react';

import {Button} from '@/src/components/ui/Button';
import {CopyErrorButton} from '@/src/components/ui/CopyErrorButton';
import {Input} from '@/src/components/ui/Input';
import {Badge} from '@/src/components/ui/Badge';
import {
  advancedActionAllowed,
  advancedActionMetadata,
  type LauncherAdvancedViewDescriptor,
} from '@/src/lib/advancedSurfaces';
import {RUNTIME_CONTRACT_INVOKE_ACTION, type RuntimeJsonSchema, type RuntimeOperationDescriptor} from '@/src/lib/runtimeSurface';

type InputValue = unknown;

function initialValue(schema: RuntimeJsonSchema, required: boolean): InputValue {
  if (schema.default !== undefined) return schema.default as InputValue;
  if (required && schema.enum && schema.enum.length > 0) return schema.enum[0];
  if (schema.type === 'boolean') return required ? false : undefined;
  return '';
}

function displayValue(value: InputValue, schema: RuntimeJsonSchema): string {
  if (schema.type === 'object' || schema.type === 'array') {
    if (value === undefined || value === null) return '';
    if (typeof value === 'string') return value;
    return JSON.stringify(value, null, 2) ?? '';
  }
  return String(value ?? '');
}

function isMissing(value: InputValue | undefined, schema: RuntimeJsonSchema): boolean {
  if (value === undefined || (typeof value === 'string' && value.trim() === '')) return true;
  if (value !== null) return false;
  return schema.type !== 'null' && !(schema.enum ?? []).some((option) => option === null);
}

function enumValueToken(value: InputValue, options: unknown[]): string {
  const index = options.findIndex((option) => {
    if (Object.is(option, value)) return true;
    if (typeof option !== 'object' || option === null || typeof value !== 'object' || value === null) {
      return false;
    }
    try {
      return JSON.stringify(option) === JSON.stringify(value);
    } catch {
      return false;
    }
  });
  return index < 0 ? '' : String(index);
}

function enumOptionLabel(value: unknown): string {
  if (value === null) return 'null';
  if (typeof value === 'object') return JSON.stringify(value) ?? '';
  return String(value);
}

export function OperationInputForm({
  operation,
  descriptor,
  busy,
  canInvoke,
  onInvoke,
}: {
  operation: RuntimeOperationDescriptor;
  descriptor: LauncherAdvancedViewDescriptor;
  busy: boolean;
  /** The parent descriptor/action gate must explicitly authorize invocation. */
  canInvoke: boolean;
  onInvoke: (payload: Record<string, unknown>) => Promise<void>;
}) {
  const actionMetadata = advancedActionMetadata(descriptor);
  const descriptorAllowsInvocation = advancedActionAllowed(descriptor, RUNTIME_CONTRACT_INVOKE_ACTION)
    && actionMetadata.showContractInvocationUi
    && actionMetadata.requiresAuthoritativeInvokableOperation;
  const invocationAllowed = canInvoke
    && descriptorAllowsInvocation
    && operation.action === RUNTIME_CONTRACT_INVOKE_ACTION
    && operation.invokable;
  const properties = Object.entries(operation.input_schema?.properties ?? {});
  const required = new Set(operation.input_schema?.required ?? []);
  const [values, setValues] = useState<Record<string, InputValue>>({});
  const [validationError, setValidationError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const submittingRef = useRef(false);
  const formBusy = busy || submitting;
  const schemaSignature = JSON.stringify(operation.input_schema ?? {});

  useEffect(() => {
    setValues(Object.fromEntries(properties.map(([name, schema]) => [name, initialValue(schema, required.has(name))])));
    setValidationError(null);
  }, [operation.action, operation.contract_id, operation.operation_id, schemaSignature]);

  const updateValue = (name: string, value: InputValue) => {
    setValues((current) => ({...current, [name]: value}));
    setValidationError(null);
  };

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy || submittingRef.current) return;
    if (!invocationAllowed) {
      setValidationError('This operation is not currently authorized by the accepted Contract action metadata.');
      return;
    }
    for (const name of required) {
      const schema = properties.find(([propertyName]) => propertyName === name)?.[1];
      if (!schema || isMissing(values[name], schema)) {
        setValidationError(`Required input “${name}” is missing.`);
        return;
      }
    }

    const payload: Record<string, unknown> = {};
    for (const [name, schema] of properties) {
      const value = values[name];
      if (isMissing(value, schema)) {
        if (required.has(name)) {
          setValidationError(`Required input “${name}” is missing.`);
          return;
        }
        continue;
      }
      if (schema.type === 'number' || schema.type === 'integer') {
        const parsed = typeof value === 'number' ? value : Number(String(value).trim());
        if (!Number.isFinite(parsed)) {
          setValidationError(`Input “${name}” must be a number.`);
          return;
        }
        if (schema.type === 'integer' && !Number.isInteger(parsed)) {
          setValidationError(`Input “${name}” must be an integer.`);
          return;
        }
        if (schema.enum && !schema.enum.some((option) => Object.is(option, parsed))) {
          setValidationError(`Input “${name}” must be one of the declared values.`);
          return;
        }
        payload[name] = parsed;
      } else if (schema.type === 'object' || schema.type === 'array') {
        let parsed: unknown;
        if (typeof value !== 'string') {
          parsed = value;
        } else {
          try {
            parsed = JSON.parse(value) as unknown;
          } catch {
            setValidationError(`Input “${name}” must contain valid JSON.`);
            return;
          }
        }
        if (
          (schema.type === 'object' && (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)))
          || (schema.type === 'array' && !Array.isArray(parsed))
        ) {
          setValidationError(`Input “${name}” must contain a JSON ${schema.type}.`);
          return;
        }
        payload[name] = parsed;
      } else {
        payload[name] = value;
      }
    }
    setValidationError(null);
    submittingRef.current = true;
    setSubmitting(true);
    try {
      await onInvoke(payload);
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
    }
  };

  if (!descriptorAllowsInvocation) return null;

  return (
    <form className="flex flex-col gap-4" onSubmit={handleSubmit}>
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="outline">Schema-driven input</Badge>
        {required.size > 0 ? <span className="text-xs text-text-muted">Required fields are marked.</span> : null}
      </div>
      {properties.length === 0 ? (
        <p className="text-sm text-text-muted">This operation declares no input properties.</p>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2">
          {properties.map(([name, schema]) => {
            const value = values[name];
            const label = schema.title || name;
            const helper = schema.description || (required.has(name) ? 'Required' : undefined);
            if (schema.enum && schema.enum.length > 0) {
              return (
                <label key={name} className="flex min-w-0 flex-col gap-1.5 text-sm font-medium text-text-main">
                  <span>{label}{required.has(name) ? <span className="ml-1 text-destructive">*</span> : null}</span>
                  <select
                    className="min-h-11 w-full rounded-lg border border-border bg-bg-main px-3 py-2 text-sm text-text-main focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]"
                    value={enumValueToken(value, schema.enum)}
                    onChange={(event) => {
                      const index = Number(event.target.value);
                      updateValue(name, Number.isInteger(index) && index >= 0 ? schema.enum?.[index] : undefined);
                    }}
                    aria-label={label}
                    disabled={formBusy}
                  >
                    {!required.has(name) ? <option value="">Select a value</option> : null}
                    {schema.enum.map((option, index) => (
                      <option key={`${name}-${index}`} value={String(index)}>{enumOptionLabel(option)}</option>
                    ))}
                  </select>
                  {helper ? <span className="text-xs font-normal text-text-muted">{helper}</span> : null}
                </label>
              );
            }
            if (schema.type === 'boolean') {
              return (
                <label key={name} className="flex min-h-11 items-center gap-3 rounded-lg border border-border bg-bg-main px-3 py-2 text-sm font-medium text-text-main">
                  <input
                    type="checkbox"
                    checked={Boolean(value)}
                    onChange={(event) => updateValue(name, event.target.checked)}
                    aria-label={label}
                    disabled={formBusy}
                    className="h-4 w-4 accent-[var(--accent)]"
                  />
                  <span>{label}{required.has(name) ? <span className="ml-1 text-destructive">*</span> : null}</span>
                  {helper ? <span className="ml-auto text-xs font-normal text-text-muted">{helper}</span> : null}
                </label>
              );
            }
            if (schema.type === 'object' || schema.type === 'array') {
              return (
                <label key={name} className="flex min-w-0 flex-col gap-1.5 text-sm font-medium text-text-main sm:col-span-2">
                  <span>{label}{required.has(name) ? <span className="ml-1 text-destructive">*</span> : null}</span>
                  <textarea
                    className="min-h-28 w-full rounded-lg border border-border bg-bg-main px-3 py-2 font-mono text-xs text-text-main focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]"
                    value={displayValue(value, schema)}
                    onChange={(event) => updateValue(name, event.target.value)}
                    aria-label={label}
                    disabled={formBusy}
                  />
                  {helper ? <span className="text-xs font-normal text-text-muted">{helper}</span> : null}
                </label>
              );
            }
            return (
              <Input
                key={name}
                id={`operation-${operation.operation_id}-${name}`}
                label={label}
                helperText={helper}
                required={required.has(name)}
                type={schema.type === 'number' || schema.type === 'integer' ? 'number' : 'text'}
                value={displayValue(value, schema)}
                onChange={(event) => updateValue(name, event.target.value)}
                disabled={formBusy}
              />
            );
          })}
        </div>
      )}
      {validationError ? <div className="flex items-start gap-2 text-sm text-destructive" role="alert"><AlertCircle aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0" /><p className="min-w-0 flex-1 break-words">{validationError}</p><CopyErrorButton label="Copy validation error" text={validationError} /></div> : null}
      <Button
        type="submit"
        className="min-h-11 self-start"
        loading={formBusy}
        disabled={formBusy || !invocationAllowed}
        aria-label="Invoke declared contract operation"
      >
        Invoke declared operation
      </Button>
    </form>
  );
}
