// Prompt bar, answer panel, and model picker. No external scripts; runs offline.
(function () {
  "use strict";
  var form = document.getElementById("ask-form");
  var input = document.getElementById("ask-input");
  var panel = document.getElementById("answer-panel");
  if (!form || !panel) return;
  var qEl = document.getElementById("answer-q");
  var ansEl = document.getElementById("answer-text");
  var toolsEl = document.getElementById("answer-tools");

  function esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  // Tiny safe renderer: escape first, then allow **bold** and "* " / "- " bullet lines.
  function renderAnswer(text) {
    var out = [], inList = false;
    String(text).split("\n").forEach(function (raw) {
      var line = esc(raw).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
      var m = line.match(/^\s*[*-]\s+(.*)$/);
      if (m) {
        if (!inList) { out.push("<ul>"); inList = true; }
        out.push("<li>" + m[1] + "</li>");
      } else {
        if (inList) { out.push("</ul>"); inList = false; }
        out.push(line + "\n");
      }
    });
    if (inList) out.push("</ul>");
    return out.join("");
  }
  function renderTools(calls) {
    if (!calls || !calls.length) {
      return '<p class="dim">No tool was called for this question, so nothing here backs the answer.</p>';
    }
    return calls.map(function (c) {
      var args = JSON.stringify(c.args || {});
      return '<div class="tool"><div class="call">' + esc(c.tool) + " " + esc(args) + "</div>" +
        '<div class="head">' + esc(c.headline) + "</div>" +
        (c.lines && c.lines.length ? "<pre>" + esc(c.lines.join("\n")) + "</pre>" : "") + "</div>";
    }).join("");
  }
  function open() { panel.hidden = false; }
  function close() { panel.hidden = true; }
  function post(url, body) {
    return fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {})
    }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); });
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var q = input.value.trim();
    if (!q) return;
    qEl.textContent = q;
    ansEl.innerHTML = '<div class="busy">Asking the model…</div>';
    toolsEl.innerHTML = "";
    open();
    post("/api/ask", { question: q }).then(function (res) {
      if (!res.ok) {
        ansEl.innerHTML = '<p class="warn">' + esc(res.body.error || "Something went wrong.") + "</p>";
        return;
      }
      ansEl.innerHTML = renderAnswer(res.body.answer);
      toolsEl.innerHTML = renderTools(res.body.calls);
      input.value = "";
    }).catch(function () {
      ansEl.innerHTML = '<p class="warn">Could not reach the local server. Is it still running?</p>';
    });
  });

  document.getElementById("answer-close").addEventListener("click", close);
  document.getElementById("answer-reset").addEventListener("click", function () {
    post("/api/reset").then(function () { close(); input.focus(); });
  });
  document.addEventListener("keydown", function (e) {
    var typing = /^(INPUT|TEXTAREA|SELECT)$/.test((document.activeElement || {}).tagName || "");
    if (e.key === "/" && !typing) { e.preventDefault(); input.focus(); }
    if (e.key === "Escape") { if (!panel.hidden) close(); else input.blur(); }
  });

  // Models page: pick the model the prompt bar uses.
  var picker = document.getElementById("model-picker");
  if (picker) {
    picker.addEventListener("submit", function (e) {
      e.preventDefault();
      var chosen = picker.querySelector("input[name=model]:checked");
      var msg = document.getElementById("model-msg");
      if (!chosen) { msg.textContent = "Pick a model first."; return; }
      post("/api/model", { model: chosen.value }).then(function (res) {
        if (res.ok) {
          msg.textContent = "Using " + res.body.model + ".";
          var tag = document.getElementById("model-name");
          if (tag) tag.textContent = res.body.model;
        } else {
          msg.textContent = res.body.error || "Could not switch models.";
        }
      });
    });
  }
})();
