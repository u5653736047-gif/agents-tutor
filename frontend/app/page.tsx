import { AppShell } from "@/components/app-shell";
import type { paths } from "@/contracts/api.generated";
import { apiBaseUrl } from "@/lib/api-base-url";

export const dynamic = "force-dynamic";

type HealthzResponse = paths["/healthz"]["get"]["responses"][200]["content"]["application/json"];

async function readHealth(): Promise<HealthzResponse | null> {
  try {
    const response = await fetch(`${apiBaseUrl}/healthz`, { cache: "no-store" });
    if (!response.ok) {
      return null;
    }
    return (await response.json()) as HealthzResponse;
  } catch {
    return null;
  }
}

export default async function Home() {
  const health = await readHealth();
  const connected = health?.status === "ok";

  return <AppShell apiConnected={connected} />;
}
