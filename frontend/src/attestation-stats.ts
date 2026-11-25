import { duration } from "./format";

interface Distribution {
  readonly p50: number | null;
  readonly p95: number | null;
}

interface Stats {
  readonly completed: number;
  readonly succeeded: number;
  readonly failed: number;
  readonly worker_ms: Distribution;
  readonly command_ms: Distribution;
  readonly publication_ms: Distribution;
}

function latency(ms: number | null): string {
  if (ms === null) return "—";
  return ms < 1000 ? `${String(ms)} ms` : duration(ms / 1000);
}

export function mountAttestationStats(panel: HTMLElement): void {
  const card = document.createElement("article");
  card.className = "chart-card";
  const heading = document.createElement("h2");
  heading.textContent = "Build attestations · Last 24 hours";
  const status = document.createElement("p");
  status.setAttribute("role", "status");
  const refresh = document.createElement("button");
  refresh.type = "button";
  refresh.textContent = "Refresh";
  const details = document.createElement("dl");
  card.append(heading, refresh, status, details);
  panel.replaceChildren(card);

  function value(label: string, text: string): void {
    const term = document.createElement("dt");
    term.textContent = label;
    const description = document.createElement("dd");
    description.textContent = text;
    details.append(term, description);
  }

  async function update(): Promise<void> {
    refresh.disabled = true;
    details.replaceChildren();
    status.textContent = "Loading attestation statistics…";
    try {
      const response = await fetch("/api/attestation-stats");
      if (!response.ok) throw new Error("Attestation statistics unavailable");
      const stats = (await response.json()) as Stats;
      if (stats.completed === 0) {
        status.textContent = "No completed attestations in this period.";
        return;
      }
      status.textContent = "";
      value("Completed", String(stats.completed));
      value("Succeeded", String(stats.succeeded));
      value("Failed", String(stats.failed));
      value(
        "Worker time (p50 / p95)",
        `${latency(stats.worker_ms.p50)} / ${latency(stats.worker_ms.p95)}`,
      );
      value(
        "Command time (p50 / p95)",
        `${latency(stats.command_ms.p50)} / ${latency(stats.command_ms.p95)}`,
      );
      value(
        "Attestation publication (p50 / p95)",
        `${latency(stats.publication_ms.p50)} / ${latency(stats.publication_ms.p95)}`,
      );
    } catch {
      status.textContent = "Attestation statistics are unavailable.";
    } finally {
      refresh.disabled = false;
    }
  }

  refresh.addEventListener("click", () => void update());
  void update();
}
