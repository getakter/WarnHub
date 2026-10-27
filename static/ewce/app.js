/* WarnHub UI helpers: raw-JSON toggle, method reload, pattern syntax checks, confirmations. */
(function () {
  "use strict";

  // "Advanced: edit raw JSON" toggle on schema forms
  document.querySelectorAll(".schema-form").forEach(function (card) {
    var toggle = card.querySelector(".schema-raw-toggle");
    if (!toggle) return;
    var apply = function () {
      card.querySelector(".schema-fields").classList.toggle("d-none", toggle.checked);
      card.querySelector(".schema-raw").classList.toggle("d-none", !toggle.checked);
    };
    toggle.addEventListener("change", apply);
    apply();
  });

  // Changing the acquisition method reloads the page so the right plugin form is shown
  document.querySelectorAll("[data-reload-on-change]").forEach(function (select) {
    select.addEventListener("change", function () {
      if (!confirm("Switch method? Unsaved changes on this page will be lost.")) return;
      var url = new URL(window.location.href);
      url.searchParams.set(select.dataset.reloadOnChange, select.value);
      window.location.href = url.toString();
    });
  });

  // "Check" buttons next to regex / CSS selector / XPath fields
  document.querySelectorAll(".btn-check-pattern").forEach(function (button) {
    button.addEventListener("click", function () {
      var input = button.parentElement.querySelector("input, textarea");
      var result = button.closest(".mb-3").querySelector(".pattern-result");
      var body = new FormData();
      body.append("format", input.dataset.testFormat);
      body.append("value", input.value);
      if (input.tagName === "TEXTAREA") body.append("lines", "1");
      fetch(window.EWCE.validateUrl, {method: "POST", body: body, headers: {"X-CSRFToken": window.EWCE.csrf}})
        .then(function (r) { return r.json(); })
        .then(function (data) {
          result.className = "pattern-result small " + (data.ok ? "text-success" : "text-danger");
          result.textContent = data.ok ? "✓ " + data.message : "✗ " + data.error;
        })
        .catch(function (err) { result.textContent = "Check failed: " + err; });
    });
  });

  // Textareas with data-test-format (lists of patterns) get a Check button too
  document.querySelectorAll("textarea[data-test-format]").forEach(function (area) {
    if (area.parentElement.classList.contains("input-group")) return;
    var wrap = document.createElement("div");
    wrap.className = "input-group";
    area.parentNode.insertBefore(wrap, area);
    wrap.appendChild(area);
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn-outline-secondary btn-check-pattern-dyn";
    btn.innerHTML = '<i class="bi bi-check2-circle"></i> Check';
    wrap.appendChild(btn);
    var out = document.createElement("div");
    out.className = "pattern-result small";
    wrap.after(out);
    btn.addEventListener("click", function () {
      var body = new FormData();
      body.append("format", area.dataset.testFormat);
      body.append("value", area.value);
      body.append("lines", "1");
      fetch(window.EWCE.validateUrl, {method: "POST", body: body, headers: {"X-CSRFToken": window.EWCE.csrf}})
        .then(function (r) { return r.json(); })
        .then(function (data) {
          out.className = "pattern-result small " + (data.ok ? "text-success" : "text-danger");
          out.textContent = data.ok ? "✓ " + data.message : "✗ " + data.error;
        });
    });
  });

  // Confirm buttons
  document.querySelectorAll("[data-confirm]").forEach(function (el) {
    el.addEventListener("click", function (e) {
      if (!confirm(el.dataset.confirm)) e.preventDefault();
    });
  });

  // Keep the active tab in the URL hash
  var hash = window.location.hash;
  if (hash) {
    var trigger = document.querySelector('[data-bs-toggle="tab"][data-bs-target="' + hash + '"]');
    if (trigger && window.bootstrap) bootstrap.Tab.getOrCreateInstance(trigger).show();
  }
  document.querySelectorAll('[data-bs-toggle="tab"]').forEach(function (el) {
    el.addEventListener("shown.bs.tab", function () { history.replaceState(null, "", el.dataset.bsTarget); });
  });
})();
