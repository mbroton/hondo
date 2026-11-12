type Cue = { at: number; state: string };

function cues(el: HTMLElement): Cue[] {
  return (el.dataset.tl ?? "")
    .split(",")
    .filter(Boolean)
    .map((part) => {
      const [at, state] = part.split(":");
      return { at: Number(at), state };
    });
}

function nodes(root: HTMLElement): HTMLElement[] {
  return [...root.querySelectorAll<HTMLElement>("[data-tl]")];
}

/**
 * Sets each `[data-tl="ms:state,..."]` element's `data-s` to `state` at `ms`
 * after the call. Returns a function that cancels the pending cues.
 */
export function play(root: HTMLElement, onCue?: () => void): () => void {
  reset(root);
  const timers = nodes(root).flatMap((el) =>
    cues(el).map(({ at, state }) =>
      window.setTimeout(() => {
        el.dataset.s = state;
        onCue?.();
      }, at),
    ),
  );
  return () => timers.forEach((t) => window.clearTimeout(t));
}

/** Puts every element in its last state, with no motion. */
export function finish(root: HTMLElement): void {
  for (const el of nodes(root)) {
    const last = cues(el).at(-1);
    if (last) el.dataset.s = last.state;
  }
}

export function reset(root: HTMLElement): void {
  for (const el of nodes(root)) delete el.dataset.s;
}

export function duration(root: HTMLElement): number {
  return Math.max(0, ...nodes(root).flatMap((el) => cues(el).map((c) => c.at)));
}

export const reducedMotion = () =>
  matchMedia("(prefers-reduced-motion: reduce)").matches;
