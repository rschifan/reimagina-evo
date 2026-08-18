/** Format an arbitrary number with adaptive decimal places — integers
 *  pass through, ``|n|>=100`` rounds to 0dp, ``|n|>=1`` to 2dp, smaller
 *  to 3dp. Locale-aware (uses ``toLocaleString``) so thousands
 *  separators match the user's runtime locale. */
export function fmtNumber(value: number): string {
  if (Number.isInteger(value)) return value.toLocaleString();
  const abs = Math.abs(value);
  const digits = abs >= 100 ? 0 : abs >= 1 ? 2 : 3;
  return value.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}


/** Escape the five HTML-significant characters for safe insertion
 *  into innerHTML / template-literal markup. Use when a user-supplied
 *  string is concatenated into HTML rather than set via textContent. */
export function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
