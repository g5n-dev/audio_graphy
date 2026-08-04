import { IconClose } from "@arco-design/web-react/icon";
import type { FormEvent, ReactNode } from "react";

/**
 * 治理系弹窗的外壳。
 *
 * 抽象自仓库里 9 份形状完全相同的手写弹窗，DOM 与它们逐字节一致——这样既有页面
 * 迁移过来时，基于 role 与可访问名的断言不需要改。
 *
 * 刻意不做三件事：
 * - 不加 focus trap。加了会改变 9 个既有弹窗的键盘行为并推翻它们的测试。
 * - 不用 portal。现有实现直接渲染在原地，层级已经调好。
 * - 不引 Arco Modal。它会把 Modal 及其依赖从 lazy chunk 拉回首屏，与
 *   vite.config.ts 里 manualChunks 的注释直接冲突。
 *
 * 首个输入的 autoFocus 仍由调用方自己放，与现状一致。
 *
 * 样式依赖 TagGovernance/tagGovernance.css，不像 StatusChip / Metric 那样自带 CSS：
 * 弹窗的规则与 .ag-review-page、.ag-tag-run-page 共享了四处分组选择器，拆出来就得
 * 复制声明体，留下两处需要同步的定义。消费方本来就都会引入那张样式表，代价不对等。
 */
export function GovernanceDialog({
  id,
  kicker,
  title,
  pending,
  onClose,
  onSubmit,
  submitLabel,
  pendingLabel,
  cancelLabel = "取消",
  closeLabel = "关闭",
  danger = false,
  submitDisabled = false,
  error,
  errorAction,
  className,
  children,
}: {
  /** aria-labelledby 的目标 id。 */
  id: string;
  kicker: string;
  title: ReactNode;
  pending: boolean;
  onClose: () => void;
  onSubmit: () => void;
  submitLabel: string;
  pendingLabel: string;
  cancelLabel?: string;
  closeLabel?: string;
  /** 确认销毁类操作把主按钮渲染成危险色。 */
  danger?: boolean;
  submitDisabled?: boolean;
  /** 已格式化的错误文案；调用方自己走 getErrorMessage。 */
  error?: string | null;
  /** 错误文案之后的行动入口，例如「重试」按钮。 */
  errorAction?: ReactNode;
  /** 追加到 section 的尺寸修饰类。 */
  className?: string;
  children: ReactNode;
}) {
  const submit = (event: FormEvent) => {
    event.preventDefault();
    onSubmit();
  };

  return (
    <div className="ag-dialog-backdrop">
      <section
        className={
          className ? `ag-governance-dialog ${className}` : "ag-governance-dialog"
        }
        role="dialog"
        aria-modal="true"
        aria-labelledby={id}
        onKeyDown={(event) => {
          if (event.key === "Escape" && !pending) onClose();
        }}
      >
        <header>
          <div>
            <span className="ag-card-kicker">{kicker}</span>
            <h2 id={id}>{title}</h2>
          </div>
          <button
            type="button"
            aria-label={closeLabel}
            disabled={pending}
            onClick={onClose}
          >
            <IconClose />
          </button>
        </header>
        <form onSubmit={submit}>
          {children}
          {error && (
            <p className="ag-inline-feedback is-error" role="alert">
              {error}
              {errorAction}
            </p>
          )}
          <footer>
            <button
              type="button"
              className="is-secondary"
              disabled={pending}
              onClick={onClose}
            >
              {cancelLabel}
            </button>
            <button
              type="submit"
              className={danger ? "is-danger" : undefined}
              disabled={pending || submitDisabled}
            >
              {pending ? pendingLabel : submitLabel}
            </button>
          </footer>
        </form>
      </section>
    </div>
  );
}
