// Behaviour is delegated from here so CSP can stay strict and htmx-swapped
// fragments keep working without inline handlers.

(function () {
  var REDUCE = window.matchMedia
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Animate removals without layout jumps.
  function collapse(el, done) {
    if (!el || el.dataset.qlCollapsing === "1") return;
    el.dataset.qlCollapsing = "1";
    if (REDUCE) { if (done) done(); return; }
    var h = el.getBoundingClientRect().height;
    el.style.overflow = "hidden";
    el.style.height = h + "px";
    el.style.transition =
      "height 280ms ease 40ms, opacity 200ms ease, " +
      "margin 280ms ease 40ms, padding 280ms ease 40ms";
    void el.offsetHeight;  // force reflow so the start values stick
    el.style.opacity = "0";
    el.style.height = "0px";
    el.style.marginTop = "0";
    el.style.marginBottom = "0";
    el.style.paddingTop = "0";
    el.style.paddingBottom = "0";
    if (done) setTimeout(done, 320);
  }

  function markSearchDownloadQueued(form) {
    if (!form || !form.matches || !form.matches("[data-search-download-form]")) return;
    var item = form.closest("[data-search-item]");
    var key = item && item.dataset.searchKey;
    var root = item && item.closest("[data-search-results-root]");
    var forms = [form];
    if (root && key) {
      forms = Array.prototype.map.call(root.querySelectorAll("[data-search-item]"), function (peer) {
        if (peer.dataset.searchKey !== key) return null;
        return peer.querySelector("[data-search-download-form]");
      }).filter(Boolean);
    }
    forms.forEach(function (f) {
      var b = f.querySelector("button[type=submit]");
      if (!b) return;
      b.disabled = true;
      if (b.classList.contains("ql-download-icon-button")) {
        b.setAttribute("aria-label", "Queued");
        b.setAttribute("title", "Queued");
        b.classList.add("is-queued");
      } else {
        b.textContent = "Queued";
      }
      b.classList.remove("ql-btn-primary");
      b.classList.add("is-disabled");
      if (!b.classList.contains("ql-download-icon-button")) {
        b.classList.add("ql-btn-secondary");
      }
    });
  }

  // Disable a search-result button only after a real queue success.
  document.addEventListener("htmx:afterRequest", function (evt) {
    var form = evt.target;
    if (!form || !form.matches || !form.matches("form[data-queue-button]")) return;
    if (!evt.detail || !evt.detail.successful) return;
    var xhr = evt.detail.xhr;
    if (!xhr || xhr.responseText.indexOf("ql-notice-success") === -1) return;
    markSearchDownloadQueued(form);
  });

  // Surface failed htmx requests instead of failing silently.
  document.addEventListener("htmx:responseError", function () {
    showToast("That didn't go through. Try again in a moment.", "error");
  });
  document.addEventListener("htmx:sendError", function () {
    showToast("Couldn't reach the server. Check your connection and try again.", "error");
  });

  // Animate a fully hidden artist group before htmx removes it.
  document.addEventListener("htmx:beforeSwap", function (evt) {
    var t = evt.detail && evt.detail.target;
    if (!t || !t.matches || !t.matches("details[data-artist]")) return;
    if ((evt.detail.serverResponse || "").trim() !== "") return;
    collapse(t);
    // Recount after the delayed removal has actually finished.
    setTimeout(function () {
      document.body.dispatchEvent(new CustomEvent("qlHidden"));
    }, 360);
  });

  // Plain POST forms need immediate feedback while the redirect starts.
  document.addEventListener("submit", function (evt) {
    var form = evt.target;
    if (!form || !form.matches || !form.matches("form[data-busy-submit]")) return;
    var b = (evt.submitter && evt.submitter.type === "submit" ? evt.submitter : null)
          || form.querySelector("button[type=submit]");
    if (!b || b.disabled) return;
    setTimeout(function () {
      b.dataset.busyHtml = b.innerHTML;
      b.disabled = true;
      b.classList.add("is-disabled");
      b.innerHTML =
        '<span class="ql-spinner ql-inline-spinner" aria-hidden="true"></span> Starting...';
    }, 0);
  });

  // BFCache can restore a disabled submit button.
  window.addEventListener("pageshow", function (evt) {
    if (!evt.persisted) return;
    document.querySelectorAll("form[data-busy-submit] button[type=submit]").forEach(function (b) {
      if (b.dataset.busyHtml == null) return;
      b.disabled = false;
      b.classList.remove("is-disabled");
      b.innerHTML = b.dataset.busyHtml;
      delete b.dataset.busyHtml;
    });
  });

  // Shared confirm prompt for destructive submits and links.
  document.addEventListener("click", function (evt) {
    var el = evt.target.closest && evt.target.closest("[data-confirm]");
    if (!el) return;
    if (!window.confirm(el.getAttribute("data-confirm"))) {
      evt.preventDefault();
      evt.stopPropagation();
    }
  });

  // Lock-busy retry button.
  document.addEventListener("click", function (evt) {
    if (evt.target.closest && evt.target.closest("[data-reload]")) {
      evt.preventDefault();
      location.reload();
    }
  });

  // Show/hide password and token fields.
  document.addEventListener("click", function (evt) {
    var btn = evt.target.closest && evt.target.closest("[data-toggle-password]");
    if (!btn) return;
    var f = document.getElementById(btn.getAttribute("data-toggle-password"));
    if (!f) return;
    f.type = f.type === "password" ? "text" : "password";
    var isVisible = f.type === "text";
    var label = isVisible ? btn.dataset.hideLabel : btn.dataset.showLabel;
    var labelNode = btn.querySelector("[data-toggle-password-label]");
    var showIcon = btn.querySelector("[data-icon-show]");
    var hideIcon = btn.querySelector("[data-icon-hide]");
    btn.setAttribute("aria-pressed", isVisible ? "true" : "false");
    if (label) {
      btn.setAttribute("aria-label", label);
      btn.setAttribute("title", label);
      if (labelNode) labelNode.textContent = label;
    }
    if (showIcon) showIcon.classList.toggle("hidden", isVisible);
    if (hideIcon) hideIcon.classList.toggle("hidden", !isVisible);
  });

  // Keep the search field metadata in step with the search mode.
  document.addEventListener("change", function (evt) {
    var radio = evt.target;
    if (!radio.matches || !radio.matches('input[name="kind"]')) return;
    var form = radio.closest("form");
    var q = form && form.querySelector('input[name="q"]');
    if (!q) return;
    var placeholder = "Album title";
    if (radio.value === "artist") placeholder = "Artist name";
    if (radio.value === "track") placeholder = "Track title";
    q.setAttribute("placeholder", placeholder);
    q.setAttribute("aria-label", placeholder);
  });

  // Search result version lists use a real button so the row itself does not
  // mix "expand" and "download" tap targets.
  document.addEventListener("click", function (evt) {
    var btn = evt.target.closest && evt.target.closest("[data-version-toggle]");
    if (!btn) return;
    var panel = document.getElementById(btn.getAttribute("aria-controls"));
    if (!panel) return;
    var open = btn.getAttribute("aria-expanded") === "true";
    var nextOpen = !open;
    btn.setAttribute("aria-expanded", nextOpen ? "true" : "false");
    panel.classList.toggle("hidden", !nextOpen);
    var label = btn.querySelector("[data-version-label]");
    if (label) {
      label.textContent = nextOpen
        ? (btn.dataset.hideLabel || "Hide versions")
        : (btn.dataset.showLabel || "Show versions");
    }
  });

  // Light/dark toggle.
  document.addEventListener("click", function (evt) {
    var btn = evt.target.closest && evt.target.closest("#theme-toggle, [data-theme-toggle]");
    if (!btn) return;
    var next = document.documentElement.getAttribute("data-theme") === "winter"
      ? "night" : "winter";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("ql-theme", next); } catch (e) { /* private mode */ }
    var m = document.querySelector('meta[name="theme-color"]');
    if (m) m.setAttribute("content", next === "winter" ? "#f7f2e8" : "#100d09");
  });

  // Keep the mobile drawer aria-expanded in step with the actual open state.
  function drawerIsOpen(dd) {
    return dd.classList.contains("ql-mobile-drawer-open") || dd.hasAttribute("open");
  }
  function syncDrawerExpanded(dd) {
    var btn = dd.querySelector("[aria-expanded]");
    if (btn) btn.setAttribute("aria-expanded", drawerIsOpen(dd).toString());
  }
  document.addEventListener("focusin", function (evt) {
    var dd = evt.target.closest && evt.target.closest(".ql-mobile-drawer");
    if (dd) syncDrawerExpanded(dd);
  });
  document.addEventListener("focusout", function (evt) {
    var dd = evt.target.closest && evt.target.closest(".ql-mobile-drawer");
    if (!dd) return;
    // focusout fires before the new focus settles, so re-check next tick.
    setTimeout(function () { syncDrawerExpanded(dd); }, 0);
  });

  // Tap-driven drawer support for mobile browsers.
  function closeDrawer(dd) {
    dd.classList.remove("ql-mobile-drawer-open");
    dd.removeAttribute("open");
    if (dd.contains(document.activeElement) && document.activeElement.blur) {
      document.activeElement.blur();
    }
    var b = dd.querySelector("[aria-expanded]");
    if (b) b.setAttribute("aria-expanded", "false");
  }
  window.qlCloseDropdowns = function (root) {
    var scope = root || document;
    var nodes = [];
    if (scope.matches && scope.matches(".ql-mobile-drawer")) nodes.push(scope);
    scope.querySelectorAll(".ql-mobile-drawer.ql-mobile-drawer-open, details.ql-mobile-drawer[open]")
      .forEach(function (dd) {
        if (nodes.indexOf(dd) === -1) nodes.push(dd);
      });
    nodes.forEach(closeDrawer);
  };
  document.addEventListener("click", function (evt) {
    var btn = evt.target.closest && evt.target.closest("[aria-haspopup='menu']");
    var trigger = (btn && btn.closest(".ql-mobile-drawer")) ? btn : null;
    var triggerDd = trigger ? trigger.closest(".ql-mobile-drawer") : null;
    // Close outside taps and other open drawers.
    document.querySelectorAll(".ql-mobile-drawer.ql-mobile-drawer-open").forEach(function (o) {
      if (o !== triggerDd) closeDrawer(o);
    });
    if (!triggerDd) return;
    evt.preventDefault();
    if (drawerIsOpen(triggerDd)) {
      closeDrawer(triggerDd);
    } else {
      triggerDd.classList.add("ql-mobile-drawer-open");
      triggerDd.setAttribute("open", "");
      trigger.setAttribute("aria-expanded", "true");
    }
  });
  document.addEventListener("click", function (evt) {
    var btn = evt.target.closest && evt.target.closest("[data-close-mobile-menu]");
    if (!btn) return;
    var dd = btn.closest(".ql-mobile-drawer");
    if (dd) closeDrawer(dd);
  });

  // One-shot flash flags should not stay in the URL after first paint.
  var FLASH_PARAMS = ["approved", "stale", "saved", "queued", "connected",
                      "unverified", "mode", "error"];
  function cleanFlashUrl() {
    if (typeof URL !== "function" || !history.replaceState) return;
    try {
      var url = new URL(location.href);
      var touched = false;
      FLASH_PARAMS.forEach(function (k) {
        if (url.searchParams.has(k)) { url.searchParams.delete(k); touched = true; }
      });
      if (touched) {
        var qs = url.searchParams.toString();
        history.replaceState(null, "", url.pathname + (qs ? "?" + qs : "") + url.hash);
      }
    } catch (e) { /* malformed URL — leave it alone */ }
  }
  function fade(el) { collapse(el, function () { if (el.parentNode) el.remove(); }); }
  function autoDismissFlashes() {
    // Keep warnings/errors visible; auto-clear low-risk notices and toasts.
    document.querySelectorAll("[data-flash].ql-notice-success, [data-flash].ql-notice-info")
      .forEach(function (el) { setTimeout(function () { fade(el); }, 6000); });
    document.querySelectorAll("#download-toast [data-flash]")
      .forEach(function (el) { setTimeout(function () { fade(el); }, 8000); });
  }
  // Clear stale flashes after a job moves on.
  window.qlDismissAllFlashes = function () {
    document.querySelectorAll("[data-flash]").forEach(fade);
  };

  // Programmatic toast for async failures and review actions.
  function showToast(message, kind) {
    var host = document.getElementById("download-toast");
    if (!host) return;
    var el = document.createElement("div");
    var noticeKind = kind || "info";
    el.className = "ql-notice ql-notice-" + noticeKind;
    el.setAttribute("data-flash", "");
    el.setAttribute("data-flash-kind", noticeKind);
    el.setAttribute("role", "status");
    var span = document.createElement("span");
    span.textContent = message;
    el.appendChild(span);
    host.appendChild(el);
    setTimeout(function () { fade(el); }, 8000);
  }
  window.qlShowToast = showToast;

  function initCoverFallbacks() {
    document.querySelectorAll("img.ql-cover, img.ql-grid-cover, img.ql-result-art").forEach(function (img) {
      if (img.dataset.coverFallbackWired === "1") return;
      img.dataset.coverFallbackWired = "1";
      img.addEventListener("error", function () {
        var fallback = document.createElement("span");
        fallback.className = img.className + " ql-cover-placeholder";
        fallback.setAttribute("aria-hidden", "true");
        img.replaceWith(fallback);
      }, { once: true });
    });
  }

  function initSearchResults() {
    document.querySelectorAll("[data-search-results-root]").forEach(function (root) {
      if (root.dataset.searchWired === "1") return;
      root.dataset.searchWired = "1";
      var selected = {};
      var bulkBar = root.querySelector("[data-search-bulk-bar]");
      var selectedCount = root.querySelector("[data-search-selected-count]");
      var selectAll = root.querySelector("[data-search-select-all]");
      var bulkButton = root.querySelector("[data-search-bulk-download]");
      var clearButton = root.querySelector("[data-search-clear-selection]");
      var hideOwnedButton = root.querySelector("[data-search-hide-owned]");

      function selectedKeys() {
        return Object.keys(selected).filter(function (k) { return selected[k]; });
      }
      function boxesFor(key) {
        return Array.prototype.filter.call(root.querySelectorAll("[data-search-select]"),
          function (cb) { return cb.dataset.searchKey === key; });
      }
      function visibleItem(item) {
        return item && item.offsetParent !== null;
      }
      function syncBoxes() {
        root.querySelectorAll("[data-search-select]").forEach(function (cb) {
          cb.checked = !!selected[cb.dataset.searchKey];
        });
        var keys = selectedKeys();
        if (bulkBar) bulkBar.classList.toggle("hidden", keys.length === 0);
        if (selectedCount) {
          selectedCount.textContent = keys.length + " selected";
        }
        if (selectAll) {
          var selectable = Array.prototype.filter.call(root.querySelectorAll("[data-search-select]"),
            function (cb) { return visibleItem(cb.closest("[data-search-item]")); });
          var selectableKeys = {};
          selectable.forEach(function (cb) { selectableKeys[cb.dataset.searchKey] = true; });
          var allKeys = Object.keys(selectableKeys);
          var picked = allKeys.filter(function (k) { return selected[k]; }).length;
          selectAll.checked = allKeys.length > 0 && picked === allKeys.length;
          selectAll.indeterminate = picked > 0 && picked < allKeys.length;
        }
      }
      function setView(name) {
        root.querySelectorAll("[data-search-view-panel]").forEach(function (panel) {
          panel.classList.toggle("hidden", panel.dataset.searchViewPanel !== name);
        });
        root.querySelectorAll("[data-search-view]").forEach(function (btn) {
          btn.classList.toggle("is-active", btn.dataset.searchView === name);
        });
      }
      root.addEventListener("change", function (evt) {
        var cb = evt.target.closest && evt.target.closest("[data-search-select]");
        if (cb) {
          selected[cb.dataset.searchKey] = cb.checked;
          boxesFor(cb.dataset.searchKey).forEach(function (peer) {
            if (peer !== cb) peer.checked = cb.checked;
          });
          syncBoxes();
          return;
        }
        if (evt.target.closest && evt.target.closest("[data-search-select-all]")) {
          var on = evt.target.checked;
          root.querySelectorAll("[data-search-select]").forEach(function (box) {
            if (!visibleItem(box.closest("[data-search-item]"))) return;
            selected[box.dataset.searchKey] = on;
          });
          syncBoxes();
        }
      });
      root.addEventListener("click", function (evt) {
        var view = evt.target.closest && evt.target.closest("[data-search-view]");
        if (view) {
          evt.preventDefault();
          setView(view.dataset.searchView || "table");
          syncBoxes();
          return;
        }
        if (hideOwnedButton && evt.target.closest("[data-search-hide-owned]")) {
          evt.preventDefault();
          var next = hideOwnedButton.getAttribute("aria-pressed") !== "true";
          hideOwnedButton.setAttribute("aria-pressed", next ? "true" : "false");
          root.classList.toggle("is-filtering-owned", next);
          syncBoxes();
          return;
        }
        if (clearButton && evt.target.closest("[data-search-clear-selection]")) {
          evt.preventDefault();
          selected = {};
          syncBoxes();
          return;
        }
        if (bulkButton && evt.target.closest("[data-search-bulk-download]")) {
          evt.preventDefault();
          bulkDownload();
        }
      });
      function csrf() {
        var m = document.querySelector('meta[name="csrf-token"]');
        return m ? m.content : "";
      }
      function firstFormForKey(key) {
        var items = Array.prototype.filter.call(root.querySelectorAll("[data-search-item]"),
          function (item) { return item.dataset.searchKey === key; });
        for (var i = 0; i < items.length; i++) {
          var form = items[i].querySelector("[data-search-download-form]");
          if (form) return form;
        }
        return null;
      }
      function postForm(form) {
        return fetch(form.action || "/download", {
          method: "POST",
          headers: {
            "HX-Request": "true",
            "X-CSRF-Token": csrf(),
          },
          body: new FormData(form),
        }).then(function (r) {
          return r.text().then(function (text) {
            return { ok: r.ok, text: text };
          });
        });
      }
      function bulkDownload() {
        var keys = selectedKeys();
        if (!keys.length || bulkButton.disabled) return;
        var forms = keys.map(firstFormForKey).filter(Boolean);
        if (!forms.length) return;
        var original = bulkButton.textContent;
        bulkButton.disabled = true;
        bulkButton.textContent = "Queueing...";
        var queued = 0;
        var skipped = 0;
        var failed = 0;
        var chain = Promise.resolve();
        forms.forEach(function (form) {
          chain = chain.then(function () {
            return postForm(form).then(function (res) {
              if (!res.ok || res.text.indexOf("ql-notice-error") >= 0) failed += 1;
              else if (res.text.indexOf("ql-notice-warning") >= 0) {
                skipped += 1;
                markSearchDownloadQueued(form);
              } else {
                queued += 1;
                markSearchDownloadQueued(form);
              }
            }).catch(function () { failed += 1; });
          });
        });
        chain.then(function () {
          bulkButton.disabled = false;
          bulkButton.textContent = original;
          selected = {};
          syncBoxes();
          var parts = [];
          if (queued) parts.push(queued + " queued");
          if (skipped) parts.push(skipped + " already queued");
          if (failed) parts.push(failed + " failed");
          showToast(parts.length ? parts.join(", ") + "." : "Nothing queued.", failed ? "error" : "success");
        });
      }
      setView("table");
      syncBoxes();
    });
  }

  // Live job and queue streams.

  function unitSuffix(p) {
    return p.unit ? " " + p.unit + (p.total === 1 ? "" : "s") : "";
  }

  function fmtProgress(p, verb, withItem) {
    if (withItem === undefined) withItem = true;
    var item = (withItem && p.item) ? " · " + p.item : "";
    if (p.total > 0) {
      return verb + " " + p.current + " / " + p.total + unitSuffix(p) + item;
    }
    if (p.phase) return p.phase + item;
    return "";
  }

  function jobActivityFallback(kind, status) {
    if (status === "scanning") return "Scanning";
    if (kind === "upgrade") return "Upgrading";
    if (kind === "downsample") return "Downsampling";
    if (kind === "repair") return "Repairing";
    if (kind === "lyrics") return "Fetching lyrics";
    if (kind === "migration") return "Migrating";
    return (kind === "download" || ["album", "library", "new_releases"].indexOf(kind) >= 0)
      ? "Downloading" : "Running";
  }

  // Dashboard and queue progress cards.
  function wireStreamCard(card) {
    if (card.dataset.sseWired === "1") return;
    card.dataset.sseWired = "1";
    var id = card.dataset.jobId;
    var surface = card.dataset.jobSurface;   // "dashboard" | "queue"
    var status = card.dataset.jobStatus;
    var kind = card.dataset.jobKind || "";
    var runFallback = jobActivityFallback(kind, status);
    if (!id || !surface) return;
    // Once a queued card starts, refresh it into the live running layout.
    var flippedFromPending = false;
    var progId = (surface === "dashboard" ? "dash-prog-" : "card-prog-") + id;
    var containerId = surface === "dashboard" ? "dashboard-work"
                    : "queue-body";
    var reconnect = surface === "dashboard" ? document.getElementById("dash-reconnect-" + id) : null;
    var src = new EventSource("/api/jobs/" + id + "/stream");
    function shut() {
      try { src.close(); } catch (e) {}
      document.removeEventListener("htmx:beforeSwap", onSwap);
    }
    function onSwap(e) {
      if (!e.detail || !e.detail.target) return;
      if (e.detail.target.id === containerId
          || (surface === "dashboard" && e.detail.target.id === "dashboard-active")) shut();
    }
    document.addEventListener("htmx:beforeSwap", onSwap);
    if (reconnect) {
      src.onopen = function () {
        reconnect.classList.add("hidden");
      };
      src.onerror = function () {
        if (src.readyState === EventSource.CLOSED) {
          reconnect.textContent = "Connection lost. Reload to see the latest.";
          reconnect.classList.remove("is-warning");
          reconnect.classList.add("is-error");
          reconnect.classList.remove("hidden");
        } else {
          reconnect.classList.remove("is-error");
          reconnect.classList.add("is-warning");
          reconnect.classList.remove("hidden");
        }
      };
    }
    src.addEventListener("progress", function (e) {
      var p; try { p = JSON.parse(e.data); } catch (_) { return; }
      // Re-render once a pending queue card starts.
      if (surface === "queue" && status === "pending" && !flippedFromPending) {
        flippedFromPending = true;
        shut();
        if (window.htmx) {
          window.htmx.ajax("GET", "/queue",
            { target: "#queue-body", swap: "outerHTML", select: "#queue-body" });
        }
        return;
      }
      var el = document.getElementById(progId);
      if (!el) return;
      var txt = fmtProgress(p, status === "scanning" ? "Scanning" : (p.phase || runFallback),
                            surface !== "dashboard");
      if (txt) el.textContent = txt;
      var bar = document.getElementById("card-bar-" + id);
      if (bar && p.total > 0) {
        var fill = bar.querySelector("i");
        if (fill) fill.style.width = Math.min(100, (p.current / p.total) * 100) + "%";
      }
    });
    src.addEventListener("done", function (e) {
      shut();
      var endStatus = (e && e.data) ? ("" + e.data).trim() : "";
      if (endStatus === "failed") {
        var t = card.querySelector('a[href^="/jobs/"]');
        var label = ((t && t.textContent) || "A job").replace(/\s+/g, " ").trim();
        showToast(label + " failed. Open History for details.", "error");
      }
      if (surface === "dashboard") {
        if (window.htmx) {
          window.htmx.ajax("GET", "/",
            { target: "#dashboard-work", swap: "outerHTML", select: "#dashboard-work" });
        } else { location.reload(); }
      } else if (window.htmx) {
        window.htmx.ajax("GET", "/queue",
          { target: "#queue-body", swap: "outerHTML", select: "#queue-body" });
      } else { location.reload(); }
    });
  }

  function initStreamCards() {
    document.querySelectorAll("[data-job-card]").forEach(wireStreamCard);
  }

  // Single-job progress and review pages.
  function initJobContent() {
    var jc = document.getElementById("job-content");
    if (!jc || jc.dataset.jobWired === "1") return;
    var view = jc.dataset.jobView;
    var id = jc.dataset.jobId;
    if (!view || !id) return;
    jc.dataset.jobWired = "1";
    if (view === "review") wireReview(id);
    else if (view === "progress") wireProgress(id);
  }

  function wireProgress(id) {
    var logEl = document.getElementById("log");
    var card = document.getElementById("progress-card");
    var label = document.getElementById("prog-label");
    var count = document.getElementById("prog-count");
    var bar = document.getElementById("prog-bar");
    var item = document.getElementById("prog-item");
    var activity = document.getElementById("job-activity");
    var foundEl = document.getElementById("scan-found");
    var reconnect = document.getElementById("sse-reconnect");
    var jc = document.getElementById("job-content");
    var foundSingular = (jc && jc.dataset.progressItemSingular) || "album";
    var foundPlural = (jc && jc.dataset.progressItemPlural) || foundSingular + "s";
    // Server-relative elapsed clock for long-running scans.
    var elapsedEl = document.getElementById("scan-elapsed");
    var elapsedTimer = null;
    if (elapsedEl && elapsedEl.dataset.start) {
      var serverStart = parseFloat(elapsedEl.dataset.start);
      var serverNow = parseFloat(elapsedEl.dataset.now);
      var elapsedAtLoad = (isFinite(serverNow) ? serverNow : Date.now() / 1000) - serverStart;
      if (!(elapsedAtLoad >= 0)) elapsedAtLoad = 0;
      var clientBase = Date.now();
      var tickElapsed = function () {
        if (!document.body.contains(elapsedEl)) { if (elapsedTimer) clearInterval(elapsedTimer); return; }
        var secs = Math.max(0, Math.floor(elapsedAtLoad + (Date.now() - clientBase) / 1000));
        var mm = Math.floor(secs / 60), ss = secs % 60;
        elapsedEl.textContent = "· " + mm + ":" + (ss < 10 ? "0" : "") + ss + " elapsed";
      };
      tickElapsed();
      elapsedTimer = setInterval(tickElapsed, 1000);
    }
    var baseTitle = document.title;
    var titleSet = false;
    var foundAlbums = 0, foundArtists = 0;
    function plural(n, w) { return n + " " + w + (n === 1 ? "" : "s"); }
    function countLabel(n, one, many) { return n + " " + (n === 1 ? one : many); }
    function showFound() {
      if (!foundEl) return;
      foundEl.textContent = foundAlbums
        ? "Found " + countLabel(foundAlbums, foundSingular, foundPlural) + " across " + plural(foundArtists, "artist") + " so far…"
        : "";
    }
    var waitNote = document.getElementById("queue-wait-note");
    function clearQueuedState() {
      if (waitNote) { waitNote.classList.add("hidden"); waitNote = null; }
      if (activity && activity.textContent === "Queued") activity.textContent = "Scanning";
    }
    var src = new EventSource("/api/jobs/" + id + "/stream");
    var opened = false;
    src.onmessage = function (e) {
      if (!titleSet) { document.title = "▶ " + baseTitle; titleSet = true; }
      clearQueuedState();
      if (logEl) { logEl.appendChild(document.createTextNode(e.data + "\n")); logEl.scrollTop = logEl.scrollHeight; }
    };
    src.onopen = function () {
      if (reconnect) reconnect.classList.add("hidden");
      // Reconnect replays recent lines, so reset before appending them again.
      if (opened) {
        if (logEl) logEl.textContent = "";
        foundAlbums = 0;
        foundArtists = 0;
      }
      opened = true;
    };
    src.onerror = function () {
      if (src.readyState === EventSource.CLOSED) {
        if (elapsedTimer) { clearInterval(elapsedTimer); elapsedTimer = null; }
        if (reconnect) {
          reconnect.textContent = "Connection lost. Reload to see the latest.";
          reconnect.classList.remove("is-warning");
          reconnect.classList.add("is-error");
          reconnect.classList.remove("hidden");
        }
      } else if (reconnect) {
        reconnect.classList.remove("is-error");
        reconnect.classList.add("is-warning");
        reconnect.classList.remove("hidden");
      }
    };
    src.addEventListener("progress", function (e) {
      var p; try { p = JSON.parse(e.data); } catch (_) { return; }
      clearQueuedState();
      if (window.qlDismissAllFlashes) window.qlDismissAllFlashes();
      if (activity && p.phase && activity.textContent !== p.phase) activity.textContent = p.phase;
      if (card) card.classList.remove("hidden");
      if (label) label.textContent = p.phase || (activity && activity.textContent !== "Queued" ? activity.textContent : "Running");
      var ct = p.total > 0 ? p.current + " / " + p.total + unitSuffix(p) : (p.current ? String(p.current) : "");
      if (count) count.textContent = ct;
      if (bar) { if (p.total > 0) { bar.max = 100; bar.value = Math.round(p.current / p.total * 100); } else { bar.removeAttribute("value"); } }
      if (item) item.textContent = p.item || "";
      if (typeof p.found === "number" && p.found > foundAlbums) foundAlbums = p.found;
      if (p.hit && p.hit.albums > 0) { foundArtists += 1; showFound(); }
      else if (typeof p.found === "number") showFound();
    });
    src.addEventListener("done", function () {
      src.close();
      if (elapsedTimer) clearInterval(elapsedTimer);
      document.title = baseTitle;
      if (window.qlDismissAllFlashes) window.qlDismissAllFlashes();
      if (window.htmx) {
        var jc = document.getElementById("job-content");
        var embedded = jc && jc.dataset.embedded ? "?embedded=1" : "";
        window.htmx.ajax("GET", "/jobs/" + id + "/content" + embedded, { target: "#job-content", swap: "outerHTML" });
      } else { location.reload(); }
    });
  }

  // Paginated, server-backed review. Selection is saved server-side.
  function wireReview(id) {
    var cont = document.getElementById("review-candidates");
    var form = document.getElementById("review-form");
    if (!cont || !form) return;
    var submit = document.getElementById("review-submit");
    var summaryRow = document.getElementById("review-summary-row");
    var summaryCount = document.querySelector("#review-candidates [data-summary-count]");
    var emptyBox = document.getElementById("review-empty");
    var filterRow = document.getElementById("review-filter-row");
    var filterInput = document.getElementById("review-filter");
    var dsTotal = document.querySelector("[data-downsample-total]");
    var dismissRest = document.getElementById("review-dismiss-rest");
    var reviewKind = form.getAttribute("data-review-kind") || "";
    var isDownsampleReview = reviewKind === "downsample";
    var reviewItemSingular = cont.dataset.reviewItemSingular || "album";
    var reviewItemPlural = cont.dataset.reviewItemPlural || "albums";
    var dismissItemSingular = cont.dataset.reviewDismissSingular || "album";
    var dismissItemPlural = cont.dataset.reviewDismissPlural || "albums";

    function plural(n, w) { return n + " " + w + (n === 1 ? "" : "s"); }
    function countLabel(n, one, many) { return n + " " + (n === 1 ? one : many); }
    function dismissRestLabel(rest) {
      return (isDownsampleReview ? "Keep hi-res" : "Dismiss unselected") + " (" + rest + ")";
    }
    function artistDismissLabel(picked) {
      if (isDownsampleReview) return "Keep hi-res";
      return "Dismiss unselected";
    }
    function dismissConfirm(rest) {
      if (isDownsampleReview) {
        return "Keep " + countLabel(rest, "unselected album", "unselected albums") + " hi-res? You can downsample later.";
      }
      return "Dismiss " + countLabel(rest, "unselected " + dismissItemSingular, "unselected " + dismissItemPlural) + "? You can show them again later.";
    }
    function dismissBusyLabel() { return isDownsampleReview ? "Keeping…" : "Dismissing…"; }
    function dismissToast(count) {
      return isDownsampleReview
        ? "Kept " + plural(count, "album") + " hi-res."
        : "Dismissed " + countLabel(count, dismissItemSingular, dismissItemPlural) + ".";
    }
    function csrf() {
      var m = document.querySelector('meta[name="csrf-token"]');
      return m ? m.content : "";
    }
    function pageBox() { return document.getElementById("review-groups"); }
    function curPage() { var g = pageBox(); return g ? parseInt(g.dataset.page || "1", 10) : 1; }
    function curQuery() { return (filterInput && filterInput.value || "").trim(); }
    function curTab() {
      var b = cont.querySelector("[data-review-tab].is-active");
      return b ? b.getAttribute("data-review-tab") : "";
    }
    // Last server counts payload. Seeded from the initial render's data
    // attributes so tab switches can re-scope the bulk bar without a request.
    var lastCounts = {
      total: parseInt(cont.dataset.reviewTotal || "0", 10),
      selected: parseInt(cont.dataset.reviewSelected || "0", 10),
    };
    if (cont.dataset.reviewMissingTotal !== undefined) {
      lastCounts.missing_total = parseInt(cont.dataset.reviewMissingTotal || "0", 10);
      lastCounts.missing_selected = parseInt(cont.dataset.reviewMissingSelected || "0", 10);
      lastCounts.gap_total = parseInt(cont.dataset.reviewGapTotal || "0", 10);
      lastCounts.gap_selected = parseInt(cont.dataset.reviewGapSelected || "0", 10);
    }
    // The active tab's share of the counts — the bulk bar acts on the active
    // tab only, so it counts the active tab only. Untabbed reviews fall back
    // to the whole set.
    function tabCounts(c) {
      var tab = curTab();
      if (!tab || !c || c.missing_total === undefined) {
        return { total: c ? c.total : 0, selected: c ? c.selected : 0 };
      }
      return tab === "gaps"
        ? { total: c.gap_total, selected: c.gap_selected }
        : { total: c.missing_total, selected: c.missing_selected };
    }

    // Counts come from the server because the DOM only holds one page. The
    // bulk bar (submit + dismiss-unselected) is scoped to the active tab.
    function applyCounts(c) {
      if (!c) return;
      lastCounts = c;
      var tc = tabCounts(c);
      if (submit) {
        submit.disabled = tc.selected === 0;
        submit.textContent = tc.selected
          ? (submit.dataset.reviewVerb || "Download") + " " + tc.selected + " selected"
          : (submit.dataset.emptyLabel || "Select " + reviewItemPlural);
      }
      if (summaryCount) {
        summaryCount.textContent = countLabel(c.total, reviewItemSingular, reviewItemPlural) + " across " + plural(c.artists, "artist");
      }
      if (summaryRow) summaryRow.classList.toggle("hidden", c.total === 0);
      if (emptyBox) emptyBox.classList.toggle("hidden", c.total > 0);
      var tabsNav = cont.querySelector("[data-review-tabs]");
      if (tabsNav) tabsNav.classList.toggle("hidden", c.total === 0);
      if (filterRow && !curQuery()) filterRow.classList.toggle("hidden", c.artists < 4);
      if (dsTotal) {
        dsTotal.textContent = c.reclaimable_label
          ? " · ~" + c.reclaimable_label + " reclaimable" : "";
      }
      cont.dataset.reviewTotal = c.total;
      cont.dataset.reviewSelected = c.selected;
      var master = cont.querySelector("[data-select-master]");
      if (master) {
        master.checked = tc.total > 0 && tc.selected >= tc.total;
        master.indeterminate = tc.selected > 0 && tc.selected < tc.total;
      }
      // A library review's tabs carry per-tab totals; keep the tab count
      // chips honest as hides and dismissals shrink the sets.
      if (c.missing_total !== undefined) {
        var mc = cont.querySelector('[data-tab-count="missing"]');
        var gc = cont.querySelector('[data-tab-count="gaps"]');
        if (mc) mc.textContent = c.missing_total;
        if (gc) gc.textContent = c.gap_total;
      }
      updateDismissRest();
    }

    function updateDismissRest() {
      if (!dismissRest) return;
      var tc = tabCounts(lastCounts);
      var rest = tc.total - tc.selected;
      dismissRest.classList.toggle("hidden", rest <= 0);
      dismissRest.textContent = dismissRestLabel(rest);
    }

    function post(url, body) {
      return fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          "X-CSRF-Token": csrf(),
        },
        body: body,
      });
    }

    // Save one checkbox to the server, then refresh counts from its response.
    function saveTick(cb) {
      var previous = !cb.checked;
      function revert() {
        cb.checked = previous;
        cb.style.outline = "2px solid #ef4444";
        setTimeout(function () { cb.style.outline = ""; }, 1500);
      }
      // Serialize per-box saves so the server matches the checkbox.
      cb.disabled = true;
      var body = "cid=" + encodeURIComponent(cb.value) + "&checked=" + (cb.checked ? "1" : "0");
      post("/jobs/" + id + "/select", body)
        .then(function (r) { return r.ok ? r.json() : Promise.reject(); })
        .then(function (c) { cb.disabled = false; if (c) applyCounts(c); updateHideLabels(); updateArtistChecks(); })
        .catch(function () { cb.disabled = false; revert(); });
    }

    // Guard against overlapping whole-set writes.
    var bulkBusy = false;
    function bulkSelect(on, scope) {
      if (bulkBusy) return Promise.resolve();
      bulkBusy = true;
      var body = "on=" + (on ? "1" : "0") + "&scope=" + scope +
                 "&tab=" + encodeURIComponent(curTab());
      if (scope === "page") {
        pageBox() && pageBox().querySelectorAll(".cb").forEach(function (cb) {
          body += "&cid=" + encodeURIComponent(cb.value);
        });
      }
      return post("/jobs/" + id + "/select-all", body)
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (c) {
          bulkBusy = false;
          if (!c) { flashSelectError(); return; }
          applyCounts(c);
          var on2 = on;
          if (scope === "all" || scope === "page") {
            pageBox() && pageBox().querySelectorAll(".cb").forEach(function (cb) { cb.checked = on2; });
          }
          updateHideLabels();
          updateArtistChecks();
        })
        .catch(function () { bulkBusy = false; flashSelectError(); });
    }

    function flashSelectError() {
      var box = pageBox();
      if (!box) return;
      box.querySelectorAll(".cb").forEach(function (cb) {
        cb.style.outline = "2px solid #ef4444";
        setTimeout(function () { cb.style.outline = ""; }, 1500);
      });
    }

    function groupSelect(det, on) {
      if (det._selecting) return;
      var pending = Array.prototype.filter.call(det.querySelectorAll(".cb"),
        function (cb) { return cb.checked !== on; });
      if (!pending.length) return;
      det._selecting = true;
      var failed = false;
      var chain = Promise.resolve(null);
      pending.forEach(function (cb) {
        chain = (function (box, prev) {
          return prev.then(function (acc) {
            if (failed) return acc;
            return post("/jobs/" + id + "/select",
              "cid=" + encodeURIComponent(box.value) + "&checked=" + (on ? "1" : "0"))
              .then(function (r) {
                if (!r.ok) { failed = true; return acc; }
                box.checked = on;
                return r.json();
              })
              .catch(function () { failed = true; return acc; });
          });
        }(cb, chain));
      });
      chain.then(function (c) {
        det._selecting = false;
        if (c) applyCounts(c);
        if (failed) flashSelectError();
        updateHideLabels();
        updateArtistChecks();
      });
    }

    // Label each artist action for what it will drop or keep.
    function updateHideLabels() {
      var box = pageBox();
      if (!box) return;
      box.querySelectorAll("[data-hide]").forEach(function (btn) {
        var det = btn.closest("details");
        var lbl = btn.querySelector("[data-hide-label]");
        if (!det || !lbl) return;
        var total = det.querySelectorAll(".cb").length;
        var picked = det.querySelectorAll(".cb:checked").length;
        btn.classList.toggle("hidden", total > 0 && picked === total);
        lbl.textContent = artistDismissLabel(picked);
      });
    }

    // Keep artist checkboxes in sync with their albums.
    function updateArtistChecks() {
      var box = pageBox();
      if (!box) return;
      box.querySelectorAll("[data-artist-select]").forEach(function (cb) {
        var det = cb.closest("details");
        if (!det) return;
        var total = det.querySelectorAll(".cb").length;
        var picked = det.querySelectorAll(".cb:checked").length;
        cb.checked = total > 0 && picked === total;
        cb.indeterminate = picked > 0 && picked < total;
      });
    }

    // Append the next page in place.
    function loadMore(page) {
      if (loading) return;
      loading = true;
      var url = "/jobs/" + id + "/review?page=" + (page || 2) +
                "&q=" + encodeURIComponent(curQuery()) +
                "&tab=" + encodeURIComponent(curTab());
      fetch(url, { headers: { "HX-Request": "true" } })
        .then(function (r) { return r.ok ? r.text() : null; })
        .then(function (txt) {
          loading = false;
          if (txt == null) { showToast("Couldn't load those results. Try again.", "error"); return; }
          var tmp = document.createElement("div");
          tmp.innerHTML = txt;
          var liveGroups = document.getElementById("review-groups");
          var newGroups = tmp.querySelector("#review-groups");
          if (liveGroups && newGroups) {
            while (newGroups.firstElementChild) liveGroups.appendChild(newGroups.firstElementChild);
            liveGroups.dataset.page = newGroups.dataset.page || liveGroups.dataset.page;
          }
          var oldMore = document.getElementById("review-loadmore");
          if (oldMore) oldMore.remove();
          var newMore = tmp.querySelector("#review-loadmore");
          if (newMore && liveGroups) liveGroups.parentNode.appendChild(newMore);
          if (window.htmx) window.htmx.process(document.getElementById("review-page"));
          updateHideLabels();
          updateArtistChecks();
        })
        .catch(function () { loading = false; showToast("Couldn't load those results. Check your connection.", "error"); });
    }

    // Fetch and swap one review page.
    var loading = false;
    function loadPage(page, query) {
      if (loading) return;
      loading = true;
      var url = "/jobs/" + id + "/review?page=" + (page || 1) +
                "&q=" + encodeURIComponent(query || "") +
                "&tab=" + encodeURIComponent(curTab());
      fetch(url, { headers: { "HX-Request": "true" } })
        .then(function (r) { return r.ok ? r.text() : null; })
        .then(function (txt) {
          loading = false;
          if (txt == null) { showToast("Couldn't load those results. Try again.", "error"); return; }
          var host = document.getElementById("review-page");
          if (host) {
            var openArtists = {};
            host.querySelectorAll("details[data-artist][open]").forEach(function (d) {
              if (d.dataset.artist) openArtists[d.dataset.artist] = true;
            });
            host.innerHTML = txt;
            host.querySelectorAll("details[data-artist]").forEach(function (d) {
              if (d.dataset.artist && openArtists[d.dataset.artist]) d.open = true;
            });
            if (window.htmx) window.htmx.process(host);
            updateHideLabels();
            updateArtistChecks();
          }
        })
        .catch(function () { loading = false; showToast("Couldn't load those results. Check your connection.", "error"); });
    }

    // Delegated interactions keep swapped pages wired.
    cont.addEventListener("change", function (e) {
      if (e.target.classList && e.target.classList.contains("cb")) saveTick(e.target);
      var asel = e.target.closest("[data-artist-select]");
      if (asel) { var ad = asel.closest("details"); if (ad) groupSelect(ad, asel.checked); }
    });
    cont.addEventListener("click", function (e) {
      var t = e.target;
      if (t.closest("[data-hide]")) return;
      // The artist checkbox sits inside a <summary>: its own activation runs
      // instead of the summary's, but stop the bubble so nothing else fires.
      if (t.closest("[data-artist-select]")) { e.stopPropagation(); return; }
      var allBtn = t.closest("[data-select-all]");
      if (allBtn) { bulkSelect(allBtn.getAttribute("data-select-all") === "1", "all"); return; }
      var pageBtn = t.closest("[data-select-page]");
      if (pageBtn) { bulkSelect(true, "page"); return; }
      var more = t.closest("[data-load-more]");
      if (more) { loadMore(parseInt(more.getAttribute("data-next-page") || "2", 10)); return; }
      var tabBtn = t.closest("[data-review-tab]");
      if (tabBtn && !tabBtn.classList.contains("is-active")) {
        cont.querySelectorAll("[data-review-tab]").forEach(function (b) {
          var on2 = b === tabBtn;
          b.classList.toggle("is-active", on2);
          if (on2) b.setAttribute("aria-current", "true");
          else b.removeAttribute("aria-current");
        });
        var tabField = document.getElementById("review-tab-field");
        if (tabField) tabField.value = curTab();
        applyCounts(lastCounts);  // re-scope the bulk bar to the new tab
        loadPage(1, curQuery());
        return;
      }
      var expBtn = t.closest("[data-expand-all]");
      if (expBtn) {
        var openAll = expBtn.getAttribute("data-expand-all") === "1";
        var box = pageBox();
        if (box) {
          box.querySelectorAll("details[data-artist]").forEach(function (d) { d.open = openAll; });
        }
        // The one control flips between the two actions.
        expBtn.setAttribute("data-expand-all", openAll ? "0" : "1");
        expBtn.textContent = openAll ? "Collapse all" : "Expand all";
        return;
      }
    });
    cont.addEventListener("change", function (evt) {
      var master = evt.target.closest && evt.target.closest("[data-select-master]");
      if (!master) return;
      var btn = cont.querySelector('[data-select-all="' + (master.checked ? "1" : "0") + '"]');
      if (btn) btn.click();
    });
    if (dismissRest) {
      dismissRest.addEventListener("click", function () {
        var tc = tabCounts(lastCounts);
        var rest = tc.total - tc.selected;
        if (rest <= 0) return;
        if (!window.confirm(dismissConfirm(rest))) return;
        var prev = dismissRest.textContent;
        dismissRest.disabled = true;
        dismissRest.textContent = dismissBusyLabel();
        post("/jobs/" + id + "/dismiss-rest", "tab=" + encodeURIComponent(curTab()))
          .then(function (r) { return r.ok ? r.json() : Promise.reject(); })
          .then(function (c) {
            if (c.review_done) { location.reload(); return; }
            dismissRest.disabled = false;
            applyCounts(c);
            loadPage(1, curQuery());
            if (c.hidden) showToast(dismissToast(c.hidden), "success");
          })
          .catch(function () {
            dismissRest.disabled = false;
            dismissRest.textContent = prev;
            flashSelectError();
          });
      });
    }

    // Filter across the full server-side result set.
    var filterTimer = null;
    if (filterInput) {
      filterInput.addEventListener("input", function () {
        if (filterTimer) clearTimeout(filterTimer);
        filterTimer = setTimeout(function () { loadPage(1, curQuery()); }, 250);
      });
      // Enter should filter, not submit the review form.
      filterInput.addEventListener("keydown", function (e) {
        if (e.key === "Enter") e.preventDefault();
      });
    }

    // Refresh counts and reload a page if hiding empties it.
    function onQlHidden(e) {
      var d = e.detail || {};
      if (d.counts) applyCounts(d.counts);
      var box = pageBox();
      if (box && box.querySelectorAll(":scope > details").length === 0) {
        var p = curPage();
        loadPage(p > 1 ? p - 1 : 1, curQuery());
      } else {
        updateHideLabels();
      }
    }
    document.body.addEventListener("qlHidden", onQlHidden);

    updateHideLabels();

    // Keep review pages in sync across tabs.
    var rsrc = new EventSource("/api/jobs/" + id + "/review-stream");
    function shutReview() {
      try { rsrc.close(); } catch (e) {}
      document.body.removeEventListener("qlHidden", onQlHidden);
      document.removeEventListener("htmx:beforeSwap", onReviewSwap);
    }
    function onReviewSwap(e) {
      if (e.detail && e.detail.target && e.detail.target.id === "job-content") shutReview();
    }
    document.addEventListener("htmx:beforeSwap", onReviewSwap);
    rsrc.addEventListener("review", function () {
      loadPage(curPage(), curQuery());
      post("/jobs/" + id + "/select", "cid=&checked=0")  // no-op tick → returns counts
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (c) { if (c) applyCounts(c); });
    });
    rsrc.addEventListener("closed", function (e) {
      // An archived/restored review has no live producer, so the server ends
      // the stream with "inactive" right away. Hides and ticks still work
      // there, so keep the count listeners — only stop the dead stream.
      if ((e.data || "") === "inactive") {
        try { rsrc.close(); } catch (err) {}
        return;
      }
      shutReview();
      if (window.htmx) {
        var jc = document.getElementById("job-content");
        var embedded = jc && jc.dataset.embedded ? "?embedded=1" : "";
        window.htmx.ajax("GET", "/jobs/" + id + "/content" + embedded, { target: "#job-content", swap: "outerHTML" });
      } else { location.reload(); }
    });
    rsrc.onerror = function () {
      if (rsrc.readyState === EventSource.CLOSED) shutReview();
    };
  }

  function initAll() {
    autoDismissFlashes();
    initCoverFallbacks();
    initSearchResults();
    initStreamCards();
    initJobContent();
  }

  // Re-scan swapped content for flashes and streams.
  document.addEventListener("htmx:afterSwap", initAll);
  cleanFlashUrl();
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initAll);
  } else {
    initAll();
  }
})();
