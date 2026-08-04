import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { useTabList } from "@/components/governance/useTabList";
import {
  getPromptArtifact,
  getPromptLabReadiness,
  listPromptArtifacts,
} from "@/api/services";
import { useAuthStore } from "@/stores/auth";
import type { PromptArtifact, PromptArtifactSummary } from "@/types/api";

import { CompilePanel } from "./CompilePanel";
import { DiffPanel } from "./DiffPanel";
import { GradientPanel } from "./GradientPanel";
import { ReadinessPanel } from "./ReadinessPanel";
import { ReplayPanel } from "./ReplayPanel";
import "../TagGovernance/tagGovernance.css";
import "./promptLab.css";

const TABS = [
  { id: "readiness", label: "数据就绪", description: "门槛、覆盖与冷启动引导" },
  { id: "compile", label: "编译运行", description: "编译器、预算与产物列表" },
  { id: "diff", label: "Prompt 差异", description: "候选与基线逐行对照" },
  { id: "gradients", label: "梯度与补丁", description: "逐条接受或拒绝修改建议" },
  { id: "replay", label: "回放对比", description: "按标签的指标变化与门禁" },
] as const;

type TabId = (typeof TABS)[number]["id"];

const TAB_IDS = new Set<string>(TABS.map((tab) => tab.id));

/** 严格解析：拒绝负数、零、前导零、超大值，避免把垃圾参数发给后端。 */
function parseArtifactId(raw: string | null): number | null {
  if (!raw || !/^[1-9]\d{0,9}$/.test(raw)) return null;
  const id = Number(raw);
  return Number.isSafeInteger(id) && id <= 2_147_483_647 ? id : null;
}

