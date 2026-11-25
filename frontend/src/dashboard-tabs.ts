export interface DashboardTab {
  readonly id: string;
  readonly label: string;
  readonly panel: HTMLElement;
  readonly timeWindow?: boolean;
}

export function mountTabs(tabs: readonly DashboardTab[], controls: HTMLElement): HTMLElement {
  const nav = document.createElement("nav");
  nav.className = "dashboard-nav";
  const list = document.createElement("div");
  list.className = "dashboard-tabs";
  list.setAttribute("role", "tablist");
  list.setAttribute("aria-label", "Dashboard views");
  nav.append(list);

  const buttons = tabs.map((tab) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = tab.label;
    button.id = `tab-${tab.id}`;
    button.setAttribute("role", "tab");
    button.setAttribute("aria-controls", `panel-${tab.id}`);
    tab.panel.id = `panel-${tab.id}`;
    tab.panel.setAttribute("role", "tabpanel");
    tab.panel.setAttribute("aria-labelledby", button.id);
    tab.panel.tabIndex = 0;
    list.append(button);
    return button;
  });

  function select(index: number, scroll: boolean): void {
    tabs.forEach((tab, i) => {
      const active = index === i;
      tab.panel.hidden = !active;
      const button = buttons[i];
      if (button !== undefined) {
        button.tabIndex = active ? 0 : -1;
        button.setAttribute("aria-selected", String(active));
      }
    });
    controls.hidden = tabs[index]?.timeWindow !== true;
    if (scroll && nav.getBoundingClientRect().top <= 0) {
      window.scrollTo({ top: 0, behavior: "instant" });
    }
  }

  buttons.forEach((button, index) => {
    button.addEventListener("click", () => {
      select(index, true);
    });
    button.addEventListener("keydown", (event) => {
      const last = buttons.length - 1;
      const next = (
        {
          ArrowLeft: index === 0 ? last : index - 1,
          ArrowRight: index === last ? 0 : index + 1,
          Home: 0,
          End: last,
        } as Record<string, number | undefined>
      )[event.key];
      if (next === undefined) return;
      event.preventDefault();
      select(next, true);
      buttons[next]?.focus({ preventScroll: true });
    });
  });
  select(0, false);
  return nav;
}
