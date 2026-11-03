// Auto-login: fill the login form and submit it - again only if it was never answered.
//
// Submitting the REAL form keeps the CSRF token intact, so this needs to know
// nothing about the app's auth. It only acts while the login inputs are present,
// so after signing in it is a no-op.
//
// Credentials and selectors come from config.js, which the launcher generates per
// profile and loads first (same content_scripts entry -> same isolated world, so
// its globals are visible here). Nothing secret lives in this file.
(function () {
  // Written switched off by --no-autologin: the extension stays in the profile
  // (Chrome keeps loading it) and does nothing.
  if (AUTOLOGIN.enabled === false) return;
  var LOGIN = AUTOLOGIN.login, PASSWORD = AUTOLOGIN.password;
  var SEL = AUTOLOGIN.selectors;

  // How a login page that comes back is read. Rejected credentials come back as a
  // re-rendered login page showing the app's error (SEL.error) - a new document,
  // where this script runs again; resubmitting the same bad password would only
  // lock the account, so that stops it at once. A login page that comes back
  // WITHOUT the error was never answered - the server was restarting, or the
  // connection dropped mid-submit - so it is tried again, up to MAX_TRIES per tab
  // in case the error is not one the selector recognises. sessionStorage is
  // scoped to this tab + origin and survives those reloads.
  //
  // A rejection is remembered too: a clean login page reached later in the same tab
  // (a scenario going back to the app) must not get the bad password again.
  var TRIED = "autologin-tried";
  var MAX_TRIES = 3;
  function saved() {
    try {
      var state = JSON.parse(sessionStorage.getItem(TRIED) || "null");
      return state && state.login === LOGIN ? state : {count: 0};
    } catch (e) { return {count: 0}; }   // nothing saved, or what an older version wrote
  }
  function save(count, refused) {
    try {
      if (count) {
        sessionStorage.setItem(TRIED, JSON.stringify({login: LOGIN, count: count, rejected: !!refused}));
      } else { sessionStorage.removeItem(TRIED); }
    } catch (e) {}
  }
  function rejected() {
    try { return !!(SEL.error && document.querySelector(SEL.error)); } catch (e) { return false; }
  }
  function fill() {
    var l = document.querySelector(SEL.login);
    var p = document.querySelector(SEL.password);
    if (!l || !p) return false;
    var form = l.form || l.closest('form');
    if (!form) return false;
    var state = saved(), count = state.count;
    if (state.rejected || (count && rejected())) {
      save(count, true);
      console.warn("[auto-login] " + LOGIN + ": the login page came back with an error - "
                   + "not retrying, check the password in users.json or sign in by hand.");
      return true;  // handled: stop watching this page
    }
    if (count >= MAX_TRIES) {
      console.warn("[auto-login] " + LOGIN + ": submitted " + count + " times in this tab and "
                   + "the login page keeps coming back - not retrying, sign in by hand.");
      return true;
    }
    if (count) {
      console.info("[auto-login] " + LOGIN + ": the login page came back with no error - "
                   + "the last submit was not answered, trying again.");
    }
    save(count + 1);  // before submitting: the page that comes back must see this
    l.value = LOGIN; p.value = PASSWORD;
    l.dispatchEvent(new Event('input', {bubbles: true}));
    p.dispatchEvent(new Event('input', {bubbles: true}));
    var b = form.querySelector(SEL.submit) || form.querySelector('button');
    if (b) { b.click(); } else { form.submit(); }
    return true;
  }
  // No password field means we are past the login page (signed in), so forget the
  // attempt: a session that times out later in this tab still gets one fresh try.
  if (!document.querySelector(SEL.password)) save(0);
  if (fill()) return;
  var obs = new MutationObserver(function () { if (fill()) obs.disconnect(); });
  obs.observe(document.documentElement, {childList: true, subtree: true});
  setTimeout(function () { obs.disconnect(); }, 10000);
})();