export default function PromptLabPage() {
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const isAdmin = useAuthStore((state) => state.user?.role === "admin");

  const [activeTab, setActiveTab] = useState<TabId>(() => {
    const requested = searchParams.get("tab");
    return requested && TAB_IDS.has(requested) ? (requested as TabId) : "readiness";
  });
  const [artifactId, setArtifactId] = useState<number | null>(() =>
    parseArtifactId(searchParams.get("artifact")),
  );

  useEffect(() => {
    const requested = searchParams.get("tab");
    if (requested && TAB_IDS.has(requested)) setActiveTab(requested as TabId);
    setArtifactId(parseArtifactId(searchParams.get("artifact")));
  }, [searchParams]);

  const writeParams = useCallback(
    (next: { tab?: TabId; artifact?: number | null }) => {
      const params = new URLSearchParams(searchParams);
      if (next.tab) params.set("tab", next.tab);
      if (next.artifact === null) params.delete("artifact");
      else if (next.artifact !== undefined) params.set("artifact", String(next.artifact));
      // replace 而不是 push：切 Tab 不该污染浏览器后退栈。
      setSearchParams(params, { replace: true });
    },
    [searchParams, setSearchParams],
  );

  const selectTabId = useCallback(
    (id: TabId) => {
      setActiveTab(id);
      writeParams({ tab: id });
    },
    [writeParams],
  );

  const { tabProps } = useTabList<TabId>({
    tabs: TABS,
    activeId: activeTab,
    onSelect: selectTabId,
  });

  const readiness = useQuery({
    queryKey: ["prompt-lab", "readiness"],
    queryFn: getPromptLabReadiness,
    enabled: activeTab === "readiness" || activeTab === "compile",
    retry: false,
  });

  // 产物元数据（状态、候选版本 id）从列表缓存里取，Tab 3/4/5 不再各发一次详情请求。
  const artifacts = useQuery({
    queryKey: ["prompt-lab", "artifacts", "all"],
    queryFn: () => listPromptArtifacts({ limit: 50 }),
    retry: false,
  });

  const cachedArtifact: PromptArtifactSummary | undefined = useMemo(
    () => artifacts.data?.items.find((item) => item.id === artifactId),
    [artifacts.data, artifactId],
  );

  // 列表只取最新 50 条，超过就找不到——而 Diff 和梯度两个 Tab 拿的是裸 artifactId，
  // 照样工作。只有复盘 Tab 依赖这个对象，于是它会对着一个明明选中的产物说
  // 「先在「编译运行」里选择一个产物」。深链和按状态筛选都能走到这里。
  const fallbackArtifact = useQuery({
    queryKey: ["prompt-lab", "artifact", artifactId],
    queryFn: () => getPromptArtifact(artifactId!),
    enabled: artifactId !== null && artifacts.isSuccess && cachedArtifact === undefined,
    retry: false,
  });

  const selectedArtifact: PromptArtifactSummary | undefined =
    cachedArtifact ?? fallbackArtifact.data;

  const selectArtifact = useCallback(
    (id: number) => {
      setArtifactId(id);
      writeParams({ tab: "diff", artifact: id });
      setActiveTab("diff");
    },
    [writeParams],
  );

  const onArtifactCreated = useCallback(
    (artifact: PromptArtifact) => {
      setArtifactId(artifact.id);
      writeParams({ artifact: artifact.id });
      void queryClient.invalidateQueries({ queryKey: ["prompt-lab", "artifacts"] });
    },
    [queryClient, writeParams],
  );

  return (
    <main className="ag-governance-page ag-prompt-lab-page">
      <header className="ag-governance-hero">
        <div>
          <span className="ag-eyebrow">PROMPT COMPILATION · HUMAN IN THE LOOP</span>
          <h1>提示词实验室</h1>
          <p>
            把打标提示词从人工手艺变成可度量、可回放、可审阅、可回滚的资产。每次编译
            只产出待复核的候选，是否上线仍由既有的评估与灰度门禁决定。
          </p>
        </div>
        <div className="ag-governance-hero__actions">
          <Link to="/tag-governance">返回标签治理</Link>
          <Link to="/tag-review" className="is-secondary">
            进入人工复核
          </Link>
        </div>
      </header>

      <nav className="ag-governance-tabs" role="tablist" aria-label="提示词实验室视图">
        {TABS.map((tab, index) => (
          <button
            type="button"
            key={tab.id}
            id={`prompt-lab-tab-${tab.id}`}
            aria-controls={`prompt-lab-panel-${tab.id}`}
            className={activeTab === tab.id ? "is-active" : undefined}
            onClick={() => selectTabId(tab.id)}
            {...tabProps(tab.id, index)}
          >
            <strong>{tab.label}</strong>
            <span>{tab.description}</span>
          </button>
        ))}
      </nav>

      <section
        className="ag-governance-panel"
        role="tabpanel"
        id={`prompt-lab-panel-${activeTab}`}
        aria-labelledby={`prompt-lab-tab-${activeTab}`}
        tabIndex={0}
      >
        {activeTab === "readiness" && (
          <ReadinessPanel
            data={readiness.data}
            pending={readiness.isPending}
            error={readiness.error}
            onRetry={() => void readiness.refetch()}
          />
        )}
        {activeTab === "compile" && (
          <CompilePanel
            isAdmin={isAdmin}
            readiness={readiness.data}
            selectedArtifactId={artifactId}
            onSelectArtifact={selectArtifact}
          />
        )}
        {activeTab === "diff" && (
          <DiffPanel
            artifactId={artifactId}
            isAdmin={isAdmin}
            onArtifactCreated={onArtifactCreated}
            onClearArtifact={() => {
              setArtifactId(null);
              writeParams({ artifact: null });
            }}
            onGoToCompile={() => selectTabId("compile")}
          />
        )}
        {activeTab === "gradients" && (
          <GradientPanel
            artifactId={artifactId}
            isAdmin={isAdmin}
            onArtifactCreated={onArtifactCreated}
            onGoToCompile={() => selectTabId("compile")}
          />
        )}
        {activeTab === "replay" && (
          <ReplayPanel
            artifact={selectedArtifact}
            onGoToCompile={() => selectTabId("compile")}
          />
        )}
      </section>
    </main>
  );
}
