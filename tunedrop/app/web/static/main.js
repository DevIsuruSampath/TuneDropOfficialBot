(function () {
  "use strict";

  var timerWrap = document.getElementById("timer");
  if (!timerWrap) return;

  var timerH = document.getElementById("timer-h");
  var timerM = document.getElementById("timer-m");
  var timerS = document.getElementById("timer-s");
  var dlBtn = document.getElementById("dl-btn");

  var expiresMs = new Date(timerWrap.dataset.expires).getTime();
  if (isNaN(expiresMs)) {
    timerH.textContent = "00";
    timerM.textContent = "00";
    timerS.textContent = "00";
    return;
  }

  var warned = false;
  var expired = false;
  var intervalId = null;

  function pad(n) {
    return n < 10 ? "0" + n : String(n);
  }

  function tick() {
    var diff = expiresMs - Date.now();
    if (diff <= 0) diff = 0;

    timerH.textContent = pad(Math.floor(diff / 3600000));
    timerM.textContent = pad(Math.floor((diff % 3600000) / 60000));
    timerS.textContent = pad(Math.floor((diff % 60000) / 1000));

    if (!warned && diff < 3600000 && diff > 0) {
      warned = true;
      timerWrap.classList.add("warning");
    }

    if (diff === 0 && !expired) {
      expired = true;
      timerWrap.classList.remove("warning");
      timerWrap.classList.add("expired-timer");
      if (dlBtn) dlBtn.classList.add("hidden");
      clearInterval(intervalId);
    }
  }

  tick();
  intervalId = setInterval(tick, 1000);

  if (dlBtn) {
    dlBtn.addEventListener("click", function () {
      // Re-enable after 3 seconds in case download fails
      var btn = dlBtn;
      btn.style.pointerEvents = "none";
      setTimeout(function () {
        btn.style.pointerEvents = "";
      }, 3000);
    });
  }
})();
