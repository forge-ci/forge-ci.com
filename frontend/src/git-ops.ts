import { duration } from "./format";
import { UpdateGate } from "./live-dom";

interface Job {
  readonly id: number;
  readonly run_id: number;
  readonly attempt: number;
  readonly workflow: string | null;
  readonly title: string;
  readonly branch: string | null;
  readonly name: string;
  readonly status: string;
  readonly conclusion: string | null;
  readonly runner: string | null;
  readonly placement: string;
  readonly created_at: number;
  readonly started_at: number | null;
  readonly completed_at: number | null;
  readonly steps: readonly (readonly [string, string, string | null])[];
}

interface Repository {
  readonly name: string;
  readonly observed_at: number | null;
  readonly partial: boolean;
  readonly jobs: readonly Job[];
}

interface Snapshot {
  readonly observed_at: number;
  readonly repositories: readonly Repository[];
}

interface RunnerEvent {
  readonly host: string;
  readonly runner: string;
  readonly job: string;
  readonly phase: string;
  readonly result: string | null;
  readonly observed_at: number;
}

interface Feed {
  readonly actions: Snapshot | null;
  readonly runner_events: readonly RunnerEvent[];
}

function node<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  text?: string,
): HTMLElementTagNameMap[K] {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  return element;
}

function table(
  headers: readonly string[],
  rows: readonly (readonly (string | Node)[])[],
): HTMLElement {
  const scroll = node("div");
  scroll.className = "ops-scroll";
  const result = node("table");
  const heading = result.createTHead().insertRow();
  for (const label of headers) {
    const cell = node("th", label);
    cell.scope = "col";
    heading.append(cell);
  }
  const body = result.createTBody();
  for (const row of rows) {
    const tr = body.insertRow();
    for (const value of row) tr.insertCell().append(value);
  }
  scroll.append(result);
  return scroll;
}

const running = (job: Job): boolean => job.status === "in_progress";
const waiting = (job: Job): boolean => job.status !== "completed" && !running(job);
const failed = (job: Job): boolean =>
  job.conclusion === "failure" || job.conclusion === "timed_out";
const lastDay = (job: Job): boolean =>
  job.completed_at !== null && Date.now() / 1000 - job.completed_at <= 86400;

function percentile(numbers: number[], fraction: number): string {
  numbers.sort((a, b) => a - b);
  const result = numbers[Math.ceil(numbers.length * fraction) - 1];
  return result === undefined ? "—" : duration(result);
}

function runtime(job: Job): string {
  if (job.started_at === null || waiting(job)) return "—";
  return duration(Math.max(0, (job.completed_at ?? Date.now() / 1000) - job.started_at));
}

function details(repo: string, job: Job, expanded: Set<number>): HTMLElement {
  const row = node("details");
  row.open = expanded.has(job.id);
  row.addEventListener("toggle", () => {
    if (row.open) expanded.add(job.id);
    else expanded.delete(job.id);
  });
  const summary = node("summary", job.name);
  const link = node("a", "Open job on GitHub");
  link.href = `https://github.com/${repo}/actions/runs/${String(job.run_id)}/job/${String(job.id)}`;
  row.append(
    summary,
    node("p", job.title),
    node("p", `${job.branch ?? "Unknown branch"} · Attempt ${String(job.attempt)}`),
    link,
  );
  if (job.runner !== null) row.append(node("p", job.runner));
  if (job.steps.length > 0) {
    const steps = node("ol");
    for (const [name, status, conclusion] of job.steps)
      steps.append(node("li", `${name} · ${conclusion ?? status}`));
    row.append(steps);
  }
  return row;
}

function option(value: string, label: string): HTMLOptionElement {
  const item = node("option", label);
  item.value = value;
  return item;
}

