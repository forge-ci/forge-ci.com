// In-place DOM patching for the live dashboard.
//
// Re-rendering a panel with replaceChildren() throws away every text node the
// user may have selected, so a refresh every second made the page impossible
// to copy from. morph() instead walks a freshly built tree alongside the live
// one and only touches nodes whose text or attributes actually changed, so
// unchanged text keeps its identity (and any selection inside it).

function sameShape(a: Node, b: Node): boolean {
  return a.nodeType === b.nodeType && a.nodeName === b.nodeName;
}

function patchAttributes(live: Element, fresh: Element): void {
  for (const { name } of [...live.attributes]) {
    if (!fresh.hasAttribute(name)) live.removeAttribute(name);
  }
  for (const { name, value } of fresh.attributes) {
    if (live.getAttribute(name) !== value) live.setAttribute(name, value);
  }
}

/** Make `live`'s children match `fresh`'s, reusing nodes position by position. */
export function morphChildren(live: Node, fresh: Node): void {
  const want = [...fresh.childNodes];
  let current = live.firstChild;
  for (const next of want) {
    if (current === null) {
      live.appendChild(next);
      continue;
    }
    if (sameShape(current, next)) {
      morphNode(current, next);
      current = current.nextSibling;
    } else {
      live.replaceChild(next, current);
      current = next.nextSibling;
    }
  }
  while (current !== null) {
    const extra = current;
    current = current.nextSibling;
    live.removeChild(extra);
  }
}

function morphNode(live: Node, fresh: Node): void {
  if (live.nodeType === Node.TEXT_NODE || live.nodeType === Node.COMMENT_NODE) {
    if (live.nodeValue !== fresh.nodeValue) live.nodeValue = fresh.nodeValue;
    return;
  }
  if (live instanceof Element && fresh instanceof Element) patchAttributes(live, fresh);
  morphChildren(live, fresh);
}

/** Replace `live`'s content with `nodes`, patching in place. */
export function morph(live: Element, ...nodes: Node[]): void {
  const fresh = live.ownerDocument.createElement(live.tagName);
  fresh.append(...nodes);
  morphChildren(live, fresh);
}

/** True while the user has a non-empty text selection that touches `root`. */
export function hasSelectionIn(root: Node, selection: Selection | null): boolean {
  if (selection === null || selection.isCollapsed || selection.rangeCount === 0) return false;
  for (let i = 0; i < selection.rangeCount; i += 1) {
    const range = selection.getRangeAt(i);
    if (range.toString() === "") continue;
    if (root.contains(range.commonAncestorContainer) || range.intersectsNode(root)) return true;
  }
  return false;
}

/** True while the pointer hovers, or keyboard focus sits, inside a table in `root`. */
export function isInteracting(root: Element, active: Element | null): boolean {
  if (active !== null && root.contains(active) && active.closest("table") !== null) return true;
  return root.querySelector("table:hover") !== null;
}

/**
 * Runs DOM updates unless the user is reading/selecting inside `root`; a
 * deferred update is applied as soon as the selection clears.
 */
export class UpdateGate {
  private pending: (() => void) | null = null;

  constructor(
    private readonly root: Element,
    private readonly doc: Document = root.ownerDocument,
  ) {
    doc.addEventListener("selectionchange", () => {
      this.flush();
    });
    root.addEventListener("pointerleave", () => {
      this.flush();
    });
    root.addEventListener("focusout", () => {
      this.flush();
    });
  }

  blocked(): boolean {
    return (
      hasSelectionIn(this.root, this.doc.getSelection()) ||
      isInteracting(this.root, this.doc.activeElement)
    );
  }

  /** Run `update` now, or once the user is done; only the latest update is kept. */
  run(update: () => void): boolean {
    if (this.blocked()) {
      this.pending = update;
      return false;
    }
    this.pending = null;
    update();
    return true;
  }

  get deferred(): boolean {
    return this.pending !== null;
  }

  flush(): void {
    if (this.pending !== null && !this.blocked()) {
      const update = this.pending;
      this.pending = null;
      update();
    }
  }
}
