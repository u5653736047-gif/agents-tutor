import type { components } from "@/contracts/api.generated";

export type AgentRole = components["schemas"]["AgentRole"];

export type AgentRolePresentation = {
  badgeClassName: string;
  label: string;
};

export const agentRolePresentation = {
  supervisor: {
    badgeClassName:
      "border-role-supervisor/30 bg-role-supervisor/10 text-role-supervisor",
    label: "Supervisor",
  },
  teaching_assistant: {
    badgeClassName:
      "border-role-teaching-assistant/30 bg-role-teaching-assistant/10 text-role-teaching-assistant",
    label: "助教",
  },
  learning_assistant: {
    badgeClassName:
      "border-role-learning-assistant/30 bg-role-learning-assistant/10 text-role-learning-assistant",
    label: "助学",
  },
  evaluator: {
    badgeClassName:
      "border-role-evaluator/30 bg-role-evaluator/10 text-role-evaluator",
    label: "评价",
  },
} satisfies Record<AgentRole, AgentRolePresentation>;
