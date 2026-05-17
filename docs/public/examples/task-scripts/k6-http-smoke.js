import http from "k6/http";
import { check, sleep } from "k6";

export const options = {
  vus: Number(__ENV.VUS || "1"),
  duration: __ENV.DURATION || "20s",
  thresholds: {
    http_req_failed: ["rate<0.05"],
    http_req_duration: ["p(95)<1000"],
  },
};

const baseUrl = (__ENV.BASE_URL || "http://demo-target:8080").replace(/\/$/, "");

export default function () {
  const response = http.get(`${baseUrl}/api/ping`, {
    tags: { endpoint_name: "GET /api/ping" },
  });

  check(response, {
    "status is 200": res => res.status === 200,
    "body has pong": res => String(res.body || "").includes("pong"),
  });

  sleep(1);
}
