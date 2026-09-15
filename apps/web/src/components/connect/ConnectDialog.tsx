"use client";

import { useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowUpRight, CheckCircle2, ShieldAlert, XCircle } from "lucide-react";
import { api, AssariumApiError } from "@/lib/api";
import type { Connection, ConnectionTestResult, SourceSpec } from "@/lib/types";
import { Button } from "@/components/ui/Button";
import { Callout } from "@/components/ui/Callout";
import { Dialog } from "@/components/ui/Dialog";
import { Field, inputClass } from "@/components/ui/Field";
import { cn } from "@/lib/cn";
import { groupFields, SpecFieldInput, type FormValues } from "./SpecForm";
import { sourceIcon } from "./sourceIcon";

function initialValues(spec: SourceSpec, authMethod: string): FormValues {
  const values: FormValues = {};
  const method = spec.auth_methods.find((m) => m.id === authMethod);
  for (const field of [...spec.fields, ...(method?.fields ?? [])]) {
    if (field.default !== null && field.default !== undefined) {
      values[field.name] = field.default as string | number | boolean;
    }
  }
  return values;
}

/** Prefer the method the driver marked recommended over whatever happens to be first. */
function defaultAuthMethod(spec: SourceSpec): string {
  if (spec.auth_methods.length === 0) return "default";
  return (spec.auth_methods.find((m) => m.recommended) ?? spec.auth_methods[0]).id;
}

