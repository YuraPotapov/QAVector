// The Worker behind a plain Node HTTP server - for QAVector's own end-to-end test
// (tests/test_integrations_signin_service.py), which drives it with a loopback Atlassian.
// Not under test/: the test runner would start it and wait for it forever.
// Usage: SIGNIN_ENV='{"PUBLIC_URL": ...}' node serve.mjs   -> prints "ready <port>".
import http from "node:http";
import { webcrypto } from "node:crypto";

if (!globalThis.crypto) globalThis.crypto = webcrypto;
const { handle } = await import("../src/worker.js");
const env = JSON.parse(process.env.SIGNIN_ENV || "{}");

const server = http.createServer(async (incoming, outgoing) => {
  const chunks = [];
  for await (const chunk of incoming) chunks.push(chunk);
  const body = chunks.length ? Buffer.concat(chunks) : undefined;
  const request = new Request("http://127.0.0.1" + incoming.url, {
    method: incoming.method, headers: incoming.headers,
    body: ["GET", "HEAD"].includes(incoming.method) ? undefined : body });
  const answer = await handle(request, Object.assign({}, env, {
    PUBLIC_URL: env.PUBLIC_URL || "http://127.0.0.1:" + server.address().port }));
  outgoing.writeHead(answer.status, Object.fromEntries(answer.headers));
  outgoing.end(Buffer.from(await answer.arrayBuffer()));
});
server.listen(Number(process.env.SIGNIN_PORT || 0), "127.0.0.1", () => {
  console.log("ready " + server.address().port);
});
