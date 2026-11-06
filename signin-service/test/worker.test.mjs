// The sign-in service, run with Node's own test runner: node --test signin-service/test
import { test } from "node:test";
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";

if (!globalThis.crypto) globalThis.crypto = webcrypto;

const { handle, b64url, unb64url, signRelay, openRelay } = await import("../src/worker.js");

const ENV = { ATLASSIAN_CLIENT_ID: "client-1", ATLASSIAN_CLIENT_SECRET: "secret-1",
              RELAY_KEY: "relay-key-not-a-real-one", PUBLIC_URL: "https://signin.example.invalid",
              ATLASSIAN_AUTH: "https://auth.example.invalid" };
const STATE = "s".repeat(32);

async function desktopKey() {
  const pair = await crypto.subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, true,
                                               ["deriveBits"]);
  const raw = new Uint8Array(await crypto.subtle.exportKey("raw", pair.publicKey));
  return { pair, key: b64url(raw) };
}

async function unseal(pair, state, sealed) {
  const bytes = unb64url(sealed);
  const theirs = await crypto.subtle.importKey("raw", bytes.slice(0, 65),
    { name: "ECDH", namedCurve: "P-256" }, false, []);
  const shared = await crypto.subtle.deriveBits({ name: "ECDH", public: theirs },
                                                pair.privateKey, 256);
  const material = await crypto.subtle.importKey("raw", shared, "HKDF", false, ["deriveKey"]);
  const key = await crypto.subtle.deriveKey(
    { name: "HKDF", hash: "SHA-256", salt: new Uint8Array(32),
      info: new TextEncoder().encode("qavector-signin-v1") },
    material, { name: "AES-GCM", length: 256 }, false, ["decrypt"]);
  const plain = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: bytes.slice(65, 77), additionalData: new TextEncoder().encode(state) },
    key, bytes.slice(77));
  return JSON.parse(new TextDecoder().decode(plain));
}

function get(path) {
  return handle(new Request("https://signin.example.invalid" + path), ENV);
}

function withFetch(answer, run) {
  const seen = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    seen.push({ url, body: JSON.parse(init.body) });
    return new Response(JSON.stringify(answer.body), { status: answer.status });
  };
  return run(seen).finally(() => { globalThis.fetch = original; });
}

test("start sends the browser to Atlassian with the service's app and a signed state", async () => {
  const { key } = await desktopKey();
  const answer = await get("/atlassian/start?port=50123&state=" + STATE + "&key=" + key);
  assert.equal(answer.status, 302);
  const target = new URL(answer.headers.get("location"));
  assert.equal(target.origin + target.pathname, "https://auth.example.invalid/authorize");
  assert.equal(target.searchParams.get("client_id"), "client-1");
  assert.equal(target.searchParams.get("redirect_uri"),
               "https://signin.example.invalid/atlassian/callback");
  assert.ok(target.searchParams.get("scope").includes("offline_access"));
  const fields = await openRelay(ENV, target.searchParams.get("state"),
                                 Math.floor(Date.now() / 1000));
  assert.deepEqual([fields.p, fields.s, fields.k], [50123, STATE, key]);
  assert.ok(!answer.headers.get("location").includes("secret-1"));
});

test("start refuses a link it could not have been asked for", async () => {
  for (const query of ["port=80&state=" + STATE + "&key=AAAA", "port=50123&state=x&key=AAAA",
                       "port=50123&state=" + STATE + "&key=AAAA"]) {
    assert.equal((await get("/atlassian/start?" + query)).status, 400, query);
  }
});

test("the callback exchanges the code and seals the tokens to the desktop's key", async () => {
  const { pair, key } = await desktopKey();
  const relay = await signRelay(ENV, { p: 50123, s: STATE, k: key,
                                       t: Math.floor(Date.now() / 1000) });
  await withFetch({ status: 200, body: { access_token: "access-1", refresh_token: "refresh-1",
                                         expires_in: 3600, scope: "read:jira-work" } },
    async (seen) => {
      const answer = await get("/atlassian/callback?code=code-1&state=" + relay);
      assert.equal(answer.status, 302);
      const back = new URL(answer.headers.get("location"));
      assert.equal(back.origin + back.pathname, "http://127.0.0.1:50123/callback");
      assert.equal(back.searchParams.get("state"), STATE);
      assert.ok(!answer.headers.get("location").includes("access-1"));
      assert.deepEqual(seen[0].body, {
        client_id: "client-1", client_secret: "secret-1", grant_type: "authorization_code",
        code: "code-1", redirect_uri: "https://signin.example.invalid/atlassian/callback" });
      const tokens = await unseal(pair, STATE, back.searchParams.get("sealed"));
      assert.deepEqual(tokens, { access_token: "access-1", refresh_token: "refresh-1",
                                 expires_in: 3600, scope: "read:jira-work" });
    });
});

test("a forged or old state is refused and Atlassian is not asked", async () => {
  const { key } = await desktopKey();
  const old = await signRelay(ENV, { p: 50123, s: STATE, k: key, t: 1 });
  const forged = (await signRelay(ENV, { p: 50123, s: STATE, k: key,
                                         t: Math.floor(Date.now() / 1000) })).split(".")[0]
                 + "." + b64url(new Uint8Array(32));
  await withFetch({ status: 200, body: {} }, async (seen) => {
    for (const relay of [old, forged, "nonsense"]) {
      assert.equal((await get("/atlassian/callback?code=c&state=" + relay)).status, 400);
    }
    assert.equal(seen.length, 0);
  });
});

test("a refusal in the browser goes back to the desktop to be said", async () => {
  const { key } = await desktopKey();
  const relay = await signRelay(ENV, { p: 50123, s: STATE, k: key,
                                       t: Math.floor(Date.now() / 1000) });
  const answer = await get("/atlassian/callback?error=access_denied&state=" + relay);
  const back = new URL(answer.headers.get("location"));
  assert.equal(back.searchParams.get("error"), "access_denied");
  assert.equal(back.searchParams.get("sealed"), null);
});

test("refresh adds the secret and answers with tokens only", async () => {
  await withFetch({ status: 200, body: { access_token: "access-2", refresh_token: "refresh-2",
                                         expires_in: 3600, extra: "x" } }, async (seen) => {
    const answer = await handle(new Request("https://signin.example.invalid/atlassian/refresh", {
      method: "POST", body: JSON.stringify({ refresh_token: "refresh-1" }) }), ENV);
    assert.equal(answer.status, 200);
    assert.deepEqual(await answer.json(), { access_token: "access-2", refresh_token: "refresh-2",
                                            expires_in: 3600, scope: "" });
    assert.deepEqual(seen[0].body, { client_id: "client-1", client_secret: "secret-1",
                                     grant_type: "refresh_token", refresh_token: "refresh-1" });
  });
});

test("a refused refresh says so with Atlassian's reason", async () => {
  await withFetch({ status: 403, body: { error: "invalid_grant",
                                         error_description: "expired" } }, async () => {
    const answer = await handle(new Request("https://signin.example.invalid/atlassian/refresh", {
      method: "POST", body: JSON.stringify({ refresh_token: "old" }) }), ENV);
    assert.equal(answer.status, 403);
    assert.deepEqual(await answer.json(), { error: "invalid_grant", error_description: "expired" });
  });
  const empty = await handle(new Request("https://signin.example.invalid/atlassian/refresh", {
    method: "POST", body: "{}" }), ENV);
  assert.equal(empty.status, 400);
});
