import type { TaskScriptVersionContent } from '@/services/taskApi'

export type DemoScriptKey =
  | 'HTTP'
  | 'GRPC'
  | '混合标准'
  | '文件读取'
  | 'xk6-file'
  | '全局变量'
  | '上下文变量'
  | '结果 checks'
  | '终止阈值'
  | 'nacos-http'
  | 'nacos-grpc'
  | 'xk6-grpc'
  | 'xk6-kafka'
  | 'xk6-redis'

type DemoScriptRecord = {
  title: string
  description: string
  preview: TaskScriptVersionContent
}

const BUILTIN_UPDATED_AT = '2026-03-30T14:00:00+08:00'

const qaHttpK6 = `import http from "k6/http";
import { check } from "k6";
import exec from "k6/execution";

const GET_ENDPOINT_NAME = "GET /qa/http-get";
const POST_ENDPOINT_NAME = "POST /qa/http-post";

const raw = open(\`\${__ENV.PTP_DATA_DIR || "."}/\${__ENV.DATA_FILE || "test_data.csv"}\`);
const rows = raw.trim().split(/\\r?\\n/).slice(1).map(line => line.trim()).filter(Boolean);
const baseUrl = (__ENV.BASE_URL || "http://demo-target:8080").replace(/\\/+$/, "");
const totalTargetTps = Number(__ENV.target_tps || __ENV.TARGET_TPS || "0");
const podCount = Math.max(1, Number(__ENV.pod_count || __ENV.POD_COUNT || "1"));
const endpointCount = 2;
const durationSeconds = Math.max(1, Number(__ENV.PTP_DURATION_SECONDS || __ENV.duration || "300"));

function gcd(a, b) {
  let left = Math.abs(a);
  let right = Math.abs(b);
  while (right > 0) {
    const next = left % right;
    left = right;
    right = next;
  }
  return Math.max(1, left || 1);
}

function buildArrivalRateScenario(totalTps, workers, endpoints) {
  const normalizedTps = Number.isFinite(totalTps) ? Math.max(0, Math.floor(totalTps)) : 0;
  const denominator = Math.max(1, Math.floor(workers) * Math.floor(endpoints));
  if (normalizedTps <= 0) {
    return null;
  }
  const divisor = gcd(normalizedTps, denominator);
  const rate = Math.max(1, Math.floor(normalizedTps / divisor));
  const timeUnitSeconds = Math.max(1, Math.floor(denominator / divisor));
  const preAllocatedVUs = Math.max(
    1,
    Number(__ENV.PTP_THREAD_COUNT || __ENV.threads || __ENV.vus || "1"),
    Math.ceil(normalizedTps / denominator),
  );
  return {
    executor: "constant-arrival-rate",
    rate,
    timeUnit: \`\${timeUnitSeconds}s\`,
    duration: \`\${durationSeconds}s\`,
    preAllocatedVUs,
    maxVUs: Math.max(preAllocatedVUs, preAllocatedVUs * 4),
  };
}

const scenarioTemplate = buildArrivalRateScenario(totalTargetTps, podCount, endpointCount);

export const options = {
  thresholds: {
    [\`http_reqs{name:\${GET_ENDPOINT_NAME}}\`]: ["count>=0"],
    [\`http_reqs{name:\${POST_ENDPOINT_NAME}}\`]: ["count>=0"],
    [\`http_req_duration{name:\${GET_ENDPOINT_NAME}}\`]: ["p(95)>=0"],
    [\`http_req_duration{name:\${POST_ENDPOINT_NAME}}\`]: ["p(95)>=0"],
  },
  ...(scenarioTemplate
    ? {
        scenarios: {
          get_endpoint: {
            ...scenarioTemplate,
            exec: "runGetScenario",
          },
          post_endpoint: {
            ...scenarioTemplate,
            exec: "runPostScenario",
          },
        },
      }
    : {}),
};

function pickSeq() {
  const index = exec.scenario.iterationInTest % rows.length;
  return rows[index];
}

function buildRequestContext() {
  const seq = pickSeq();
  const rand = Math.floor(Math.random() * 900000 + 100000);
  return { seq, rand };
}

function runGetRequest(seq, rand) {
  const response = http.get(\`\${baseUrl}/qa/http-get?seq=\${seq}&rand=\${rand}\`, {
    tags: { name: GET_ENDPOINT_NAME, endpoint_name: GET_ENDPOINT_NAME },
  });
  check(response, {
    "GET status is 200": (res) => res.status === 200,
  });
}

function runPostRequest(seq, rand) {
  const response = http.post(\`\${baseUrl}/qa/http-post\`, JSON.stringify({ seq, rand }), {
    headers: { "Content-Type": "application/json" },
    tags: { name: POST_ENDPOINT_NAME, endpoint_name: POST_ENDPOINT_NAME },
  });
  check(response, {
    "POST status is 200": (res) => res.status === 200,
  });
}

export function runGetScenario() {
  const { seq, rand } = buildRequestContext();
  runGetRequest(seq, rand);
}

export function runPostScenario() {
  const { seq, rand } = buildRequestContext();
  runPostRequest(seq, rand);
}

export default function () {
  const { seq, rand } = buildRequestContext();
  runGetRequest(seq, rand);
  runPostRequest(seq, rand);
}
`

