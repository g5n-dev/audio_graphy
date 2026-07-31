/**
 * Durable state used by the Sites demo worker.
 *
 * The production application persists these resources in MySQL. The hosted
 * demonstration uses D1 so refreshes and multi-tab sessions observe the same
 * workflow state instead of accepting writes into process memory.
 */
export const CREATE_DEMO_RECORDS_TABLE = `
CREATE TABLE IF NOT EXISTS demo_records (
  namespace TEXT NOT NULL,
  record_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (namespace, record_id)
)`;

export const CREATE_DEMO_RECORDS_INDEX = `
CREATE INDEX IF NOT EXISTS idx_demo_records_tenant_namespace
ON demo_records (tenant_id, namespace, updated_at DESC)`;

export const CREATE_DEMO_SEQUENCES_TABLE = `
CREATE TABLE IF NOT EXISTS demo_sequences (
  namespace TEXT PRIMARY KEY,
  value INTEGER NOT NULL
)`;

export const CREATE_DEMO_AUDIT_TABLE = `
CREATE TABLE IF NOT EXISTS demo_audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL,
  action TEXT NOT NULL,
  resource_type TEXT NOT NULL,
  resource_id TEXT NOT NULL,
  detail_json TEXT NOT NULL,
  created_at TEXT NOT NULL
)`;

export const CREATE_DEMO_AUDIT_INDEX = `
CREATE INDEX IF NOT EXISTS idx_demo_audit_tenant_created
ON demo_audit_events (tenant_id, created_at DESC, id DESC)`;

export const DEMO_SCHEMA_STATEMENTS = [
  CREATE_DEMO_RECORDS_TABLE,
  CREATE_DEMO_RECORDS_INDEX,
  CREATE_DEMO_SEQUENCES_TABLE,
  CREATE_DEMO_AUDIT_TABLE,
  CREATE_DEMO_AUDIT_INDEX,
] as const;
