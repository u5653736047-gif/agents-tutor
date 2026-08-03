"use client";

import { SendHorizontal } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { useChatStore } from "@/stores/chat-store";

type SendShortcut = {
  isComposing: boolean;
  key: string;
  shiftKey: boolean;
};

type ChatInputContentProps = {
  isSending: boolean;
  onChange(value: string): void;
  onSubmit(): void;
  value: string;
};

export function normalizeMessage(value: string): string | null {
  const message = value.trim();
  return message || null;
}

export function isSendShortcut({ isComposing, key, shiftKey }: SendShortcut) {
  return key === "Enter" && !shiftKey && !isComposing;
}

export function ChatInputContent({
  isSending,
  onChange,
  onSubmit,
  value,
}: ChatInputContentProps) {
  const isEmpty = normalizeMessage(value) === null;

  return (
    <form
      className="flex items-end gap-3"
      data-slot="chat-input"
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      <textarea
        aria-label="输入消息"
        className="min-h-24 flex-1 rounded-lg border border-input bg-background px-3 py-2 text-body text-foreground outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:bg-muted"
        disabled={isSending}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={(event) => {
          if (
            isSendShortcut({
              isComposing: event.nativeEvent.isComposing,
              key: event.key,
              shiftKey: event.shiftKey,
            })
          ) {
            event.preventDefault();
            onSubmit();
          }
        }}
        placeholder="输入消息，Enter 发送，Shift+Enter 换行"
        rows={3}
        value={value}
      />
      <Button disabled={isSending || isEmpty} type="submit">
        <SendHorizontal aria-hidden className="size-4" />
        发送
      </Button>
    </form>
  );
}

export function ChatInput() {
  const isSending = useChatStore((state) => state.isSending);
  const sendMessage = useChatStore((state) => state.sendMessage);
  const [value, setValue] = useState("");

  const handleSubmit = () => {
    const message = normalizeMessage(value);
    if (!message || isSending) {
      return;
    }

    setValue("");
    void sendMessage(message);
  };

  return (
    <ChatInputContent
      isSending={isSending}
      onChange={setValue}
      onSubmit={handleSubmit}
      value={value}
    />
  );
}
