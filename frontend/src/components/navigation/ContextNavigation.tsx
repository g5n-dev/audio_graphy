import {
  IconArrowLeft,
  IconBranch,
  IconFileAudio,
  IconStorage,
} from "@arco-design/web-react/icon";
import { Link, NavLink } from "react-router-dom";
import "./ContextNavigation.css";

function tabClassName({ isActive }: { isActive: boolean }): string {
  return isActive ? "ag-context-tab is-active" : "ag-context-tab";
}

export function ReceptionContextTabs({
  receptionId,
}: {
  receptionId: string;
}) {
  const encodedId = encodeURIComponent(receptionId);
  const receptionBase = `/receptions/${encodedId}`;

  return (
    <div className="ag-context-navigation ag-context-navigation--reception">
      <div className="ag-context-navigation__identity">
        <Link to="/receptions">
          <IconArrowLeft />
          接待中心
        </Link>
        <span>接待 #{receptionId}</span>
      </div>
      <nav
        className="ag-context-navigation__tabs"
        aria-label="接待详情视图"
      >
        <NavLink
          className={tabClassName}
          to={`${receptionBase}/workspace`}
        >
          <IconFileAudio />
          调听与切分
        </NavLink>
        <NavLink
          className={tabClassName}
          to={`${receptionBase}/graph`}
        >
          <IconBranch />
          关系与溯源
        </NavLink>
      </nav>
      <Link
        className="ag-context-navigation__related"
        to="/reception-flow"
      >
        查看跨接待状态路径
      </Link>
    </div>
  );
}

export function InsightContextTabs() {
  return (
    <div className="ag-context-navigation ag-context-navigation--insights">
      <div className="ag-context-navigation__identity">
        <span>对话洞察</span>
        <strong>跨接待分析</strong>
      </div>
      <nav
        className="ag-context-navigation__tabs"
        aria-label="对话洞察视图"
      >
        <NavLink
          className={tabClassName}
          to="/reception-flow"
        >
          <IconBranch />
          状态路径
        </NavLink>
        <NavLink
          className={tabClassName}
          to="/tag-insights"
        >
          <IconStorage />
          标签洞察
        </NavLink>
      </nav>
      <Link className="ag-context-navigation__related" to="/receptions">
        返回接待中心
      </Link>
    </div>
  );
}
