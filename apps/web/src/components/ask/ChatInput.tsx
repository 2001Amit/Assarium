"use client";

import { useRef, useState } from "react";
import { SendHorizontal } from "lucide-react";
import { cn } from "@/lib/cn";

interface ChatInputProps {
  onSend: (message: string) => void;
  disabled?: boolean;
}

export function ChatInput({ onSend, disabled }: ChatInputProps) {
  const [value, setValue] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const submit = () => {
    const trimmed = value.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setValue("");
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  const handleInput = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setValue(e.target.value);
    // Auto-resize.
    const target = e.target;
    target.style.height = "auto";
    target.style.height = `${Math.min(target.scrollHeight, 160)}px`;
  };

  return (
    <div className="border-t border-[var(--line)] bg-[var(--surface)] px-4 py-3">
      <div className="mx-auto flex max-w-[720px] items-end gap-2">
        <div className="relative min-h-[36px] min-w-0 flex-1">
          <textarea
            ref={textareaRef}
            value={value}
            onChange={handleInput}
            onKeyDown={handleKeyDown}
            disabled={disabled}
            placeholder="Ask a question about your data..."
            rows={1}
            className={cn(
              "w-full resize-none rounded-[var(--radius-lg)] border border-[var(--line-strong)] bg-[var(--surface)] px-3.5 py-2",
              "text-[13px] text-[var(--text)] placeholder:text-[var(--text-subtle)]",
              "outline-none transition-colors focus:border-[var(--accent)]",
              "disabled:opacity-50",
            )}
          />
        </div>

        <button
          onClick={submit}
          disabled={!value.trim() || disabled}
          className={cn(
            "flex size-9 shrink-0 items-center justify-center rounded-[var(--radius-lg)]",
            "bg-[var(--accent)] text-[var(--accent-contrast)]",
            "transition-opacity disabled:opacity-40",
          )}
          aria-label="Send"
        >
          <SendHorizontal className="size-4" aria-hidden />
        </button>
      </div>
    </div>
  );
}
