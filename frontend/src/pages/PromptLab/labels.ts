/**
 * Prompt 实验室域的状态标签。
 *
 * `rejected` 在标签治理里是「未达标」（评估没过门禁），在这里是「已拒绝」
 * （人工否决了补丁）——同一个状态码，两种含义。覆盖而不是改共享表。
 */
export const PROMPT_LAB_STATUS_LABELS: Readonly<Record<string, string>> = {
  draft: "草稿",
  review: "待复核",
  accepted: "已采纳",
  rejected: "已拒绝",
  superseded: "已被取代",
  pending: "待决定",
  masked: "已脱敏",
  synthetic: "合成改写",
};

export const PATCH_KIND_LABELS: Readonly<Record<string, string>> = {
  instruction_rewrite: "指令重写",
  constraint_add: "新增约束",
  rule_clarification: "规则澄清",
};
