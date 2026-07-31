/**
 * Query page — natural language search with dual-channel retrieval.
 *
 * Shows: query input, LLM answer, citations list, retrieval stats.
 */

import { useState } from "react";
import { Card, Input, Button, Typography, List, Tag, Spin, Statistic, Grid } from "@arco-design/web-react";
import { query as queryApi } from "@/api/services";
import type { EdgeConfidence, QueryResponse } from "@/types/api";
import { getErrorMessage } from "@/utils/errors";

const { Text, Paragraph } = Typography;
const { Row, Col } = Grid;

/** Citation confidence is a provenance grade, not a probability. */
const CONFIDENCE_LABEL: Record<EdgeConfidence, { text: string; color: string }> = {
  EXTRACTED: { text: "原文抽取", color: "green" },
  INFERRED: { text: "推断", color: "blue" },
  AMBIGUOUS: { text: "存疑", color: "orange" },
  DEPRECATED: { text: "已降级", color: "gray" },
};

export default function QueryPage() {
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<QueryResponse | null>(null);
  const [error, setError] = useState<string>("");

  const handleSearch = async () => {
    if (!input.trim()) return;
    setLoading(true);
    setError("");
    setResult(null);
    try {
      const resp = await queryApi(input.trim());
      setResult(resp);
    } catch (err: unknown) {
      setError(getErrorMessage(err, "查询失败"));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div>
      <header className="ag-feature-header">
        <div>
          <span className="ag-eyebrow">NATURAL LANGUAGE QUERY · 自然语言检索</span>
          <h1>智能问答</h1>
          <p>以自然语言提问，借助向量与图谱双通道检索，返回带引用的答案。</p>
        </div>
      </header>

      <div style={{ padding: 24 }}>
      <Card style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", gap: 12 }}>
          <Input
            placeholder="输入自然语言查询, 例如: '本月哪些录音提到了新能源产品?'"
            value={input}
            onChange={setInput}
            onPressEnter={handleSearch}
            size="large"
          />
          <Button
            type="primary"
            size="large"
            loading={loading}
            onClick={handleSearch}
          >
            搜索
          </Button>
        </div>
      </Card>

      {error && (
        <Card style={{ marginBottom: 16, borderColor: "#f53f3f" }}>
          <Text style={{ color: "#f53f3f" }}>{error}</Text>
        </Card>
      )}

      {loading && (
        <div style={{ textAlign: "center", padding: 48 }}>
          <Spin size={40} tip="检索中..." />
        </div>
      )}

      {result && (
        <>
          <Card title="回答" style={{ marginBottom: 16 }}>
            <Paragraph>{result.answer || "(无回答)"}</Paragraph>
            {result.retrieval_stats && (
              <Row gutter={16}>
                <Col span={6}>
                  <Statistic title="向量检索" value={result.retrieval_stats.naive_hits} />
                </Col>
                <Col span={6}>
                  <Statistic title="图谱检索" value={result.retrieval_stats.graph_hits} />
                </Col>
                <Col span={6}>
                  <Statistic title="时间过滤" value={result.retrieval_stats.filtered_by_time} />
                </Col>
                <Col span={6}>
                  <Statistic title="LLM过滤" value={result.retrieval_stats.filtered_by_judge} />
                </Col>
              </Row>
            )}
          </Card>

          {result.citations.length > 0 && (
            <Card title={`引用 (${result.citations.length})`}>
              <List>
                {result.citations.map((cite, i) => (
                  <List.Item key={i}>
                    <div style={{ width: "100%" }}>
                      <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 4 }}>
                        <Tag color="blue">{cite.entity}</Tag>
                        <Text style={{ fontSize: 14, color: "#86909c" }}>
                          录音 #{cite.recording_id}
                        </Text>
                        <Tag color={CONFIDENCE_LABEL[cite.confidence]?.color ?? "gray"}>
                          {CONFIDENCE_LABEL[cite.confidence]?.text ?? cite.confidence}
                        </Tag>
                      </div>
                      {cite.transcript_snippet && (
                        <Paragraph style={{ margin: 0, color: "#4e5969", fontSize: 14 }}>
                          "{cite.transcript_snippet}"
                        </Paragraph>
                      )}
                    </div>
                  </List.Item>
                ))}
              </List>
            </Card>
          )}
        </>
      )}
      </div>
    </div>
  );
}
