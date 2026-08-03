import { agentRolePresentation, type AgentRole } from "@/lib/agent-roles";
import { cn } from "@/lib/utils";

type AgentBadgeProps = {
  agent: AgentRole;
  className?: string;
};

export function AgentBadge({ agent, className }: AgentBadgeProps) {
  const presentation = agentRolePresentation[agent];

  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border px-2 py-0.5 text-caption font-medium",
        presentation.badgeClassName,
        className,
      )}
      data-slot="agent-badge"
    >
      {presentation.label}
    </span>
  );
}
