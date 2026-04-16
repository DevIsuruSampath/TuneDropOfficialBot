(function () {
  "use strict";

  var GATE_TIMEOUT_MS = 5000;
  var CHECK_INTERVAL_MS = 500;

  /* ── File size formatter ── */
  var sizeEl = document.getElementById("file-size");
  if (sizeEl && sizeEl.dataset.bytes) {
    var bytes = parseInt(sizeEl.dataset.bytes, 10);
    if (bytes > 0) {
      var units = ["B", "KB", "MB", "GB"];
      var i = 0;
      var size = bytes;
      while (size >= 1024 && i < units.length - 1) { size /= 1024; i++; }
      sizeEl.textContent = size.toFixed(i === 0 ? 0 : 1) + " " + units[i];
    }
  }

  /* ── Ad-blocker Detection & Friendly Gate ── */
  var gate = document.getElementById("ad-gate");
  var bait = document.getElementById("ad-bait");

  if (gate) {
    var stateLoading = document.getElementById("ad-gate-loading");
    var stateBlocked = document.getElementById("ad-gate-blocked");
    var stateThanks = document.getElementById("ad-gate-thanks");
    var reloadBtn = document.getElementById("ad-gate-reload");

    // Show loading spinner immediately
    gate.classList.add("visible");
    stateLoading.classList.add("active");

    // "Got it" button dismisses the gate
    if (reloadBtn) {
      reloadBtn.addEventListener("click", function () {
        dismissGate();
      });
    }

    var resolved = false;
    var elapsed = 0;
    var countdownEl = document.getElementById("ad-gate-countdown");

    // Fast-poll: check every 500ms, dismiss as soon as ads load
    var pollId = setInterval(function () {
      elapsed += CHECK_INTERVAL_MS;

      // Update countdown display
      if (countdownEl) {
        var remaining = Math.ceil((GATE_TIMEOUT_MS - elapsed) / 1000);
        if (remaining < 0) remaining = 0;
        countdownEl.textContent = remaining;
      }

      if (isAdBlockerActive()) {
        // Ad blocker detected — stop trying
        resolved = true;
        clearInterval(pollId);
        stateLoading.classList.remove("active");
        stateBlocked.classList.add("active");
        refreshSponsoredAreas(true);
        return;
      }

      if (detectAds()) {
        resolved = true;
        clearInterval(pollId);
        showThanks();
        refreshSponsoredAreas(false);
        startPostGateMonitoring();
        return;
      }

      // Timeout: no ads detected
      if (elapsed >= GATE_TIMEOUT_MS) {
        resolved = true;
        clearInterval(pollId);
        stateLoading.classList.remove("active");
        stateBlocked.classList.add("active");
        refreshSponsoredAreas(true);
        startPostGateMonitoring();
      }
    }, CHECK_INTERVAL_MS);
  }

  /**
   * Check if bait element is hidden/removed by an ad blocker.
   * Returns true if ad blocker is active.
   */
  function isAdBlockerActive() {
    if (!bait) return false;
    if (bait.offsetHeight === 0 || !bait.parentNode) return true;
    var s = getComputedStyle(bait);
    if (s.display === "none" || s.visibility === "hidden") return true;
    return false;
  }

  /**
   * Detect if ads are working: bait visible + wrapper iframes exist.
   */
  function detectAds() {
    if (isAdBlockerActive()) return false;
    var wrappers = document.querySelectorAll("iframe[data-ad-wrapper]");
    return wrappers.length > 0;
  }

  /**
   * Update sponsored area visibility based on ad state.
   */
  function refreshSponsoredAreas(blocked) {
    var areas = document.querySelectorAll(".sponsored-area");
    for (var i = 0; i < areas.length; i++) {
      if (blocked) {
        areas[i].classList.add("sponsored-blocked");
      } else {
        areas[i].classList.remove("sponsored-blocked");
      }
    }
  }

  var postGateStarted = false;

  function startPostGateMonitoring() {
    if (postGateStarted) return;
    postGateStarted = true;
    var checks = 0;
    var maxChecks = 5; // 5 × 2000ms = 10s (was 30 × 500ms = 15s)
    var id = setInterval(function () {
      refreshSponsoredAreas(isAdBlockerActive());
      checks++;
      if (checks >= maxChecks) clearInterval(id);
    }, 2000);
  }

  function showThanks() {
    if (!gate) return;
    stateLoading.classList.remove("active");
    stateThanks.classList.add("active");

    // Animate countdown: 3 → 2 → 1 → dismiss
    var thanksCountdownEl = document.getElementById("ad-gate-thanks-countdown");
    var remaining = 3;
    var id = setInterval(function () {
      remaining--;
      if (thanksCountdownEl) {
        thanksCountdownEl.textContent = remaining > 0 ? remaining : "0";
      }
      if (remaining <= 0) {
        clearInterval(id);
        dismissGate();
      }
    }, 1000);
  }

  function dismissGate() {
    if (!gate) return;
    gate.classList.remove("visible");
    stateLoading.classList.remove("active");
    stateBlocked.classList.remove("active");
    stateThanks.classList.remove("active");
  }

  /* ── Download Button ── */
  var dlBtn = document.getElementById("dl-btn");

  if (dlBtn) {
    dlBtn.addEventListener("click", function (e) {
      // Open smartlink in new tab if configured
      var sl = dlBtn.dataset.smartlink;
      if (sl) {
        window.open(sl, "_blank", "noopener");
      }

      // Show loading state
      if (dlBtn.classList.contains("loading")) return;
      dlBtn.classList.add("loading");
      var label = dlBtn.querySelector(".dl-btn-label");
      label.textContent = "Downloading\u2026";

      // Mark done after short delay (download has started by then)
      setTimeout(function () {
        dlBtn.classList.remove("loading");
        dlBtn.classList.add("done");
        label.textContent = "Done!";
        setTimeout(function () {
          dlBtn.classList.remove("done");
          label.textContent = "Download";
        }, 3000);
      }, 1500);
    });
  }

})();
