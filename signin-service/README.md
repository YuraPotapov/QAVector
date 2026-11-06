# QAVector sign-in service

This is "Sign in with Atlassian" for QAVector, without a client secret on anybody's
desktop.

Atlassian's OAuth 2.0 (3LO) needs the app's client secret to turn a sign-in into
tokens, and it offers no official way around that for desktop programs. A secret
shipped inside QAVector would be readable by everyone who has QAVector. So the
secret lives in this small Cloudflare Worker, which a team deploys once. Every
QAVector built with the Worker's address then signs in with one click: the browser
opens, the person allows access, and they pick the site.

The Worker keeps nothing. It has three endpoints:

| Endpoint | What it does |
|---|---|
| `GET /atlassian/start?port&state&key` | Opened in the browser by QAVector. `key` is a one-time P-256 public key that QAVector made for this sign-in. Redirects to Atlassian's consent page, with a signed state that records where the tokens go. |
| `GET /atlassian/callback?code&state` | Atlassian comes back here. The Worker checks the signature and age of the state, exchanges the code using the secret, seals the tokens to QAVector's key (ECDH, HKDF-SHA256, AES-256-GCM, bound to the sign-in's state), and redirects to `http://127.0.0.1:<port>/callback`. Only the QAVector that made the key can open them. The browser's history holds ciphertext only. |
| `POST /atlassian/refresh` | Body `{"refresh_token"}`. A refresh needs the secret too, so it comes here. Answers with the new tokens only. |

## Deploy it

1. **Register the Atlassian app** at
   [developer.atlassian.com/console/myapps](https://developer.atlassian.com/console/myapps/).
   - Create an *OAuth 2.0 integration*.
   - *Permissions*: add the Jira API with `read:jira-work`, `write:jira-work` and
     `read:jira-user`.
   - *Authorization*: set the callback URL to `https://<the Worker's address>/atlassian/callback`.
   - *Distribution*: enable sharing. Without it, only the app's owner can sign in.
     No approval is needed. Until Atlassian reviews the app (through the
     Marketplace process), people see a note that it "has not yet been reviewed
     by Atlassian".
   - Copy the app's Client ID and Secret.

2. **Deploy the Worker** with Node and
   [Wrangler](https://developers.cloudflare.com/workers/wrangler/):

   ```sh
   cd signin-service
   cp wrangler.toml.example wrangler.toml    # set PUBLIC_URL; this file is git-ignored
   npx wrangler login
   npx wrangler secret put ATLASSIAN_CLIENT_ID
   npx wrangler secret put ATLASSIAN_CLIENT_SECRET
   npx wrangler secret put RELAY_KEY          # any long random string, e.g. openssl rand -hex 32
   npx wrangler deploy
   ```

   `PUBLIC_URL` must be the address Atlassian calls back, the same one as in
   step 1. Opening it in a browser shows a one-line page.

3. **Build QAVector with the address.** The address goes into the package, never
   into the code:

   ```sh
   QAVECTOR_SIGNIN_SERVICE=https://signin.yourcompany.example packaging/build_deb.sh
   ```

   On Windows, set the same variable before `packaging/build_exe.ps1`. To try it
   from a checkout, export `QAVECTOR_SIGNIN_SERVICE` before starting QAVector.
   In developer mode, *Add connection → Sign-in setup…* also takes a service
   address for one project.

Rotating `RELAY_KEY` only fails the sign-ins that are in progress at that moment.
Rotating the client secret needs no change in QAVector. Revoking it in the
console signs everyone out at their next refresh.

## Test it

```sh
node --test signin-service/test/
```

These run the Worker with a mocked Atlassian. QAVector's
`tests/test_integrations_signin_service.py` runs it under Node through
`local/serve.mjs`, in front of a loopback Atlassian, and signs in with QAVector's
own client. That test checks that the desktop opens what the Worker sealed, and
that the secret and the tokens never appear in an address the browser visits.