const qaGrpcK6 = `import grpc from "k6/net/grpc";
import { check } from "k6";
import exec from "k6/execution";

const SAY_HELLO_ENDPOINT_NAME = "hello.Hello/SayHello";
const SAY_HELLO_AGAIN_ENDPOINT_NAME = "hello.Hello/SayHelloAgain";

const raw = open(\`\${__ENV.PTP_DATA_DIR || "."}/\${__ENV.DATA_FILE || "test_data.csv"}\`);
const rows = raw.trim().split(/\\r?\\n/).slice(1).map(line => line.trim()).filter(Boolean);
const client = new grpc.Client();
const host = (__ENV.GRPC_HOST || "").trim();
const protoDir = __ENV.PTP_PROTO_DIR || "/app/tests/fixtures/grpc_demo";
const totalTargetTps = Number(__ENV.target_tps || __ENV.TARGET_TPS || "0");
const podCount = Math.max(1, Number(__ENV.pod_count || __ENV.POD_COUNT || "1"));
const endpointCount = 2;
const durationSeconds = Math.max(1, Number(__ENV.PTP_DURATION_SECONDS || __ENV.duration || "300"));

if (!host) throw new Error("GRPC_HOST is required");
client.load([protoDir], "hello.proto");

function gcd(a, b) {
  let left = Math.abs(a);
  let right = Math.abs(b);
  while (right > 0) {
    const next = left % right;
    left = right;
    right = next;
  }
  return Math.max(1, left || 1);
}

function buildArrivalRateScenario(totalTps, workers, endpoints) {
  const normalizedTps = Number.isFinite(totalTps) ? Math.max(0, Math.floor(totalTps)) : 0;
  const denominator = Math.max(1, Math.floor(workers) * Math.floor(endpoints));
  if (normalizedTps <= 0) {
    return null;
  }
  const divisor = gcd(normalizedTps, denominator);
  const rate = Math.max(1, Math.floor(normalizedTps / divisor));
  const timeUnitSeconds = Math.max(1, Math.floor(denominator / divisor));
  const preAllocatedVUs = Math.max(
    1,
    Number(__ENV.PTP_THREAD_COUNT || __ENV.threads || __ENV.vus || "1"),
    Math.ceil(normalizedTps / denominator),
  );
  return {
    executor: "constant-arrival-rate",
    rate,
    timeUnit: \`\${timeUnitSeconds}s\`,
    duration: \`\${durationSeconds}s\`,
    preAllocatedVUs,
    maxVUs: Math.max(preAllocatedVUs, preAllocatedVUs * 4),
  };
}

const scenarioTemplate = buildArrivalRateScenario(totalTargetTps, podCount, endpointCount);

export const options = {
  thresholds: {
    [\`grpc_req_duration{name:\${SAY_HELLO_ENDPOINT_NAME}}\`]: ["p(95)>=0"],
    [\`grpc_req_duration{name:\${SAY_HELLO_AGAIN_ENDPOINT_NAME}}\`]: ["p(95)>=0"],
  },
  ...(scenarioTemplate
    ? {
        scenarios: {
          say_hello_endpoint: {
            ...scenarioTemplate,
            exec: "runSayHelloScenario",
          },
          say_hello_again_endpoint: {
            ...scenarioTemplate,
            exec: "runSayHelloAgainScenario",
          },
        },
      }
    : {}),
};

function pickSeq() {
  const index = exec.scenario.iterationInTest % rows.length;
  return rows[index];
}

let connected = false;

function ensureConnected() {
  if (!connected) {
    client.connect(host, { plaintext: true });
    connected = true;
  }
}

function buildName() {
  const seq = pickSeq();
  const rand = Math.floor(Math.random() * 900000 + 100000);
  return \`seq-\${seq}-rand-\${rand}\`;
}

function invokeSayHello(name) {
  ensureConnected();
  return client.invoke("hello.Hello/SayHello", { name }, {
    tags: { name: SAY_HELLO_ENDPOINT_NAME, endpoint_name: SAY_HELLO_ENDPOINT_NAME },
  });
}

function invokeSayHelloAgain(name) {
  ensureConnected();
  return client.invoke("hello.Hello/SayHelloAgain", { name }, {
    tags: { name: SAY_HELLO_AGAIN_ENDPOINT_NAME, endpoint_name: SAY_HELLO_AGAIN_ENDPOINT_NAME },
  });
}

export function runSayHelloScenario() {
  const name = buildName();
  const response = invokeSayHello(name);
  check(response, {
    "gRPC status OK": (res) => res && res.status === grpc.StatusOK,
    "gRPC echoes name": (res) => res && res.message && res.message.message === \`Hello, \${name}\`,
  });
}

export function runSayHelloAgainScenario() {
  const name = buildName();
  const response = invokeSayHelloAgain(name);
  check(response, {
    "gRPC Again status OK": (res) => res && res.status === grpc.StatusOK,
    "gRPC Again echoes name": (res) => res && res.message && res.message.message === \`Hello again, \${name}\`,
  });
}

export default function () {
  const name = buildName();
  const hello = invokeSayHello(name);
  const helloAgain = invokeSayHelloAgain(name);
  check({ hello, helloAgain }, {
    "gRPC status OK": ({ hello: res }) => res && res.status === grpc.StatusOK,
    "gRPC echoes name": ({ hello: res }) => res && res.message && res.message.message === \`Hello, \${name}\`,
    "gRPC Again status OK": ({ helloAgain: res }) => res && res.status === grpc.StatusOK,
    "gRPC Again echoes name": ({ helloAgain: res }) => res && res.message && res.message.message === \`Hello again, \${name}\`,
  });
}

export function teardown() {
  if (connected) {
    client.close();
    connected = false;
  }
}
`

