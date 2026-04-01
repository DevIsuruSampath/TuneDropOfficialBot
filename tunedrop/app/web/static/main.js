(function () {
  "use strict";

  var GATE_TIMEOUT_MS = 3000;
  var CHECK_INTERVAL_MS = 300;
  var RELOAD_KEY = "tunedrop_adblock_reload";

  /* ── Ad-blocker Detection & Gate ── */
  var gate = document.getElementById("ad-gate");

  if (gate) {
    var stateLoading = document.getElementById("ad-gate-loading");
    var stateBlocked = document.getElementById("ad-gate-blocked");
    var reloadBtn = document.getElementById("ad-gate-reload");

    // Show loading spinner immediately
    gate.classList.add("visible");
    stateLoading.classList.add("active");

    if (reloadBtn) {
      reloadBtn.addEventListener("click", function () {
        sessionStorage.setItem(RELOAD_KEY, "1");
        location.reload();
      });
    }

    var resolved = false;
    var elapsed = 0;
    var countdownEl = document.getElementById("ad-gate-countdown");

    // Fast-poll: check every 300ms, dismiss as soon as ads load
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
        dismissGate();
        refreshSponsoredAreas();
        startPostGateMonitoring();
        return;
      }

      // Timeout: 3s passed, no ads → show blocked
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

  function dismissGate() {
    gate.classList.remove("visible");

    // If user just came back from a reload, show thanks toast
    if (sessionStorage.getItem(RELOAD_KEY)) {
      sessionStorage.removeItem(RELOAD_KEY);
      showThanksToast();
    }
  }

  function showThanksToast() {
    var el = document.createElement("div");
    el.className = "thanks-toast";
    el.textContent = "Thank you for your support \u2764\uFE0F";
    document.body.appendChild(el);
    setTimeout(function () { el.classList.add("fade-out"); }, 2500);
    setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 3000);
  }

  /* ── Smartlink on Download Click ── */
  var dlBtn = document.getElementById("dl-btn");

  if (dlBtn) {
    var dlClicked = false;
    dlBtn.addEventListener("click", function (e) {
      if (dlClicked) { e.preventDefault(); return; }
      dlClicked = true;
      dlBtn.style.pointerEvents = "none";
      dlBtn.style.opacity = "0.6";

      var smartlink = dlBtn.dataset.smartlink;
      if (smartlink) {
        e.preventDefault();
        window.open(smartlink, "_blank", "noopener");
        setTimeout(function () {
          window.location.href = dlBtn.href;
        }, 150);
      }

      setTimeout(function () {
        dlBtn.style.pointerEvents = "";
        dlBtn.style.opacity = "";
        dlClicked = false;
      }, 3000);
    });
  }

  /* ── Countdown Timer ── */
  var timerWrap = document.getElementById("timer");
  if (!timerWrap) return;

  var timerH = document.getElementById("timer-h");
  var timerM = document.getElementById("timer-m");
  var timerS = document.getElementById("timer-s");

  var expiresMs = new Date(timerWrap.dataset.expires).getTime();
  if (isNaN(expiresMs)) {
    if (timerH) timerH.textContent = "00";
    if (timerM) timerM.textContent = "00";
    if (timerS) timerS.textContent = "00";
    return;
  }

  var warned = false;
  var expired = false;
  var intervalId = null;
  var announceEl = document.getElementById("timer-announce");
  var lastAnnouncedMins = -1;

  function pad(n) {
    return n < 10 ? "0" + n : String(n);
  }

  function announce(msg) {
    if (announceEl) announceEl.textContent = msg;
  }

  function tick() {
    var diff = expiresMs - Date.now();
    if (diff <= 0) diff = 0;

    var h = Math.floor(diff / 3600000);
    var m = Math.floor((diff % 3600000) / 60000);
    var s = Math.floor((diff % 60000) / 1000);
    var totalMins = Math.ceil(diff / 60000);

    if (timerH) timerH.textContent = pad(h);
    if (timerM) timerM.textContent = pad(m);
    if (timerS) timerS.textContent = pad(s);

    // Announce at significant thresholds for screen readers
    if (totalMins !== lastAnnouncedMins) {
      if (totalMins === 30 || totalMins === 10 || totalMins === 5 || totalMins === 1) {
        announce("Link expires in " + totalMins + " minutes");
      }
      lastAnnouncedMins = totalMins;
    }

    if (!warned && diff < 3600000 && diff > 0) {
      warned = true;
      timerWrap.classList.add("warning");
    }

    if (diff === 0 && !expired) {
      expired = true;
      timerWrap.classList.remove("warning");
      timerWrap.classList.add("expired-timer");
      if (dlBtn) dlBtn.classList.add("hidden");
      announce("Download link has expired");
      clearInterval(intervalId);
    }
  }

  tick();
  intervalId = setInterval(tick, 1000);
})();
