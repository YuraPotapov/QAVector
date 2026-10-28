// Auto-login: fill the login form and submit it, once per tab.
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

  // Submit at most once per tab. Rejected credentials come back as a re-rendered
  // login page - a new document, where this script would run again and resubmit the
  // same bad password over and over. sessionStorage is scoped to this tab + origin
  // and survives that reload, so it remembers the attempt across it.
  var TRIED = "autologin-tried";
  function tried() {
    try { return sessionStorage.getItem(TRIED) === LOGIN; } catch (e) { return false; }
  }
  function setTried(on) {
    try {
      if (on) { sessionStorage.setItem(TRIED, LOGIN); } else { sessionStorage.removeItem(TRIED); }
    } catch (e) {}
  }
  function fill() {
    var l = document.querySelector(SEL.login);
    var p = document.querySelector(SEL.password);
    if (!l || !p) return false;
    var form = l.form || l.closest('form');
    if (!form) return false;
    if (tried()) {
      console.warn("[auto-login] " + LOGIN + ": already submitted once in this tab and the "
                   + "login page came back - not retrying, check the password in users.json "
                   + "or sign in by hand.");
      return true;  // handled: stop watching this page
    }
    setTried(true);  // before submitting: the reload after a failed login must see this
    l.value = LOGIN; p.value = PASSWORD;
    l.dispatchEvent(new Event('input', {bubbles: true}));
    p.dispatchEvent(new Event('input', {bubbles: true}));
    var b = form.querySelector(SEL.submit) || form.querySelector('button');
    if (b) { b.click(); } else { form.submit(); }
    return true;
  }
  // No password field means we are past the login page (signed in), so forget the
  // attempt: a session that times out later in this tab still gets one fresh try.
  if (!document.querySelector(SEL.password)) setTried(false);
  if (fill()) return;
  var obs = new MutationObserver(function () { if (fill()) obs.disconnect(); });
  obs.observe(document.documentElement, {childList: true, subtree: true});
  setTimeout(function () { obs.disconnect(); }, 10000);
})();
