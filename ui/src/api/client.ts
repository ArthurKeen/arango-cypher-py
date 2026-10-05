export interface ConnectRequest {
  url: string;
  database: string;
  username: string;
  password: string;
  // Optional tenant binding. When set, the backend binds the session
  // to this tenant so Layers 4–6 inject `FILTER <doc>.<tenantField> ==
  // @tenantId` and refuse cross-tenant reads. Required to query
  // tenant-scoped collections; leave empty for single-tenant /
  // reference-only databases. `tenantKey` defaults to `tenantId` when
  // omitted (denormalised-tenant schemas with no `Tenant` collection
  // use the same value for both).
  tenantId?: string;
  tenantKey?: string;
}

export interface ConnectResponse {
  token: string;
  databases: string[];
  // The database the session opened. /connect/platform chooses it when the
  // request names none (the mount database may not be one the user can open).
  database?: string | null;
}

// GET /connect/platform — whether this page came through the platform
// gateway with the user's platform login, so the Workbench can open a
// session without asking for credentials.
export interface PlatformStatus {
  available: boolean;
  // The database a platform session opens by default: the instance's mount.
  database: string;
  reason: string | null;
}

export interface ConnectDefaults {
  url: string;
  database: string;
  username: string;
  password?: string;
}

export interface TranslateRequest {
  cypher: string;
  mapping: Record<string, unknown>;
  params?: Record<string, unknown>;
  extensions_enabled?: boolean;
}

export interface TranslateResponse {
  aql: string;
  bind_vars: Record<string, unknown>;
  warnings: Array<{ message: string }>;
  elapsed_ms?: number;
}

export interface ExecuteResponse {
  results: unknown[];
  aql: string;
  bind_vars: Record<string, unknown>;
  warnings: Array<{ message: string }>;
  exec_ms?: number;
  translate_ms?: number;
}

export interface ExplainResponse {
  aql: string;
  bind_vars: Record<string, unknown>;
  plan: unknown;
  translate_ms?: number;
}

export interface ProfileResponse {
  aql: string;
  bind_vars: Record<string, unknown>;
  results: unknown[];
  statistics: Record<string, unknown>;
  profile: unknown;
  translate_ms?: number;
}

function authHeaders(token: string): Record<string, string> {
  // Use a custom header — the ArangoDB platform proxy strips Authorization:Bearer
  // (it uses that header for its own JWT auth) before forwarding to the container.
  return { "X-Arango-Session": token };
}

// The API base for a page served at `pathname`. Root-relative
// fetch("/connect") would hit the domain root — on the platform that is
// ArangoDB itself, which answers `unknown path '/connect'` — so every call is
// prefixed with the directory the SPA was served from. The SPA is mounted at
// the service root (the platform app launcher's target), or one level down at
// …/frontend/ (AMP) or …/ui/ (legacy / local-dev), where the API is the parent:
//   /_service/uds/_db/<db>/<instance>/          → /_service/uds/_db/<db>/<instance>
//   /_service/uds/_db/<db>/<instance>/frontend/ → /_service/uds/_db/<db>/<instance>
//   /frontend/ , /ui/ , / (local dev)           → ""
// Only a trailing `…html` segment is treated as a file: the launcher may open
// the mount without its trailing slash, and an instance name is a directory.
// Whole segments are compared, so an instance named `ui-demo` is not a mount.
export function apiBaseFor(pathname: string): string {
  const segments = pathname.split("/");
  if (/\.html?$/i.test(segments[segments.length - 1] ?? "")) segments.pop();
  while (segments.length > 0 && segments[segments.length - 1] === "") segments.pop();
  const last = segments[segments.length - 1];
  if (last === "frontend" || last === "ui") segments.pop();
  return segments.join("/");
}

function apiBase(): string {
  return apiBaseFor(window.location.pathname);
}

// Shown in the UI whenever the backend returns 401. The raw
// `{"message":"Unauthorized"}` payload from ArangoDB / the platform
// proxy is technically correct but reads as a cryptic error; the
// session has simply expired (tokens are short-lived) and the user
// needs to sign in again.
export const AUTH_EXPIRED_MESSAGE =
  "Your session has expired. Please re-authenticate to the database.";

