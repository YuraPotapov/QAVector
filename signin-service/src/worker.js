// QAVector sign-in service: "Sign in with Atlassian" without a secret on anybody's desktop.
//
// Atlassian's OAuth 2.0 (3LO) needs the app's client secret to turn a sign-in into
// tokens, and a secret shipped inside a public desktop program is no secret. So the
// secret lives here, in the Worker's own secrets, and the desktop never sees it.
//
// The Worker keeps nothing. A sign-in carries everything it needs in the state it
// sends to Atlassian, signed so nobody can change where the tokens go:
//
//   1. GET /atlassian/start?port&state&key   - from the desktop's browser. `key` is
//      the desktop's one-time P-256 public key. Redirects to Atlassian's consent page.
//   2. GET /atlassian/callback?code&state    - Atlassian comes back. The code is
//      exchanged with the secret, and the tokens are sealed to the desktop's key
//      (ECDH, HKDF-SHA256, AES-256-GCM) and handed to http://127.0.0.1:<port>/callback.
//      Only the program that made the key can open them; the browser's history holds
//      ciphertext.
//   3. POST /atlassian/refresh {refresh_token} - a refresh needs the secret too.
//
// Secrets (wrangler secret put): ATLASSIAN_CLIENT_ID, ATLASSIAN_CLIENT_SECRET,
// RELAY_KEY (any long random string). Variables: PUBLIC_URL (this Worker's own address).
// ATLASSIAN_AUTH overrides https://auth.atlassian.com - for tests only.

const SCOPES = "read:jira-work write:jira-work read:jira-user offline_access";
const INFO = "qavector-signin-v1";
// How long a sign-in may take between start and Atlassian coming back.
const RELAY_SECONDS = 600;

const encoder = new TextEncoder();

export function b64url(bytes) {
  let text = "";
  for (const byte of new Uint8Array(bytes)) text += String.fromCharCode(byte);
  return btoa(text).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function unb64url(text) {
  const padded = String(text).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(padded + "===".slice((padded.length + 3) % 4));
  return Uint8Array.from(raw, (char) => char.charCodeAt(0));
}

function authBase(env) {
  return (env.ATLASSIAN_AUTH || "https://auth.atlassian.com").replace(/\/$/, "");
}

function publicUrl(env) {
  return String(env.PUBLIC_URL || "").replace(/\/$/, "");
}

function page(status, text) {
  const safe = String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;");
  return new Response(
    "<!doctype html><meta charset=utf-8><title>QAVector</title>" +
      "<body style='font:16px sans-serif;margin:3em'>" + safe + "</body>",
    { status, headers: { "content-type": "text/html; charset=utf-8",
                         "cache-control": "no-store" } });
}

function json(status, body) {
  return new Response(JSON.stringify(body), {
    status, headers: { "content-type": "application/json", "cache-control": "no-store" } });
}

async function hmacKey(env) {
  return crypto.subtle.importKey("raw", encoder.encode(env.RELAY_KEY || ""),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign", "verify"]);
}

// The state Atlassian carries for us: where to hand the tokens, and to whom.
export async function signRelay(env, fields) {
  const body = b64url(encoder.encode(JSON.stringify(fields)));
  const signature = await crypto.subtle.sign("HMAC", await hmacKey(env), encoder.encode(body));
  return body + "." + b64url(signature);
}

export async function openRelay(env, relay, now) {
  const [body, signature] = String(relay || "").split(".");
  if (!body || !signature) return null;
  let valid = false;
  try {
    valid = await crypto.subtle.verify("HMAC", await hmacKey(env), unb64url(signature),
                                       encoder.encode(body));
  } catch (error) {
    return null;
  }
  if (!valid) return null;
  let fields;
  try {
    fields = JSON.parse(new TextDecoder().decode(unb64url(body)));
  } catch (error) {
    return null;
  }
  if (!fields || Math.abs(now - Number(fields.t)) > RELAY_SECONDS) return null;
  return fields;
}

function validPort(value) {
  const port = Number(value);
  return Number.isInteger(port) && port >= 1024 && port <= 65535 ? port : 0;
}

// Tokens sealed to the desktop's one-time key: broker key (65) | iv (12) | ciphertext.
export async function seal(desktopKey, state, payload) {
  const theirs = await crypto.subtle.importKey("raw", desktopKey,
    { name: "ECDH", namedCurve: "P-256" }, false, []);
  const mine = await crypto.subtle.generateKey({ name: "ECDH", namedCurve: "P-256" },
                                               true, ["deriveBits"]);
  const shared = await crypto.subtle.deriveBits({ name: "ECDH", public: theirs },
                                                mine.privateKey, 256);
  const material = await crypto.subtle.importKey("raw", shared, "HKDF", false, ["deriveKey"]);
  const key = await crypto.subtle.deriveKey(
    { name: "HKDF", hash: "SHA-256", salt: new Uint8Array(32), info: encoder.encode(INFO) },
    material, { name: "AES-GCM", length: 256 }, false, ["encrypt"]);
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv, additionalData: encoder.encode(state) }, key,
    encoder.encode(JSON.stringify(payload)));
  const raw = new Uint8Array(await crypto.subtle.exportKey("raw", mine.publicKey));
  const sealed = new Uint8Array(raw.length + iv.length + ciphertext.byteLength);
  sealed.set(raw, 0);
  sealed.set(iv, raw.length);
  sealed.set(new Uint8Array(ciphertext), raw.length + iv.length);
  return b64url(sealed);
}

