(function () {
  "use strict";
  var D = JSON.parse(document.getElementById("data").textContent);
  var $ = function (id) { return document.getElementById(id); };
  var cv = $("cv"), ctx = cv.getContext("2d");
  var M = null, G = 0, t = 0, timer = null;
  var PRICE_LO = 0.002;   // the price colour scale starts at 0.2% of the run's maximum
  var PRICE_STOPS = [[0, [255, 255, 255]], [0.33, [255, 236, 150]], [0.66, [240, 150, 50]], [1, [150, 25, 20]]];
  var CLR_STOPS = [[0, [150, 25, 20]], [0.25, [240, 150, 50]], [0.4, [255, 236, 150]], [0.6, [223, 238, 224]], [1, [255, 255, 255]]];
  function bytes(s) { var b = atob(s), u = new Uint8Array(b.length); for (var i = 0; i < b.length; i++) u[i] = b.charCodeAt(i); return u; }
  function ramp(stops, v) {
    v = Math.max(0, Math.min(1, v));
    for (var k = 1; k < stops.length; k++) if (v <= stops[k][0]) {
      var a = stops[k - 1], b = stops[k], u = (v - a[0]) / (b[0] - a[0]);
      return [0, 1, 2].map(function (j) { return Math.round(a[1][j] * (1 - u) + b[1][j] * u); });
    }
    return stops[stops.length - 1][1];
  }
  function unrle(b64, n) {
    var raw = bytes(b64), out = new Uint8Array(n), o = 0;
    for (var k = 0; k + 1 < raw.length; k += 2) { out.fill(raw[k], o, o + raw[k + 1]); o += raw[k + 1]; }
    return out;
  }
  var STATES = null, CLRS = null, PRIS = null, loaded = 0;
  function readVarint(raw, pos) {
    var n = 0, shift = 0, b;
    do { b = raw[pos++]; n += (b & 0x7F) * Math.pow(2, shift); shift += 7; } while (b & 0x80);
    return [n, pos];
  }
  function applyDelta(raw, C, P) {
    // raw: count, then (gap, clearance, price) per changed cell; the arrays hold the previous capture's values
    var p = readVarint(raw, 0), count = p[0], pos = p[1], idx = -1;
    for (var j = 0; j < count; j++) {
      var q = readVarint(raw, pos); pos = q[1]; idx += q[0] + 1;
      C[idx] = raw[pos++]; P[idx] = raw[pos++];
    }
  }
  function loadStates(done) {
    // decode every capture's map, clearance and price before the robot is allowed to move: no robot on unloaded cells
    var n = M.poses.length, N = M.crop.H * M.crop.W;
    STATES = new Array(n); CLRS = new Array(n); PRIS = new Array(n); loaded = 0;
    var curC = new Uint8Array(N), curP = new Uint8Array(N);
    $("play").disabled = true; $("slider").disabled = true;
    $("result").textContent = "loading the map: 0 of " + n + " captures";
    function step() {
      var end = Math.min(loaded + 12, n);
      for (; loaded < end; loaded++) {
        STATES[loaded] = unrle(M.states[loaded], N);
        applyDelta(bytes(M.deltas[loaded]), curC, curP);
        CLRS[loaded] = curC.slice(); PRIS[loaded] = curP.slice();
      }
      if (loaded < n) { $("result").textContent = "loading the map: " + loaded + " of " + n + " captures"; setTimeout(step, 0); }
      else { $("play").disabled = false; $("slider").disabled = false; done(); }
    }
    step();
  }
  function fieldAt(i) { return { state: STATES[i], price: PRIS[i], clr: CLRS[i] }; }
  function toPx(x, y) { return [x / 0.05 - M.crop.ox, M.crop.H - 1 - (y / 0.05 - M.crop.oy)]; }
  function drawImage(i) {
    var d = fieldAt(i), H = M.crop.H, W = M.crop.W, byClr = $("colour").value === "clr";
    cv.width = W; cv.height = H;
    var img = ctx.createImageData(W, H), px = img.data;
    var pLo = PRICE_LO * M.pmax;
    for (var r = 0; r < H; r++) for (var c = 0; c < W; c++) {
      var src = (H - 1 - r) * W + c, o = (r * W + c) * 4, s = d.state[src], col;
      if (s === 1) {
        if (byClr) col = ramp(CLR_STOPS, (d.clr[src] / 255 * M.clr_max - 0.25) / 0.75);
        else {
          var phi = d.price[src] / 255 * M.pmax;
          col = phi >= pLo ? ramp(PRICE_STOPS, (Math.log(phi) - Math.log(pLo)) / (Math.log(M.pmax) - Math.log(pLo))) : [255, 255, 255];
        }
      } else if (s === 2) col = $("l_band").checked ? [188, 212, 234] : [236, 236, 236];
      else if (s === 3) col = $("l_obs").checked ? [17, 17, 17] : [236, 236, 236];
      else if (s === 4) col = $("l_obs").checked ? [75, 44, 110] : [236, 236, 236];
      else col = $("l_unk").checked ? [214, 214, 214] : [236, 236, 236];
      px[o] = col[0]; px[o + 1] = col[1]; px[o + 2] = col[2]; px[o + 3] = 255;
    }
    ctx.putImageData(img, 0, 0);
    if ($("l_charts").checked) {
      M.charts[i].forEach(function (c) { var p = toPx(c[0], c[1]); ctx.fillStyle = "#1e6fc8"; ctx.beginPath(); ctx.arc(p[0], p[1], 2.5, 0, 6.3); ctx.fill(); });
    }
  }
  function drawRoute(route) {
    if (!$("l_route").checked || !route) return;
    ctx.lineJoin = "round"; ctx.lineCap = "round";
    ctx.strokeStyle = "rgba(200,30,30,0.35)"; ctx.lineWidth = 10; ctx.beginPath();
    route.forEach(function (q, j) { var p = toPx(q[0], q[1]); if (j) ctx.lineTo(p[0], p[1]); else ctx.moveTo(p[0], p[1]); });
    ctx.stroke();
    ctx.strokeStyle = "#c81e1e"; ctx.lineWidth = 2; ctx.beginPath();
    route.forEach(function (q, j) { var p = toPx(q[0], q[1]); if (j) ctx.lineTo(p[0], p[1]); else ctx.moveTo(p[0], p[1]); });
    ctx.stroke();
  }
  function drawGoal(i) {
    var g = M.goals[G], p = toPx(g.x, g.y);
    ctx.fillStyle = "#2e8b57"; ctx.beginPath(); ctx.arc(p[0], p[1], 3, 0, 6.3); ctx.fill();
    ctx.strokeStyle = "#2e8b57"; ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(p[0], p[1], 7, 0, 6.3); ctx.stroke();
  }
  function drawRobot(i) {
    var P = M.poses;
    if ($("l_trail").checked) {
      ctx.strokeStyle = "#7a7a7a"; ctx.lineWidth = 1.5; ctx.beginPath();
      for (var j = 0; j <= i; j++) { var p = toPx(P[j][0], P[j][1]); if (j) ctx.lineTo(p[0], p[1]); else ctx.moveTo(p[0], p[1]); }
      ctx.stroke();
    }
    var q = P[i], h = 0.25, cs = Math.cos(q[2]), sn = Math.sin(q[2]);
    var corners = [[h, h], [-h, h], [-h, -h], [h, -h]].map(function (u) {
      return toPx(q[0] + u[0] * cs - u[1] * sn, q[1] + u[0] * sn + u[1] * cs);
    });
    ctx.fillStyle = "rgba(17,17,17,0.85)"; ctx.beginPath();
    corners.forEach(function (p, j) { if (j) ctx.lineTo(p[0], p[1]); else ctx.moveTo(p[0], p[1]); });
    ctx.closePath(); ctx.fill();
    var nose = toPx(q[0] + h * cs, q[1] + h * sn), mid = toPx(q[0], q[1]);
    ctx.strokeStyle = "#ffffff"; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.moveTo(mid[0], mid[1]); ctx.lineTo(nose[0], nose[1]); ctx.stroke();
  }
  function draw() {
    if (!STATES) return;
    var i = t;
    drawImage(i);
    drawRoute(M.routes[G][i]);
    drawGoal(i);
    drawRobot(i);
    var q = M.poses[i], c = M.checks[G][i];
    $("when").textContent = "capture " + (i + 1) + " of " + M.poses.length + " · robot " + q[0].toFixed(2) + ", " + q[1].toFixed(2) + " m";
    var txt, cls = "";
    if (c.fails.length) { cls = "bad"; txt = "goal " + (G + 1) + ": checks failing: " + c.fails.join(", "); }
    else if (c.length != null) txt = "goal " + (G + 1) + ": route " + c.length.toFixed(2) + " m, all checks pass";
    else txt = "goal " + (G + 1) + ": no route at this capture";
    $("result").innerHTML = '<span class="' + cls + '">' + txt + "</span>";
  }
  function setT(v) { t = Math.max(0, Math.min(M.poses.length - 1, v)); $("slider").value = t; draw(); }
  function stop() { if (timer) { clearInterval(timer); timer = null; } $("play").textContent = "play"; $("play").className = ""; }
  function summary() {
    var rows = [];
    Object.keys(D[$("map").value]).forEach(function (b) {
      var R = D[$("map").value][b], n = 0, w = 0, f = 0, reasons = {};
      R.checks.forEach(function (per) { per.forEach(function (c) { n++; if (c.length != null) w++; if (c.fails.length) f++; c.fails.forEach(function (x) { reasons[x] = (reasons[x] || 0) + 1; }); }); });
      rows.push("<tr><td>" + b + "</td><td>" + n + "</td><td>" + w + "</td><td>" + f + "</td><td>" + (Object.keys(reasons).map(function (x) { return x + " " + reasons[x]; }).join(", ") || "none") + "</td></tr>");
    });
    $("summary").innerHTML = "<tr><th>branch</th><th>capture-goal pairs</th><th>with route</th><th>failing</th><th>reasons</th></tr>" + rows.join("");
  }
  function loadBranch() {
    M = D[$("map").value][$("branch").value];
    STATES = null;
    $("slider").max = M.poses.length - 1;
    var gs = $("goal"); gs.innerHTML = "";
    M.goals.forEach(function (g, k) { var o = document.createElement("option"); o.value = k; o.textContent = "goal " + (k + 1) + " (recorded capture " + (g.i + 1) + ")"; gs.appendChild(o); });
    G = Math.min(G, M.goals.length - 1); gs.value = G;
    stop();
    summary();
    t = Math.min(t, M.poses.length - 1);
    loadStates(function () { setT(t); });
  }
  function loadMap() {
    var b = $("branch"); b.innerHTML = "";
    Object.keys(D[$("map").value]).forEach(function (k) { var o = document.createElement("option"); o.value = k; o.textContent = k; b.appendChild(o); });
    t = 0; loadBranch();
  }
  var mapSel = $("map");
  Object.keys(D).forEach(function (m) { var o = document.createElement("option"); o.value = m; o.textContent = m; mapSel.appendChild(o); });
  $("slider").addEventListener("input", function () { stop(); setT(+$("slider").value); });
  mapSel.addEventListener("change", loadMap);
  $("branch").addEventListener("change", loadBranch);
  $("goal").addEventListener("change", function () { G = +$("goal").value; draw(); });
  ["colour", "l_band", "l_obs", "l_unk", "l_route", "l_trail", "l_charts"].forEach(function (id) { $(id).addEventListener("change", draw); });
  $("play").addEventListener("click", function () {
    if (timer) { stop(); return; }
    $("play").textContent = "pause"; $("play").className = "on";
    if (t >= M.poses.length - 1) setT(0);
    timer = setInterval(function () { if (t >= M.poses.length - 1) { stop(); return; } setT(t + 1); }, +$("speed").value);
  });
  var qs = new URLSearchParams(location.search);
  if (qs.get("map") && D[qs.get("map")]) mapSel.value = qs.get("map");
  loadMap();
  if (qs.get("branch") && D[mapSel.value][qs.get("branch")]) { $("branch").value = qs.get("branch"); loadBranch(); }
  if (qs.get("goal")) { G = +qs.get("goal"); $("goal").value = G; }
  if (qs.get("t")) setT(+qs.get("t")); else draw();
})();

