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

  /* —— Agent chat room —— */
  var chatRoot = document.getElementById("chat-room");
  if (chatRoot) {
    var chatApi = chatRoot.dataset.apiUrl;
    var chatReady = chatRoot.dataset.ready === "1";
    var chatMessages = document.getElementById("chat-messages");
    var chatForm = document.getElementById("chat-form");
    var chatInput = document.getElementById("chat-input");
    var chatStatus = document.getElementById("chat-status");
    var chatClear = document.getElementById("chat-clear");
    var chatHistory = [];
    var chatBusy = false;
    var chatAbort = null;
    var chatTurnId = null;
    var chatCancelUrl = chatRoot.dataset.cancelUrl || "";
    var chatCancelRequested = false;
    var chatSettled = false;
    var phraseTimer = null;
    var loadingPhrases = [
      "در حال وصل شدن به مدل…",
      "در حال خواندن فایل‌های وردپرس…",
      "ایجنت در حال کار روی ورک‌اسپیس است…",
      "چند لحظه، پاسخ را آماده می‌کنم…"
    ];

    function setChatStatus(text, kind) {
      if (!chatStatus) return;
      chatStatus.textContent = text;
      chatStatus.classList.remove("busy", "err");
      if (kind) chatStatus.classList.add(kind);
    }

    function scrollChat() {
      if (chatMessages) chatMessages.scrollTop = chatMessages.scrollHeight;
    }

    function makeAvatar(letter) {
      var avatar = document.createElement("span");
      avatar.className = "chat-avatar";
      avatar.setAttribute("aria-hidden", "true");
      avatar.textContent = letter;
      return avatar;
    }

    function escapeHtml(s) {
      return String(s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }

    function isWordChar(ch) {
      return !!ch && /[A-Za-z0-9_]/.test(ch);
    }

    function isEscaped(src, index) {
      var slashes = 0;
      var j = index - 1;
      while (j >= 0 && src.charAt(j) === "\\") {
        slashes += 1;
        j -= 1;
      }
      return slashes % 2 === 1;
    }

    function isMdSpecial(ch) {
      return "`*_[]!~\\".indexOf(ch) !== -1;
    }

    function safeUrl(url) {
      var value = String(url || "").trim();
      if (!value || /[\u0000-\u0020"'<>\\]/.test(value)) return "";
      var proto = /^([a-z][a-z0-9+.-]*):/i.exec(value);
      if (!proto) return "";
      var name = proto[1].toLowerCase();
      if (name !== "http" && name !== "https" && name !== "mailto") return "";
      return value;
    }

    function linkify(raw) {
      var re = /https?:\/\/[^\s<]+/g;
      var html = "";
      var last = 0;
      var match;
      while ((match = re.exec(raw))) {
        html += escapeHtml(raw.slice(last, match.index)).replace(/\n/g, "<br>");
        var url = match[0].replace(/[),.;:!?]+$/g, "");
        var tail = match[0].slice(url.length);
        var safe = safeUrl(url);
        if (safe) {
          html +=
            '<a href="' +
            escapeHtml(safe) +
            '" target="_blank" rel="noopener noreferrer">' +
            escapeHtml(url) +
            "</a>";
        } else {
          html += escapeHtml(match[0]);
          tail = "";
        }
        html += escapeHtml(tail);
        last = match.index + match[0].length;
      }
      html += escapeHtml(raw.slice(last)).replace(/\n/g, "<br>");
      return html;
    }

    function findCloser(src, openAt, marker) {
      var from = openAt + marker.length;
      while (from < src.length) {
        var at = src.indexOf(marker, from);
        if (at === -1) return -1;
        if (at < openAt + marker.length + 1) {
          from = at + marker.length;
          continue;
        }
        var prev = src.charAt(at - 1);
        var next = src.charAt(at + marker.length);
        if (/\s/.test(prev) || isEscaped(src, at)) {
          from = at + marker.length;
          continue;
        }
        if (marker.charAt(0) === "_" && isWordChar(next)) {
          from = at + marker.length;
          continue;
        }
        return at;
      }
      return -1;
    }

    function canOpen(src, index, marker) {
      if (isEscaped(src, index)) return false;
      if (src.slice(index, index + marker.length) !== marker) return false;
      var after = src.charAt(index + marker.length);
      if (!after || /\s/.test(after)) return false;
      if (marker.charAt(0) === "_" && isWordChar(index > 0 ? src.charAt(index - 1) : "")) {
        return false;
      }
      return findCloser(src, index, marker) !== -1;
    }

    function parseLink(src, index) {
      if (src.charAt(index) !== "[") return null;
      var close = src.indexOf("]", index + 1);
      if (close === -1 || src.charAt(close + 1) !== "(") return null;
      var end = src.indexOf(")", close + 2);
      if (end === -1) return null;
      var rawUrl = src.slice(close + 2, end).trim();
      var titled = /^(\S+)\s+["'].*["']$/.exec(rawUrl);
      if (titled) rawUrl = titled[1];
      return { text: src.slice(index + 1, close), url: rawUrl, end: end + 1 };
    }

    function renderInline(raw) {
      var src = String(raw == null ? "" : raw);
      var html = "";
      var i = 0;
      var markers = ["~~", "**", "__", "*"];
      var tags = { "~~": "del", "**": "strong", __: "strong", "*": "em" };

      while (i < src.length) {
        if (src.charAt(i) === "\\" && i + 1 < src.length && isMdSpecial(src.charAt(i + 1))) {
          html += escapeHtml(src.charAt(i + 1));
          i += 2;
          continue;
        }

        if (src.charAt(i) === "`" && !isEscaped(src, i)) {
          var ticks = 0;
          while (src.charAt(i + ticks) === "`") ticks += 1;
          var fence = new Array(ticks + 1).join("`");
          var closeCode = src.indexOf(fence, i + ticks);
          if (closeCode !== -1) {
            var code = src.slice(i + ticks, closeCode);
            if (code.length >= 2 && code.charAt(0) === " " && code.charAt(code.length - 1) === " ") {
              code = code.slice(1, -1);
            }
            html += "<code>" + escapeHtml(code) + "</code>";
            i = closeCode + ticks;
            continue;
          }
        }

        if (src.charAt(i) === "!" && src.charAt(i + 1) === "[") {
          var image = parseLink(src, i + 1);
          if (image) {
            var imgUrl = safeUrl(image.url);
            if (imgUrl) {
              html +=
                '<img src="' +
                escapeHtml(imgUrl) +
                '" alt="' +
                escapeHtml(image.text) +
                '" loading="lazy" referrerpolicy="no-referrer">';
            } else if (image.text) {
              html += escapeHtml(image.text);
            }
            i = image.end;
            continue;
          }
        }

        if (src.charAt(i) === "[") {
          var link = parseLink(src, i);
          if (link) {
            var href = safeUrl(link.url);
            var label = renderInline(link.text);
            if (href) {
              html +=
                '<a href="' +
                escapeHtml(href) +
                '" target="_blank" rel="noopener noreferrer">' +
                label +
                "</a>";
            } else {
              html += label;
            }
            i = link.end;
            continue;
          }
        }

        var opened = false;
        for (var m = 0; m < markers.length; m++) {
          var marker = markers[m];
          if (!canOpen(src, i, marker)) continue;
          var endMark = findCloser(src, i, marker);
          html +=
            "<" +
            tags[marker] +
            ">" +
            renderInline(src.slice(i + marker.length, endMark)) +
            "</" +
            tags[marker] +
            ">";
          i = endMark + marker.length;
          opened = true;
          break;
        }
        if (opened) continue;

        var next = i + 1;
        while (next < src.length && !isMdSpecial(src.charAt(next))) next += 1;
        html += linkify(src.slice(i, next));
        i = next;
      }
      return html;
    }

    function isBlank(line) {
      return /^\s*$/.test(line);
    }

    function splitCells(line) {
      var s = line.trim();
      if (s.indexOf("|") === -1) return [];
      if (s.charAt(0) === "|") s = s.slice(1);
      if (s.length && s.charAt(s.length - 1) === "|") s = s.slice(0, -1);
      return s.split("|").map(function (cell) {
        return cell.trim();
      });
    }

    function isTableSep(line) {
      var cells = splitCells(line);
      if (!cells.length) return false;
      for (var c = 0; c < cells.length; c++) {
        if (!/^:?-{3,}:?$/.test(cells[c])) return false;
      }
      return true;
    }

    function isTableAt(lines, index) {
      return (
        index + 1 < lines.length &&
        lines[index].indexOf("|") !== -1 &&
        isTableSep(lines[index + 1])
      );
    }

    function isBlockStart(line) {
      return (
        /^( {0,3})(`{3,}|~{3,})/.test(line) ||
        /^(#{1,6})\s+\S/.test(line) ||
        /^(\*{3,}|-{3,}|_{3,})\s*$/.test(line) ||
        /^>\s?/.test(line) ||
        /^[-*+]\s+\S/.test(line) ||
        /^\d+[.)]\s+\S/.test(line)
      );
    }

    function openingFence(line) {
      var m = /^( {0,3})(`{3,}|~{3,})(.*)$/.exec(line);
      if (!m) return null;
      if (m[2].charAt(0) === "`" && m[3].indexOf("`") !== -1) return null;
      return { mark: m[2].charAt(0), len: m[2].length, info: m[3].trim() };
    }

    function closingFence(line, open) {
      var m = /^( {0,3})(`{3,}|~{3,})\s*$/.exec(line);
      if (!m) return false;
      return m[2].charAt(0) === open.mark && m[2].length >= open.len;
    }

    function renderTable(lines, index) {
      var header = splitCells(lines[index]);
      var rowIndex = index + 2;
      var rows = [];
      while (
        rowIndex < lines.length &&
        lines[rowIndex].indexOf("|") !== -1 &&
        !isBlank(lines[rowIndex]) &&
        !isBlockStart(lines[rowIndex])
      ) {
        rows.push(splitCells(lines[rowIndex]));
        rowIndex += 1;
      }
      var table = '<div class="chat-table-wrap"><table><thead><tr>';
      for (var h = 0; h < header.length; h++) {
        table += "<th>" + renderInline(header[h]) + "</th>";
      }
      table += "</tr></thead><tbody>";
      for (var r = 0; r < rows.length; r++) {
        table += "<tr>";
        var count = Math.max(header.length, rows[r].length);
        for (var c = 0; c < count; c++) {
          table += "<td>" + renderInline(rows[r][c] || "") + "</td>";
        }
        table += "</tr>";
      }
      table += "</tbody></table></div>";
      return { html: table, next: rowIndex };
    }

    function renderList(lines, index, ordered) {
      var re = ordered ? /^\d+[.)]\s+/ : /^[-*+]\s+/;
      var items = [];
      var cursor = index;
      while (cursor < lines.length && re.test(lines[cursor])) {
        items.push(lines[cursor].replace(re, ""));
        cursor += 1;
        while (
          cursor < lines.length &&
          /^ {2,}\S/.test(lines[cursor]) &&
          !isBlockStart(lines[cursor].replace(/^ {1,3}/, ""))
        ) {
          items[items.length - 1] += "\n" + lines[cursor].replace(/^ +/, "");
          cursor += 1;
        }
      }
      var list = ordered ? "<ol>" : "<ul>";
      for (var n = 0; n < items.length; n++) {
        var body = items[n];
        var task = /^\[([ xX])\]\s+/.exec(body);
        var prefix = "";
        if (!ordered && task) {
          prefix = task[1].toLowerCase() === "x" ? "☑ " : "☐ ";
          body = body.replace(/^\[([ xX])\]\s+/, "");
        }
        list += "<li>" + prefix + renderInline(body) + "</li>";
      }
      list += ordered ? "</ol>" : "</ul>";
      return { html: list, next: cursor };
    }

    function renderChatMarkdown(src, depth) {
      var lines = String(src == null ? "" : src).replace(/\r\n?/g, "\n").split("\n");
      if ((depth || 0) > 8) return "<p>" + renderInline(lines.join("\n")) + "</p>";
      var html = [];
      var i = 0;
      while (i < lines.length) {
        if (isBlank(lines[i])) {
          i += 1;
          continue;
        }

        var fence = openingFence(lines[i]);
        if (fence) {
          var buf = [];
          i += 1;
          while (i < lines.length && !closingFence(lines[i], fence)) {
            buf.push(lines[i]);
            i += 1;
          }
          if (i < lines.length) i += 1;
          var lang = (fence.info || "").split(/\s+/)[0];
          var cls = /^[A-Za-z0-9_+.#-]{1,32}$/.test(lang) ? ' class="language-' + lang + '"' : "";
          html.push("<pre><code" + cls + ">" + escapeHtml(buf.join("\n")) + "</code></pre>");
          continue;
        }

        var heading = /^(#{1,6})\s+(\S.*)$/.exec(lines[i]);
        if (heading) {
          var level = heading[1].length;
          html.push(
            "<h" + level + ">" + renderInline(heading[2].replace(/\s+#+$/, "")) + "</h" + level + ">"
          );
          i += 1;
          continue;
        }

        if (/^(\*{3,}|-{3,}|_{3,})\s*$/.test(lines[i])) {
          html.push("<hr>");
          i += 1;
          continue;
        }

        if (/^>\s?/.test(lines[i])) {
          var quote = [];
          while (i < lines.length && /^>\s?/.test(lines[i])) {
            quote.push(lines[i].replace(/^>\s?/, ""));
            i += 1;
          }
          html.push(
            "<blockquote>" + renderChatMarkdown(quote.join("\n"), (depth || 0) + 1) + "</blockquote>"
          );
          continue;
        }

        if (/^[-*+]\s+\S/.test(lines[i])) {
          var ul = renderList(lines, i, false);
          html.push(ul.html);
          i = ul.next;
          continue;
        }

        if (/^\d+[.)]\s+\S/.test(lines[i])) {
          var ol = renderList(lines, i, true);
          html.push(ol.html);
          i = ol.next;
          continue;
        }

        if (isTableAt(lines, i)) {
          var table = renderTable(lines, i);
          html.push(table.html);
          i = table.next;
          continue;
        }

        var para = [lines[i]];
        i += 1;
        while (
          i < lines.length &&
          !isBlank(lines[i]) &&
          !isBlockStart(lines[i]) &&
          !isTableAt(lines, i)
        ) {
          para.push(lines[i]);
          i += 1;
        }
        html.push("<p>" + renderInline(para.join("\n")) + "</p>");
      }
      return html.join("");
    }

    function appendBubble(role, text) {
      var row = document.createElement("div");
      row.className = "chat-row " + role;
      if (role === "user") row.appendChild(makeAvatar("ش"));
      if (role === "assistant") row.appendChild(makeAvatar("ا"));
      var el = document.createElement("div");
      el.className = "chat-bubble " + role;
      if (role === "user" || role === "assistant") {
        var meta = document.createElement("div");
        meta.className = "chat-meta";
        meta.textContent = role === "user" ? "شما" : "ایجنت";
        el.appendChild(meta);
      }
      var body = document.createElement("div");
      body.className = "chat-text";
      if (role === "user" || role === "assistant") {
        body.className = "chat-text md";
        body.innerHTML = renderChatMarkdown(text);
      } else {
        body.textContent = text;
      }
      el.appendChild(body);
      row.appendChild(el);
      chatMessages.appendChild(row);
      scrollChat();
      return row;
    }

    function hideChatLoading() {
      if (phraseTimer) {
        clearInterval(phraseTimer);
        phraseTimer = null;
      }
      var el = document.getElementById("chat-loading");
      if (el && el.parentNode) el.parentNode.removeChild(el);
    }

    function showChatLoading() {
      hideChatLoading();
      var row = document.createElement("div");
      row.className = "chat-row assistant";
      row.id = "chat-loading";
      row.setAttribute("role", "status");
      row.appendChild(makeAvatar("ا"));
      var bubble = document.createElement("div");
      bubble.className = "chat-bubble assistant is-loading";
      var dots = document.createElement("div");
      dots.className = "chat-typing";
      dots.setAttribute("aria-hidden", "true");
      for (var i = 0; i < 3; i++) dots.appendChild(document.createElement("i"));
      var label = document.createElement("p");
      label.className = "chat-loading-text";
      label.id = "chat-loading-text";
      label.textContent = loadingPhrases[0];
      bubble.appendChild(dots);
      bubble.appendChild(label);
      row.appendChild(bubble);
      chatMessages.appendChild(row);
      var n = 0;
      phraseTimer = setInterval(function () {
        if (chatCancelRequested) return;
        n = (n + 1) % loadingPhrases.length;
        var node = document.getElementById("chat-loading-text");
        if (node) node.textContent = loadingPhrases[n];
        setChatStatus(loadingPhrases[n], "busy");
        scrollChat();
      }, 2200);
      scrollChat();
    }

    function newTurnId() {
      if (window.crypto && typeof crypto.randomUUID === "function") {
        return crypto.randomUUID();
      }
      return (
        "turn-" +
        Date.now().toString(36) +
        "-" +
        Math.random().toString(36).slice(2, 12)
      );
    }

    var chatsUrl = chatRoot.dataset.chatsUrl || "";
    var activeChatId = "";
    var chatIndex = [];
    var chatLoadSeq = 0;
    var chatShownId = "";
    var chatsReady = false;

    function chatItemUrl(id) {
      return chatsUrl.replace(/\/$/, "") + "/" + encodeURIComponent(id);
    }

    function chatFetch(url, options) {
      var opts = options || {};
      var headers = { Accept: "application/json" };
      if (opts.body) headers["Content-Type"] = "application/json";
      return fetch(url, {
        method: opts.method || "GET",
        credentials: "same-origin",
        headers: headers,
        body: opts.body
      }).then(function (r) {
        return r.json().then(
          function (data) {
            return { ok: r.ok, status: r.status, data: data || {} };
          },
          function () {
            return { ok: false, status: r.status, data: {} };
          }
        );
      });
    }

    function announce(row) {
      if (row) row.setAttribute("aria-live", "polite");
      return row;
    }

    function titleFromText(text) {
      var line = String(text || "").replace(/\s+/g, " ").trim();
      if (!line) return "گفتگوی جدید";
      if (line.length > 48) return line.slice(0, 48).replace(/\s+$/, "") + "…";
      return line;
    }

    function findChat(id) {
      var i;
      for (i = 0; i < chatIndex.length; i++) {
        if (chatIndex[i].id === id) return chatIndex[i];
      }
      return null;
    }

    function upsertIndex(summary) {
      if (!summary || !summary.id) return;
      var next = {
        id: summary.id,
        title: summary.title || "گفتگوی جدید",
        updated_at: summary.updated_at || 0,
        message_count: summary.message_count || 0
      };
      var found = false;
      var i;
      for (i = 0; i < chatIndex.length; i++) {
        if (chatIndex[i].id !== next.id) continue;
        chatIndex[i] = next;
        found = true;
        break;
      }
      if (!found) chatIndex.unshift(next);
      chatIndex.sort(function (a, b) {
        return (b.updated_at || 0) - (a.updated_at || 0);
      });
    }

    function renderChatList() {
      var box = document.getElementById("chat-list-items");
      if (!box) return;
      box.innerHTML = "";
      chatIndex.forEach(function (item) {
        var row = document.createElement("div");
        row.className = "chat-thread" + (item.id === activeChatId ? " is-active" : "");
        var open = document.createElement("button");
        open.type = "button";
        open.className = "chat-thread-open";
        open.setAttribute("aria-current", item.id === activeChatId ? "true" : "false");
        var label = item.title || "گفتگوی جدید";
        open.title = label;
        var span = document.createElement("span");
        span.textContent = label;
        open.appendChild(span);
        open.addEventListener("click", function () {
          openChat(item.id);
        });
        row.appendChild(open);
        var onlyEmpty = chatIndex.length === 1 && !(item.message_count > 0);
        if (!onlyEmpty) {
          var del = document.createElement("button");
          del.type = "button";
          del.className = "chat-thread-delete";
          del.setAttribute("aria-label", "حذف گفتگو");
          del.title = "حذف گفتگو";
          del.textContent = "×";
          del.addEventListener("click", function (e) {
            e.preventDefault();
            e.stopPropagation();
            deleteChat(item.id);
          });
          row.appendChild(del);
        }
        box.appendChild(row);
      });
    }

    function renderThread(messages) {
      chatHistory = [];
      if (!chatMessages) return;
      chatMessages.innerHTML = "";
      var list = Array.isArray(messages) ? messages : [];
      var i;
      for (i = 0; i < list.length; i++) {
        var item = list[i];
        if (!item || (item.role !== "user" && item.role !== "assistant")) continue;
        if (typeof item.content !== "string" || !item.content) continue;
        appendBubble(item.role, item.content);
        chatHistory.push({ role: item.role, content: item.content });
      }
      if (chatHistory.length > 40) chatHistory = chatHistory.slice(-40);
      if (!chatHistory.length) {
        var hint = appendBubble("sys", "این گفتگو خالی است. پیام بنویسید.");
        if (hint) hint.id = "chat-empty";
      }
    }

    function openChat(id, force) {
      if (!id || chatBusy) return Promise.resolve();
      if (!force && id === activeChatId && id === chatShownId) return Promise.resolve();
      var seq = ++chatLoadSeq;
      activeChatId = id;
      chatShownId = "";
      renderChatList();
      return chatFetch(chatItemUrl(id)).then(function (res) {
        if (seq !== chatLoadSeq) return;
        if (!res.ok || !res.data.chat) {
          chatHistory = [];
          if (chatMessages) chatMessages.innerHTML = "";
          announce(appendBubble("err", (res.data && res.data.error) || "این گفتگو باز نشد."));
          return;
        }
        upsertIndex(res.data.chat);
        renderThread(res.data.chat.messages || []);
        renderChatList();
        chatShownId = id;
      }).catch(function () {
        if (seq !== chatLoadSeq) return;
        announce(appendBubble("err", "این گفتگو باز نشد."));
      });
    }

    function createChat() {
      if (!chatsReady || chatBusy || !chatsUrl) return;
      var current = findChat(activeChatId);
      if (current && !(current.message_count > 0)) {
        if (chatInput && chatReady) chatInput.focus();
        return;
      }
      var seq = ++chatLoadSeq;
      chatFetch(chatsUrl, { method: "POST", body: "{}" }).then(function (res) {
        if (seq !== chatLoadSeq) return;
        if (!res.ok || !res.data.chat) {
          announce(appendBubble("err", (res.data && res.data.error) || "گفتگوی جدید ساخته نشد."));
          return;
        }
        upsertIndex(res.data.chat);
        activeChatId = res.data.chat.id;
        chatShownId = res.data.chat.id;
        renderChatList();
        renderThread(res.data.chat.messages || []);
        if (chatInput && chatReady) chatInput.focus();
      }).catch(function () {
        if (seq !== chatLoadSeq) return;
        announce(appendBubble("err", "گفتگوی جدید ساخته نشد."));
      });
    }

    function deleteChat(id) {
      if (!chatsReady || chatBusy || !id) return;
      if (!window.confirm("این گفتگو حذف شود؟")) return;
      var seq = ++chatLoadSeq;
      chatFetch(chatItemUrl(id), { method: "DELETE" }).then(function (res) {
        if (seq !== chatLoadSeq) return;
        if (!res.ok || !res.data || res.data.ok !== true) {
          announce(appendBubble("err", (res.data && res.data.error) || "حذف گفتگو انجام نشد."));
          return;
        }
        chatIndex = Array.isArray(res.data.chats) ? res.data.chats : [];
        activeChatId = res.data.active_id || (chatIndex[0] && chatIndex[0].id) || "";
        renderChatList();
        if (activeChatId) openChat(activeChatId, true);
      }).catch(function () {
        if (seq !== chatLoadSeq) return;
        announce(appendBubble("err", "حذف گفتگو انجام نشد."));
      });
    }

    function reloadChatIndex() {
      if (!chatsUrl) return Promise.resolve();
      return chatFetch(chatsUrl).then(function (res) {
        if (!res.data || res.data.ok !== true) return;
        chatIndex = Array.isArray(res.data.chats) ? res.data.chats : chatIndex;
        renderChatList();
      }).catch(function () {});
    }

    function forgetOptimisticUser() {
      if (chatHistory.length && chatHistory[chatHistory.length - 1].role === "user") {
        chatHistory.pop();
      }
      if (!chatMessages) return;
      var rows = chatMessages.querySelectorAll(".chat-row.user");
      var last = rows.length ? rows[rows.length - 1] : null;
      if (last && last.parentNode) last.parentNode.removeChild(last);
    }

    function bootChats() {
      if (!chatsUrl) {
        chatsReady = true;
        if (chatClear && !chatBusy) chatClear.disabled = false;
        announce(appendBubble("err", "گفتگوها خوانده نشد."));
        return;
      }
      chatFetch(chatsUrl).then(function (res) {
        if (!res.data || res.data.ok !== true) {
          announce(appendBubble("err", (res.data && res.data.error) || "گفتگوها خوانده نشد."));
          return;
        }
        chatIndex = Array.isArray(res.data.chats) ? res.data.chats : [];
        activeChatId = res.data.active_id || (chatIndex[0] && chatIndex[0].id) || "";
        renderChatList();
        if (activeChatId) return openChat(activeChatId, true);
      }).catch(function () {
        announce(appendBubble("err", "گفتگوها خوانده نشد."));
      }).then(function () {
        chatsReady = true;
        if (chatClear && !chatBusy) chatClear.disabled = false;
      });
    }

    function setChatBusy(on) {
      chatBusy = on;
      chatRoot.classList.toggle("is-busy", on);
      chatRoot.setAttribute("aria-busy", on ? "true" : "false");
      if (chatInput) chatInput.disabled = on || !chatReady;
      if (chatClear) chatClear.disabled = on || !chatsReady;
      var btn = chatForm && chatForm.querySelector(".chat-send");
      if (btn) {
        btn.disabled = !chatReady;
        btn.classList.toggle("is-cancel", on);
        var label = on ? "لغو کار ایجنت" : "ارسال";
        btn.setAttribute("aria-label", label);
        btn.title = label;
      }
      if (on) showChatLoading();
      else hideChatLoading();
    }

    function cancelChat() {
      if (!chatBusy || chatCancelRequested) return;
      chatCancelRequested = true;
      setChatStatus("در حال لغو…", "busy");
      var loadingText = document.getElementById("chat-loading-text");
      if (loadingText) loadingText.textContent = "در حال لغو…";
      var id = chatTurnId;
      if (!id || !chatCancelUrl) {
        if (chatAbort) chatAbort.abort();
        return;
      }
      fetch(chatCancelUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ turn_id: id }),
      })
        .then(function (r) {
          if (!r.ok && !chatSettled && chatAbort) chatAbort.abort();
        })
        .catch(function () {
          if (!chatSettled && chatAbort) chatAbort.abort();
        });
    }

    function threadHasTurn(messages, sent) {
      var n = messages.length;
      if (!n) return "";
      var last = messages[n - 1];
      if (last && last.role === "user" && last.content === sent) return "user";
      if (n >= 2) {
        var prev = messages[n - 2];
        if (
          prev &&
          prev.role === "user" &&
          prev.content === sent &&
          last &&
          last.role === "assistant"
        ) {
          return "assistant";
        }
      }
      return "";
    }

    function delay(ms) {
      return new Promise(function (resolve) {
        setTimeout(resolve, ms);
      });
    }

    /* When the long chat POST drops (proxy/browser), the agent often still
       finishes on the server. Poll until the reply lands or the turn ends. */
    function recoverDroppedTurn(chatId, sentText) {
      var maxAttempts = 90;
      var intervalMs = 2000;

      function chatStillRunning(res) {
        if (!res || !res.data) return false;
        if (res.data.running === true) return true;
        if (res.data.chat && res.data.chat.running === true) return true;
        return false;
      }

      function poll(attempt) {
        if (chatCancelRequested) {
          return Promise.resolve({ outcome: "cancelled" });
        }
        return chatFetch(chatItemUrl(chatId)).then(
          function (res) {
            if (chatCancelRequested) {
              return { outcome: "cancelled" };
            }
            var messages =
              (res.ok && res.data && res.data.chat && res.data.chat.messages) || [];
            var kept = threadHasTurn(messages, sentText);
            if (kept === "assistant") {
              if (activeChatId === chatId && res.data && res.data.chat) {
                upsertIndex(res.data.chat);
                renderThread(messages);
                renderChatList();
                chatShownId = chatId;
              }
              return { outcome: "assistant" };
            }
            if (chatCancelRequested) {
              return { outcome: "cancelled" };
            }
            var running = chatStillRunning(res);
            if (!running && kept === "user") {
              return { outcome: "failed", kept: kept };
            }
            if (!running && attempt >= 2) {
              return { outcome: "failed", kept: kept };
            }
            if (attempt >= maxAttempts) {
              return { outcome: "timeout", kept: kept };
            }
            setChatStatus("ارتباط موقتاً قطع شد؛ در حال بازیابی پاسخ…", "busy");
            var loadingText = document.getElementById("chat-loading-text");
            if (loadingText) {
              loadingText.textContent = "ارتباط موقتاً قطع شد؛ در حال بازیابی پاسخ…";
            } else {
              showChatLoading();
              loadingText = document.getElementById("chat-loading-text");
              if (loadingText) {
                loadingText.textContent =
                  "ارتباط موقتاً قطع شد؛ در حال بازیابی پاسخ…";
              }
            }
            return delay(intervalMs).then(function () {
              return poll(attempt + 1);
            });
          },
          function () {
            if (chatCancelRequested) return { outcome: "cancelled" };
            if (attempt >= maxAttempts) {
              return { outcome: "timeout" };
            }
            setChatStatus("ارتباط موقتاً قطع شد؛ در حال بازیابی پاسخ…", "busy");
            return delay(intervalMs).then(function () {
              return poll(attempt + 1);
            });
          }
        );
      }

      showChatLoading();
      setChatStatus("ارتباط موقتاً قطع شد؛ در حال بازیابی پاسخ…", "busy");
      return poll(0).then(function (result) {
        hideChatLoading();
        var outcome = (result && result.outcome) || "failed";
        if (outcome === "assistant") {
          setChatStatus(chatReady ? "آماده" : "نیاز به تنظیم API", null);
          return;
        }
        if (outcome === "cancelled") {
          announce(appendBubble("sys", "کار ایجنت لغو شد."));
          setChatStatus(chatReady ? "آماده" : "نیاز به تنظیم API", null);
          return;
        }
        if (!(result && result.kept) && chatInput && !chatInput.value) {
          chatInput.value = sentText;
        }
        announce(appendBubble("err", "ارتباط با سرور برقرار نشد."));
        setChatStatus("خطا", "err");
      });
    }

    function sendChat(text) {
      text = (text || "").trim();
      if (!text || chatBusy || !chatReady || !activeChatId) return;
      var sentChatId = activeChatId;
      var empty = document.getElementById("chat-empty");
      if (empty && empty.parentNode) empty.parentNode.removeChild(empty);

      announce(appendBubble("user", text));
      chatHistory.push({ role: "user", content: text });
      if (chatHistory.length > 40) chatHistory = chatHistory.slice(-40);
      var listed = findChat(sentChatId);
      if (listed) {
        if (!listed.title || listed.title === "گفتگوی جدید") listed.title = titleFromText(text);
        listed.message_count = (listed.message_count || 0) + 1;
        listed.updated_at = Date.now() / 1000;
        renderChatList();
      }
      if (chatInput) chatInput.value = "";
      chatCancelRequested = false;
      chatSettled = false;
      chatTurnId = newTurnId();
      chatAbort = new AbortController();
      setChatBusy(true);
      setChatStatus("در حال وصل شدن به مدل…", "busy");

      fetch(chatApi, {
        method: "POST",
        credentials: "same-origin",
        signal: chatAbort.signal,
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          message: text,
          chat_id: sentChatId,
          turn_id: chatTurnId,
        }),
      })
        .then(function (r) {
          return r.json().then(function (data) {
            return { data: data || {} };
          });
        })
        .then(function (res) {
          if (chatSettled) return;
          chatSettled = true;
          var data = res.data;
          hideChatLoading();
          if (data.chat) {
            upsertIndex(data.chat);
            renderChatList();
          }
          if (data.cancelled || (chatCancelRequested && !(data.ok && data.reply))) {
            announce(appendBubble("sys", "کار ایجنت لغو شد."));
            setChatStatus(chatReady ? "آماده" : "نیاز به تنظیم API", null);
            return;
          }
          if (data.ok && data.reply) {
            announce(appendBubble("assistant", data.reply));
            chatHistory.push({ role: "assistant", content: data.reply });
            if (chatHistory.length > 40) chatHistory = chatHistory.slice(-40);
            setChatStatus("آماده", null);
            return;
          }
          if (!data.chat) {
            forgetOptimisticUser();
            return reloadChatIndex().then(function () {
              announce(appendBubble("err", data.error || "پاسخ نامعتبر از سرور."));
              setChatStatus("خطا", "err");
            });
          }
          announce(appendBubble("err", data.error || "پاسخ نامعتبر از سرور."));
          setChatStatus("خطا", "err");
        })
        .catch(function (err) {
          if (chatSettled) return;
          chatSettled = true;
          if (chatCancelRequested || (err && err.name === "AbortError")) {
            hideChatLoading();
            announce(appendBubble("sys", "کار ایجنت لغو شد."));
            setChatStatus(chatReady ? "آماده" : "نیاز به تنظیم API", null);
            return;
          }
          return recoverDroppedTurn(sentChatId, text);
        })
        .finally(function () {
          chatAbort = null;
          chatTurnId = null;
          setChatBusy(false);
          if (chatInput && chatReady) chatInput.focus();
          scrollChat();
        });
    }

    if (chatForm) {
      chatForm.addEventListener("submit", function (e) {
        e.preventDefault();
        if (chatBusy) {
          cancelChat();
          return;
        }
        sendChat(chatInput && chatInput.value);
      });
    }

    if (chatInput) {
      chatInput.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          sendChat(chatInput.value);
        }
      });
    }

    if (chatClear) {
      chatClear.disabled = true;
      chatClear.addEventListener("click", function () {
        createChat();
      });
    }

    bootChats();
    if (chatInput && chatReady) chatInput.focus();
  }
})();
