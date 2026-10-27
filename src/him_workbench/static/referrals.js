// Sends the referral forms as JSON (the app only accepts JSON posts), then reloads. Runs offline.
(function () {
  "use strict";
  document.querySelectorAll("form[data-ref]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var msg = form.querySelector(".ref-msg");
      var body = {};
      new FormData(form).forEach(function (v, k) { body[k] = v; });
      fetch(form.dataset.ref, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
      }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); })
        .then(function (res) {
          if (res.ok) { window.location.reload(); }
          else if (msg) { msg.textContent = res.body.error || "Could not save."; }
        })
        .catch(function () { if (msg) msg.textContent = "Could not reach the app."; });
    });
  });
})();