const qaMixedStandardK6 = `import grpc from "k6/net/grpc";
import http from "k6/http";
import { check } from "k6";
import exec from "k6/execution";

const HTTP_GET_ENDPOINT_NAME = "GET /qa/http-get";
const HTTP_POST_ENDPOINT_NAME = "POST /qa/http-post";
const GRPC_SAY_HELLO_ENDPOINT_NAME = "hello.Hello/SayHello";
const GRPC_SAY_HELLO_AGAIN_ENDPOINT_NAME = "hello.Hello/SayHelloAgain";

const MIXED_WEIGHTS = [
  { key: "http_get", weight: 15 },
  { key: "http_post", weight: 15 },
  { key: "grpc_hello", weight: 35 },
  { key: "grpc_hello_again", weight: 35 },
];
const WEIGHT_TOTAL = MIXED_WEIGHTS.reduce((sum, item) => sum + item.weight, 0);

const raw = open(\`\${__ENV.PTP_DATA_DIR || "."}/\${__ENV.DATA_FILE || "test_data.csv"}\`);
const rows = raw.trim().split(/\\r?\\n/).slice(1).map(line => line.trim()).filter(Boolean);
const httpBaseUrl = (__ENV.BASE_URL || "http://demo-target:8080").replace(/\\/+$/, "");
const grpcHost = (__ENV.GRPC_HOST || "demo-target:50051").trim();
const protoDir = __ENV.PTP_PROTO_DIR || ".";
const totalTargetTps = Math.max(0, Number(__ENV.target_tps || __ENV.TARGET_TPS || "0"));
const podCount = Math.max(1, Number(__ENV.pod_count || __ENV.POD_COUNT || "1"));
const durationSeconds = Math.max(1, Number(__ENV.PTP_DURATION_SECONDS || __ENV.duration || "300"));
const threadCount = Math.max(1, Number(__ENV.PTP_THREAD_COUNT || __ENV.threads || __ENV.vus || "1"));

const client = new grpc.Client();
client.load([protoDir], "hello.proto");

function pickSeq() {
  return rows[exec.scenario.iterationInTest % rows.length];
}

function buildName() {
  const seq = pickSeq();
  const rand = Math.floor(Math.random() * 900000 + 100000);
  return { seq, rand, name: \`seq-\${seq}-rand-\${rand}\` };
}

function buildWeightedScenario(weight) {
  if (totalTargetTps <= 0) {
    return null;
  }
  const perAgentTps = totalTargetTps / podCount;
  const rate = Math.max(1, Math.round(perAgentTps * weight));
  return {
    executor: "constant-arrival-rate",
    rate,
    timeUnit: \`\${WEIGHT_TOTAL}s\`,
    duration: \`\${durationSeconds}s\`,
    preAllocatedVUs: threadCount,
    maxVUs: Math.max(threadCount, threadCount * 4),
  };
}

function buildScenarios() {
  if (totalTargetTps <= 0) {
    return undefined;
  }
  return Object.fromEntries(
    MIXED_WEIGHTS.map(item => [
      item.key,
      {
        ...buildWeightedScenario(item.weight),
        exec: item.key,
      },
    ]),
  );
}

function resolveAction(iteration) {
  const normalized = iteration % WEIGHT_TOTAL;
  let cursor = 0;
  for (const item of MIXED_WEIGHTS) {
    cursor += item.weight;
    if (normalized < cursor) {
      return item.key;
    }
  }
  return MIXED_WEIGHTS[MIXED_WEIGHTS.length - 1].key;
}

export const options = {
  ...(buildScenarios() ? { scenarios: buildScenarios() } : { vus: threadCount, duration: \`\${durationSeconds}s\` }),
};

let connected = false;
function ensureConnected() {
  if (!connected) {
    client.connect(grpcHost, { plaintext: true });
    connected = true;
  }
}

function executeAction(action) {
  const { seq, rand, name } = buildName();
  if (action === "http_get") {
    const response = http.get(\`\${httpBaseUrl}/qa/http-get?seq=\${seq}&rand=\${rand}\`, {
      tags: { name: HTTP_GET_ENDPOINT_NAME, endpoint_name: HTTP_GET_ENDPOINT_NAME },
    });
    check(response, { "HTTP GET status is 200": res => res.status === 200 });
    return;
  }
  if (action === "http_post") {
    const response = http.post(\`\${httpBaseUrl}/qa/http-post\`, JSON.stringify({ seq, rand }), {
      headers: { "Content-Type": "application/json" },
      tags: { name: HTTP_POST_ENDPOINT_NAME, endpoint_name: HTTP_POST_ENDPOINT_NAME },
    });
    check(response, { "HTTP POST status is 200": res => res.status === 200 });
    return;
  }
  ensureConnected();
  if (action === "grpc_hello") {
    const response = client.invoke("hello.Hello/SayHello", { name }, {
      tags: { name: GRPC_SAY_HELLO_ENDPOINT_NAME, endpoint_name: GRPC_SAY_HELLO_ENDPOINT_NAME },
    });
    check(response, { "GRPC SayHello status OK": res => res && res.status === grpc.StatusOK });
    return;
  }
  const response = client.invoke("hello.Hello/SayHelloAgain", { name }, {
    tags: { name: GRPC_SAY_HELLO_AGAIN_ENDPOINT_NAME, endpoint_name: GRPC_SAY_HELLO_AGAIN_ENDPOINT_NAME },
  });
  check(response, { "GRPC SayHelloAgain status OK": res => res && res.status === grpc.StatusOK });
}

export function http_get() { executeAction("http_get"); }
export function http_post() { executeAction("http_post"); }
export function grpc_hello() { executeAction("grpc_hello"); }
export function grpc_hello_again() { executeAction("grpc_hello_again"); }

export default function () {
  executeAction(resolveAction(exec.scenario.iterationInTest));
}

export function teardown() {
  if (connected) {
    client.close();
    connected = false;
  }
}
`

