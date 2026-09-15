"use client";

import { useState } from "react";
import { Filter as FilterIcon, X } from "lucide-react";
import { cn } from "@/lib/cn";
import type { MetricFilter, SemanticModel } from "@/lib/types";

interface FilterBarProps {
  model: SemanticModel;
  filters: MetricFilter[];
  onChange: (filters: MetricFilter[]) => void;
  connectionId: string;
}

export function FilterBar({ model, filters, onChange, connectionId }: FilterBarProps) {
  const [adding, setAdding] = useState(false);
  const [selectedField, setSelectedField] = useState("");
  const [selectedValues, setSelectedValues] = useState<string[]>([]);
  const [loadedValues, setLoadedValues] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);

  // Collect all filterable dimensions from the model.
  const dimensions: { reference: string; label: string }[] = [];
  for (const entity of model.entities) {
    for (const attr of entity.attributes) {
      if (attr.hidden || attr.contains_pii) continue;
      if (attr.role === "dimension" || attr.role === "key") {
        dimensions.push({
          reference: `${entity.id}.${attr.name}`,
          label: `${entity.label} / ${attr.label}`,
        });
      }
    }
  }

  const loadValues = async (field: string) => {
    setLoading(true);
    try {
      const response = await fetch(`/api/connections/${connectionId}/semantic/values`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ field, limit: 100 }),
      });
      if (response.ok) {
        const data = await response.json();
        setLoadedValues(data.values?.map(String) ?? []);
      }
    } finally {
      setLoading(false);
    }
  };

  const addFilter = () => {
    if (!selectedField || selectedValues.length === 0) return;
    const newFilter: MetricFilter = {
      field: selectedField,
      operator: "in",
      values: selectedValues,
    };
    onChange([...filters, newFilter]);
    setAdding(false);
    setSelectedField("");
    setSelectedValues([]);
    setLoadedValues([]);
  };

  const removeFilter = (index: number) => {
    onChange(filters.filter((_, i) => i !== index));
  };

  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-[var(--line)] bg-[var(--surface)] px-4 py-2">
      <FilterIcon className="size-3.5 text-[var(--text-subtle)]" aria-hidden />
      <span className="text-[11.5px] font-medium text-[var(--text-subtle)]">Filters</span>

      {/* Active filter pills */}
      {filters.map((filter, i) => {
        const dim = dimensions.find((d) => d.reference === filter.field);
        return (
          <div
            key={i}
            className="inline-flex items-center gap-1 rounded-[var(--radius-sm)] border border-[var(--accent)]/25 bg-[var(--accent)]/8 px-2 py-0.5 text-[11px] font-medium text-[var(--accent)]"
          >
            <span>{dim?.label ?? filter.field}</span>
            <span className="text-[var(--accent)]/60">{filter.operator}</span>
            <span className="max-w-[120px] truncate">
              {filter.values.map(String).join(", ")}
            </span>
            <button
              onClick={() => removeFilter(i)}
              className="ml-0.5 rounded-sm hover:bg-[var(--accent)]/20"
            >
              <X className="size-3" aria-hidden />
            </button>
          </div>
        );
      })}

      {/* Add filter */}
      {adding ? (
        <div className="flex items-center gap-1.5">
          <select
            value={selectedField}
            onChange={(e) => {
              setSelectedField(e.target.value);
              setSelectedValues([]);
              if (e.target.value) loadValues(e.target.value);
            }}
            className="h-6 rounded-[var(--radius-sm)] border border-[var(--line-strong)] bg-[var(--surface)] px-1.5 text-[11px] text-[var(--text)]"
          >
            <option value="">Select field</option>
            {dimensions.map((d) => (
              <option key={d.reference} value={d.reference}>
                {d.label}
              </option>
            ))}
          </select>

          {selectedField && (
            <select
              multiple
              value={selectedValues}
              onChange={(e) =>
                setSelectedValues(
                  Array.from(e.target.selectedOptions, (o) => o.value),
                )
              }
              className="h-16 min-w-[120px] rounded-[var(--radius-sm)] border border-[var(--line-strong)] bg-[var(--surface)] px-1.5 text-[11px] text-[var(--text)]"
            >
              {loading ? (
                <option disabled>Loading...</option>
              ) : (
                loadedValues.map((v) => (
                  <option key={v} value={v}>
                    {v}
                  </option>
                ))
              )}
            </select>
          )}

          <button
            onClick={addFilter}
            disabled={!selectedField || selectedValues.length === 0}
            className="h-6 rounded-[var(--radius-sm)] bg-[var(--accent)] px-2 text-[11px] font-medium text-[var(--accent-contrast)] disabled:opacity-40"
          >
            Add
          </button>
          <button
            onClick={() => {
              setAdding(false);
              setSelectedField("");
              setSelectedValues([]);
            }}
            className="h-6 px-1 text-[11px] text-[var(--text-muted)] hover:text-[var(--text)]"
          >
            Cancel
          </button>
        </div>
      ) : (
        <button
          onClick={() => setAdding(true)}
          className="rounded-[var(--radius-sm)] border border-dashed border-[var(--line-strong)] px-2 py-0.5 text-[11px] text-[var(--text-subtle)] transition-colors hover:border-[var(--accent)] hover:text-[var(--accent)]"
        >
          + Add filter
        </button>
      )}
    </div>
  );
}