export function mountGitOps(root: HTMLElement): void {
  const section = node("section");
  section.className = "git-ops";
  const status = node("p", "Loading GitHub Actions data…");
  status.className = "ops-status";
  const cards = node("div");
  cards.className = "ops-cards";
  const repositories = node("div");
  const activity = node("div");
  const events = node("div");
  const filter = node("select");
  filter.setAttribute("aria-label", "Filter by repository");
  const state = node("select");
  state.setAttribute("aria-label", "Filter by status");
  state.append(
    option("active", "Active jobs"),
    option("running", "Running"),
    option("waiting", "Waiting"),
    option("completed", "Recent completions"),
  );
  const search = node("input");
  search.type = "search";
  search.placeholder = "Search job or workflow";
  search.setAttribute("aria-label", search.placeholder);
  const controls = node("div");
  controls.className = "ops-filters";
  controls.append(filter, state, search);
  const expanded = new Set<number>();
  let feed: Feed | null = null;
  let visible = 60;
  const showMore = node("button", "Show more jobs");
  showMore.type = "button";
  showMore.addEventListener("click", () => {
    visible += 60;
    renderJobs();
  });
  const repositoryDetails = node("details");
  repositoryDetails.className = "dashboard-disclosure";
  repositoryDetails.append(
    node("summary", "Repository metrics"),
    node(
      "p",
      "Recent history samples up to 12 completed workflow runs per repository in the last 24 hours.",
    ),
    repositories,
  );
  const runnerDetails = node("details");
  runnerDetails.className = "dashboard-disclosure";
  runnerDetails.append(node("summary", "Runner event history"), events);
  const jobDetails = node("details");
  jobDetails.className = "dashboard-disclosure";
  jobDetails.append(node("summary", "Job activity"), controls, activity, showMore);
  section.append(
    node("h2", "Git Ops"),
    status,
    cards,
    repositoryDetails,
    runnerDetails,
    jobDetails,
  );
  root.append(section);
  const gate = new UpdateGate(section);

  function renderJobs(): void {
    const term = search.value.toLowerCase().trim();
    const selected = (feed?.actions?.repositories ?? []).flatMap((repo) =>
      repo.jobs
        .filter(
          (job) =>
            (filter.value === "" || repo.name === filter.value) &&
            (state.value === "completed"
              ? lastDay(job)
              : state.value === "running"
                ? running(job)
                : state.value === "waiting"
                  ? waiting(job)
                  : job.status !== "completed") &&
            `${job.name} ${job.workflow ?? ""} ${job.title} ${job.branch ?? ""}`
              .toLowerCase()
              .includes(term),
        )
        .map((job) => ({ repo: repo.name, job })),
    );
    selected.sort((a, b) =>
      state.value === "completed"
        ? (b.job.completed_at ?? 0) - (a.job.completed_at ?? 0)
        : Number(running(b.job)) - Number(running(a.job)) || a.job.created_at - b.job.created_at,
    );
    activity.replaceChildren(
      selected.length === 0
        ? node("p", "No matching jobs in the current snapshot.")
        : table(
            ["Repository", "Workflow", "Job", "Status", "Placement", "Runtime"],
            selected
              .slice(0, visible)
              .map(({ repo, job }) => [
                repo,
                job.workflow ?? "Unknown workflow",
                details(repo, job, expanded),
                (job.conclusion ?? job.status).replaceAll("_", " "),
                job.placement,
                runtime(job),
              ]),
          ),
    );
    showMore.hidden = selected.length <= visible;
  }

  function render(): void {
    const snapshot = feed?.actions ?? null;
    const repositoryRows = snapshot?.repositories ?? [];
    const fresh = repositoryRows.filter(
      (repo) => repo.observed_at !== null && Date.now() / 1000 - repo.observed_at <= 360,
    );
    const jobs = fresh.flatMap((repo) => repo.jobs);
    const partial =
      fresh.length !== repositoryRows.length || repositoryRows.some((repo) => repo.partial);
    status.textContent =
      snapshot === null
        ? "Waiting for the GitHub Actions collector."
        : `${String(fresh.length)} of ${String(repositoryRows.length)} repositories reporting · ${partial ? "Limited coverage · " : ""}Last poll ${new Date(snapshot.observed_at * 1000).toLocaleTimeString()}`;
    const stats: [string, number][] = [
      ["GitHub running", jobs.filter(running).length],
      ["GitHub waiting", jobs.filter(waiting).length],
      ["Running on Dell", jobs.filter((j) => running(j) && j.placement === "Dell").length],
      [
        "GitHub-hosted running",
        jobs.filter((j) => running(j) && j.placement === "GitHub hosted").length,
      ],
      ["Completed jobs · 24h sample", jobs.filter(lastDay).length],
    ];
    cards.replaceChildren(
      ...stats.map(([label, count]) => {
        const card = node("div");
        card.className = "host-card ops-stat";
        card.append(node("span", label), node("strong", fresh.length === 0 ? "—" : String(count)));
        return card;
      }),
    );
    repositories.replaceChildren(
      table(
        [
          "Repository",
          "Running",
          "Waiting",
          "Dell",
          "Completed",
          "Failed",
          "p95 runtime",
          "Coverage",
        ],
        repositoryRows.map((repo) => {
          const active = repo.jobs.filter(running);
          const completed = repo.jobs.filter(lastDay);
          const durations = completed.flatMap((j) =>
            j.started_at === null || j.completed_at === null
              ? []
              : [Math.max(0, j.completed_at - j.started_at)],
          );
          const link = node("a", repo.name);
          link.href = `https://github.com/${repo.name}/actions`;
          return [
            link,
            String(active.length),
            String(repo.jobs.filter(waiting).length),
            String(active.filter((j) => j.placement === "Dell").length),
            String(completed.length),
            String(completed.filter(failed).length),
            percentile(durations, 0.95),
            !fresh.includes(repo)
              ? "Stale or unavailable"
              : repo.partial
                ? "Capped or incomplete"
                : "Current",
          ];
        }),
      ),
    );
    const current = filter.value;
    filter.replaceChildren(
      option("", "All repositories"),
      ...repositoryRows.map((repo) => option(repo.name, repo.name)),
    );
    filter.value = repositoryRows.some((repo) => repo.name === current) ? current : "";
    renderJobs();
    const lifecycle = feed?.runner_events ?? [];
    function eventRepository(event: RunnerEvent): string {
      const runner = `${event.host}-${event.runner.replace("-runner-", "-")}`;
      const candidates = repositoryRows.flatMap((repo) =>
        repo.jobs.flatMap((job) => {
          const time = event.phase === "started" ? job.started_at : job.completed_at;
          if (job.runner !== runner || job.name !== event.job || time === null) return [];
          return [{ name: repo.name, distance: Math.abs(event.observed_at - time) }];
        }),
      );
      candidates.sort((a, b) => a.distance - b.distance);
      const match = candidates[0];
      return match !== undefined && match.distance <= 300 ? match.name : "—";
    }
    events.replaceChildren(
      lifecycle.length === 0
        ? node("p", "Waiting for lifecycle events from the runners.")
        : table(
            ["Time", "Repository", "Host", "Runner", "Job", "Event"],
            lifecycle
              .slice(0, 30)
              .map((event) => [
                new Date(event.observed_at * 1000).toLocaleTimeString(),
                eventRepository(event),
                event.host,
                event.runner,
                event.job,
                event.result === null ? event.phase : `${event.phase} · ${event.result}`,
              ]),
          ),
    );
  }

  async function refresh(): Promise<void> {
    try {
      const response = await fetch("/api/git-ops");
      if (!response.ok) throw new Error(String(response.status));
      feed = (await response.json()) as Feed;
      // Hold the refresh while the user selects or hovers a table here.
      gate.run(render);
    } catch {
      status.textContent = "Git Ops data could not be refreshed.";
    }
  }

  for (const control of [filter, state, search])
    control.addEventListener("input", () => {
      visible = 60;
      renderJobs();
    });
  void refresh();
  window.setInterval(() => {
    void refresh();
  }, 30000);
}