const k6DataDemo = `const dataDir = __ENV.PTP_DATA_DIR || '.';
const dataFile = (__ENV.PTP_DATA_FILES || 'users.txt').split(',').find(name => name.trim().endsWith('.txt')) || 'users.txt';
const raw = open(\`\${dataDir}/\${dataFile}\`);
const lines = raw.trim().split(/\\r?\\n/);

if (lines.length < 2) throw new Error(\`runtime text asset \${dataFile} is empty\`);
const [username, token] = lines[1].split('|');
if (!username || !token) throw new Error(\`runtime text asset \${dataFile} does not contain username|token rows\`);

console.log(JSON.stringify({ data_file: dataFile, username, token }));
export const options = { vus: 1, iterations: 1 };
export default function () {}
`

const xk6FileDemo = `import file from "k6/x/file";

const outputPath = __ENV.output_file || "/tmp/xk6-file-demo.txt";

export const options = { vus: 1, iterations: 1 };

export default function () {
  file.writeString(outputPath, "xk6-file demo\\n");
  file.appendString(outputPath, "second line\\n");
  console.log(JSON.stringify({ demo: "xk6-file", outputPath }));
}
`

const wrapDemo = (
  fileName: string,
  title: string,
  description: string,
  content: string,
): DemoScriptRecord => ({
  title,
  description,
  preview: {
    version: 'builtin-demo',
    file_name: fileName,
    created_by_name: 'builtin-demo-registry',
    updated_at: BUILTIN_UPDATED_AT,
    content,
  },
})