async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const { headers: extraHeaders, ...rest } = options;
  const res = await fetch(apiBase() + path, {
    ...rest,
    headers: {
      "Content-Type": "application/json",
      ...(extraHeaders as Record<string, string>),
    },
  });
  if (!res.ok) {
    if (res.status === 401) {
      // Drain the body so the connection is released, but don't
      // bother surfacing its contents — the generic re-auth prompt
      // is more useful than e.g. "Unauthorized" or "token expired".
      await res.text().catch(() => "");
      throw new ApiError(401, AUTH_EXPIRED_MESSAGE);
    }
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new ApiError(res.status, body.detail ?? body);
  }
  return res.json();
}

function formatDetail(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    const obj = detail as Record<string, unknown>;
    // Prefer the human-readable `message` when present. Structured
    // refusals (tenant scope, Layer-4 rewrite) carry a machine `error`
    // *kind* plus a human `message`; transpiler 422s carry the message
    // directly in `error`. Checking `message` first surfaces the useful
    // text in both shapes instead of e.g. "tenant_scope_violation".
    if (typeof obj.message === "string") return obj.message;
    if (typeof obj.error === "string") return obj.error;
    if (typeof obj.detail === "string") return obj.detail;
  }
  return JSON.stringify(detail);
}

// Pull a stable error `code` (e.g. UNSUPPORTED, NOT_IMPLEMENTED,
// tenant_scope_violation) out of a structured error body so callers can
// branch on it — most importantly to decide whether to offer the
// "Generate AQL with AI" fallback for a non-transpilable Cypher query.
function extractCode(detail: unknown): string | undefined {
  if (detail && typeof detail === "object") {
    const obj = detail as Record<string, unknown>;
    if (typeof obj.code === "string") return obj.code;
  }
  return undefined;
}

export class ApiError extends Error {
  status: number;
  detail: unknown;
  code?: string;

  constructor(status: number, detail: unknown) {
    super(formatDetail(detail));
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.code = extractCode(detail);
  }
}

// Codes the deterministic Cypher->AQL transpiler raises when a query uses a
// feature outside its supported subset. These are recoverable via the
// LLM-backed Cypher->AQL fallback (unlike syntax errors or tenant refusals).
const TRANSPILE_FALLBACK_CODES = new Set(["UNSUPPORTED", "NOT_IMPLEMENTED"]);

export function isTranspileFallbackError(err: unknown): boolean {
  return (
    err instanceof ApiError &&
    err.status === 422 &&
    !!err.code &&
    TRANSPILE_FALLBACK_CODES.has(err.code)
  );
}

export function isAuthError(err: unknown): boolean {
  return err instanceof ApiError && err.status === 401;
}

export async function exportMappingOwl(
  mapping: unknown,
): Promise<{ turtle: string }> {
  return request("/mapping/export-owl", {
    method: "POST",
    body: JSON.stringify({ mapping }),
  });
}

export interface ImportedOwlMapping {
  conceptualSchema: unknown;
  physicalMapping: unknown;
  metadata: unknown;
}

export async function importMappingOwl(turtle: string): Promise<ImportedOwlMapping> {
  return request("/mapping/import-owl", {
    method: "POST",
    body: JSON.stringify({ turtle }),
  });
}

export async function getConnectDefaults(): Promise<ConnectDefaults> {
  return request("/connect/defaults");
}

