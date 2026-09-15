"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ChevronDown, MessagesSquare, Zap } from "lucide-react";
import { api, AssariumApiError } from "@/lib/api";
import { cn } from "@/lib/cn";
import type { ChatMessage, ChatResponse, Connection } from "@/lib/types";
import { PageHeader } from "@/components/PageHeader";
import { ChatInput } from "@/components/ask/ChatInput";
import { ChatPanel } from "@/components/ask/ChatPanel";
import { Callout } from "@/components/ui/Callout";
import { EmptyState } from "@/components/ui/EmptyState";
import { inputClass } from "@/components/ui/Field";

function AskWorkspace() {
  const params = useSearchParams();
  const [connectionId, setConnectionId] = useState<string | null>(
    params.get("connection"),
  );
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [aiAvailable, setAiAvailable] = useState<boolean | null>(null);

  // Connections list.
  const { data: connections } = useQuery({
    queryKey: ["connections"],
    queryFn: () => api.get<Connection[]>("/api/connections"),
  });

  useEffect(() => {
    if (!connectionId && connections?.length) setConnectionId(connections[0].id);
  }, [connections, connectionId]);

  // Check AI availability.
  useEffect(() => {
    api
      .get<{ available: boolean }>("/api/ai/status")
      .then((res) => setAiAvailable(res.available))
      .catch(() => setAiAvailable(false));
  }, []);

  // Send message.
  const sendMessage = useMutation({
    mutationFn: async (userMessage: string) => {
      const userChat: ChatMessage = { role: "user", content: userMessage };
      const updatedMessages = [...messages, userChat];
      setMessages(updatedMessages);

      const response = await api.post<ChatResponse>(
        `/api/connections/${connectionId}/chat`,
        { messages: updatedMessages },
      );
      return response;
    },
    onSuccess: (response) => {
      setMessages((prev) => [...prev, response.message]);
    },
    onError: (error) => {
      const errorMessage: ChatMessage = {
        role: "assistant",
        content:
          error instanceof AssariumApiError
            ? error.message
            : "Something went wrong. Please try again.",
      };
      setMessages((prev) => [...prev, errorMessage]);
    },
  });

  const handleSend = useCallback(
    (message: string) => {
      if (!connectionId) return;
      sendMessage.mutate(message);
    },
    [connectionId, sendMessage],
  );

  const handleNewChat = () => {
    setMessages([]);
  };

  return (
    <>
      <PageHeader
        title="Ask"
        description="Question your data in plain English. Answers compile through the semantic model."
        actions={
          <>
            <div className="relative">
              <select
                aria-label="Connection"
                value={connectionId ?? ""}
                onChange={(event) => {
                  setConnectionId(event.target.value);
                  setMessages([]);
                }}
                className={cn(
                  inputClass,
                  "h-7 appearance-none py-0 pr-7 text-[12.5px]",
                )}
              >
                {connections?.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </select>
              <ChevronDown
                className="pointer-events-none absolute right-2 top-1/2 size-3 -translate-y-1/2 text-[var(--text-subtle)]"
                aria-hidden
              />
            </div>

            {messages.length > 0 && (
              <button
                onClick={handleNewChat}
                className="rounded-[var(--radius-md)] border border-[var(--line-strong)] px-2.5 py-1 text-[12px] font-medium text-[var(--text-muted)] transition-colors hover:bg-[var(--surface-hover)] hover:text-[var(--text)]"
              >
                New chat
              </button>
            )}
          </>
        }
      />

      {aiAvailable === false && (
        <div className="px-4 pt-3">
          <Callout tone="caution" title="AI not configured">
            Set <code className="font-mono text-[11px]">ASSARIUM_AZURE_OPENAI_ENDPOINT</code>{" "}
            and <code className="font-mono text-[11px]">ASSARIUM_AZURE_OPENAI_API_KEY</code> to
            enable the chat feature. Dashboards work without AI.
          </Callout>
        </div>
      )}

      {!connectionId ? (
        <EmptyState
          icon={MessagesSquare}
          title="No connection selected"
          description="Choose a connection to start asking questions about your data."
        />
      ) : (
        <div className="flex min-h-0 flex-1 flex-col">
          <ChatPanel messages={messages} loading={sendMessage.isPending} />
          <ChatInput
            onSend={handleSend}
            disabled={!connectionId || sendMessage.isPending || aiAvailable === false}
          />
        </div>
      )}
    </>
  );
}

export default function AskPage() {
  return (
    <Suspense fallback={<PageHeader title="Ask" />}>
      <AskWorkspace />
    </Suspense>
  );
}
