(function () {
  "use strict";

  /* —— Sidebar drawer (mobile, from right) —— */
  var sidebar = document.getElementById("sidebar");
  var backdrop = document.getElementById("sidebar-backdrop");
  var toggle = document.getElementById("menu-toggle");

  function setOpen(open) {
    if (!sidebar) return;
    document.body.classList.toggle("sidebar-open", open);
    if (toggle) toggle.setAttribute("aria-expanded", open ? "true" : "false");
    if (backdrop) backdrop.hidden = !open;
  }

  if (toggle) {
    toggle.addEventListener("click", function () {
      setOpen(!document.body.classList.contains("sidebar-open"));
    });
  }
  if (backdrop) {
    backdrop.addEventListener("click", function () {
      setOpen(false);
    });
  }
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") setOpen(false);
  });
  window.addEventListener("resize", function () {
    if (window.matchMedia("(min-width: 900px)").matches) setOpen(false);
  });

  /* —— Custom select —— */
  function initCustomSelects() {
    document.querySelectorAll("[data-custom-select]").forEach(function (root) {
      if (root.dataset.ready) return;
      root.dataset.ready = "1";

      var native = root.querySelector("select");
      var trigger = root.querySelector(".cs-trigger");
      var labelEl = root.querySelector(".cs-label");
      var list = root.querySelector(".cs-list");
      if (!native || !trigger || !labelEl || !list) return;

      function syncLabel() {
        var opt = native.options[native.selectedIndex];
        labelEl.textContent = opt ? opt.textContent : "";
        labelEl.title = opt ? opt.value : "";
        list.querySelectorAll(".cs-option").forEach(function (li) {
          li.classList.toggle("selected", li.dataset.value === native.value);
        });
      }

      function close() {
        root.classList.remove("open");
        trigger.setAttribute("aria-expanded", "false");
      }

      function open() {
        root.classList.add("open");
        trigger.setAttribute("aria-expanded", "true");
        var sel = list.querySelector(".cs-option.selected");
        if (sel) sel.scrollIntoView({ block: "nearest" });
      }

      list.innerHTML = "";
      Array.prototype.forEach.call(native.options, function (opt) {
        var li = document.createElement("li");
        li.className = "cs-option";
        li.setAttribute("role", "option");
        li.dataset.value = opt.value;
        li.textContent = opt.textContent;
        li.title = opt.value;
        if (opt.selected) li.classList.add("selected");
        li.addEventListener("click", function () {
          native.value = opt.value;
          syncLabel();
          close();
          native.dispatchEvent(new Event("change", { bubbles: true }));
        });
        list.appendChild(li);
      });

      trigger.addEventListener("click", function (e) {
        e.preventDefault();
        if (root.classList.contains("open")) close();
        else open();
      });

      document.addEventListener("click", function (e) {
        if (!root.contains(e.target)) close();
      });

      trigger.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          if (root.classList.contains("open")) close();
          else open();
        } else if (e.key === "Escape") {
          close();
        }
      });

      native.addEventListener("change", syncLabel);
      syncLabel();
    });
  }

  initCustomSelects();

  /* —— Monitor live refresh —— */
  var monitorRoot = document.getElementById("monitor-root");
  if (monitorRoot) {
    var statsUrl = monitorRoot.dataset.statsUrl;
    var intervalMs = 3000;

    function fmtBytesFromKb(kb) {
      var b = kb * 1024;
      if (b < 1024) return b + " B";
      var units = ["KB", "MB", "GB", "TB"];
      var v = b / 1024;
      var i = 0;
      while (v >= 1024 && i < units.length - 1) {
        v /= 1024;
        i++;
      }
      return v.toFixed(v >= 100 ? 0 : 1) + " " + units[i];
    }

    function fmtBytes(b) {
      if (b < 1024) return b + " B";
      var units = ["KB", "MB", "GB", "TB"];
      var v = b / 1024;
      var i = 0;
      while (v >= 1024 && i < units.length - 1) {
        v /= 1024;
        i++;
      }
      return v.toFixed(v >= 100 ? 0 : 1) + " " + units[i];
    }

    function fmtUptime(sec) {
      if (sec == null) return "—";
      sec = Math.floor(sec);
      var d = Math.floor(sec / 86400);
      var h = Math.floor((sec % 86400) / 3600);
      var m = Math.floor((sec % 3600) / 60);
      var parts = [];
      if (d) parts.push(d + " روز");
      if (h || d) parts.push(h + " ساعت");
      parts.push(m + " دقیقه");
      return parts.join(" و ");
    }

    function levelClass(pct) {
      if (pct >= 90) return "crit";
      if (pct >= 75) return "warn";
      return "ok";
    }

    function setBar(el, pct) {
      if (!el) return;
      var fill = el.querySelector(".bar-fill");
      var num = el.querySelector(".meter-value");
      var p = Math.max(0, Math.min(100, pct || 0));
      if (fill) {
        fill.style.width = p + "%";
        fill.className = "bar-fill " + levelClass(p);
      }
      if (num) num.textContent = p.toFixed(1) + "%";
      el.classList.remove("ok", "warn", "crit");
      el.classList.add(levelClass(p));
    }

    function render(data) {
      setBar(document.getElementById("meter-cpu"), data.cpu_percent);
      setBar(document.getElementById("meter-mem"), data.memory && data.memory.percent);
      setBar(document.getElementById("meter-swap"), data.swap && data.swap.percent);

      var memDetail = document.getElementById("mem-detail");
      if (memDetail && data.memory) {
        memDetail.textContent =
          fmtBytesFromKb(data.memory.used_kb) +
          " از " +
          fmtBytesFromKb(data.memory.total_kb) +
          " (آزاد: " +
          fmtBytesFromKb(data.memory.available_kb) +
          ")";
      }

      var swapDetail = document.getElementById("swap-detail");
      if (swapDetail && data.swap) {
        if (!data.swap.total_kb) {
          swapDetail.textContent = "سواپ فعال نیست";
        } else {
          swapDetail.textContent =
            fmtBytesFromKb(data.swap.used_kb) +
            " از " +
            fmtBytesFromKb(data.swap.total_kb);
        }
      }

      var loadEl = document.getElementById("loadavg");
      if (loadEl && data.loadavg) {
        loadEl.textContent = data.loadavg.map(function (n) {
          return n.toFixed(2);
        }).join("  ·  ");
      }

      var upEl = document.getElementById("uptime");
      if (upEl) upEl.textContent = fmtUptime(data.uptime_seconds);

      var procEl = document.getElementById("proc-count");
      if (procEl) {
        procEl.textContent =
          data.process_count != null ? String(data.process_count) : "—";
      }

      var ts = document.getElementById("stats-ts");
      if (ts && data.timestamp) {
        try {
          ts.textContent = new Date(data.timestamp).toLocaleString("fa-IR");
        } catch (_) {
          ts.textContent = data.timestamp;
        }
      }

      var diskBox = document.getElementById("disk-meters");
      if (diskBox && Array.isArray(data.disks)) {
        diskBox.innerHTML = "";
        data.disks.forEach(function (d) {
          var card = document.createElement("div");
          card.className = "meter-card " + levelClass(d.percent);
          card.innerHTML =
            '<div class="meter-head">' +
            '<span class="meter-name ltr">' +
            d.path +
            "</span>" +
            '<span class="meter-value">' +
            d.percent.toFixed(1) +
            "%</span></div>" +
            '<div class="bar"><div class="bar-fill ' +
            levelClass(d.percent) +
            '" style="width:' +
            d.percent +
            '%"></div></div>' +
            '<p class="meter-detail">' +
            fmtBytes(d.used) +
            " از " +
            fmtBytes(d.total) +
            " (آزاد: " +
            fmtBytes(d.free) +
            ")</p>";
          diskBox.appendChild(card);
        });
      }
    }

    function tick() {
      if (document.hidden) return;
      fetch(statsUrl, { credentials: "same-origin", headers: { Accept: "application/json" } })
        .then(function (r) {
          if (!r.ok) throw new Error("bad status");
          return r.json();
        })
        .then(render)
        .catch(function () {
          /* keep last values */
        });
    }

    tick();
    setInterval(tick, intervalMs);
  }

  /* —— Interactive console —— */
  var consoleRoot = document.getElementById("console");
  if (consoleRoot) {
    var apiUrl = consoleRoot.dataset.apiUrl;
    var linesEl = document.getElementById("console-lines");
    var form = document.getElementById("console-form");
    var input = document.getElementById("console-input");
    var body = document.getElementById("console-body");
    var history = [];
    var histIdx = -1;
    var draft = "";
    var busy = false;

    function scrollBottom() {
      if (linesEl) linesEl.scrollTop = linesEl.scrollHeight;
    }

    function appendEl(tag, className, text) {
      var el = document.createElement(tag);
      if (className) el.className = className;
      if (text != null) el.textContent = text;
      linesEl.appendChild(el);
      scrollBottom();
      return el;
    }

    function appendCmd(cmd) {
      var row = document.createElement("div");
      row.className = "console-cmd";
      var prompt = document.createElement("span");
      prompt.className = "console-prompt";
      prompt.textContent = "root@container:~#";
      row.appendChild(prompt);
      row.appendChild(document.createTextNode(" "));
      var cmdSpan = document.createElement("span");
      cmdSpan.className = "ltr";
      cmdSpan.textContent = cmd;
      row.appendChild(cmdSpan);
      linesEl.appendChild(row);
      scrollBottom();
    }

    function setBusy(on) {
      busy = on;
      if (input) input.disabled = on;
      var btn = form && form.querySelector(".console-run");
      if (btn) btn.disabled = on;
    }

    function clearScreen() {
      linesEl.innerHTML = "";
      appendEl("div", "console-sys", "صفحه پاک شد.");
    }

    function runCommand(cmd) {
      cmd = (cmd || "").trim();
      if (!cmd || busy) return;

      if (cmd === "clear" || cmd === "cls") {
        appendCmd(cmd);
        clearScreen();
        input.value = "";
        histIdx = -1;
        return;
      }

      if (!history.length || history[history.length - 1] !== cmd) {
        history.push(cmd);
      }
      histIdx = -1;
      draft = "";

      appendCmd(cmd);
      input.value = "";
      var busyEl = appendEl("div", "console-busy", "در حال اجرا…");
      setBusy(true);

      fetch(apiUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ command: cmd }),
      })
        .then(function (r) {
          if (!r.ok) throw new Error("bad status");
          return r.json();
        })
        .then(function (data) {
          if (busyEl.parentNode) busyEl.parentNode.removeChild(busyEl);
          var out = data.output || "";
          if (out) {
            var pre = appendEl("pre", "console-out" + (data.ok === false ? " err" : ""), out);
            void pre;
          }
          if (data.exit_code != null) {
            appendEl(
              "div",
              "console-exit" + (data.exit_code !== 0 ? " fail" : ""),
              "[exit " + data.exit_code + "]"
            );
          } else if (!data.ok && !out) {
            appendEl("div", "console-out err", "اجرا ناموفق بود.");
          }
        })
        .catch(function () {
          if (busyEl.parentNode) busyEl.parentNode.removeChild(busyEl);
          appendEl("div", "console-out err", "ارتباط با سرور برقرار نشد.");
        })
        .finally(function () {
          setBusy(false);
          if (input) input.focus();
          scrollBottom();
        });
    }

    if (form) {
      form.addEventListener("submit", function (e) {
        e.preventDefault();
        runCommand(input.value);
      });
    }

    if (input) {
      input.addEventListener("keydown", function (e) {
        if (e.key === "ArrowUp") {
          e.preventDefault();
          if (!history.length) return;
          if (histIdx < 0) {
            draft = input.value;
            histIdx = history.length - 1;
          } else if (histIdx > 0) {
            histIdx -= 1;
          }
          input.value = history[histIdx];
        } else if (e.key === "ArrowDown") {
          e.preventDefault();
          if (histIdx < 0) return;
          if (histIdx < history.length - 1) {
            histIdx += 1;
            input.value = history[histIdx];
          } else {
            histIdx = -1;
            input.value = draft;
          }
        } else if (e.key === "l" && e.ctrlKey) {
          e.preventDefault();
          clearScreen();
        } else if (e.key === "c" && e.ctrlKey && busy) {
          /* no cancel support with current mechanism — ignore */
        }
      });
    }

    if (body) {
      body.addEventListener("click", function (e) {
        if (e.target === body || e.target === linesEl || e.target.classList.contains("console-sys")) {
          if (input && !busy) input.focus();
        }
      });
    }

    if (input) input.focus();
  }
})();
