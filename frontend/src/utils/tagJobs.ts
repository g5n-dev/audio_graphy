/**
 * Shared tag-job status vocabulary and polling policy.
 *
 * Both the run detail page and the reception workspace watch the same job
 * rows; keeping the terminal set in one place is what stops the two views
 * from disagreeing about whether a run is still moving.
 */

import type { TagJobStatus } from "@/types/api";

/** `succeeded` is the backend's alias for `completed` — both are终态. */
export const TERMINAL_TAG_JOB_STATUSES = new Set<TagJobStatus>([
  "completed",
  "succeeded",
  "failed",
  "cancelled",
]);

export function isTerminalTagJob(status: TagJobStatus | undefined): boolean {
  return status !== undefined && TERMINAL_TAG_JOB_STATUSES.has(status);
}

/**
 * Poll while the job is still moving, stop once it settles.
 *
 * An undefined status means the first fetch has not landed yet — polling then
 * would re-fire against a request that may be failing, with nothing on screen
 * saying so.
 */
export function tagJobPollInterval(
  status: TagJobStatus | undefined,
): number | false {
  if (status === undefined) return false;
  return isTerminalTagJob(status) ? false : 3_000;
}
