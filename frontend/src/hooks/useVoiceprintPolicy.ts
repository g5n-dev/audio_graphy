/**
 * useVoiceprintPolicy — read-only voiceprint merge/sampling policy.
 *
 * Wraps GET /speakers/voiceprint-policy with a long staleTime: the policy
 * only changes on redeploy, so every consumer (badges, quality drawer)
 * shares one cached fetch.
 */

import { useQuery } from "@tanstack/react-query";
import { getVoiceprintPolicy } from "@/api/speakers";
import type { VoiceprintPolicyResponse } from "@/types/api";

export function useVoiceprintPolicy(options?: { enabled?: boolean }) {
  return useQuery<VoiceprintPolicyResponse>({
    queryKey: ["voiceprint-policy"],
    queryFn: getVoiceprintPolicy,
    staleTime: 5 * 60_000,
    enabled: options?.enabled ?? true,
  });
}
