import { useCallback, useRef } from "react";
import type { KeyboardEvent } from "react";

/**
 * WAI-ARIA 标签页的键盘导航与 roving tabindex。
 *
 * 仓库里有三份手写实现（TagGovernance、TagInsights、GraphExplorer），彼此有两处
 * 差异：GraphExplorer 在写完 searchParams 之后才移焦（同步移焦会被重渲染吞掉），
 * 另外两处是同步移焦。`focusMode` 就是为了容纳这个差异——本 hook 的 API 做成能
 * 覆盖三种形态，但**暂不迁移既有页面**：那会碰它们的焦点断言，属于夹带。
 *
 * TODO: 单独一次改动把三处调用点迁过来。
 */
export interface TabListOptions<Id extends string> {
  tabs: readonly { readonly id: Id }[];
  activeId: Id;
  onSelect: (id: Id) => void;
  focusMode?: "sync" | "animation-frame";
}

export interface TabProps {
  role: "tab";
  "aria-selected": boolean;
  tabIndex: 0 | -1;
  ref: (element: HTMLButtonElement | null) => void;
  onKeyDown: (event: KeyboardEvent<HTMLButtonElement>) => void;
}

export interface TabListApi<Id extends string> {
  /** 按索引选中并移焦，索引超界时环绕。 */
  selectTab: (index: number) => void;
  onTabKeyDown: (event: KeyboardEvent<HTMLButtonElement>, index: number) => void;
  tabProps: (id: Id, index: number) => TabProps;
}

export function useTabList<Id extends string>({
  tabs,
  activeId,
  onSelect,
  focusMode = "sync",
}: TabListOptions<Id>): TabListApi<Id> {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);

  const focusTab = useCallback(
    (index: number) => {
      const focus = () => refs.current[index]?.focus();
      if (focusMode === "animation-frame") {
        requestAnimationFrame(focus);
        return;
      }
      focus();
    },
    [focusMode],
  );

  const selectTab = useCallback(
    (index: number) => {
      if (tabs.length === 0) return;
      const wrapped = ((index % tabs.length) + tabs.length) % tabs.length;
      const tab = tabs[wrapped];
      if (!tab) return;
      onSelect(tab.id);
      focusTab(wrapped);
    },
    [focusTab, onSelect, tabs],
  );

  const onTabKeyDown = useCallback(
    (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
      if (event.key === "ArrowRight") {
        selectTab(index + 1);
      } else if (event.key === "ArrowLeft") {
        selectTab(index - 1);
      } else if (event.key === "Home") {
        selectTab(0);
      } else if (event.key === "End") {
        selectTab(tabs.length - 1);
      } else {
        // 只吞掉导航键，其余交回浏览器——否则 Tab、Enter、空格都会失灵。
        return;
      }
      event.preventDefault();
    },
    [selectTab, tabs.length],
  );

  const tabProps = useCallback(
    (id: Id, index: number): TabProps => ({
      role: "tab",
      "aria-selected": id === activeId,
      // roving tabindex：整个 tablist 在 Tab 键序列里只占一个停靠点。
      tabIndex: id === activeId ? 0 : -1,
      ref: (element: HTMLButtonElement | null) => {
        refs.current[index] = element;
      },
      onKeyDown: (event: KeyboardEvent<HTMLButtonElement>) =>
        onTabKeyDown(event, index),
    }),
    [activeId, onTabKeyDown],
  );

  return { selectTab, onTabKeyDown, tabProps };
}
