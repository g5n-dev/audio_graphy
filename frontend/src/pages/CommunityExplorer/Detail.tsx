/**
 * Community detail sub-page — rendered inside CommunityExplorer when
 * the user drills into a single community. Shows the full summary text
 * plus the L8 member node list (architecture §25.15).
 */

import { Card, Descriptions, Tag, Typography } from "@arco-design/web-react";
import type { CommunityHit } from "@/api/advancedGraph";

const { Title } = Typography;

export default function CommunityDetail({
  community,
}: {
  community: CommunityHit;
}) {
  return (
    <Card style={{ marginTop: 16 }}>
      <Title heading={5}>
        Community #{community.community_id}
        <Tag color="arc-blue" style={{ marginLeft: 8 }}>
          level {community.level}
        </Tag>
      </Title>
      <Descriptions
        column={2}
        data={[
          { label: "Title", value: community.title },
          { label: "Member count", value: community.member_count },
          { label: "Score", value: community.score.toFixed(4) },
        ]}
        style={{ marginBottom: 16 }}
      />
      <Title heading={6}>Summary</Title>
      <p style={{ whiteSpace: "pre-wrap" }}>{community.summary}</p>
    </Card>
  );
}