async function tokenRequest(env, body) {
  const answer = await fetch(authBase(env) + "/oauth/token", {
    method: "POST", headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify(Object.assign({ client_id: env.ATLASSIAN_CLIENT_ID,
                                         client_secret: env.ATLASSIAN_CLIENT_SECRET }, body)),
  });
  let payload = {};
  try {
    payload = await answer.json();
  } catch (error) {
    payload = {};
  }
  return { status: answer.status, payload };
}

function tokens(payload) {
  // Only what the desktop needs - never anything Atlassian adds about the app.
  return { access_token: payload.access_token, refresh_token: payload.refresh_token || "",
           expires_in: payload.expires_in || 3600, scope: payload.scope || "" };
}

async function start(request, env, url) {
  const port = validPort(url.searchParams.get("port"));
  const state = url.searchParams.get("state") || "";
  const key = url.searchParams.get("key") || "";
  if (!port || state.length < 16 || state.length > 128 || !/^[A-Za-z0-9_-]+$/.test(key)) {
    return page(400, "This sign-in link is not one QAVector made. Start again from QAVector.");
  }
  if (unb64url(key).length !== 65) {
    return page(400, "This sign-in link is not one QAVector made. Start again from QAVector.");
  }
  const relay = await signRelay(env, { p: port, s: state, k: key,
                                       t: Math.floor(Date.now() / 1000) });
  const target = new URL(authBase(env) + "/authorize");
  target.search = new URLSearchParams({
    audience: "api.atlassian.com", client_id: env.ATLASSIAN_CLIENT_ID, scope: SCOPES,
    redirect_uri: publicUrl(env) + "/atlassian/callback", state: relay,
    response_type: "code", prompt: "consent" }).toString();
  return Response.redirect(target.toString(), 302);
}

async function callback(request, env, url) {
  const fields = await openRelay(env, url.searchParams.get("state"),
                                 Math.floor(Date.now() / 1000));
  if (!fields) {
    return page(400, "This sign-in has expired or was not started by QAVector. " +
                     "Start again from QAVector.");
  }
  const back = new URL("http://127.0.0.1:" + validPort(fields.p) + "/callback");
  back.searchParams.set("state", fields.s);
  const code = url.searchParams.get("code");
  if (!code) {
    back.searchParams.set("error", url.searchParams.get("error_description")
                                   || url.searchParams.get("error") || "not signed in");
    return Response.redirect(back.toString(), 302);
  }
  const { status, payload } = await tokenRequest(env, {
    grant_type: "authorization_code", code,
    redirect_uri: publicUrl(env) + "/atlassian/callback" });
  if (status !== 200 || !payload.access_token) {
    back.searchParams.set("error", payload.error_description || payload.error
                                   || "Atlassian refused the sign-in (" + status + ")");
    return Response.redirect(back.toString(), 302);
  }
  back.searchParams.set("sealed", await seal(unb64url(fields.k), fields.s, tokens(payload)));
  return Response.redirect(back.toString(), 302);
}

async function refresh(request, env) {
  let body = {};
  try {
    body = await request.json();
  } catch (error) {
    body = {};
  }
  if (!body || typeof body.refresh_token !== "string" || !body.refresh_token) {
    return json(400, { error: "invalid_request", error_description: "refresh_token is needed" });
  }
  const { status, payload } = await tokenRequest(env, {
    grant_type: "refresh_token", refresh_token: body.refresh_token });
  if (status !== 200 || !payload.access_token) {
    return json(status >= 400 && status < 500 ? status : 502, {
      error: payload.error || "refresh_failed",
      error_description: payload.error_description || "Atlassian refused the refresh" });
  }
  return json(200, tokens(payload));
}

export async function handle(request, env) {
  const url = new URL(request.url);
  if (request.method === "GET" && url.pathname === "/atlassian/start") {
    return start(request, env, url);
  }
  if (request.method === "GET" && url.pathname === "/atlassian/callback") {
    return callback(request, env, url);
  }
  if (request.method === "POST" && url.pathname === "/atlassian/refresh") {
    return refresh(request, env);
  }
  if (request.method === "GET" && url.pathname === "/") {
    return page(200, "QAVector sign-in service. Nothing to see here: QAVector opens it.");
  }
  return page(404, "Not here.");
}

export default { fetch: handle };