export function ConnectDialog({
  spec,
  open,
  onClose,
}: {
  spec: SourceSpec;
  open: boolean;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [authMethod, setAuthMethod] = useState(() => defaultAuthMethod(spec));
  const [values, setValues] = useState<FormValues>(() => initialValues(spec, authMethod));
  const [name, setName] = useState(spec.name);
  const [test, setTest] = useState<ConnectionTestResult | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);

  const method = spec.auth_methods.find((m) => m.id === authMethod);
  const Icon = sourceIcon(spec.icon);

  const grouped = useMemo(
    () => groupFields([...spec.fields, ...(method?.fields ?? [])], values),
    [spec.fields, method, values],
  );

  const setValue = (key: string, value: string | number | boolean) => {
    setValues((previous) => ({ ...previous, [key]: value }));
    setTest(null);
    setFieldErrors(({ [key]: _removed, ...rest }) => rest);
  };

  const switchAuth = (id: string) => {
    setAuthMethod(id);
    // Keep shared fields; drop anything belonging to the method being left behind.
    const shared = new Set(spec.fields.map((f) => f.name));
    setValues((previous) => {
      const next: FormValues = initialValues(spec, id);
      for (const [key, value] of Object.entries(previous)) {
        if (shared.has(key)) next[key] = value;
      }
      return next;
    });
    setTest(null);
    setFieldErrors({});
  };

  const handleApiError = (error: unknown) => {
    if (error instanceof AssariumApiError) {
      const missing = error.details?.fields;
      if (Array.isArray(missing)) {
        const byLabel = new Map(
          [...spec.fields, ...(method?.fields ?? [])].map((f) => [f.label, f.name]),
        );
        const next: Record<string, string> = {};
        for (const label of missing as string[]) {
          const key = byLabel.get(label);
          if (key) next[key] = "Required";
        }
        setFieldErrors(next);
        setFormError(null);
        return;
      }
      setFormError(error.message);
      return;
    }
    setFormError("The request could not be completed.");
  };

  const testMutation = useMutation({
    mutationFn: () =>
      api.post<ConnectionTestResult>("/api/connections/test", {
        source_id: spec.source_id,
        auth_method: authMethod,
        values,
      }),
    onMutate: () => {
      setTest(null);
      setFormError(null);
      setFieldErrors({});
    },
    onSuccess: setTest,
    onError: handleApiError,
  });

  const saveMutation = useMutation({
    mutationFn: () =>
      api.post<Connection>("/api/connections", {
        name: name.trim(),
        source_id: spec.source_id,
        auth_method: authMethod,
        values,
      }),
    onMutate: () => {
      setFormError(null);
      setFieldErrors({});
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["connections"] });
      onClose();
    },
    onError: handleApiError,
  });

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={`Connect to ${spec.name}`}
      description={spec.summary}
      footer={
        <>
          <div className="mr-auto flex min-w-0 items-center gap-2">
            {test && (
              <span
                className={cn(
                  "flex min-w-0 items-center gap-1.5 text-[12px]",
                  test.ok ? "text-[var(--color-positive)]" : "text-[var(--color-critical)]",
                )}
              >
                {test.ok ? (
                  <CheckCircle2 className="size-3.5 shrink-0" aria-hidden />
                ) : (
                  <XCircle className="size-3.5 shrink-0" aria-hidden />
                )}
                <span className="truncate">
                  {test.ok
                    ? `Connected${test.latency_ms !== null ? ` in ${test.latency_ms} ms` : ""}`
                    : test.message}
                </span>
              </span>
            )}
          </div>
          <Button onClick={onClose}>Cancel</Button>
          <Button onClick={() => testMutation.mutate()} loading={testMutation.isPending}>
            Test connection
          </Button>
          <Button
            variant="primary"
            onClick={() => saveMutation.mutate()}
            loading={saveMutation.isPending}
            disabled={!name.trim()}
          >
            Save connection
          </Button>
        </>
      }
    >
      <div className="space-y-5">
        {!spec.available && (
          <Callout tone="caution" title="Driver not installed on this server">
            <p className="mt-0.5">
              Install it and restart the API:{" "}
              <code className="rounded-[var(--radius-xs)] bg-[var(--surface-sunken)] px-1 py-px font-mono text-[11.5px]">
                {spec.install_hint}
              </code>
            </p>
          </Callout>
        )}

        <Field label="Connection name" required>
          {(id) => (
            <input
              id={id}
              className={inputClass}
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="Production warehouse"
            />
          )}
        </Field>

        {spec.auth_methods.length > 0 && (
          <section>
            <p className="mb-2 text-[12px] font-medium text-[var(--text-muted)]">
              Authentication
            </p>
            <div className="grid gap-1.5">
              {spec.auth_methods.map((option) => {
                const selected = option.id === authMethod;
                return (
                  <button
                    key={option.id}
                    type="button"
                    onClick={() => switchAuth(option.id)}
                    className={cn(
                      "flex w-full items-start gap-2.5 rounded-[var(--radius-md)] border px-3 py-2 text-left transition-colors duration-100",
                      selected
                        ? "border-[var(--accent)] bg-[var(--accent-soft)]"
                        : "border-[var(--line)] hover:border-[var(--line-strong)] hover:bg-[var(--surface-hover)]",
                    )}
                  >
                    <span
                      className={cn(
                        "mt-[3px] flex size-3.5 shrink-0 items-center justify-center rounded-full border",
                        selected ? "border-[var(--accent)]" : "border-[var(--line-strong)]",
                      )}
                    >
                      {selected && <span className="size-1.5 rounded-full bg-[var(--accent)]" />}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="flex flex-wrap items-center gap-1.5">
                        <span className="text-[12.5px] font-medium text-[var(--text)]">
                          {option.label}
                        </span>
                        {option.recommended && (
                          <span className="text-[10.5px] font-medium uppercase tracking-[0.05em] text-[var(--accent)]">
                            recommended
                          </span>
                        )}
                        {option.deprecated && (
                          <span className="text-[10.5px] font-medium uppercase tracking-[0.05em] text-[var(--color-caution)]">
                            deprecated
                          </span>
                        )}
                      </span>
                      <span className="mt-0.5 block text-[11.5px] leading-relaxed text-[var(--text-muted)]">
                        {option.description}
                      </span>
                    </span>
                  </button>
                );
              })}
            </div>
          </section>
        )}

        {method?.notice && (
          <Callout tone={method.deprecated ? "caution" : "info"}>{method.notice}</Callout>
        )}

        {grouped.map(([group, fields]) => (
          <section key={group}>
            <p className="mb-2 text-[12px] font-medium text-[var(--text-muted)]">{group}</p>
            <div className="grid gap-3.5 sm:grid-cols-2">
              {fields.map((field) => (
                <div
                  key={field.name}
                  className={
                    field.type === "textarea" || field.type === "boolean" ? "sm:col-span-2" : ""
                  }
                >
                  <SpecFieldInput
                    field={field}
                    value={values[field.name]}
                    error={fieldErrors[field.name]}
                    onChange={(value) => setValue(field.name, value)}
                  />
                </div>
              ))}
            </div>
          </section>
        ))}

        {formError && <Callout tone="critical">{formError}</Callout>}

        {test?.ok && Object.keys(test.details).length > 0 && (
          <div className="rounded-[var(--radius-md)] border border-[var(--line)] bg-[var(--surface-sunken)] px-3 py-2">
            <p className="mb-1.5 text-[11px] font-medium uppercase tracking-[0.05em] text-[var(--text-subtle)]">
              Server reported
            </p>
            <dl className="grid gap-x-6 gap-y-1 text-[12px] sm:grid-cols-2">
              {test.server_version && (
                <div className="flex justify-between gap-3">
                  <dt className="text-[var(--text-muted)]">Version</dt>
                  <dd className="truncate font-mono text-[11.5px]">{test.server_version}</dd>
                </div>
              )}
              {Object.entries(test.details).map(([key, value]) => (
                <div key={key} className="flex justify-between gap-3">
                  <dt className="text-[var(--text-muted)]">{key.replace(/_/g, " ")}</dt>
                  <dd className="truncate font-mono text-[11.5px]">{String(value)}</dd>
                </div>
              ))}
            </dl>
          </div>
        )}

        <footer className="flex items-center gap-4 border-t border-[var(--line)] pt-3.5 text-[11.5px] text-[var(--text-subtle)]">
          <span className="flex items-center gap-1.5">
            <ShieldAlert className="size-3.5" aria-hidden />
            Credentials are encrypted before storage and are never returned to the browser.
          </span>
          {spec.docs_url && (
            <a
              href={spec.docs_url}
              target="_blank"
              rel="noreferrer noopener"
              className="ml-auto flex shrink-0 items-center gap-1 text-[var(--text-muted)] hover:text-[var(--accent)]"
            >
              <Icon className="size-3" aria-hidden />
              Provider setup guide
              <ArrowUpRight className="size-3" aria-hidden />
            </a>
          )}
        </footer>
      </div>
    </Dialog>
  );
}