export async function connect(req: ConnectRequest): Promise<ConnectResponse> {
  return request("/connect", {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export async function getPlatformStatus(): Promise<PlatformStatus> {
  return request("/connect/platform");
}

// Open a session as the signed-in platform user. No credentials: the
// gateway forwards the platform login with the request.
export async function connectPlatform(database?: string): Promise<ConnectResponse> {
  return request("/connect/platform", {
    method: "POST",
    body: JSON.stringify(database ? { database } : {}),
  });
}

export async function disconnect(token: string): Promise<void> {
  await request("/disconnect", {
    method: "POST",
    headers: authHeaders(token),
  });
}

export async function translateCypher(
  req: TranslateRequest,
): Promise<TranslateResponse> {
  return request("/translate", {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export async function executeCypher(
  req: TranslateRequest,
  token: string,
): Promise<ExecuteResponse> {
  return request("/execute", {
    method: "POST",
    body: JSON.stringify(req),
    headers: authHeaders(token),
  });
}

export async function explainCypher(
  req: TranslateRequest,
  token: string,
): Promise<ExplainResponse> {
  return request("/explain", {
    method: "POST",
    body: JSON.stringify(req),
    headers: authHeaders(token),
  });
}

export async function profileCypher(
  req: TranslateRequest,
  token: string,
): Promise<ProfileResponse> {
  return request("/aql-profile", {
    method: "POST",
    body: JSON.stringify(req),
    headers: authHeaders(token),
  });
}

export async function getCypherProfile(): Promise<Record<string, unknown>> {
  return request("/cypher-profile");
}

// An example mined from the connected database's own saved AQL queries
// (`arango-cypher-py mine-examples`): the question and Cypher were checked to
// return the same documents as the saved query.
export interface MinedExample {
  question: string;
  cypher: string;
  params: Record<string, unknown>;
  aql: string;
  graph: string | null;
  source: { collection: string; key: string; name: string; description: string };
  verification: { verdict: string; verified_at: string; model?: string };
}

export async function getMinedExamples(
  token: string,
  limit: number = 50,
): Promise<{ examples: MinedExample[] }> {
  return request(`/examples?limit=${limit}`, { headers: authHeaders(token) });
}

// Mined examples that belong to the selected named graph (and those tied to
// no graph); every example when no graph is selected.
export function examplesForGraph(examples: MinedExample[], graph: string | null): MinedExample[] {
  if (!graph) return examples;
  return examples.filter((e) => !e.graph || e.graph === graph);
}

export interface SampleQuery {
  id: string;
  description: string;
  cypher: string;
  dataset: string;
  expected_min_count?: number;
}

export async function getSampleQueries(
  dataset?: string,
): Promise<{ queries: SampleQuery[] }> {
  const qs = dataset ? `?dataset=${encodeURIComponent(dataset)}` : "";
  return request(`/sample-queries${qs}`);
}

// WP-S3c: an inverted/ArangoSearch index suggestion emitted by the NL entity
// resolver when a fuzzy name probe fell back to a full collection scan. The UI
// offers one-click creation via `createIndex`.
export interface IndexAdvisory {
  collection: string;
  field: string;
  reason: string;
  suggestedIndex?: {
    type: string;
    name: string;
    fields: Array<{ name: string; analyzer?: string }>;
  };
}

export interface NL2CypherResponse {
  cypher: string;
  explanation: string;
  confidence: number;
  method: string;
  elapsed_ms?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  advisories?: IndexAdvisory[];
}

export interface TenantContext {
  property: string;
  value: string;
  display?: string;
}

export interface NL2CypherOptions {
  useLlm?: boolean;
  useFewshot?: boolean;
  useEntityResolution?: boolean;
  sessionToken?: string;
  tenantContext?: TenantContext | null;
  // WP-29 Part 4 / WP-30: optional retry hint forwarded to the LLM
  // prompt builder (seeds ``retry_context`` on the first attempt).
  // WP-30 will drive this from the "Regenerate from NL with error
  // hint" action on translate failure.
  retryContext?: string;
}

export async function nl2Cypher(
  question: string,
  mapping: Record<string, unknown>,
  opts: NL2CypherOptions | boolean = {},
): Promise<NL2CypherResponse> {
  // Back-compat: older call sites pass `useLlm` as a bare boolean.
  const options: NL2CypherOptions =
    typeof opts === "boolean" ? { useLlm: opts } : opts;
  const body: Record<string, unknown> = { question, mapping };
  if (options.useLlm !== undefined) body.use_llm = options.useLlm;
  if (options.useFewshot !== undefined) body.use_fewshot = options.useFewshot;
  if (options.useEntityResolution !== undefined) {
    body.use_entity_resolution = options.useEntityResolution;
  }
  if (options.sessionToken) body.session_token = options.sessionToken;
  if (options.tenantContext) body.tenant_context = options.tenantContext;
  if (options.retryContext) body.retry_context = options.retryContext;
  return request("/nl2cypher", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export interface CreateIndexResponse {
  created: boolean;
  collection: string;
  field: string;
  message?: string;
  index?: unknown;
}

// WP-S3c: create the inverted index an IndexAdvisory recommends. Authenticated
// (uses the session token) since it mutates the connected database. The backend
// reconstructs the index spec from collection+field+analyzer, so we only send
// those validated fields. Idempotent server-side (`created:false` if it exists).
export async function createIndex(
  token: string,
  advisory: IndexAdvisory,
): Promise<CreateIndexResponse> {
  const analyzer = advisory.suggestedIndex?.fields?.[0]?.analyzer ?? "text_en";
  const name = advisory.suggestedIndex?.name;
  return request("/schema/index/create", {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({
      collection: advisory.collection,
      field: advisory.field,
      analyzer,
      ...(name ? { name } : {}),
    }),
  });
}

export interface NL2AqlResponse {
  aql: string;
  bind_vars: Record<string, unknown>;
  explanation: string;
  confidence: number;
  method: string;
  elapsed_ms?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
}

export async function executeAql(
  aql: string,
  bindVars: Record<string, unknown>,
  token: string,
): Promise<ExecuteResponse> {
  return request("/execute-aql", {
    method: "POST",
    body: JSON.stringify({ aql, bind_vars: bindVars }),
    headers: authHeaders(token),
  });
}

export async function nl2Aql(
  question: string,
  mapping: Record<string, unknown>,
  tenantContext?: TenantContext | null,
  cypher?: string | null,
): Promise<NL2AqlResponse> {
  const body: Record<string, unknown> = { question, mapping };
  if (tenantContext) body.tenant_context = tenantContext;
  // When set, the backend translates this Cypher to AQL instead of
  // answering `question` — the "Generate AQL with AI" recovery path.
  if (cypher) body.cypher = cypher;
  return request("/nl2aql", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export interface TenantRecord {
  // Full ArangoDB document id, e.g. "Tenant/<uuid>". This is the
  // canonical tenant identifier — universal, indexed, and not
  // dependent on a schema-specific field like TENANT_HEX_ID.
  id: string;
  // Bare _key portion of `id` (the part after the slash). Used for
  // the Cypher `{_key: '...'}` shorthand in generated queries.
  key: string;
  name: string | null;
  subdomain: string | null;
  hex_id: string | null;
}

export interface TenantsResponse {
  detected: boolean;
  tenants: TenantRecord[];
  // Resolved ArangoDB collection name the catalog query was run
  // against. Surfaced so the UI can explain *why* detection
  // succeeded or failed (e.g. "looked for collection `Tenants`,
  // not found") instead of silently hiding the selector.
  collection?: string | null;
  // "client" when the UI passed an explicit collection query
  // param, "heuristic" when we fell back to the literal "Tenant"
  // name. Reported back so empty results are explainable.
  source?: "client" | "heuristic";
}

// Pluck the physical collection name backing the conceptual
// `Tenant` entity from the introspected mapping. Returns null when
// no mapping is present yet or no Tenant entity exists. Mirrors the
// transpiler's lookup (physical_mapping.entities.<Label>.collectionName)
// — we resolve client-side to keep the API a pure GET and avoid
// shipping the entire mapping back over the wire.
export function resolveTenantCollectionName(
  mapping: Record<string, unknown> | null | undefined,
): string | null {
  if (!mapping) return null;
  const pm =
    (mapping.physical_mapping as Record<string, unknown> | undefined) ??
    (mapping.physicalMapping as Record<string, unknown> | undefined);
  const ents = pm?.entities as Record<string, unknown> | undefined;
  const tenant = ents?.["Tenant"] as Record<string, unknown> | undefined;
  if (!tenant) return null;
  const coll = (tenant.collectionName ?? tenant.collection) as unknown;
  return typeof coll === "string" && coll.length > 0 ? coll : null;
}

export async function listTenants(
  token: string,
  mapping?: Record<string, unknown> | null,
): Promise<TenantsResponse> {
  // GET-only — older deployed services still understand the bare
  // `/tenants` request, so a freshly built UI talking to a
  // not-yet-restarted backend degrades to the heuristic path
  // instead of failing with 405. When we know the real collection
  // name (from the introspected mapping) we send it as a query
  // parameter so the server queries the right collection without
  // needing to receive the full mapping in the body.
  const collection = resolveTenantCollectionName(mapping);
  const path = collection
    ? `/tenants?collection=${encodeURIComponent(collection)}`
    : "/tenants";
  return request(path, { headers: authHeaders(token) });
}

// Tenant record as returned by /tenants/discover for the denormalised
// path: `key`/`id`/`name` all carry the bare tenant id (there is no
// `Tenant` collection to source rich fields from), plus a doc count
// so the picker can show how much data each tenant has.
export interface DiscoveredTenant extends TenantRecord {
  docs?: number;
}

export interface TenantDiscoverResponse {
  // Whether the analysed schema is tenant-scoped at all. When false the
  // UI hides the tenant picker entirely (single-tenant / reference-only).
  multiTenant: boolean;
  // How tenants are enumerated: a dedicated `Tenant` collection, the
  // distinct values of a denormalised field, or none.
  scope: "collection" | "denorm" | "none";
  // The denormalised tenant field (e.g. "tenantId") when scope=denorm.
  tenantField: string | null;
  tenants: DiscoveredTenant[];
  // Collections actually probed for denormalised tenant values.
  collections: string[];
}

// Discover selectable tenants *after* schema analysis. Unlike
// `listTenants`, this POSTs the introspected mapping so the server can
// build the tenant-scope manifest and enumerate tenants whether they
// live in a `Tenant` collection or only as denormalised field values.
export async function discoverTenants(
  token: string,
  mapping?: Record<string, unknown> | null,
): Promise<TenantDiscoverResponse> {
  return request("/tenants/discover", {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({ mapping: mapping ?? null }),
  });
}

export interface BindTenantResponse {
  tenant_id: string | null;
  tenant_key: string | null;
  bound: boolean;
}

// Re-bind (or clear, when tenantId is null) the active session's tenant
// without re-authenticating. Called when the user picks a tenant in the
// post-analysis picker; Layers 4–6 then scope every subsequent query.
export async function bindTenant(
  token: string,
  tenantId: string | null,
  tenantKey?: string | null,
): Promise<BindTenantResponse> {
  return request("/session/tenant", {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({
      tenantId,
      tenantKey: tenantKey ?? tenantId,
    }),
  });
}

// ---------------------------------------------------------------------------
// Named-graph scoping (PRD §17)
// ---------------------------------------------------------------------------

export interface GraphEdgeDefinition {
  edgeCollection: string | null;
  from: string[];
  to: string[];
}

export interface NamedGraph {
  name: string;
  edgeDefinitions: GraphEdgeDefinition[];
  vertexCollections: string[];
  orphanCollections: string[];
  collectionCount: number;
}

export interface GraphsResponse {
  graphs: NamedGraph[];
}

// List the connected database's named graphs so the UI can offer an
// optional scope selector. Degrades to an empty list on older backends.
export async function listGraphs(token: string): Promise<GraphsResponse> {
  try {
    return await request("/graphs", { headers: authHeaders(token) });
  } catch {
    return { graphs: [] };
  }
}

export interface BindGraphResponse {
  graph_name: string | null;
  bound: boolean;
}

// Bind (or clear, when graphName is null) the active session's named-graph
// scope. After binding, schema introspection only considers that graph's
// collections (PRD §17).
export async function bindGraph(
  token: string,
  graphName: string | null,
): Promise<BindGraphResponse> {
  return request("/session/graph", {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({ graphName }),
  });
}

export interface NlSamplesResponse {
  queries: string[];
  elapsed_ms?: number;
}

export async function suggestNlQueries(
  mapping: Record<string, unknown>,
  count: number = 8,
  useLlm: boolean = true,
): Promise<NlSamplesResponse> {
  return request("/nl-samples", {
    method: "POST",
    body: JSON.stringify({ mapping, count, use_llm: useLlm }),
  });
}

export interface IntrospectPropertyInfo {
  field: string;
  type: string;
  required?: boolean;
  sentinelValues?: string[];
  numericLike?: boolean;
  sampleValues?: string[];
}

export interface IntrospectEntity {
  label: string;
  collection: string;
  style: string;
  properties: Record<string, IntrospectPropertyInfo>;
  typeField?: string;
  typeValue?: string;
  estimatedCount?: number;
}

export interface RelationshipStatistics {
  edgeCount: number;
  avgOutDegree: number;
  avgInDegree: number;
  cardinalityPattern: string;
  selectivity: number;
}

export interface IntrospectRelationship {
  type: string;
  edgeCollection: string;
  style: string;
  domain?: string | null;
  range?: string | null;
  properties: Record<string, IntrospectPropertyInfo>;
  typeField?: string;
  typeValue?: string;
  statistics?: RelationshipStatistics;
  edgeCount?: number;
}

export interface SchemaWarning {
  code: string;
  message: string;
  // "info" describes normal operation and stays out of the warning banner.
  severity?: "info" | "warning" | "error";
  install_hint?: string;
}

export interface IntrospectResult {
  entities: IntrospectEntity[];
  relationships: IntrospectRelationship[];
  warnings?: SchemaWarning[];
  // Catalog model: "ready" when a mapping was served; "pending" when the
  // database has not been analyzed yet (the sidecar/background warm is working
  // on it). Absent on older servers — treat undefined as "ready".
  status?: "ready" | "pending";
}

// The service normalizes schema warnings to {code, message, severity}; this
// is the defence for older servers and bundles cached before that, which
// pass analyzer warnings through as plain strings ("LLM provider not
// configured; ..."). The banner renders `message`, keys dismissals on `code`
// and hides `info`, so every entry needs a real message and a code that is
// unique to it.
const BASELINE_NOTE_PREFIX = "LLM provider not configured";
const SEVERITIES = new Set(["info", "warning", "error"]);

// A stable code for a message: a readable slug plus a short hash of the whole
// text, so messages sharing a prefix, or in non-Latin scripts, never collide.
export function noteCode(message: string): string {
  let hash = 0x811c9dc5;
  for (const ch of message) {
    hash ^= ch.codePointAt(0) ?? 0;
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  const slug = message
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40);
  return `note:${slug ? `${slug}-` : ""}${hash.toString(16).padStart(8, "0")}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function normalizeSchemaWarnings(raw: unknown): SchemaWarning[] {
  if (!Array.isArray(raw)) return [];
  const out: SchemaWarning[] = [];
  for (const w of raw) {
    const text = typeof w === "string" ? w : isRecord(w) && typeof w.message === "string" ? w.message : null;
    const message = text?.trim();
    if (!message) {
      if (isRecord(w) && typeof w.code === "string") console.warn("Schema warning without a message dropped:", w.code);
      continue;
    }
    const code = isRecord(w) && typeof w.code === "string" && w.code ? w.code : noteCode(message);
    const given = isRecord(w) && typeof w.severity === "string" && SEVERITIES.has(w.severity) ? w.severity : null;
    const severity = (given ?? (message.startsWith(BASELINE_NOTE_PREFIX) ? "info" : "warning")) as SchemaWarning["severity"];
    const entry: SchemaWarning = { code, message, severity };
    if (isRecord(w) && typeof w.install_hint === "string" && w.install_hint) entry.install_hint = w.install_hint;
    out.push(entry);
  }
  return out;
}

export async function introspectSchema(
  token: string,
  sample = 50,
  force = false,
): Promise<IntrospectResult> {
  const params = new URLSearchParams({ sample: String(sample) });
  if (force) params.set("force", "true");
  const result = await request<IntrospectResult>(`/schema/introspect?${params}`, {
    headers: authHeaders(token),
  });
  return { ...result, warnings: normalizeSchemaWarnings(result.warnings) };
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/**
 * Introspect, polling while the catalog reports "pending".
 *
 * Schema analysis runs out of band (catalog sidecar), so a freshly-seen
 * database may report `status: "pending"` until the background warm finishes.
 * This polls with a short backoff until the schema is ready or `maxAttempts`
 * is exhausted, invoking `onPending` so the UI can show a "preparing" notice.
 * The final result is returned regardless — callers should check `status` and
 * surface a retry affordance if it is still "pending".
 */
export async function introspectSchemaUntilReady(
  token: string,
  opts: {
    sample?: number;
    force?: boolean;
    maxAttempts?: number;
    delayMs?: number;
    onPending?: (attempt: number) => void;
  } = {},
): Promise<IntrospectResult> {
  const { sample = 50, force = false, maxAttempts = 8, delayMs = 2000, onPending } = opts;
  let last: IntrospectResult = await introspectSchema(token, sample, force);
  let attempt = 1;
  while (last.status === "pending" && attempt < maxAttempts) {
    onPending?.(attempt);
    await sleep(delayMs);
    // Subsequent polls never force — we want to pick up the background warm's
    // result from the catalog, not trigger another synchronous rebuild.
    last = await introspectSchema(token, sample, false);
    attempt += 1;
  }
  return last;
}

export interface ForceReacquireResult {
  source: { kind: string | null; notes: string | null };
  warnings: SchemaWarning[];
  entity_count: number;
  relationship_count: number;
}

// Hard reacquire path. Calls get_mapping(strategy="analyzer", force_refresh=True)
// on the backend, which raises ImportError (HTTP 503) when the analyzer is
// missing instead of silently returning a heuristic-built bundle. Use this
// when /schema/invalidate-cache + /schema/introspect would just re-serve a
// poisoned heuristic mapping (e.g. analyzer was installed after the cache
// was first populated).
export async function forceReacquireSchema(
  token: string,
): Promise<ForceReacquireResult> {
  const result = await request<ForceReacquireResult>(`/schema/force-reacquire`, {
    method: "POST",
    headers: authHeaders(token),
  });
  return { ...result, warnings: normalizeSchemaWarnings(result.warnings) };
}

export function introspectToMapping(
  result: IntrospectResult,
): Record<string, unknown> {
  const entities: Record<string, unknown>[] = [];
  const physEntities: Record<string, unknown> = {};
  const entityStats: Record<string, Record<string, unknown>> = {};
  const relStats: Record<string, Record<string, unknown>> = {};

  for (const e of result.entities) {
    const propNames = Object.keys(e.properties);
    entities.push({
      name: e.label,
      labels: [e.label],
      properties: propNames.map((p) => ({ name: p })),
    });
    const physEnt: Record<string, unknown> = {
      style: e.style || "COLLECTION",
      collectionName: e.collection,
      properties: e.properties,
    };
    if (e.typeField) {
      physEnt.typeField = e.typeField;
      physEnt.typeValue = e.typeValue;
    }
    if (e.estimatedCount != null) {
      physEnt.estimatedCount = e.estimatedCount;
      entityStats[e.label] = { estimated_count: e.estimatedCount };
    }
    physEntities[e.label] = physEnt;
  }

  const rels: Record<string, unknown>[] = [];
  const physRels: Record<string, unknown> = {};

  for (const r of result.relationships) {
    const propNames = Object.keys(r.properties);
    rels.push({
      type: r.type,
      fromEntity: r.domain || "Any",
      toEntity: r.range || "Any",
      properties: propNames.map((p) => ({ name: p })),
    });
    const physRel: Record<string, unknown> = {
      style: r.style || "DEDICATED_COLLECTION",
      edgeCollectionName: r.edgeCollection,
      domain: r.domain || undefined,
      range: r.range || undefined,
      properties: r.properties,
    };
    if (r.typeField) {
      physRel.typeField = r.typeField;
      physRel.typeValue = r.typeValue;
    }
    if (r.edgeCount != null) physRel.edgeCount = r.edgeCount;
    if (r.statistics) {
      physRel.statistics = r.statistics;
      relStats[r.type] = {
        edge_count: r.statistics.edgeCount,
        avg_out_degree: r.statistics.avgOutDegree,
        avg_in_degree: r.statistics.avgInDegree,
        cardinality_pattern: r.statistics.cardinalityPattern,
        selectivity: r.statistics.selectivity,
      };
    }
    physRels[r.type] = physRel;
  }

  const metadata: Record<string, unknown> = {};
  if (Object.keys(entityStats).length || Object.keys(relStats).length) {
    metadata.statistics = {
      entities: entityStats,
      relationships: relStats,
    };
  }

  return {
    conceptual_schema: { entities, relationships: rels },
    physical_mapping: { entities: physEntities, relationships: physRels },
    metadata,
  };
}

// ---------------------------------------------------------------------------
// Corrections (local learning)
// ---------------------------------------------------------------------------

export interface CorrectionRecord {
  id: number;
  cypher: string;
  mapping_hash: string;
  database: string;
  original_aql: string;
  corrected_aql: string;
  bind_vars: Record<string, unknown>;
  created_at: string;
  note: string;
}

export async function saveCorrection(body: {
  cypher: string;
  mapping: Record<string, unknown>;
  database?: string;
  original_aql: string;
  corrected_aql: string;
  bind_vars?: Record<string, unknown>;
  note?: string;
}): Promise<{ id: number; status: string }> {
  return request("/corrections", { method: "POST", body: JSON.stringify(body) });
}

export async function listCorrections(
  limit = 100,
): Promise<{ corrections: CorrectionRecord[] }> {
  return request(`/corrections?limit=${limit}`);
}

export async function deleteCorrection(id: number): Promise<{ status: string }> {
  return request(`/corrections/${id}`, { method: "DELETE" });
}

export async function deleteAllCorrections(): Promise<{ status: string; count: number }> {
  return request("/corrections", { method: "DELETE" });
}
