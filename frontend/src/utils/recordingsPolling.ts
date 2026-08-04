/**
 * Polling policy for the recordings list.
 *
 * Lives outside RecordingsPage.tsx so the page file keeps a single
 * component export (react-refresh) and the policy stays unit-testable.
 */

import type { RecordingListItem } from "@/types/api";

/**
 * Poll interval for the list while any visible row is still moving through
 * the pipeline; `false` stops polling once everything is settled so an idle
 * list page does not hammer the backend.
 */
export function recordingsPollInterval(
  items: ReadonlyArray<Pick<RecordingListItem, "status">> | undefined,
): number | false {
  const active = items?.some(
    (item) => item.status === "queued" || item.status === "processing",
  );
  return active ? 5000 : false;
}
