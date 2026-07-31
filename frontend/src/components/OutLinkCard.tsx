/**
 * OutLinkCard — a lightweight entry card that renders an icon, title,
 * description, and a React Router `<Link>` button.
 *
 * Used by RecordingDetail's four export tabs ("图谱关系" / "说话人" /
 * "接待" / "时间演化") to give the inspector a one-click jump into the
 * related view without embedding the full page.
 */

import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { Button, Card, Typography } from "@arco-design/web-react";
import "./OutLinkCard.css";

const { Text } = Typography;

export interface OutLinkCardProps {
  /** Arco Design icon component, e.g. `<IconBranch />`. */
  icon: ReactNode;
  /** Card title, e.g. "图谱关系". */
  title: string;
  /** Short description shown below the title. */
  description: string;
  /** React Router target path. */
  to: string;
  /** Optional button label (defaults to "查看详情 →"). */
  buttonLabel?: string;
}

export function OutLinkCard({
  icon,
  title,
  description,
  to,
  buttonLabel = "查看详情 →",
}: OutLinkCardProps): JSX.Element {
  return (
    <Card className="ag-outlink-card">
      <div className="ag-outlink-card__icon" aria-hidden="true">
        {icon}
      </div>
      <h4 className="ag-outlink-card__title">{title}</h4>
      <Text className="ag-outlink-card__desc">{description}</Text>
      <Link to={to} className="ag-outlink-card__link">
        <Button type="text" className="ag-outlink-card__button">
          {buttonLabel}
        </Button>
      </Link>
    </Card>
  );
}
