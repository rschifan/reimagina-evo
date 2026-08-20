import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** Merge conditional class lists, letting later Tailwind utilities win over
 *  earlier ones of the same kind. The shadcn convention, and the reason a
 *  component's `class` prop can override its own defaults. */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
