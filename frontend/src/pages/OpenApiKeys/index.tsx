/**
 * Open API keys — the admin console for machine credentials.
 *
 * The mint response is the ONLY place the key plaintext and the webhook
 * signing secret ever exist: the backend stores a hash and derives the
 * secret, so this page's one-time reveal panel is the operator's single
 * chance to copy them. Losing them has one remedy — mint a new key — and
 * the panel says so instead of letting anyone find out later.
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Card, Input, Table, Tag } from "@arco-design/web-react";
import { createApiKey, listApiKeys, revokeApiKey } from "@/api/services";
import type { ApiKeyMintResponse, ApiKeyResource } from "@/types/api";
import { PanelState } from "@/components/PanelState";
import { getErrorMessage } from "@/utils/errors";

function SecretRow({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="ag-openapi-secret-row">
      <span className="ag-openapi-secret-label">{label}</span>
      <code className="ag-openapi-secret-value">{value}</code>
      <Button
        size="small"
        onClick={() => {
          void navigator.clipboard?.writeText(value).then(() => {
            setCopied(true);
            window.setTimeout(() => setCopied(false), 2_000);
          });
        }}
      >
        {copied ? "已复制" : "复制"}
      </Button>
    </div>
  );
}

/** 吊销走两步确认:第一次点击只把按钮换成确认态,离焦即复原。 */
function RevokeButton({
  keyName,
  pending,
  onRevoke,
}: {
  keyName: string;
  pending: boolean;
  onRevoke: () => void;
}) {
  const [arming, setArming] = useState(false);
  if (!arming) {
    return (
      <Button size="small" status="danger" onClick={() => setArming(true)}>
        吊销
      </Button>
    );
  }
  return (
    <Button
      size="small"
      status="danger"
      type="primary"
      loading={pending}
      onBlur={() => setArming(false)}
      onClick={onRevoke}
      aria-label={`确认吊销 ${keyName}`}
    >
      确认吊销（调用方将立即 401）
    </Button>
  );
}

export default function OpenApiKeysPage() {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [minted, setMinted] = useState<ApiKeyMintResponse | null>(null);
  const [operationError, setOperationError] = useState<string | null>(null);

  const query = useQuery({
    queryKey: ["integration-api-keys"],
    queryFn: listApiKeys,
    retry: false,
  });

  const mintMutation = useMutation({
    mutationFn: () => createApiKey(name.trim()),
    onSuccess: (response) => {
      setMinted(response);
      setName("");
      setOperationError(null);
      void queryClient.invalidateQueries({ queryKey: ["integration-api-keys"] });
    },
    onError: (error) => setOperationError(getErrorMessage(error)),
  });

  const revokeMutation = useMutation({
    mutationFn: (keyId: number) => revokeApiKey(keyId),
    onSuccess: () => {
      setOperationError(null);
      void queryClient.invalidateQueries({ queryKey: ["integration-api-keys"] });
    },
    onError: (error) => setOperationError(getErrorMessage(error)),
  });

  const items = query.data?.items ?? [];

  return (
    <div>
      <header className="ag-feature-header">
        <div>
          <span className="ag-eyebrow">OPEN API · 系统对接</span>
          <h1>开放接口密钥</h1>
          <p>
            外部系统凭 API key 上传录音、查询计算状态并接收签名回调。密钥只存
            哈希、验签密钥由主密钥派生——两者都只在签发那一刻显示一次。对接
            契约见仓库 docs/integration.md。
          </p>
        </div>
      </header>

      <div style={{ padding: 24 }}>
        {operationError && (
          <p className="ag-inline-error" role="alert">
            {operationError}
          </p>
        )}

        {minted && (
          <Card className="ag-openapi-reveal" style={{ marginBottom: 16 }}>
            <strong role="alert">
              密钥已签发——下面两个值只显示这一次,关掉就再也看不到,只能重新签发。
            </strong>
            <SecretRow label="API Key" value={minted.api_key} />
            <SecretRow label="回调验签密钥" value={minted.webhook_secret} />
            <Button size="small" onClick={() => setMinted(null)}>
              我已保存,关闭
            </Button>
          </Card>
        )}

        <Card style={{ marginBottom: 16 }}>
          <form
            style={{ display: "flex", gap: 12, alignItems: "center" }}
            onSubmit={(event) => {
              event.preventDefault();
              if (name.trim()) mintMutation.mutate();
            }}
          >
            <Input
              style={{ width: 260 }}
              value={name}
              onChange={setName}
              maxLength={64}
              placeholder="密钥用途,如 crm-sync"
              aria-label="新密钥名称"
            />
            <Button
              type="primary"
              htmlType="submit"
              loading={mintMutation.isPending}
              disabled={!name.trim()}
            >
              签发密钥
            </Button>
          </form>
        </Card>

        <PanelState
          pending={query.isPending}
          error={query.error}
          empty={items.length === 0}
          emptyTitle="还没有 API 密钥"
          emptyDescription="签发一把给外部系统,它就能上传录音并接收回调。"
          onRetry={() => void query.refetch()}
        >
          <Table
            rowKey="id"
            data={items}
            pagination={false}
            columns={[
              { title: "名称", dataIndex: "name" },
              {
                title: "状态",
                render: (_value: unknown, row: ApiKeyResource) =>
                  row.active ? (
                    <Tag color="green">生效中</Tag>
                  ) : (
                    <Tag color="gray">已吊销</Tag>
                  ),
              },
              {
                title: "签发于",
                render: (_value: unknown, row: ApiKeyResource) =>
                  new Date(row.created_at).toLocaleString("zh-CN"),
              },
              {
                title: "最近使用",
                render: (_value: unknown, row: ApiKeyResource) =>
                  row.last_used_at
                    ? new Date(row.last_used_at).toLocaleString("zh-CN")
                    : "从未",
              },
              {
                title: "操作",
                render: (_value: unknown, row: ApiKeyResource) =>
                  row.active ? (
                    <RevokeButton
                      keyName={row.name}
                      pending={
                        revokeMutation.isPending &&
                        revokeMutation.variables === row.id
                      }
                      onRevoke={() => revokeMutation.mutate(row.id)}
                    />
                  ) : null,
              },
            ]}
          />
        </PanelState>
      </div>
    </div>
  );
}