const globalVariablesDemo = `import http from "k6/http";
import { check } from "k6";

const baseUrl = __ENV.BASE_URL || "https://example.test";
const tenant = __ENV.TENANT || "ptp-demo";
const token = __ENV.GLOBAL_TOKEN || "replace-me";

export default function () {
  const response = http.get(\`\${baseUrl}/api/demo?tenant=\${tenant}\`, {
    headers: { Authorization: \`Bearer \${token}\` },
  });
  check(response, {
    "status is 200": (res) => res.status === 200,
  });
}
`

const contextVariablesDemo = `import http from "k6/http";
import { check } from "k6";

export default function () {
  const loginRes = http.post("https://example.test/login", JSON.stringify({ user: "demo" }), {
    headers: { "Content-Type": "application/json" },
  });
  const traceId = loginRes.headers["X-Trace-Id"] || "missing-trace";
  const orderRes = http.get(\`https://example.test/orders?trace_id=\${traceId}\`);
  check(orderRes, {
    "order status is 200": (res) => res.status === 200,
  });
}
`

const checksDemo = `import http from "k6/http";
import { check } from "k6";

export default function () {
  const response = http.get("https://example.test/health");
  check(response, {
    "status is 200": (res) => res.status === 200,
    "body includes ok": (res) => String(res.body || "").includes("ok"),
  });
}
`

const thresholdDemo = `import http from "k6/http";

export const options = {
  thresholds: {
    http_req_failed: ["rate<0.01"],
    http_req_duration: ["p(95)<800"],
  },
};

export default function () {
  http.get("https://example.test/orders");
}
`

const nacosHttpDemo = `import http from "k6/http";
import { check } from "k6";
import nacos from "k6/x/nacos";

export function setup() {
  nacos.init(
    "nacosClient1",
    __ENV.NACOS_HOST || "nacos",
    Number(__ENV.NACOS_PORT || "8848"),
    __ENV.NACOS_USERNAME || "nacos",
    __ENV.NACOS_PASSWORD || "nacos",
    __ENV.NACOS_NAMESPACE || "default"
  );
}

export default function () {
  const serviceName = (__ENV.service_name || __ENV.NACOS_SERVICE_NAME || "ptp-agent").trim();
  const groupName = (__ENV.group_name || __ENV.NACOS_GROUP || "DEFAULT_GROUP").trim();
  const requestPath = (__ENV.request_path || __ENV.REQUEST_PATH || "/health").trim() || "/health";
  const instance = nacos.selectOneHealthyInstance("nacosClient1", serviceName, groupName);
  const baseUrl = \`http://\${instance.ip}:\${instance.port}\`;
  const response = http.get(\`\${baseUrl}\${requestPath}\`);

  console.log(JSON.stringify({ demo: "nacos-http", baseUrl, requestPath, status: response.status }));
  check(response, {
    "nacos discovered health status is 200": (res) => res.status === 200,
    "nacos discovered health body includes ok": (res) => String(res.body || "").includes('"status":"ok"'),
  });
}
`

