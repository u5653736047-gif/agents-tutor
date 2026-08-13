"use client";

// assistant-ui 接入(T4):自定义消息组件(UserMessage / AssistantMessage)。
// 视觉对齐旧 MessageRow(conversation-panel.tsx L175-238):
//   - 用户:右对齐主色气泡 + 附件区(AttachmentPreview 鉴权 Blob 链路,
//     经 metadata.custom.attachments 从转换器透传);
//   - 助手:左对齐卡片气泡 + AgentBadge(metadata.custom.agent)+
//     MessagePrimitive.Parts 分派 part 渲染器。
// 引用列表与反馈按钮的消息 footer 挂载属 T7,本骨架不含。

import { MessagePrimitive, useAuiState } from "@assistant-ui/react";

import { AgentBadge } from "@/components/agent-badge";
import { AttachmentPreview } from "@/components/conversation-panel";
import type { components } from "@/contracts/api.generated";
import { CUSTOM_METADATA_KEYS } from "@/lib/assistant/message-converter";
import type { AgentRole } from "@/lib/agent-roles";

import { AgentSwitchPart } from "./parts/agent-switch-part";
import { ReasoningPart } from "./parts/reasoning-part";
import { SubagentOutputPart } from "./parts/subagent-output-part";
import { AssistantTextPart, UserTextPart } from "./parts/text-part";
import { ToolCallPart } from "./parts/tool-call-part";

type Attachment = NonNullable<
  components["schemas"]["Message"]["attachments"]
>[number];

// part → 渲染器分派表(模块级常量,引用稳定——ThreadPrimitive.Messages 的
// memo 比较 components 引用,内联对象会导致整列重渲染)
const ASSISTANT_PART_COMPONENTS = {
  Text: AssistantTextPart,
  Reasoning: ReasoningPart,
  tools: { Fallback: ToolCallPart },
  data: {
    by_name: {
      "agent-switch": AgentSwitchPart,
      "subagent-output": SubagentOutputPart,
    },
  },
} as const;

const USER_PART_COMPONENTS = { Text: UserTextPart } as const;

// 宽容读取:metadata.custom 的值是 unknown,非法值(历史脏数据/未来扩展)
// 一律按缺失处理——与后端 message_agent_role 的「读取端宽容」哲学一致
function customMetadata<T>(
  custom: Record<string, unknown> | undefined,
  key: string,
  guard: (value: unknown) => value is T,
): T | null {
  const value = custom?.[key];
  return guard(value) ? value : null;
}

function isAgentRole(value: unknown): value is AgentRole {
  return (
    value === "supervisor" ||
    value === "teaching_assistant" ||
    value === "learning_assistant" ||
    value === "evaluator"
  );
}

function isAttachmentArray(value: unknown): value is Attachment[] {
  return (
    Array.isArray(value) &&
    value.every(
      (item) =>
        typeof item === "object" &&
        item !== null &&
        typeof (item as Attachment).file_id === "string",
    )
  );
}

export function UserMessage() {
  const attachments = useAuiState((state) =>
    customMetadata(
      state.message.metadata.custom as Record<string, unknown> | undefined,
      CUSTOM_METADATA_KEYS.attachments,
      isAttachmentArray,
    ),
  );

  return (
    <MessagePrimitive.Root
      className="flex justify-end"
      data-message-role="user"
      data-slot="user-message"
    >
      <div className="max-w-[85%] rounded-2xl rounded-br-md bg-primary px-4 py-3 text-body text-primary-foreground shadow-sm md:max-w-[75%]">
        <MessagePrimitive.Parts components={USER_PART_COMPONENTS} />
        {attachments && attachments.length > 0 ? (
          <div
            className="mt-3 flex flex-col items-start gap-2"
            data-slot="message-attachments"
          >
            {attachments.map((attachment) => (
              <AttachmentPreview
                attachment={attachment}
                key={attachment.file_id}
              />
            ))}
          </div>
        ) : null}
      </div>
    </MessagePrimitive.Root>
  );
}

export function AssistantMessage() {
  const agent = useAuiState((state) =>
    customMetadata(
      state.message.metadata.custom as Record<string, unknown> | undefined,
      CUSTOM_METADATA_KEYS.agent,
      isAgentRole,
    ),
  );

  return (
    <MessagePrimitive.Root
      className="flex justify-start"
      data-message-role="assistant"
      data-slot="assistant-message"
    >
      <div className="max-w-[90%] rounded-2xl rounded-bl-md border border-border/70 bg-card/80 px-5 py-4 text-body text-foreground shadow-sm md:max-w-[85%]">
        {agent ? (
          <AgentBadge agent={agent} />
        ) : (
          <span
            className="inline-flex items-center rounded-full border border-border bg-muted px-2 py-0.5 text-caption font-medium text-muted-foreground"
            data-slot="assistant-badge-fallback"
          >
            助手
          </span>
        )}
        <div className="mt-2 flex flex-col gap-2">
          <MessagePrimitive.Parts components={ASSISTANT_PART_COMPONENTS} />
        </div>
      </div>
    </MessagePrimitive.Root>
  );
}
