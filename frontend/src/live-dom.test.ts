// @vitest-environment happy-dom
import { describe, expect, it } from "vitest";

import { hasSelectionIn, morph, UpdateGate } from "./live-dom";

function cell(text: string): HTMLElement {
  const td = document.createElement("td");
  td.textContent = text;
  return td;
}

function row(...texts: string[]): HTMLTableRowElement {
  const tr = document.createElement("tr");
  tr.append(...texts.map(cell));
  return tr;
}

describe("morph", () => {
  it("keeps unchanged text nodes and patches changed ones in place", () => {
    const body = document.createElement("tbody");
    morph(body, row("host-a", "10%"), row("host-b", "20%"));
    const keptRow = body.children[0];
    const keptText = body.children[0]?.children[0]?.firstChild;
    const changedText = body.children[1]?.children[1]?.firstChild;

    morph(body, row("host-a", "10%"), row("host-b", "35%"));

    expect(body.children[0]).toBe(keptRow);
    expect(body.children[0]?.children[0]?.firstChild).toBe(keptText);
    expect(body.children[1]?.children[1]?.firstChild).toBe(changedText);
    expect(body.textContent).toBe("host-a10%host-b35%");
  });

  it("adds, removes and replaces nodes when the shape changes", () => {
    const box = document.createElement("div");
    morph(box, document.createElement("p"), document.createElement("p"));
    morph(box, document.createElement("span"));
    expect(box.children).toHaveLength(1);
    expect(box.firstElementChild?.tagName).toBe("SPAN");
    morph(box, document.createElement("span"), document.createElement("em"));
    expect(box.children).toHaveLength(2);
  });

  it("syncs attributes", () => {
    const box = document.createElement("div");
    const a = document.createElement("span");
    a.className = "status status-live";
    a.title = "x";
    morph(box, a);
    const b = document.createElement("span");
    b.className = "status status-stale";
    morph(box, b);
    expect(box.firstElementChild?.className).toBe("status status-stale");
    expect(box.firstElementChild?.hasAttribute("title")).toBe(false);
  });
});

describe("selection guard", () => {
  it("detects a selection inside the root and defers updates until it clears", () => {
    const rootEl = document.createElement("div");
    const outside = document.createElement("p");
    outside.textContent = "outside";
    const inside = document.createElement("p");
    inside.textContent = "inside text";
    rootEl.append(inside);
    document.body.replaceChildren(rootEl, outside);
    const gate = new UpdateGate(rootEl);
    const selection = document.getSelection();
    if (selection === null) throw new Error("no selection API");

    const range = document.createRange();
    range.selectNodeContents(inside);
    selection.removeAllRanges();
    selection.addRange(range);
    expect(hasSelectionIn(rootEl, selection)).toBe(true);

    let runs = 0;
    expect(gate.run(() => (runs += 1))).toBe(false);
    expect(gate.deferred).toBe(true);
    expect(runs).toBe(0);

    selection.removeAllRanges();
    gate.flush();
    expect(runs).toBe(1);
    expect(gate.deferred).toBe(false);

    const away = document.createRange();
    away.selectNodeContents(outside);
    selection.addRange(away);
    expect(hasSelectionIn(rootEl, selection)).toBe(false);
    expect(gate.run(() => (runs += 1))).toBe(true);
    expect(runs).toBe(2);
  });
});
