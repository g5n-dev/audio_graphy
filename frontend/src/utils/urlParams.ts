/**
 * URL parameter helpers — unified entry point for the graph drilldown
 * closed loop.  All cross-page navigation links MUST use buildFocusParam;
 * all consumer pages MUST parse via the helpers here.  No page should
 * call searchParams.get("focus")?.split(":") directly.
 */

/**
 * Parse a focus parameter of the form "<type>:<id>".
 *
 * The id portion is decoded via decodeURIComponent so that Chinese
 * characters and other special values survive the URL round-trip.
 *
 * Parsing is total: a malformed parameter (bad percent-encoding such as a
 * stray "%") yields `null` instead of throwing, because consumers call this
 * from render effects where a throw blanks the whole page.
 *
 * @returns `{ type, id }` when the parameter is well-formed, or `null`.
 */
export function parseFocusParam(
  raw: string | null,
): { type: string; id: string } | null {
  if (!raw) return null;
  const idx = raw.indexOf(":");
  if (idx <= 0) return null;
  const type = raw.slice(0, idx);
  let id: string;
  try {
    id = decodeURIComponent(raw.slice(idx + 1));
  } catch {
    // URIError — the value is not a usable focus target.
    return null;
  }
  if (!type || !id) return null;
  return { type, id };
}

/**
 * Parse an "at" (millisecond) URL parameter into a number.
 *
 * @returns the millisecond value, or `null` when missing / non-numeric.
 */
export function parseAtParam(raw: string | null): number | null {
  if (!raw) return null;
  const n = Number(raw);
  if (Number.isNaN(n)) return null;
  return n;
}

/**
 * Parse "from" / "to" time-window parameters (both in milliseconds).
 *
 * @returns `{ from, to }` — each field is `number | null`.
 */
export function parseTimeRangeParams(
  fromRaw: string | null,
  toRaw: string | null,
): { from: number | null; to: number | null } {
  return {
    from: parseAtParam(fromRaw),
    to: parseAtParam(toRaw),
  };
}

/**
 * Build a focus parameter value "<type>:<id>".
 *
 * The id is encoded via encodeURIComponent so that special characters
 * (including Chinese text) are safe inside a URL query string.
 *
 * @param type  Node type string, e.g. "录音" or "speaker".
 * @param id    Node identifier (string or number).
 * @returns     Encoded value suitable for `?focus=<value>`.
 */
export function buildFocusParam(type: string, id: string | number): string {
  return `${type}:${encodeURIComponent(String(id))}`;
}