const nacosGrpcDemo = `import grpc from "k6/net/grpc";
import nacos from "k6/x/nacos";

const client = new grpc.Client();
const protoDir = __ENV.PTP_PROTO_DIR || "/app/tests/fixtures/grpc_demo";
const protoFile = __ENV.proto_file || __ENV.GRPC_PROTO_FILE || "hello.proto";
client.load([protoDir], protoFile);

export function setup() {
  nacos.init(
    "nacosClient1",
    __ENV.NACOS_HOST || "nacos",
    Number(__ENV.NACOS_PORT || "8848"),
    __ENV.NACOS_USERNAME || "nacos",
    __ENV.NACOS_PASSWORD || "nacos",
    __ENV.NACOS_NAMESPACE || "default"
  );
}

export default function () {
  const serviceName = (
    __ENV.service_name ||
    __ENV.GRPC_DEMO_NACOS_SERVICE_NAME ||
    __ENV.NACOS_SERVICE_NAME ||
    "ptp-grpc-demo"
  ).trim();
  const groupName = (__ENV.group_name || __ENV.NACOS_GROUP || "DEFAULT_GROUP").trim();
  const method = __ENV.grpc_method || __ENV.GRPC_METHOD || "hello.Hello/SayHello";
  const name = __ENV.grpc_name || __ENV.GRPC_NAME || "OpenLoadHub";
  const instance = nacos.selectOneHealthyInstance("nacosClient1", serviceName, groupName);
  const host = \`\${instance.ip}:\${instance.port}\`;

  client.connect(host, { plaintext: true, timeout: "3s" });
  const response = client.invoke(method, { name }, { timeout: "3s" });
  if (!response || response.status !== grpc.StatusOK) {
    throw new Error(\`grpc call failed: \${JSON.stringify(response)}\`);
  }
  console.log(JSON.stringify({ demo: "nacos-grpc", host, method, status: response.status, message: response.message }));
  client.close();
}
`

const xk6GrpcDemo = `import grpc from "k6/x/grpc";
import { check } from "k6";

const client = new grpc.Client();
const protoDir = __ENV.PTP_PROTO_DIR || "/app/tests/fixtures/grpc_demo";
client.load([protoDir], __ENV.proto_file || "hello.proto");

export const options = { vus: 1, iterations: 1 };

export default function () {
  const host = (__ENV.GRPC_HOST || "host.docker.internal:50052").trim();
  client.connect(host, { plaintext: true, timeout: "3s" });
  const response = client.invoke(__ENV.grpc_method || "hello.Hello/SayHello", { name: __ENV.grpc_name || "OpenLoadHub" }, { timeout: "3s" });
  check(response, {
    "xk6-grpc status OK": (res) => res && res.status === grpc.StatusOK,
  });
  client.close();
}
`

const xk6KafkaDemo = `import { Writer, Connection, SchemaRegistry, SCHEMA_TYPE_STRING } from "k6/x/kafka";

const writer = new Writer({
  brokers: [__ENV.KAFKA_BROKER || "kafka:9092"],
  topic: __ENV.KAFKA_TOPIC || "ptp-demo-topic",
});
const connection = new Connection({ address: __ENV.KAFKA_BROKER || "kafka:9092" });
const schemaRegistry = new SchemaRegistry();

if (__VU === 0) {
  connection.createTopic({ topic: __ENV.KAFKA_TOPIC || "ptp-demo-topic" });
}

export const options = { vus: 1, iterations: 1 };

export default function () {
  writer.produce({
    messages: [
      {
        key: schemaRegistry.serialize({
          data: String(Date.now()),
          schemaType: SCHEMA_TYPE_STRING,
        }),
        value: schemaRegistry.serialize({
          data: JSON.stringify({ source: "xk6-kafka-demo", ts: Date.now() }),
          schemaType: SCHEMA_TYPE_STRING,
        }),
      },
    ],
  });
}
`

