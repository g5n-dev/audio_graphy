import { DEMO_SCHEMA_STATEMENTS } from "../db/schema";

export interface D1RunResult {
  success?: boolean;
  meta?: Record<string, unknown>;
}

export interface D1Result<T> {
  results?: T[];
  success?: boolean;
}

export interface D1PreparedStatement {
  bind(...values: unknown[]): D1PreparedStatement;
  first<T = Record<string, unknown>>(): Promise<T | null>;
  all<T = Record<string, unknown>>(): Promise<D1Result<T>>;
  run(): Promise<D1RunResult>;
}

export interface D1Database {
  prepare(query: string): D1PreparedStatement;
  batch(statements: D1PreparedStatement[]): Promise<D1RunResult[]>;
}

interface RecordRow {
  payload_json: string;
}

interface AuditRow {
  id: number;
  tenant_id: string;
  action: string;
  resource_type: string;
  resource_id: string;
  detail_json: string;
  created_at: string;
}

export async function ensureDemoSchema(db: D1Database): Promise<void> {
  await db.batch(
    DEMO_SCHEMA_STATEMENTS.map((statement) => db.prepare(statement)),
  );
}

export async function getRecord<T>(
  db: D1Database,
  namespace: string,
  recordId: string | number,
  tenantId = "tenant-demo",
): Promise<T | null> {
  const row = await db
    .prepare(
      `SELECT payload_json
       FROM demo_records
       WHERE namespace = ? AND record_id = ? AND tenant_id = ?
       LIMIT 1`,
    )
    .bind(namespace, String(recordId), tenantId)
    .first<RecordRow>();
  if (!row) return null;
  return JSON.parse(row.payload_json) as T;
}

export async function listRecords<T>(
  db: D1Database,
  namespace: string,
  tenantId = "tenant-demo",
): Promise<T[]> {
  const result = await db
    .prepare(
      `SELECT payload_json
       FROM demo_records
       WHERE namespace = ? AND tenant_id = ?
       ORDER BY updated_at DESC, record_id DESC`,
    )
    .bind(namespace, tenantId)
    .all<RecordRow>();
  return (result.results ?? []).map(
    (row) => JSON.parse(row.payload_json) as T,
  );
}

export async function nextRecordId(
  db: D1Database,
  namespace: string,
  initialValue = 1000,
): Promise<number> {
  const row = await db
    .prepare(
      `INSERT INTO demo_sequences (namespace, value)
       VALUES (?, ?)
       ON CONFLICT(namespace) DO UPDATE SET value = value + 1
       RETURNING value`,
    )
    .bind(namespace, initialValue)
    .first<{ value: number }>();
  if (!row) {
    throw new Error("D1 sequence did not return an identifier");
  }
  return Number(row.value);
}

export async function putRecord(
  db: D1Database,
  namespace: string,
  recordId: string | number,
  payload: unknown,
  tenantId = "tenant-demo",
): Promise<void> {
  const now = new Date().toISOString();
  await db
    .prepare(
      `INSERT INTO demo_records (
         namespace, record_id, tenant_id, payload_json, created_at, updated_at
       ) VALUES (?, ?, ?, ?, ?, ?)
       ON CONFLICT(namespace, record_id) DO UPDATE SET
         payload_json = excluded.payload_json,
         tenant_id = excluded.tenant_id,
         updated_at = excluded.updated_at`,
    )
    .bind(
      namespace,
      String(recordId),
      tenantId,
      JSON.stringify(payload),
      now,
      now,
    )
    .run();
}

export async function putRecordWithAudit(
  db: D1Database,
  namespace: string,
  recordId: string | number,
  payload: unknown,
  action: string,
  detail: Record<string, unknown> = {},
  tenantId = "tenant-demo",
): Promise<void> {
  const now = new Date().toISOString();
  await db.batch([
    db
      .prepare(
        `INSERT INTO demo_records (
           namespace, record_id, tenant_id, payload_json, created_at, updated_at
         ) VALUES (?, ?, ?, ?, ?, ?)
         ON CONFLICT(namespace, record_id) DO UPDATE SET
           payload_json = excluded.payload_json,
           tenant_id = excluded.tenant_id,
           updated_at = excluded.updated_at`,
      )
      .bind(
        namespace,
        String(recordId),
        tenantId,
        JSON.stringify(payload),
        now,
        now,
      ),
    db
      .prepare(
        `INSERT INTO demo_audit_events (
           tenant_id, action, resource_type, resource_id, detail_json, created_at
         ) VALUES (?, ?, ?, ?, ?, ?)`,
      )
      .bind(
        tenantId,
        action,
        namespace,
        String(recordId),
        JSON.stringify(detail),
        now,
      ),
  ]);
}

export async function putRecordsWithAudit(
  db: D1Database,
  records: Array<{
    namespace: string;
    recordId: string | number;
    payload: unknown;
  }>,
  action: string,
  detail: Record<string, unknown> = {},
  tenantId = "tenant-demo",
): Promise<void> {
  const now = new Date().toISOString();
  await db.batch([
    ...records.map(({ namespace, recordId, payload }) =>
      db
        .prepare(
          `INSERT INTO demo_records (
             namespace, record_id, tenant_id, payload_json, created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(namespace, record_id) DO UPDATE SET
             payload_json = excluded.payload_json,
             tenant_id = excluded.tenant_id,
             updated_at = excluded.updated_at`,
        )
        .bind(
          namespace,
          String(recordId),
          tenantId,
          JSON.stringify(payload),
          now,
          now,
        ),
    ),
    db
      .prepare(
        `INSERT INTO demo_audit_events (
           tenant_id, action, resource_type, resource_id, detail_json, created_at
         ) VALUES (?, ?, ?, ?, ?, ?)`,
      )
      .bind(
        tenantId,
        action,
        "transaction",
        records.map((record) => `${record.namespace}:${record.recordId}`).join(","),
        JSON.stringify(detail),
        now,
      ),
  ]);
}

export async function listAuditEvents(
  db: D1Database,
  tenantId = "tenant-demo",
  limit = 100,
): Promise<Array<Record<string, unknown>>> {
  const result = await db
    .prepare(
      `SELECT id, tenant_id, action, resource_type, resource_id,
              detail_json, created_at
       FROM demo_audit_events
       WHERE tenant_id = ?
       ORDER BY created_at DESC, id DESC
       LIMIT ?`,
    )
    .bind(tenantId, Math.min(Math.max(limit, 1), 500))
    .all<AuditRow>();
  return (result.results ?? []).map((row) => ({
    id: row.id,
    tenant_id: row.tenant_id,
    action: row.action,
    actor_user_id: 1,
    resource_type: row.resource_type,
    resource_id: row.resource_id,
    detail: JSON.parse(row.detail_json) as Record<string, unknown>,
    created_at: row.created_at,
  }));
}
