import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** Merge conditional class lists, letting later Tailwind utilities win over
 *  earlier ones of the same kind. The shadcn convention, and the reason a
 *  component's `class` prop can override its own defaults. */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

/** `decodeURIComponent`, or null for a malformed escape (`%E0`, a lone `%`).
 *  The route parsers run inside `$effect`s, where a URIError from a
 *  hand-edited or truncated link would kill the route sync. */
export function decodeOrNull(s: string): string | null {
  try {
    return decodeURIComponent(s);
  } catch {
    return null;
  }
}