const xk6RedisDemo = `import redis from "k6/x/redis";

const client = new redis.Client(__ENV.REDIS_URL || "redis://redis:6379");
const key = __ENV.REDIS_KEY || "ptp:xk6:demo";

export const options = { vus: 1, iterations: 1 };

export default function () {
  return client.set(key, "xk6-redis-demo").then(() => client.get(key)).then((value) => {
    console.log(JSON.stringify({ demo: "xk6-redis", key, value: String(value) }));
  });
}
`

export const DEMO_SCRIPT_REGISTRY: Record<DemoScriptKey, DemoScriptRecord> = {
  HTTP: wrapDemo('demo-http-k6.js', 'HTTP 标准模板', '平台标准 HTTP 脚本：`target_tps` 表示总 TPS，多节点自动按 `pod_count` 分配到执行节点。', qaHttpK6),
  GRPC: wrapDemo('demo-grpc-k6.js', 'GRPC 标准模板', '平台标准 gRPC 脚本：`target_tps` 表示总 TPS，多节点自动按 `pod_count` 分配到执行节点。', qaGrpcK6),
  混合标准: wrapDemo('demo-mixed-k6-variable.js', '混合标准模板', '平台标准 mixed 脚本：HTTP + gRPC 统一按总 `target_tps` 和 `pod_count` 生成多 scenario。', qaMixedStandardK6),
  文件读取: wrapDemo('k6-data-demo.js', '文件读取 Demo', '文件读取与数据资产参考脚本', k6DataDemo),
  'xk6-file': wrapDemo('xk6-file-demo.js', 'xk6-file Demo [extension demo]', '通过 k6/x/file 读写运行时文件，验证当前定制 k6 binary 的 xk6-file 扩展可用。', xk6FileDemo),
  全局变量: wrapDemo('k6-global-vars-demo.js', '全局变量 Demo', '全局变量与鉴权 header 参考脚本', globalVariablesDemo),
  上下文变量: wrapDemo('k6-context-vars-demo.js', '上下文变量 Demo', '上下游请求上下文变量传递参考脚本', contextVariablesDemo),
  '结果 checks': wrapDemo('k6-checks-demo.js', '结果 checks Demo', 'checks 校验参考脚本', checksDemo),
  终止阈值: wrapDemo('k6-threshold-demo.js', '终止阈值 Demo', 'threshold 阈值终止参考脚本', thresholdDemo),
  'nacos-http': wrapDemo('k6-nacos-http-demo.js', 'Nacos HTTP Demo [runtime demo]', 'Platform 连通性 demo：通过 xk6-nacos 发现 ptp-agent 并访问 /health。证明服务发现链路可用，不代表业务场景脚本。如需业务场景，请自行上传含 ###{param}### 变量的业务脚本。', nacosHttpDemo),
  'nacos-grpc': wrapDemo('k6-nacos-grpc-demo.js', 'Nacos GRPC Demo [runtime demo]', 'Platform 连通性 demo：通过 Nacos 发现 ptp-grpc-demo 并使用镜像内置 hello.proto 发起 gRPC 调用。证明 gRPC runtime 链路可用，不代表业务场景脚本。', nacosGrpcDemo),
  'xk6-grpc': wrapDemo('xk6-grpc-demo.js', 'xk6-grpc Demo [compat demo]', '通过 k6/x/grpc 兼容入口访问 gRPC 服务，目标是把历史 xk6-grpc 脚本迁到可控兼容层。', xk6GrpcDemo),
  'xk6-kafka': wrapDemo('xk6-kafka-demo.js', 'xk6-kafka Demo [extension demo]', '通过 k6/x/kafka 生产一条消息，验证当前定制 k6 binary 的 Kafka 扩展可用。', xk6KafkaDemo),
  'xk6-redis': wrapDemo('xk6-redis-demo.js', 'xk6-redis Demo [extension demo]', '通过 k6/x/redis 对 Redis 做最小 SET/GET，验证当前定制 k6 binary 的 Redis 扩展可用。', xk6RedisDemo),
}

export function getDemoScriptPreview(key: string): DemoScriptRecord | null {
  return DEMO_SCRIPT_REGISTRY[key as DemoScriptKey] || null
}
