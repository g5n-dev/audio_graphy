# 外部系统对接指南(Open API)

外部系统与 AudioGraphy 的全部交互就三个动作:把录音**上传**进来、**查询**计算
状态、算完之后接收**回调推送**。本文是对接方要读的唯一文档。

设计纪律先说明白:这个通道只传 **id、状态与错误码**,永远不携带转写文本或音频
——AudioGraphy 做 PIPL 静态加密,开放接口是事件通道,不是数据出口。要内容,
拿自己的凭证调用带鉴权的详情 API。

## 1. 凭证

管理员在后台签发 API key(或直接调用):

```bash
curl -X POST http://<host>:8000/api/v1/integration/api-keys \
  -H "Authorization: Bearer <管理员 JWT>" \
  -H "Content-Type: application/json" \
  -d '{"name": "crm-sync"}'
```

响应(**两个密钥只在这里出现一次**,库里只存哈希,验签密钥由主密钥派生):

```json
{
  "key": {"id": 3, "name": "crm-sync", "active": true},
  "api_key": "agk_9f2c…",
  "webhook_secret": "6b1e…(64 hex)"
}
```

丢了没有找回,只有重发一把。吊销:`POST /api/v1/integration/api-keys/{id}/revoke`。

之后所有开放接口请求带:

```
Authorization: Bearer agk_9f2c…        # 或 X-API-Key: agk_9f2c…
```

## 2. 上传录音

```bash
curl -X POST http://<host>:8000/api/v1/open/recordings \
  -H "Authorization: Bearer agk_9f2c…" \
  -F "audio=@/path/to/call.wav" \
  -F "external_ref=crm-order-20260805-001" \
  -F "store_id=store-9" \
  -F "agent_name=销售小林" \
  -F "recorded_at=2026-08-05T10:30:00+08:00" \
  -F "callback_url=https://your-system.example.com/audiography/hook"
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `audio` | ✓ | 音频文件,ffmpeg 认识的容器都行;上限默认 512 MiB |
| `external_ref` | ✓ | **你们系统里的 id**,也是幂等键:同 ref 重发返回已存在的记录(`replayed: true`),绝不重复入库 |
| `store_id` | ✓ | 门店标识 |
| `agent_name` / `recorded_at` | | 可选元数据 |
| `callback_url` | | 算完后推送的目标;不填就只能轮询状态 |

响应 `201`:

```json
{"external_ref": "crm-order-…", "recording_id": 128, "status": "queued",
 "callback_registered": true, "replayed": false}
```

`callback_url` 默认拒绝私网/环回/云 metadata 地址(SSRF 面)。内网私有化部署
的下游本来就在私网——在 `.env` 打开 `INTEGRATION_ALLOW_PRIVATE_CALLBACK_URLS=true`,
前提是出网侧有边界控制。

## 3. 计算状态查询

按你们自己的 ref 查,不需要记我们的 id:

```bash
curl http://<host>:8000/api/v1/open/recordings/crm-order-20260805-001/status \
  -H "Authorization: Bearer agk_9f2c…"
```

```json
{
  "external_ref": "crm-order-20260805-001",
  "recording_id": 128,
  "status": "processing",
  "pipeline_state": "asr",
  "indexed_at": null,
  "terminal": false,
  "run": {"generation": 1, "state": "running", "attempt_count": 1,
          "error_code": null, "error_message": null, "finished_at": null}
}
```

`terminal: true` 时 `status` 落在 `indexed` / `ready_no_speech` / `failed` 之一;
失败时 `run.error_code` / `run.error_message` 说明原因。记录被隐私擦除后返回
`410 RECORDING_ERASED`。

## 4. 结果回调

计算到达终态时,POST 到你的 `callback_url`:

```json
{
  "event_id": "0b7c1d1e-…",
  "event_type": "recording.indexed",
  "external_ref": "crm-order-20260805-001",
  "recording_id": 128,
  "status": "indexed",
  "occurred_at": "2026-08-05T03:12:44+00:00"
}
```

`recording.failed` 额外带 `"error": {"code", "message"}`。

**投递语义**:at-least-once。`event_id` 幂等去重;`status` 字段为准。回调意图
与终态状态**同一事务**落库,宕机不会丢;投递失败按 1m/5m/30m/2h/6h 退避重试
五次,之后进 dead_letter(可在库表 `integration_callbacks` 里看到,不会静默
消失)。同一处理代次只通知一次;操作员强制重跑(新代次)会再次通知。

**验签**(强烈建议):每个请求带

```
X-AudioGraphy-Event: recording.indexed
X-AudioGraphy-Delivery: <event_id>
X-AudioGraphy-Signature: t=1754363564,v1=<hex>
```

`v1 = HMAC-SHA256(webhook_secret, "<t>." + 原始请求体字节)`。校验示例(Python):

```python
import hashlib, hmac, time

def verify(signature_header: str, body: bytes, webhook_secret: str,
           tolerance_sec: int = 300) -> bool:
    fields = dict(part.split("=", 1) for part in signature_header.split(","))
    timestamp, received = fields["t"], fields["v1"]
    if abs(time.time() - int(timestamp)) > tolerance_sec:
        return False          # 拒绝重放
    expected = hmac.new(webhook_secret.encode(),
                        f"{timestamp}.".encode() + body,
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)
```

回应 2xx 即视为送达;其他状态码与超时(10s)都会重试,处理逻辑请自行幂等。

## 5. 错误码速查

| HTTP | code | 含义 |
|---|---|---|
| 401 | `API_KEY_REQUIRED` / `API_KEY_INVALID` | 缺失 / 无效或已吊销 |
| 404 | `EXTERNAL_REF_NOT_FOUND` | 该租户下无此 ref |
| 410 | `RECORDING_ERASED` | 记录已被隐私擦除 |
| 413 | `AUDIO_TOO_LARGE` | 超过体积上限 |
| 422 | `AUDIO_EMPTY` / `CALLBACK_URL_REJECTED` | 空文件 / 回调地址被 SSRF 校验拒绝 |
| 409 | `API_KEY_NAME_TAKEN` | 签发时名称重复(管理端) |

注意:主密钥轮换会让所有 `webhook_secret` 一起轮换(它们由主密钥派生)——
轮换后请重新从签发接口获取。
