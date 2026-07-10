/* Baseline — frontend state machine.
 * Screens: home -> upload -> uploading -> analyzing -> report.
 * The API base can be overridden without a redeploy:
 *   localStorage.setItem("baseline_api", "https://...modal.run")
 */
(function () {
  "use strict";

  var API_BASE = localStorage.getItem("baseline_api") || "https://jameslaii--baseline-api.modal.run";

  var MAX_BYTES = 500 * 1024 * 1024;
  var MAX_DURATION_S = 250;
  var POLL_MS = 4000;
  var REEL_POLL_MS = 5000;
  var STALL_MS = 5 * 60 * 1000;

  var state = {
    file: null,
    duration: null,
    hand: "",
    stroke: "forehand",
    benchmark: "alcaraz",
    benchmarkDisplay: "Carlos Alcaraz",
    xhr: null,
    pollTimer: null,
    lastProgressAt: 0,
    lastPercent: -1,
  };

  function $(id) { return document.getElementById(id); }
  function invite() { return ($("invite").value || "").trim(); }

  // ---------- navigation ----------
  var screens = ["home", "upload", "uploading", "analyzing", "report"];
  function show(name) {
    screens.forEach(function (s) {
      $("scr-" + s).classList.toggle("active", s === name);
    });
    window.scrollTo(0, 0);
  }
  document.addEventListener("click", function (e) {
    var nav = e.target.closest("[data-nav]");
    if (nav) {
      stopPolling();
      stopReelPoll();
      stopReplay();
      if (state.xhr) { state.xhr.abort(); state.xhr = null; }
      show(nav.getAttribute("data-nav"));
    }
  });

  // ---------- localStorage helpers ----------
  function savedJobs() {
    try { return JSON.parse(localStorage.getItem("baseline_jobs") || "[]"); }
    catch (e) { return []; }
  }
  function rememberJob(jobId, summary, metrics) {
    var list = savedJobs().filter(function (j) { return j.job_id !== jobId; });
    var entry = { job_id: jobId, date: new Date().toISOString().slice(0, 10), summary: summary };
    if (metrics) entry.metrics = metrics;
    list.unshift(entry);
    localStorage.setItem("baseline_jobs", JSON.stringify(list.slice(0, 10)));
    renderPast();
  }
  function renderPast() {
    var list = savedJobs();
    $("past-wrap").hidden = list.length === 0;
    var host = $("past-list");
    host.textContent = "";
    list.forEach(function (j) {
      var b = document.createElement("button");
      b.className = "card";
      b.innerHTML = '<span><span class="d"></span><span class="s" style="display:block"></span></span><span class="chev">›</span>';
      b.querySelector(".d").textContent = j.summary || "Session report";
      b.querySelector(".s").textContent = j.date;
      b.addEventListener("click", function () { openJob(j.job_id); });
      host.appendChild(b);
    });
    renderProgress(list);
  }

  // Home-screen progress: avg hand-path score per session, oldest -> newest
  function renderProgress(list) {
    var pts = list.filter(function (j) { return j.metrics && j.metrics.avg_score != null; })
                  .reverse();   // stored newest-first
    $("prog-wrap").hidden = pts.length < 2;
    if (pts.length < 2) return;
    var cv = $("prog-canvas");
    var ctx = cv.getContext("2d");
    ctx.clearRect(0, 0, cv.width, cv.height);
    var pad = 10, W = cv.width, H = cv.height;
    function sx(i) { return pad + (W - 2 * pad) * i / (pts.length - 1); }
    function sy(v) { return H - pad - (H - 2 * pad) * Math.max(0, Math.min(1, v)); }
    // faint grid line at 0.5
    ctx.strokeStyle = "rgba(154,163,178,0.18)";
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(pad, sy(0.5)); ctx.lineTo(W - pad, sy(0.5)); ctx.stroke();
    // score line
    ctx.strokeStyle = "#C9A961";
    ctx.lineWidth = 2;
    ctx.lineCap = "round";
    ctx.beginPath();
    pts.forEach(function (j, i) {
      var x = sx(i), y = sy(j.metrics.avg_score);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
    pts.forEach(function (j, i) {
      ctx.fillStyle = i === pts.length - 1 ? "#FFB84D" : "#C9A961";
      ctx.beginPath();
      ctx.arc(sx(i), sy(j.metrics.avg_score), i === pts.length - 1 ? 3.4 : 2.4, 0, Math.PI * 2);
      ctx.fill();
    });
    var cur = pts[pts.length - 1].metrics, prev = pts[pts.length - 2].metrics;
    var bits = ["avg swing " + Math.round(cur.avg_score * 100) + "%"];
    if (cur.speed_max != null && prev.speed_max != null) {
      var ds = Math.round(cur.speed_max - prev.speed_max);
      bits.push((ds >= 0 ? "+" : "") + ds + " km/h vs last");
    }
    if (cur.knee != null && prev.knee != null) {
      var dk = Math.round(cur.knee - prev.knee);
      bits.push("knee " + Math.round(cur.knee) + "° (" + (dk > 0 ? "+" : "") + dk + "°)");
    }
    $("prog-note").textContent = bits.join(" · ");
  }

  // ---------- landing ----------
  $("invite").value = localStorage.getItem("baseline_invite") || "";
  $("btn-start").addEventListener("click", function () {
    $("home-err").textContent = "";
    if (!invite()) { $("home-err").textContent = "Enter your invite code first."; return; }
    localStorage.setItem("baseline_invite", invite());
    show("upload");
  });

  // ---------- upload form ----------
  var drop = $("file-drop");
  var fileInput = $("file-input");
  drop.addEventListener("click", function () { fileInput.click(); });
  drop.addEventListener("keydown", function (e) {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); }
  });
  fileInput.addEventListener("change", function () {
    var f = fileInput.files[0];
    if (!f) return;
    setFile(f);
  });

  function setFile(f) {
    $("upload-err").textContent = "";
    state.file = null;
    state.duration = null;
    if (f.size > MAX_BYTES) {
      $("upload-err").textContent = "That file is over 500 MB - export at 1080p and try again.";
      refreshRunButton();
      return;
    }
    // read duration from metadata (best effort; server re-checks)
    var url = URL.createObjectURL(f);
    var v = document.createElement("video");
    v.preload = "metadata";
    var done = false;
    function finish(dur) {
      if (done) return;
      done = true;
      URL.revokeObjectURL(url);
      if (dur && dur > MAX_DURATION_S) {
        $("upload-err").textContent = "Clip is " + Math.round(dur) + "s - trim it to 4 minutes or less.";
        drop.classList.remove("has");
        $("file-meta").textContent = "";
        refreshRunButton();
        return;
      }
      state.file = f;
      state.duration = dur || null;
      drop.classList.add("has");
      $("file-cta").textContent = "Video ready";
      $("file-meta").textContent = f.name + " · " + (f.size / 1e6).toFixed(0) + " MB" +
        (dur ? " · " + Math.round(dur) + "s" : "");
      refreshRunButton();
    }
    v.onloadedmetadata = function () { finish(v.duration); };
    v.onerror = function () { finish(null); };
    setTimeout(function () { finish(null); }, 4000);
    v.src = url;
  }

  function segWire(id, key) {
    var seg = $(id);
    seg.addEventListener("click", function (e) {
      var b = e.target.closest("button[data-v]");
      if (!b) return;
      seg.querySelectorAll("button").forEach(function (x) { x.classList.remove("on"); });
      b.classList.add("on");
      seg.setAttribute("data-value", b.getAttribute("data-v"));
      state[key] = b.getAttribute("data-v");
      refreshRunButton();
    });
  }
  segWire("seg-hand", "hand");
  segWire("seg-stroke", "stroke");

  $("pro-list").addEventListener("click", function (e) {
    var card = e.target.closest(".pro-card");
    if (!card || card.disabled) return;
    document.querySelectorAll("#pro-list .pro-card").forEach(function (c) { c.classList.remove("sel"); });
    card.classList.add("sel");
    state.benchmark = card.getAttribute("data-pro");
    state.benchmarkDisplay = card.getAttribute("data-display");
    refreshRunButton();
  });

  function refreshRunButton() {
    $("btn-run").disabled = !(state.file && state.hand && state.stroke && state.benchmark);
  }

  // ---------- upload ----------
  $("btn-run").addEventListener("click", startUpload);
  $("btn-cancel-upload").addEventListener("click", function () {
    if (state.xhr) { state.xhr.abort(); state.xhr = null; }
    show("upload");
  });

  function friendlyHttpError(xhr) {
    try {
      var body = JSON.parse(xhr.responseText);
      if (body && body.detail && body.detail.message) return body.detail.message;
    } catch (e) { /* fall through */ }
    if (xhr.status === 401) return "Invite code was rejected.";
    if (xhr.status === 413) return "Video too large - export at 1080p and retry.";
    if (xhr.status === 429) return "Today's analysis quota is used up - try tomorrow.";
    if (xhr.status === 0) return "Upload failed - check your connection and retry.";
    return "Upload failed (HTTP " + xhr.status + ").";
  }

  function startUpload() {
    if (!state.file) return;
    show("uploading");
    $("up-file").textContent = state.file.name;
    $("up-bar").style.width = "0%";
    $("up-pct").textContent = "0%";

    var q = "?handedness=" + encodeURIComponent(state.hand) +
            "&stroke=" + encodeURIComponent(state.stroke) +
            "&benchmark=" + encodeURIComponent(state.benchmark);
    var xhr = new XMLHttpRequest();
    state.xhr = xhr;
    xhr.open("POST", API_BASE + "/api/jobs" + q);
    xhr.setRequestHeader("X-Invite-Code", invite());
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = function (e) {
      if (e.lengthComputable) {
        var pct = Math.round(100 * e.loaded / e.total);
        $("up-bar").style.width = pct + "%";
        $("up-pct").textContent = pct + "%";
      }
    };
    xhr.onload = function () {
      state.xhr = null;
      if (xhr.status === 202) {
        var jobId = JSON.parse(xhr.responseText).job_id;
        openJob(jobId);
      } else {
        show("upload");
        $("upload-err").textContent = friendlyHttpError(xhr);
      }
    };
    xhr.onerror = function () {
      state.xhr = null;
      show("upload");
      $("upload-err").textContent = friendlyHttpError(xhr);
    };
    xhr.send(state.file);
  }

  // ---------- analyzing / polling ----------
  function stopPolling() {
    if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
  }

  function openJob(jobId) {
    stopPolling();
    stopReelPoll();
    stopReplay();
    location.hash = "job/" + jobId;
    show("analyzing");
    $("an-err").hidden = true;
    $("btn-an-home").hidden = true;
    $("an-grade-line").textContent = "Grading vs " + state.benchmarkDisplay;
    state.lastProgressAt = Date.now();
    state.lastPercent = -1;
    poll(jobId);
    state.pollTimer = setInterval(function () { poll(jobId); }, POLL_MS);
  }
  $("btn-an-home").addEventListener("click", function () { stopPolling(); show("home"); });

  function analysisError(title, message) {
    stopPolling();
    $("an-err").hidden = false;
    $("an-err-t").textContent = title;
    $("an-err-m").textContent = message;
    $("btn-an-home").hidden = false;
  }

  function poll(jobId) {
    fetch(API_BASE + "/api/jobs/" + jobId, {
      headers: { "X-Invite-Code": invite() },
    }).then(function (r) {
      if (r.status === 404) { analysisError("Report not found", "This report may have expired."); return null; }
      if (r.status === 401) { analysisError("Invite code rejected", "Re-enter your invite code on the home screen."); return null; }
      return r.json();
    }).then(function (entry) {
      if (!entry) return;
      var pct = entry.percent || 0;
      if (pct !== state.lastPercent) { state.lastPercent = pct; state.lastProgressAt = Date.now(); }
      $("an-pct").textContent = pct + "%";
      $("an-bar").style.width = pct + "%";
      renderStages(entry.stage || "");
      if (entry.status === "error") {
        var err = entry.error || {};
        analysisError("Analysis failed", err.message || "Something went wrong - try another clip.");
        return;
      }
      if (entry.status === "done" && entry.results) {
        stopPolling();
        renderReport(entry.results);
        return;
      }
      if (Date.now() - state.lastProgressAt > STALL_MS) {
        analysisError("Analysis stalled", "No progress for 5 minutes - the job may have died. Try again.");
      }
    }).catch(function () { /* transient network error: keep polling */ });
  }

  function renderStages(stage) {
    var order = ["normalizing", "extracting_pose", "detecting_strokes", "filtering_wing", "grading", "aggregating", "done"];
    var idx = order.indexOf(stage);
    document.querySelectorAll("#an-stages .scan-row").forEach(function (row) {
      var stages = row.getAttribute("data-stages").split(",");
      var rowIdx = order.indexOf(stages[0]);
      var rowEnd = order.indexOf(stages[stages.length - 1]);
      row.classList.remove("doing", "done");
      if (idx > rowEnd || stage === "done") row.classList.add("done");
      else if (idx >= rowIdx && idx <= rowEnd) row.classList.add("doing");
    });
  }

  // ---------- report ----------
  function chip(el, label, grade) {
    el.textContent = label;
    el.className = "chip " + (grade || "ok");
    el.hidden = false;
  }

  function tileSummary(counts) {
    return counts.good + "/" + (counts.good + counts.ok + counts.bad);
  }

  function renderReport(res) {
    var agg = res.aggregate;
    var q = res.quality;
    var strokeName = res.params.stroke.charAt(0).toUpperCase() + res.params.stroke.slice(1);

    $("rp-eyebrow").textContent = "Session Report · " + (res.created_at || "").slice(0, 10);
    $("rp-title").textContent = strokeName + " vs " + res.benchmark.display;
    $("rp-verdict").textContent = agg.verdict.label;
    $("rp-verdict-sub").textContent = agg.good_hand_path + " of " + agg.n +
      " graded swings rated GOOD on hand path";

    // typical vs best (schema 2+; older saved reports simply skip it)
    var dist = agg.distribution;
    $("rp-typical").hidden = true;
    if (dist && dist.typical && dist.best) {
      function pct(x) { return Math.round(x * 100) + "%"; }
      var typ = "Typical swing " + pct(dist.typical.score) +
                (dist.typical.knee_deg != null ? " · knee " + Math.round(dist.typical.knee_deg) + "°" : "") +
                "  —  best " + pct(dist.best.score) +
                (dist.best.knee_deg != null ? " · knee " + Math.round(dist.best.knee_deg) + "°" : "");
      $("rp-typical").textContent = typ;
      $("rp-typical").hidden = false;
    }

    // consistency + within-session trend (v3)
    $("rp-form-line").hidden = true;
    if (agg.consistency && agg.consistency.rating) {
      var parts = [agg.consistency.rating + " session"];
      if (agg.fatigue) {
        var fade = agg.fatigue.fade;
        if (fade >= 0.15) parts.push("late swings graded " + Math.round(fade * 100) + " pts lower — pace yourself");
        else if (fade <= -0.15) parts.push("you warmed up — late swings graded " + Math.round(-fade * 100) + " pts higher");
        else parts.push("steady from first ball to last");
      }
      $("rp-form-line").textContent = parts.join(" · ");
      $("rp-form-line").hidden = false;
    }

    // warnings
    var warns = (q.warnings || []);
    $("rp-warns").hidden = warns.length === 0;
    $("rp-warns").textContent = "";
    warns.forEach(function (w) {
      var d = document.createElement("div");
      d.textContent = "△ " + w.message;
      $("rp-warns").appendChild(d);
    });

    // tiles
    var t = agg.tiles;
    $("rp-drop").textContent = tileSummary(t.racket_drop);
    chip($("rp-drop-chip"), t.racket_drop.good >= agg.n * 0.6 ? "GOOD" : (t.racket_drop.bad > agg.n * 0.4 ? "SHALLOW" : "MIXED"),
         t.racket_drop.good >= agg.n * 0.6 ? "good" : (t.racket_drop.bad > agg.n * 0.4 ? "bad" : "ok"));
    $("rp-lth").textContent = tileSummary(t.low_to_high);
    chip($("rp-lth-chip"), t.low_to_high.good >= agg.n * 0.6 ? "GOOD" : (t.low_to_high.bad > agg.n * 0.4 ? "FLAT" : "MIXED"),
         t.low_to_high.good >= agg.n * 0.6 ? "good" : (t.low_to_high.bad > agg.n * 0.4 ? "bad" : "ok"));
    $("rp-fin").textContent = tileSummary(t.finish);
    chip($("rp-fin-chip"), t.finish.good >= agg.n * 0.6 ? "HIGH" : (t.finish.bad > agg.n * 0.4 ? "LOW" : "MIXED"),
         t.finish.good >= agg.n * 0.6 ? "good" : (t.finish.bad > agg.n * 0.4 ? "bad" : "ok"));
    var ch = t.contact_height;
    $("rp-ch").textContent = ch.in_zone + "/" + agg.n;
    var chLabel = ch.in_zone >= agg.n * 0.6 ? "IN ZONE" : (ch.low > ch.high_late ? ch.low + " LOW" : ch.high_late + " HIGH/LATE");
    chip($("rp-ch-chip"), chLabel, ch.in_zone >= agg.n * 0.6 ? "good" : "ok");

    // v3 tiles: hand speed + contact-out-front (older reports simply hide them)
    $("rp-speed-tile").hidden = !(agg.speed && agg.speed.max_kmh != null);
    if (agg.speed && agg.speed.max_kmh != null) {
      $("rp-speed").innerHTML = Math.round(agg.speed.max_kmh) + ' <small>km/h peak · avg ' +
        Math.round(agg.speed.avg_kmh) + "</small>";
    }
    $("rp-front-tile").hidden = !(agg.contact_front && agg.contact_front.measured);
    if (agg.contact_front && agg.contact_front.measured) {
      var cf = agg.contact_front;
      $("rp-front").innerHTML = cf.out_front + "/" + cf.measured + ' <small>out front</small>';
      var cfGood = cf.out_front >= cf.measured * 0.5;
      var cfBad = cf.behind > cf.measured * 0.33;
      chip($("rp-front-chip"),
           cfGood ? "OUT FRONT" : (cfBad ? cf.behind + " BEHIND" : "CLOSE"),
           cfGood ? "good" : (cfBad ? "bad" : "ok"));
    }

    // leg load + dial
    var ll = t.leg_load;
    $("rp-leg").innerHTML = ll.loaded + "/" + agg.n + " <small>loaded</small>";
    if (ll.avg_knee_deg != null) {
      $("rp-dial-knee").textContent = Math.round(ll.avg_knee_deg) + "°";
      $("rp-leg-vs").textContent = "you " + Math.round(ll.avg_knee_deg) + "° · " +
        res.benchmark.display.split(" ").slice(-1)[0] + " " + Math.round(ll.ref_knee_deg) + "°";
      // dial arc: fuller = more knee bend (lower angle). 180° extended -> 0%, 120° deep -> 100%.
      var frac = Math.max(0.05, Math.min(1, (180 - ll.avg_knee_deg) / 60));
      $("rp-dial-arc").setAttribute("stroke-dasharray", Math.round(frac * 100) + " 100");
      var loadedEnough = ll.loaded >= agg.n * 0.5;
      $("rp-dial-arc").setAttribute("stroke", loadedEnough ? "#3DDC84" : "#FFD54D");
      chip($("rp-leg-chip"), loadedEnough ? "LOADED" : "SIT LOWER", loadedEnough ? "good" : "ok");
    } else {
      $("rp-dial-knee").textContent = "—";
      $("rp-leg-vs").textContent = "";
      $("rp-leg-chip").hidden = true;
    }

    // hand path sparkline (best swing)
    var best = res.swings[(agg.best_swing_idx || 1) - 1] || res.swings[0];
    drawHandPath(best);
    $("rp-path-k").textContent = "Hand path · best swing (" + best.time_s + "s, " +
      best.overall.label.toLowerCase() + ")";

    // this-week plan: priority + second focus + filming tip (v3; falls back to v1 priority)
    $("rp-priority").textContent = agg.priority.sentence;
    $("rp-drill").textContent = "Drill · " + agg.priority.drill;
    $("rp-focus2").hidden = true;
    $("rp-drill2").hidden = true;
    if (agg.plan && agg.plan.focus && agg.plan.focus.length > 1) {
      var f2 = agg.plan.focus[1];
      $("rp-focus2").textContent = f2.sentence.replace("Priority this week: ", "Also: ") +
        (f2.count ? " (" + f2.count + " of " + f2.of + " swings)" : "");
      $("rp-drill2").textContent = "Drill · " + f2.drill;
      $("rp-focus2").hidden = false;
      $("rp-drill2").hidden = false;
    }
    $("rp-film").hidden = true;
    var filmTip = null;
    warns.forEach(function (w) {
      if (w.code === "low_pose_pct") filmTip = "Next clip: film closer — you were hard to see in this one.";
      if (w.code === "few_strokes" && !filmTip) filmTip = "Next clip: a longer hitting block gives a fairer report.";
      if (w.code === "no_audio" && !filmTip) filmTip = "Next clip: keep the sound on — the ball 'pock' sharpens contact detection.";
    });
    if (filmTip) {
      $("rp-film").textContent = "Filming · " + filmTip;
      $("rp-film").hidden = false;
    }

    // session stats
    $("rp-n-strokes").textContent = q.strokes_detected_total;
    $("rp-n-graded").textContent = q.strokes_graded;
    $("rp-pose-pct").textContent = Math.round(q.pose_detect_pct) + "%";

    // per-swing rows (tap to expand the skeleton replay when the data is there)
    stopReplay();
    var host = $("rp-swings");
    host.textContent = "";
    res.swings.forEach(function (s) {
      var row = document.createElement("div");
      row.className = "swing-row";
      var knee = s.verdicts.leg_load.knee_deg;
      row.innerHTML = '<span class="dot"></span><span class="t"></span><span class="lbl"></span><span class="knee"></span>';
      row.querySelector(".dot").classList.add(s.overall.grade || "ok");
      row.querySelector(".t").textContent = s.time_s + "s";
      row.querySelector(".lbl").textContent = s.overall.label + " — " + (s.overall.note || "");
      row.querySelector(".knee").textContent = knee != null ? Math.round(knee) + "°" : "—";
      if (s.skeleton && res.skeleton_connections) {
        row.classList.add("expandable");
        row.setAttribute("role", "button");
        row.tabIndex = 0;
        var rep = document.createElement("span");
        rep.className = "rep";
        rep.textContent = "▸";
        row.appendChild(rep);
        function toggle() { toggleReplay(row, s, res); }
        row.addEventListener("click", toggle);
        row.addEventListener("keydown", function (e) {
          if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
        });
      }
      host.appendChild(row);
    });

    initReelCard(res.job_id);

    rememberJob(res.job_id, strokeName + " vs " + res.benchmark.display + " · " +
      agg.good_hand_path + "/" + agg.n + " good", {
        avg_score: dist && dist.avg_score != null ? dist.avg_score : (agg.good_hand_path / agg.n),
        knee: t.leg_load.avg_knee_deg,
        speed_max: agg.speed ? agg.speed.max_kmh : null,
      });
    location.hash = "report/" + res.job_id;
    show("report");
  }

  // ---------- coach reel ----------
  var reel = { timer: null, jobId: null };
  function stopReelPoll() {
    if (reel.timer) { clearInterval(reel.timer); reel.timer = null; }
  }
  function setReelStatus(text) {
    $("reel-status").hidden = !text;
    $("reel-status").textContent = text || "";
  }
  function initReelCard(jobId) {
    stopReelPoll();
    reel.jobId = jobId;
    $("reel-video").hidden = true;
    $("reel-video").removeAttribute("src");
    $("reel-dl").hidden = true;
    $("btn-reel").hidden = false;
    $("btn-reel").disabled = false;
    setReelStatus("");
    fetchReel(jobId);   // a reel may already exist for this job
  }
  $("btn-reel").addEventListener("click", function () {
    var jobId = reel.jobId;
    if (!jobId) return;
    $("btn-reel").disabled = true;
    setReelStatus("Queuing render…");
    fetch(API_BASE + "/api/jobs/" + jobId + "/reel", {
      method: "POST",
      headers: { "X-Invite-Code": invite() },
    }).then(function (r) {
      if (r.status === 202 || r.ok) { startReelPoll(jobId); return; }
      return r.json().then(function (b) {
        throw new Error((b && b.detail && b.detail.message) || "HTTP " + r.status);
      });
    }).catch(function (err) {
      $("btn-reel").disabled = false;
      setReelStatus(err.message || "Could not start the reel - try again.");
    });
  });
  function startReelPoll(jobId) {
    stopReelPoll();
    fetchReel(jobId);
    reel.timer = setInterval(function () { fetchReel(jobId); }, REEL_POLL_MS);
  }
  function fetchReel(jobId) {
    fetch(API_BASE + "/api/jobs/" + jobId + "/reel", {
      headers: { "X-Invite-Code": invite() },
    }).then(function (r) { return r.ok ? r.json() : null; })
      .then(function (st) {
        if (!st || jobId !== reel.jobId) return;
        if (st.reel_status === "done" && st.token) {
          stopReelPoll();
          var url = API_BASE + "/api/jobs/" + jobId + "/reel.mp4?t=" + st.token;
          $("reel-video").src = url;
          $("reel-video").hidden = false;
          $("reel-dl").href = url + "&dl=1";
          $("reel-dl").hidden = false;
          $("btn-reel").hidden = true;
          setReelStatus(st.meta && st.meta.swings
            ? st.meta.swings + " swings · 0.5× speed" : "");
        } else if (st.reel_status === "queued" || st.reel_status === "running") {
          $("btn-reel").disabled = true;
          setReelStatus("Rendering… " +
            (st.reel_percent != null ? st.reel_percent + "%" : "this takes a couple of minutes"));
          if (!reel.timer) startReelPoll(jobId);
        } else if (st.reel_status === "error") {
          stopReelPoll();
          $("btn-reel").disabled = false;
          setReelStatus((st.error && st.error.message) || "Render failed - try again.");
        }
        // reel_status null = never requested: leave the button as-is
      }).catch(function () { /* transient; next poll retries */ });
  }

  // ---------- skeleton replay ----------
  var replay = { raf: null, detail: null, row: null };
  function stopReplay() {
    if (replay.raf) cancelAnimationFrame(replay.raf);
    if (replay.detail) replay.detail.remove();
    if (replay.row) {
      var r = replay.row.querySelector(".rep");
      if (r) r.textContent = "▸";
    }
    replay = { raf: null, detail: null, row: null };
  }
  function toggleReplay(row, swing, res) {
    var wasOpen = replay.row === row;
    stopReplay();
    if (wasOpen) return;
    var det = document.createElement("div");
    det.className = "swing-detail";
    var cv = document.createElement("canvas");
    cv.width = 300; cv.height = 220;
    var hint = document.createElement("div");
    hint.className = "hint";
    hint.textContent = "0.5× replay · tap to pause";
    det.appendChild(cv);
    det.appendChild(hint);
    row.after(det);
    row.querySelector(".rep").textContent = "▾";
    replay.detail = det;
    replay.row = row;
    runSkeleton(cv, swing.skeleton, res);
  }
  function runSkeleton(cv, skel, res) {
    var ctx = cv.getContext("2d");
    var conns = res.skeleton_connections;
    var wristI = res.skeleton_wrist_i || 0;
    var vw = (res.video && res.video.width) || 1920;
    var vh = (res.video && res.video.height) || 1080;
    var fps = (res.video && res.video.fps) || 30;
    var frames = skel.frames || [];

    // bounding box over all present frames, in video-pixel space (keeps proportions)
    var minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    frames.forEach(function (f) {
      if (!f) return;
      f.forEach(function (p) {
        var x = p[0] * vw, y = p[1] * vh;
        if (x < minX) minX = x; if (x > maxX) maxX = x;
        if (y < minY) minY = y; if (y > maxY) maxY = y;
      });
    });
    if (!isFinite(minX)) return;
    var pad = 18;
    var scale = Math.min((cv.width - 2 * pad) / Math.max(1, maxX - minX),
                         (cv.height - 2 * pad) / Math.max(1, maxY - minY));
    var offX = (cv.width - (maxX - minX) * scale) / 2 - minX * scale;
    var offY = (cv.height - (maxY - minY) * scale) / 2 - minY * scale;
    function px(p) { return [p[0] * vw * scale + offX, p[1] * vh * scale + offY]; }

    function markerFrame(i) {
      // nearest present frame at or before i (marker may land on a null frame)
      for (var k = i; k >= 0; k--) if (frames[k]) return frames[k];
      return null;
    }
    var markers = [
      { i: skel.low_i, label: "LOW", color: "#3DDC84" },
      { i: skel.contact_i, label: "CONTACT", color: "#F3EDE0" },
      { i: skel.fin_i, label: "FINISH", color: "#3DDC84" },
    ];

    function draw(upto) {
      ctx.clearRect(0, 0, cv.width, cv.height);
      // wrist trail up to the current frame
      ctx.strokeStyle = "#FFB84D";
      ctx.lineWidth = 2.2;
      ctx.lineCap = "round";
      ctx.beginPath();
      var started = false;
      for (var i = 0; i <= upto && i < frames.length; i++) {
        if (!frames[i]) continue;
        var w = px(frames[i][wristI]);
        if (!started) { ctx.moveTo(w[0], w[1]); started = true; }
        else ctx.lineTo(w[0], w[1]);
      }
      ctx.stroke();
      // markers already passed
      markers.forEach(function (m) {
        if (upto < m.i) return;
        var mf = markerFrame(m.i);
        if (!mf) return;
        var w = px(mf[wristI]);
        ctx.fillStyle = m.color;
        ctx.beginPath();
        ctx.arc(w[0], w[1], 3.2, 0, Math.PI * 2);
        ctx.fill();
        ctx.font = "7px ui-monospace, Consolas, monospace";
        ctx.textAlign = "center";
        ctx.fillText(m.label, Math.min(cv.width - 20, Math.max(20, w[0])), Math.min(cv.height - 4, w[1] + 12));
      });
      // skeleton for the current frame
      var f = frames[upto] || markerFrame(upto);
      if (f) {
        ctx.strokeStyle = "#3DDC84";
        ctx.lineWidth = 2;
        conns.forEach(function (c) {
          var a = px(f[c[0]]), b = px(f[c[1]]);
          ctx.beginPath();
          ctx.moveTo(a[0], a[1]);
          ctx.lineTo(b[0], b[1]);
          ctx.stroke();
        });
        ctx.fillStyle = "#F3EDE0";
        f.forEach(function (p) {
          var w = px(p);
          ctx.beginPath();
          ctx.arc(w[0], w[1], 1.6, 0, Math.PI * 2);
          ctx.fill();
        });
      }
    }

    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      draw(frames.length - 1);   // static: full trail + final pose, all markers
      return;
    }

    // draw the contact pose immediately so the panel is never blank, even if
    // rAF is throttled (hidden tab) before the animation gets going
    draw(Math.min(skel.contact_i || 0, frames.length - 1));

    // frames are every 2nd source frame; play at half speed => 4/fps seconds each
    var frameMs = 4000 / fps;
    var idx = 0, acc = 0, prevT = null, paused = false;
    cv.addEventListener("click", function () { paused = !paused; prevT = null; });
    function tick(t) {
      replay.raf = requestAnimationFrame(tick);
      if (paused) return;
      if (prevT == null) prevT = t;
      acc += t - prevT;
      prevT = t;
      if (acc < frameMs) return;
      acc = 0;
      draw(idx);
      idx = (idx + 1) % frames.length;
    }
    replay.raf = requestAnimationFrame(tick);
  }

  function drawHandPath(swing) {
    var svg = $("rp-path-svg");
    svg.textContent = "";
    var pts = (swing.hand_path && swing.hand_path.points) || [];
    if (pts.length < 3) return;
    var xs = pts.map(function (p) { return p[0]; });
    var ys = pts.map(function (p) { return p[1]; });
    var minX = Math.min.apply(null, xs), maxX = Math.max.apply(null, xs);
    var minY = Math.min.apply(null, ys), maxY = Math.max.apply(null, ys);
    var pad = 8, W = 300, H = 86;
    function sx(x) { return pad + (W - 2 * pad) * (x - minX) / ((maxX - minX) || 1); }
    function sy(y) { return H - pad - (H - 2 * pad) * (y - minY) / ((maxY - minY) || 1); }
    var ns = "http://www.w3.org/2000/svg";

    // contact plane line at y=0 (the contact point's height)
    if (minY < 0 && maxY > 0) {
      var line = document.createElementNS(ns, "line");
      line.setAttribute("x1", 0); line.setAttribute("x2", W);
      line.setAttribute("y1", sy(0)); line.setAttribute("y2", sy(0));
      line.setAttribute("stroke", "#2B2F38"); line.setAttribute("stroke-width", "1");
      svg.appendChild(line);
    }
    var d = pts.map(function (p, i) { return (i ? "L" : "M") + sx(p[0]).toFixed(1) + " " + sy(p[1]).toFixed(1); }).join(" ");
    var path = document.createElementNS(ns, "path");
    path.setAttribute("d", d);
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "#FFB84D");
    path.setAttribute("stroke-width", "2.6");
    path.setAttribute("stroke-linecap", "round");
    svg.appendChild(path);

    [["low", swing.hand_path.low], ["contact", swing.hand_path.contact], ["finish", swing.hand_path.finish]]
      .forEach(function (m) {
        if (!m[1]) return;
        var c = document.createElementNS(ns, "circle");
        c.setAttribute("cx", sx(m[1][0])); c.setAttribute("cy", sy(m[1][1])); c.setAttribute("r", "3.4");
        c.setAttribute("fill", m[0] === "contact" ? "#F3EDE0" : "#3DDC84");
        svg.appendChild(c);
        var t = document.createElementNS(ns, "text");
        t.setAttribute("x", Math.min(W - 24, Math.max(10, sx(m[1][0]))));
        t.setAttribute("y", Math.min(H - 3, sy(m[1][1]) + 14));
        t.setAttribute("font-size", "7");
        t.setAttribute("font-family", "SF Mono, ui-monospace, Consolas, monospace");
        t.setAttribute("fill", m[0] === "contact" ? "#F3EDE0" : "#3DDC84");
        t.setAttribute("text-anchor", "middle");
        t.textContent = m[0].toUpperCase();
        svg.appendChild(t);
      });
  }

  // ---------- deep link ----------
  function boot() {
    renderPast();
    var m = location.hash.match(/^#(?:job|report)\/([a-f0-9]{32})$/);
    if (m && invite()) {
      openJob(m[1]);
    }
  }
  boot();
})();
