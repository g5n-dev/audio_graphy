/**
 * useSpeakerMergeReview — shared confirm/reject mutations for the
 * SpeakerMergePending (L8 fuzzy reconfirm) queue.
 *
 * Used by the speaker detail page and the voiceprint quality drawer so
 * both invalidate the same ["speaker-merge-pending", ...] query family.
 */

import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Message } from "@arco-design/web-react";
import { confirmSpeakerMerge, rejectSpeakerMerge } from "@/api/advancedGraph";
import { getErrorMessage } from "@/utils/errors";

export interface MergeReviewInput {
  /** Optional reviewer-supplied cosine, range [-1, 1]. */
  voiceprint_score?: number;
  notes?: string;
}

export function useSpeakerMergeReview() {
  const queryClient = useQueryClient();
  const [submitting, setSubmitting] = useState(false);

  async function invalidatePending(): Promise<void> {
    await queryClient.invalidateQueries({
      queryKey: ["speaker-merge-pending"],
    });
  }

  async function confirm(
    pendingId: number,
    targetSpeakerId: number,
    input: MergeReviewInput = {},
  ): Promise<boolean> {
    setSubmitting(true);
    try {
      await confirmSpeakerMerge(pendingId, targetSpeakerId, input);
      Message.success("合并已确认");
      await invalidatePending();
      return true;
    } catch (error) {
      // A 409 means someone else already resolved this row, so the local
      // list is stale either way — refetch before the user retries.
      Message.error(getErrorMessage(error, "确认失败"));
      await invalidatePending();
      return false;
    } finally {
      setSubmitting(false);
    }
  }

  async function reject(
    pendingId: number,
    input: MergeReviewInput = {},
  ): Promise<boolean> {
    setSubmitting(true);
    try {
      await rejectSpeakerMerge(pendingId, { notes: input.notes });
      Message.success("合并已驳回");
      await invalidatePending();
      return true;
    } catch (error) {
      Message.error(getErrorMessage(error, "驳回失败"));
      await invalidatePending();
      return false;
    } finally {
      setSubmitting(false);
    }
  }

  return { confirm, reject, submitting };
}
