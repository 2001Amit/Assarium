"use client";

import { useEffect, useRef } from "react";
import { Bot, Code2, User } from "lucide-react";
import { cn } from "@/lib/cn";
import type { ChatMessage, ChatQueryResult } from "@/lib/types";
import { InlineResult } from "./InlineResult";

interface ChatPanelProps {
  messages: ChatMessage[];
  loading: boolean;
}

export function ChatPanel({ messages, loading }: ChatPanelProps) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages.length, loading]);

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="mx-auto max-w-[720px] space-y-4 px-4 py-6">
        {messages.length === 0 && !loading && <WelcomeMessage />}

        {messages.map((message, i) => (
          <MessageBubble key={i} message={message} />
        ))}

        {loading && (
          <div className="flex items-start gap-3 fade-in">
            <div className="flex size-7 shrink-0 items-center justify-center rounded-[var(--radius-md)] border border-[var(--line)] bg-[var(--surface-sunken)]">
              <Bot className="size-3.5 text-[var(--accent)]" aria-hidden />
            </div>
            <div className="flex items-center gap-1.5 pt-1.5">
              <span className="size-1.5 animate-pulse rounded-full bg-[var(--text-subtle)]" />
              <span className="size-1.5 animate-pulse rounded-full bg-[var(--text-subtle)] [animation-delay:150ms]" />
              <span className="size-1.5 animate-pulse rounded-full bg-[var(--text-subtle)] [animation-delay:300ms]" />
            </div>
          </div>
        )}

        <div ref={bottomRef} />
      </div>
    </div>
  );
}

function MessageBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === "user";

  return (
    <div className={cn("flex items-start gap-3 fade-in", isUser && "flex-row-reverse")}>
      <div
        className={cn(
          "flex size-7 shrink-0 items-center justify-center rounded-[var(--radius-md)] border border-[var(--line)]",
          isUser ? "bg-[var(--accent-soft)]" : "bg-[var(--surface-sunken)]",
        )}
      >
        {isUser ? (
          <User className="size-3.5 text-[var(--accent)]" aria-hidden />
        ) : (
          <Bot className="size-3.5 text-[var(--accent)]" aria-hidden />
        )}
      </div>

      <div
        className={cn(
          "min-w-0 max-w-[580px] rounded-[var(--radius-lg)] px-3.5 py-2.5",
          isUser
            ? "bg-[var(--accent)] text-[var(--accent-contrast)]"
            : "surface-panel",
        )}
      >
        <div className="whitespace-pre-wrap text-[13px] leading-relaxed">
          {_renderContent(message.content)}
        </div>

        {/* Inline query result */}
        {message.query_result && (
          <div className="mt-3 border-t border-[var(--line)] pt-3">
            <InlineResult result={message.query_result} />
          </div>
        )}
      </div>
    </div>
  );
}

function WelcomeMessage() {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center fade-in">
      <div className="mb-3 flex size-10 items-center justify-center rounded-[var(--radius-lg)] border border-[var(--line)] bg-[var(--surface-sunken)]">
        <Bot className="size-5 text-[var(--accent)]" aria-hidden />
      </div>
      <h2 className="text-[15px] font-semibold text-[var(--text)]">Ask your data</h2>
      <p className="mt-1.5 max-w-sm text-[12.5px] leading-relaxed text-[var(--text-muted)]">
        Ask a question about your data and get an answer backed by the semantic model.
        Every answer compiles through the governed metric layer — no free-text SQL.
      </p>
      <div className="mt-4 space-y-1.5">
        {[
          "What is total revenue this year?",
          "Show orders by status",
          "Revenue trend by month",
        ].map((q) => (
          <p
            key={q}
            className="rounded-[var(--radius-md)] border border-dashed border-[var(--line-strong)] px-3 py-1.5 text-[12px] text-[var(--text-subtle)]"
          >
            {q}
          </p>
        ))}
      </div>
    </div>
  );
}

/** Render **bold** markers in content. */
function _renderContent(content: string): React.ReactNode {
  const parts = content.split(/(\*\*[^*]+\*\*)/g);
  return parts.map((part, i) => {
    if (part.startsWith("**") && part.endsWith("**")) {
      return (
        <strong key={i} className="font-semibold">
          {part.slice(2, -2)}
        </strong>
      );
    }
    return part;
  });
}
