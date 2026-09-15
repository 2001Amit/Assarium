"use client";

import { cn } from "@/lib/cn";
import { Field, inputClass } from "@/components/ui/Field";
import type { SpecField } from "@/lib/types";

export type FormValues = Record<string, string | number | boolean>;

/** A field is rendered only when its `show_if` conditions hold against current values. */
export function isVisible(field: SpecField, values: FormValues): boolean {
  if (!field.show_if) return true;
  return Object.entries(field.show_if).every(([key, allowed]) =>
    allowed.includes(String(values[key])),
  );
}

export function groupFields(fields: SpecField[], values: FormValues) {
  const groups = new Map<string, SpecField[]>();
  for (const field of fields) {
    if (!isVisible(field, values)) continue;
    const list = groups.get(field.group) ?? [];
    list.push(field);
    groups.set(field.group, list);
  }
  return [...groups.entries()];
}

export function SpecFieldInput({
  field,
  value,
  error,
  onChange,
}: {
  field: SpecField;
  value: string | number | boolean | undefined;
  error?: string | null;
  onChange: (value: string | number | boolean) => void;
}) {
  if (field.type === "boolean") {
    const checked = Boolean(value);
    return (
      <label className="flex cursor-pointer items-start gap-2.5 py-0.5">
        <button
          type="button"
          role="switch"
          aria-checked={checked}
          onClick={() => onChange(!checked)}
          className={cn(
            "mt-0.5 h-[18px] w-8 shrink-0 rounded-full border transition-colors duration-100",
            checked
              ? "border-transparent bg-[var(--accent)]"
              : "border-[var(--line-strong)] bg-[var(--surface-sunken)]",
          )}
        >
          <span
            className={cn(
              "block size-3 rounded-full bg-white transition-transform duration-100",
              checked ? "translate-x-[15px]" : "translate-x-[2px]",
            )}
          />
        </button>
        <span className="min-w-0">
          <span className="block text-[12.5px] font-medium text-[var(--text)]">{field.label}</span>
          {field.help && (
            <span className="block text-[11.5px] leading-relaxed text-[var(--text-subtle)]">
              {field.help}
            </span>
          )}
        </span>
      </label>
    );
  }

  return (
    <Field label={field.label} help={field.help} error={error} required={field.required}>
      {(id) => {
        if (field.type === "select") {
          return (
            <select
              id={id}
              className={inputClass}
              value={String(value ?? field.default ?? "")}
              onChange={(event) => onChange(event.target.value)}
            >
              {field.options?.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          );
        }
        if (field.type === "textarea") {
          return (
            <textarea
              id={id}
              rows={field.secret ? 4 : 3}
              spellCheck={false}
              autoComplete="off"
              placeholder={field.placeholder ?? undefined}
              className={cn(inputClass, "resize-y font-mono text-[12px] leading-relaxed")}
              value={String(value ?? "")}
              onChange={(event) => onChange(event.target.value)}
            />
          );
        }
        return (
          <input
            id={id}
            type={field.type === "password" ? "password" : field.type === "number" ? "number" : "text"}
            autoComplete={field.secret ? "new-password" : "off"}
            spellCheck={false}
            placeholder={field.placeholder ?? undefined}
            className={inputClass}
            value={String(value ?? "")}
            onChange={(event) =>
              onChange(field.type === "number" ? Number(event.target.value) : event.target.value)
            }
          />
        );
      }}
    </Field>
  );
}
