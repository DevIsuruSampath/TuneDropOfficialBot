(function () {
  "use strict";

  var GATE_TIMEOUT_MS = 3000;
  var CHECK_INTERVAL_MS = 500;

  /* ── Ad-blocker Detection & Friendly Gate ── */
  var gate = document.getElementById("ad-gate");

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

      if (detectAds()) {
        resolved = true;
        clearInterval(pollId);
        showThanks();
        refreshSponsoredAreas();
        startPostGateMonitoring();
        return;
      }

      // Timeout: 3s passed, no ads → show friendly blocked message (download still works)
      if (elapsed >= GATE_TIMEOUT_MS) {
        resolved = true;
        clearInterval(pollId);
        stateLoading.classList.remove("active");
        stateBlocked.classList.add("active");
        refreshSponsoredAreas();
        startPostGateMonitoring();
      }
    }, CHECK_INTERVAL_MS);
  }

  /**
   * Detect if ads loaded by checking:
   * 1. Bait element was NOT removed/hidden by ad blocker
   * 2. At least one ad slot contains a non-wrapper iframe (actual ad)
   */
  function detectAds() {
    // Check bait element
    var bait = document.getElementById("ad-bait");
    if (bait) {
      if (bait.offsetHeight === 0 || !bait.parentNode) {
        return false;
      }
      var s = getComputedStyle(bait);
      if (s.display === "none" || s.visibility === "hidden") {
        return false;
      }
    }

    // Check if any ad slot has a non-wrapper iframe (actual ad loaded)
    var slots = document.querySelectorAll(".ad-slot");
    for (var i = 0; i < slots.length; i++) {
      var iframes = slots[i].querySelectorAll("iframe");
      for (var j = 0; j < iframes.length; j++) {
        if (!iframes[j].hasAttribute("data-ad-wrapper")) {
          return true;
        }
      }
    }

    // No ad iframes found
    return false;
  }

  /**
   * Per sponsored area: if no non-wrapper iframe inside, show unavailable message.
   */
  function refreshSponsoredAreas() {
    var areas = document.querySelectorAll(".sponsored-area");
    for (var i = 0; i < areas.length; i++) {
      var body = areas[i].querySelector(".sponsored-body");
      var hasAd = false;
      if (body) {
        var iframes = body.querySelectorAll("iframe");
        for (var j = 0; j < iframes.length; j++) {
          if (!iframes[j].hasAttribute("data-ad-wrapper")) {
            hasAd = true;
            break;
          }
        }
      }
      if (hasAd) {
        areas[i].classList.remove("sponsored-blocked");
      } else {
        areas[i].classList.add("sponsored-blocked");
      }
    }
  }

  var postGateStarted = false;

  function startPostGateMonitoring() {
    if (postGateStarted) return;
    postGateStarted = true;
    var checks = 0;
    var maxChecks = 30; // 30 × 500 ms ≈ 15 s
    var id = setInterval(function () {
      refreshSponsoredAreas();
      checks++;
      if (checks >= maxChecks) clearInterval(id);
    }, 500);
  }

  function showThanks() {
    if (!gate) return;
    stateLoading.classList.remove("active");
    stateThanks.classList.add("active");
    // Auto-dismiss after 2s
    setTimeout(function () {
      dismissGate();
    }, 2000);
  }

  function dismissGate() {
    if (!gate) return;
    gate.classList.remove("visible");
    // Reset states for next time
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
      // Let the browser handle the href naturally
    });
  }

})();
