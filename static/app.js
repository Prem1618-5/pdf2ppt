/*
 * Question Paper -> Slides : browser side.
 * Plain ES5 on purpose (old school laptops). No libraries, no build step.
 * Talks to the server using the contract in docs/EDITOR_API.md.
 *
 * Sections:
 *   1. helpers (DOM, HTTP, toast)
 *   2. upload screen + Gemini keys
 *   3. progress screen
 *   4. editor: top bar, slide list, preview, item panel
 *   5. crop editor (modal)
 *   6. keyboard + start-up
 */
(function () {
  'use strict';

  /* ================================================================
   * 1. Helpers
   * ================================================================ */

  function $(id) { return document.getElementById(id); }
  function show(el, yes) { el.className = el.className.replace(/\s*\bhidden\b/g, '') + (yes ? '' : ' hidden'); }
  function hasClass(el, c) { return (' ' + el.className + ' ').indexOf(' ' + c + ' ') >= 0; }
  function setClass(el, c, on) {
    var cur = (' ' + el.className + ' ').replace(' ' + c + ' ', ' ');
    el.className = (cur + (on ? ' ' + c : '')).replace(/^\s+|\s+$/g, '').replace(/\s+/g, ' ');
  }
  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function el(tag, cls, html) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html !== undefined) e.innerHTML = html;
    return e;
  }
  function trim(s) { return String(s || '').replace(/^\s+|\s+$/g, ''); }

  /* localStorage can throw (private window, blocked storage) */
  function storeGet(k) { try { return window.localStorage.getItem(k); } catch (e) { return null; } }
  function storeSet(k, v) { try { window.localStorage.setItem(k, v); } catch (e) {} }
  function storeDel(k) { try { window.localStorage.removeItem(k); } catch (e) {} }

  /* Turn an error response into a plain sentence. */
  function errorText(x) {
    var msg = '';
    try {
      var d = JSON.parse(x.responseText).detail;
      if (typeof d === 'string') msg = d;
      else if (d && d.length && d[0].msg) msg = d[0].msg;      // validation error list
      else if (d) msg = JSON.stringify(d);
    } catch (e) {}
    if (!msg) msg = x.status ? 'The server answered with error ' + x.status + '.' : 'Could not reach the server. Check the connection.';
    return msg;
  }

  /*
   * api(method, path, body, done[, onUploadProgress])
   * body: null, a FormData, or any object (sent as JSON).
   * done(err, data): err is a readable message or null.
   */
  function api(method, path, body, done, onUp) {
    var x = new XMLHttpRequest();
    x.open(method, path, true);
    var payload = null;
    if (body instanceof FormData) payload = body;
    else if (body !== null && body !== undefined) {
      x.setRequestHeader('Content-Type', 'application/json; charset=utf-8');
      payload = JSON.stringify(body);
    }
    if (onUp && x.upload) x.upload.onprogress = function (e) { if (e.lengthComputable) onUp(e.loaded / e.total); };
    x.onload = function () {
      if (x.status >= 200 && x.status < 300) {
        var data = null;
        try { data = x.responseText ? JSON.parse(x.responseText) : null; } catch (e) { done('The server sent an unreadable answer.', null, x); return; }
        done(null, data, x);
      } else {
        done(errorText(x), null, x);
      }
    };
    x.onerror = function () { done('Could not reach the server. Check the connection.', null, x); };
    x.send(payload);
  }

  /* Toasts: green "Saved", red errors (stay a bit longer). */
  var toastTimer = null;
  function toast(msg, isError) {
    var t = $('toast');
    t.innerHTML = esc(msg);
    t.className = 'toast' + (isError ? ' err' : '');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.className = 'toast hidden'; }, isError ? 6000 : 1600);
  }

  /* "Working..." badge while a request runs; also blocks double clicks for big edits. */
  var busyCount = 0;
  function busy(on) {
    busyCount = Math.max(0, busyCount + (on ? 1 : -1));
    show($('busy'), busyCount > 0);
  }

  function showScreen(id) {
    var ids = ['scrUpload', 'scrProgress', 'scrEditor'];
    for (var i = 0; i < ids.length; i++) show($(ids[i]), ids[i] === id);
  }

  /* job id lives in location.hash as #job=ID so a refresh reopens it */
  function hashJob() { var m = /job=([A-Za-z0-9_\-]+)/.exec(location.hash || ''); return m ? m[1] : null; }
  function setHashJob(id) {
    if (id) { location.hash = 'job=' + id; return; }
    if (window.history && history.replaceState) history.replaceState(null, '', location.pathname + location.search);
    else location.hash = '';
  }

  /* ================================================================
   * 2. Upload screen
   * ================================================================ */

  var chosenFiles = [];        // File objects, in the order the teacher picked them
  var KEY_STORE = 'qdeck_keys';

  function renderFileList() {
    var ol = $('fileList');
    ol.innerHTML = '';
    for (var i = 0; i < chosenFiles.length; i++) {
      var li = el('li', '', esc(chosenFiles[i].name) + ' ');
      var b = el('button', '', 'remove');
      b.type = 'button';
      b.setAttribute('data-i', i);
      b.onclick = function () { chosenFiles.splice(+this.getAttribute('data-i'), 1); renderFileList(); };
      li.appendChild(b);
      ol.appendChild(li);
    }
    show($('fileClearRow'), chosenFiles.length > 1);
    $('goBtn').disabled = chosenFiles.length === 0;
  }

  $('fileInput').onchange = function () {
    var f = this.files;
    for (var i = 0; i < f.length; i++) chosenFiles.push(f[i]);
    this.value = '';                       // allow picking the same file again later
    renderFileList();
  };
  $('fileClear').onclick = function () { chosenFiles = []; renderFileList(); };

  /* --- Gemini keys --- */
  function keyLines() {
    var lines = $('keysText').value.split(/[\r\n,]+/), out = [];
    for (var i = 0; i < lines.length; i++) { var k = trim(lines[i]); if (k) out.push(k); }
    return out;
  }
  function rememberKeys() {
    if ($('keysRemember').checked) storeSet(KEY_STORE, keyLines().join('\n'));
    else storeDel(KEY_STORE);
  }
  $('keysToggle').onclick = function () {
    var body = $('keysBody'), open = hasClass(body, 'hidden');
    show(body, open);
    this.innerHTML = (open ? '&#9662;' : '&#9656;') + ' Gemini API keys (optional)';
  };
  $('keysRemember').onchange = rememberKeys;
  $('keysText').onchange = rememberKeys;
  $('keysCheck').onclick = function () {
    var keys = keyLines(), out = $('keysResult');
    if (!keys.length) { out.innerHTML = '<li class="muted">Type or paste at least one key first.</li>'; return; }
    out.innerHTML = '<li class="muted">Checking&hellip;</li>';
    rememberKeys();
    api('POST', '/api/keys/check', { keys: keys }, function (err, res) {
      if (err) { out.innerHTML = '<li class="bad">' + esc(err) + '</li>'; return; }
      var html = '';
      for (var i = 0; i < (res || []).length; i++) {
        var r = res[i];
        html += '<li>' + (r.ok ? '<span class="ok">&#10003;</span> ' : '<span class="bad">&#10007;</span> ') + esc(r.masked) +
          (r.ok ? ' <span class="muted">works</span>' : ' <span class="bad">' + esc(r.error || 'not working') + '</span>') + '</li>';
      }
      out.innerHTML = html || '<li class="muted">No answer for these keys.</li>';
    });
  };
  (function loadSavedKeys() {
    var saved = storeGet(KEY_STORE);
    if (saved) {
      $('keysText').value = saved;
      $('keysRemember').checked = true;
      show($('keysBody'), true);
      $('keysToggle').innerHTML = '&#9662; Gemini API keys (optional)';
    }
  })();

  /* --- start a job --- */
  $('goBtn').onclick = function () {
    if (!chosenFiles.length) return;
    rememberKeys();
    var fd = new FormData();
    for (var i = 0; i < chosenFiles.length; i++) fd.append('files', chosenFiles[i], chosenFiles[i].name);
    fd.append('keys', keyLines().join('\n'));
    fd.append('lang', $('optLang').value);
    fd.append('render', $('optRender').value);
    startProgress('Uploading the files&hellip;');
    api('POST', '/api/jobs', fd, function (err, res) {
      if (err) { progressError(err); return; }
      jobId = res.id;
      setHashJob(jobId);
      setBar(0);
      poll();
    }, function (p) { setBar(p * 100); $('progDetail').innerHTML = Math.round(p * 100) + '% uploaded'; });
  };

  /* --- open an existing deck --- */
  $('importBtn').onclick = function () { $('importInput').click(); };
  $('importInput').onchange = function () {
    var f = this.files[0];
    this.value = '';
    if (!f) return;
    var fd = new FormData();
    fd.append('file', f, f.name);
    startProgress('Opening the deck&hellip;');
    api('POST', '/api/import', fd, function (err, res) {
      if (err) { progressError(err); return; }
      jobId = res.id;
      setHashJob(jobId);
      loadProject();
    }, function (p) { setBar(p * 100); });
  };

  /* ================================================================
   * 3. Progress screen
   * ================================================================ */

  var jobId = null, pollTimer = null;
  var STAGES = {
    queued: 'Waiting in line', starting: 'Starting', pages: 'Reading the pages', rendered: 'Reading the pages',
    analysed: 'Finding the questions', cropped: 'Cutting out the questions', building: 'Preparing the slides', done: 'Finished'
  };
  /* where each stage sits on the bar: [start %, width %] */
  var STAGE_BAR = { queued: [0, 2], starting: [2, 2], pages: [4, 8], rendered: [4, 8], analysed: [12, 58], cropped: [70, 20], building: [90, 10] };
  var KEY_STATES = { ok: 'Working', active: 'In use', exhausted: 'Free limit reached', limit: 'Free limit reached',
    quota: 'Free limit reached', invalid: 'Not valid', error: 'Error', waiting: 'Waiting', cooldown: 'Resting a minute' };

  function setBar(p) { $('progBar').style.width = Math.max(2, Math.min(100, p)) + '%'; }

  function startProgress(text) {
    clearTimeout(pollTimer);
    showScreen('scrProgress');
    show($('progError'), false);
    show($('progKeys'), false);
    $('progStage').innerHTML = text;
    $('progDetail').innerHTML = '';
    setBar(0);
  }
  function progressError(msg) {
    showScreen('scrProgress');
    $('progStage').innerHTML = 'Something went wrong';
    $('progErrorText').innerHTML = esc(msg);
    show($('progError'), true);
  }
  $('progBack').onclick = function () { startOver(); };

  function renderKeyTable(keys, tableId, bodyId) {
    if (!keys || !keys.length) { show($(tableId), false); return; }
    var html = '';
    for (var i = 0; i < keys.length; i++) {
      var k = keys[i];
      html += '<tr><td>' + esc(k.masked) + '</td><td>' + esc(KEY_STATES[k.state] || k.state || '') + '</td><td>' + esc(k.calls || 0) + '</td></tr>';
    }
    $(bodyId).innerHTML = html;
    show($(tableId), true);
  }

  function poll() {
    clearTimeout(pollTimer);
    api('GET', '/api/jobs/' + jobId + '?t=' + new Date().getTime(), null, function (err, s, x) {
      if (err) {
        if (x && x.status === 404) { progressError('This job is no longer on the server (results are deleted after a while). Please start again.'); setHashJob(null); return; }
        if (x && x.status >= 400 && x.status < 500) { progressError(err); return; }
        pollTimer = setTimeout(poll, 6000);      // network hiccup or server waking up: keep trying
        return;
      }
      if (s.state === 'done') { setBar(100); loadProject(); return; }
      if (s.state === 'error') { progressError(s.message || 'The paper could not be processed.'); return; }
      var w = STAGE_BAR[s.stage];
      if (w) setBar(w[0] + (s.n ? w[1] * s.i / s.n : 0));
      $('progStage').innerHTML = esc(STAGES[s.stage] || s.stage || 'Working') + (s.n && s.stage !== 'queued' ? ' (' + esc(s.i) + ' of ' + esc(s.n) + ')' : '') + '&hellip;';
      var detail;
      if (s.queue_ahead) detail = s.queue_ahead + ' paper(s) ahead of yours. Please wait.';
      else if (s.log && s.log.length) detail = esc(s.log[s.log.length - 1]);
      else detail = 'This takes about one minute for every three pages.';
      $('progDetail').innerHTML = detail;
      renderKeyTable(s.keys, 'progKeys', 'progKeysBody');
      pollTimer = setTimeout(poll, 2500);
    });
  }

  /* Open a job from the hash (page refresh). */
  function resumeJob(id) {
    jobId = id;
    startProgress('Opening your work&hellip;');
    poll();
  }

  function startOver() {
    clearTimeout(pollTimer);
    closeCrop();
    jobId = null; project = null; selId = null;
    setHashJob(null);
    chosenFiles = [];
    renderFileList();
    showScreen('scrUpload');
  }

  /* ================================================================
   * 4. Editor
   * ================================================================ */

  var project = null;     // the full project JSON from the server
  var selId = null;       // id of the selected item
  var FLAGS = {
    low_confidence:     ['Please check', 'red'],
    too_small_to_read:  ['Too small to read', 'red'],
    no_number:          ['No question number', 'red'],
    small_text:         ['Small text', 'amber'],
    split_stitched:     ['Joined from two columns', ''],
    page_break_stitched:['Joined across pages', ''],
    bilingual_twin:     ['Other language copy', ''],
    shared_passage:     ['Shared passage', ''],
    refers_to_passage:  ['Uses a passage', ''],
    figure:             ['Has a figure', '']
  };

  function jobPath(rest) { return '/api/jobs/' + jobId + rest; }
  function itemIndex(id) {
    if (!project) return -1;
    for (var i = 0; i < project.items.length; i++) if (project.items[i].id === id) return i;
    return -1;
  }
  function selItem() { var i = itemIndex(selId); return i >= 0 ? project.items[i] : null; }
  function blankAfter(it) { return it.blank_after === null || it.blank_after === undefined ? !!project.settings.blank_after : !!it.blank_after; }
  function thumbUrl(it) { return jobPath('/items/' + it.id + '.png?rev=' + (it.rev || 0)); }
  function pageBySeq(seq) {
    for (var i = 0; i < project.pages.length; i++) if (project.pages[i].seq === seq) return i;
    return -1;
  }

  function loadProject() {
    api('GET', jobPath('/project?t=' + new Date().getTime()), null, function (err, p) {
      if (err) { progressError(err); return; }
      showScreen('scrEditor');
      applyProject(p);
    });
  }

  /* A whole new project arrived: redraw everything. */
  function applyProject(p) {
    project = p;
    if (!project.items) project.items = [];
    if (!project.pages) project.pages = [];
    if (!project.settings) project.settings = {};
    if (itemIndex(selId) < 0) selId = project.items.length ? project.items[0].id : null;
    renderTop();
    renderList();
    renderSelected();
  }

  /* One item changed: redraw only that row, plus preview/panel if it is selected. */
  function applyItem(it) {
    var i = itemIndex(it.id);
    if (i < 0) return;
    project.items[i] = it;
    project.can_undo = true;                 // an edit just happened, so there is something to undo
    var old = $('row_' + it.id);
    if (old) old.parentNode.replaceChild(makeRow(it, i), old);
    var mk = $('blank_' + it.id);
    if (mk) show(mk, blankAfter(it));
    if (it.id === selId) renderSelected();
    renderUndo();
  }

  /* Labels change the numbering check, so quietly refresh the summary line. */
  function refreshSummary() {
    api('GET', jobPath('/project?t=' + new Date().getTime()), null, function (err, p) {
      if (err || !project) return;
      project.integrity = p.integrity;
      project.summary = p.summary;
      project.can_undo = p.can_undo;
      renderTop();
    });
  }

  /*
   * Run an edit. kind: 'item' | 'project' (what the server sends back).
   * opts.after(data) runs after the screen is updated.
   */
  var editing = false;
  function edit(method, path, body, opts) {
    opts = opts || {};
    if (opts.exclusive) { if (editing) return; editing = true; }
    busy(true);
    api(method, jobPath(path), body, function (err, res) {
      busy(false);
      if (opts.exclusive) editing = false;
      if (err) { toast(err, true); if (opts.fail) opts.fail(err); return; }
      if (res && res.project) applyProject(res.project);
      else if (res && res.item) applyItem(res.item);
      if (opts.after) opts.after(res);
      toast(opts.message || 'Saved');
    });
  }

  /* ---------- top bar ---------- */

  function renderUndo() { $('undoBtn').disabled = !project || !project.can_undo; }

  function renderTop() {
    var g = project.integrity || {}, s = project.settings, html;
    var n = project.items.length;
    html = '<b>' + n + ' slide' + (n === 1 ? '' : 's') + '</b>';
    if (g.expected) html += ' &middot; questions found: ' + esc(g.detected) + ' of ' + esc(g.expected) + ' expected';
    if (g.ok) html += ' &middot; <span class="ok">numbering complete</span>';
    var probs = [];
    if (g.gaps && g.gaps.length) probs.push('missing: ' + esc(g.gaps.join(', ')));
    if (g.duplicates && g.duplicates.length) probs.push('appear twice: ' + esc(g.duplicates.join(', ')));
    if (g.out_of_order && g.out_of_order.length) probs.push('out of order: ' + esc(g.out_of_order.join(', ')));
    if (probs.length) html += '<br><span class="bad">Check questions &mdash; ' + probs.join('; ') + '</span>';
    $('edSummary').innerHTML = html;

    $('setBlank').checked = !!s.blank_after;
    $('setLayoutFill').checked = s.layout !== 'top-right';
    $('setLayoutTR').checked = s.layout === 'top-right';
    $('setWatermark').checked = s.watermark !== 'keep';
    renderUndo();
  }

  $('setBlank').onchange = function () { edit('PATCH', '/settings', { blank_after: this.checked }); };
  $('setLayoutFill').onchange = function () { edit('PATCH', '/settings', { layout: 'fill' }); };
  $('setLayoutTR').onchange = function () { edit('PATCH', '/settings', { layout: 'top-right' }); };
  $('setWatermark').onchange = function () { edit('PATCH', '/settings', { watermark: this.checked ? 'remove' : 'keep' }); };

  function undo() {
    if (!project || !project.can_undo) return;
    edit('POST', '/undo', null, { exclusive: true, message: 'Undone' });
  }
  $('undoBtn').onclick = undo;

  $('exportBtn').onclick = function () {
    var b = this;
    b.disabled = true;
    b.innerHTML = 'Building PowerPoint&hellip;';
    busy(true);
    api('POST', jobPath('/export'), null, function (err, res) {
      busy(false);
      b.disabled = false;
      b.innerHTML = 'Download PowerPoint';
      if (err) { toast(err, true); return; }
      toast('PowerPoint ready (' + res.slides + ' slides). Downloading...');
      window.location.href = res.url || jobPath('/pptx');
    });
  };
  $('reportBtn').onclick = function () { window.open(jobPath('/report'), '_blank'); };
  $('restartBtn').onclick = function () {
    if (confirm('Start over with a new paper? Download your PowerPoint first if you need it.')) startOver();
  };

  /* ---------- slide list ---------- */

  var dragId = null;

  function chipsHtml(it) {
    var h = '', f = it.flags || [];
    for (var i = 0; i < f.length; i++) {
      var d = FLAGS[f[i]] || [String(f[i]).replace(/_/g, ' '), ''];
      h += '<span class="chip ' + d[1] + '">' + esc(d[0]) + '</span>';
    }
    if (it.custom_image) h += '<span class="chip">Your picture</span>';
    if (it.render === 'text') h += '<span class="chip">Text</span>';
    return h;
  }

  function makeRow(it, idx) {
    var row = el('div', 'slide' + (it.id === selId ? ' sel' : ''));
    row.id = 'row_' + it.id;
    row.setAttribute('draggable', 'true');
    row.setAttribute('data-id', it.id);
    var warn = (typeof it.confidence === 'number' && it.confidence < 0.6) ? '<span class="slide-warn" title="The tool is not sure about this one. Please check it.">&#9888;</span>' : '';
    var last = idx === project.items.length - 1;
    row.innerHTML =
      '<div class="slide-top"><span class="slide-num">' + (idx + 1) + '</span>' + warn +
      '<span class="slide-label">' + (esc(it.label) || '<i>(no label)</i>') + '</span>' +
      '<span class="slide-move"><button type="button" data-mv="-1" title="Move up"' + (idx === 0 ? ' disabled' : '') + '>&#9650;</button>' +
      '<button type="button" data-mv="1" title="Move down"' + (last ? ' disabled' : '') + '>&#9660;</button></span></div>' +
      '<img class="thumb" alt="" draggable="false" loading="lazy" src="' + esc(thumbUrl(it)) + '">' +
      '<div class="chips">' + chipsHtml(it) + '</div>';
    return row;
  }

  function renderList() {
    var list = $('slideList'), keepScroll = list.scrollTop;
    var frag = document.createDocumentFragment();
    for (var i = 0; i < project.items.length; i++) {
      var it = project.items[i];
      frag.appendChild(makeRow(it, i));
      var mk = el('div', 'blank-marker' + (blankAfter(it) ? '' : ' hidden'), 'blank slide');
      mk.id = 'blank_' + it.id;
      frag.appendChild(mk);
    }
    list.innerHTML = '';
    if (!project.items.length) list.innerHTML = '<p class="muted">No slides. Use &ldquo;Add a missing question&rdquo; or Undo.</p>';
    list.appendChild(frag);
    list.scrollTop = keepScroll;
  }

  function rowFromEvent(e) {
    var t = e.target;
    while (t && t !== $('slideList')) { if (t.getAttribute && t.getAttribute('data-id')) return t; t = t.parentNode; }
    return null;
  }

  /* clicks: select a row, or move it up/down */
  $('slideList').onclick = function (e) {
    var row = rowFromEvent(e);
    if (!row) return;
    var id = row.getAttribute('data-id'), mv = e.target.getAttribute && e.target.getAttribute('data-mv');
    if (mv) { moveItem(id, +mv); return; }
    select(id);
  };

  function moveItem(id, delta) {
    var i = itemIndex(id), j = i + delta;
    if (i < 0 || j < 0 || j >= project.items.length) return;
    var ids = idList();
    ids.splice(i, 1);
    ids.splice(j, 0, id);
    sendOrder(ids, id);
  }
  function idList() { var a = []; for (var i = 0; i < project.items.length; i++) a.push(project.items[i].id); return a; }
  function sendOrder(ids, keepSel) {
    selId = keepSel || selId;
    edit('POST', '/order', { ids: ids }, { exclusive: true, message: 'Order saved', after: function () { scrollToSel(); } });
  }

  /* HTML5 drag and drop */
  var dropTarget = null, dropAfter = false;
  function clearDropMark() { if (dropTarget) { setClass(dropTarget, 'drop-before', false); setClass(dropTarget, 'drop-after', false); } dropTarget = null; }
  $('slideList').ondragstart = function (e) {
    var row = rowFromEvent(e);
    if (!row) return;
    dragId = row.getAttribute('data-id');
    setClass(row, 'dragging', true);
    try { e.dataTransfer.setData('text', dragId); e.dataTransfer.effectAllowed = 'move'; } catch (er) {}
  };
  $('slideList').ondragover = function (e) {
    if (!dragId) return;
    e.preventDefault();
    var row = rowFromEvent(e);
    if (!row) return;
    var r = row.getBoundingClientRect(), after = e.clientY > r.top + r.height / 2;
    if (row !== dropTarget || after !== dropAfter) {
      clearDropMark();
      dropTarget = row; dropAfter = after;
      setClass(row, after ? 'drop-after' : 'drop-before', true);
    }
    /* auto-scroll near the edges of the list */
    var lr = this.getBoundingClientRect();
    if (e.clientY < lr.top + 40) this.scrollTop -= 12;
    else if (e.clientY > lr.bottom - 40) this.scrollTop += 12;
  };
  $('slideList').ondrop = function (e) {
    e.preventDefault();
    if (!dragId || !dropTarget) { endDrag(); return; }
    var target = dropTarget.getAttribute('data-id'), after = dropAfter, moving = dragId;
    endDrag();
    if (target === moving) return;
    var ids = idList();
    ids.splice(ids.indexOf(moving), 1);
    var at = ids.indexOf(target) + (after ? 1 : 0);
    ids.splice(at, 0, moving);
    if (ids.join() !== idList().join()) sendOrder(ids, moving);
  };
  function endDrag() {
    clearDropMark();
    var r = dragId && $('row_' + dragId);
    if (r) setClass(r, 'dragging', false);
    dragId = null;
  }
  $('slideList').ondragend = endDrag;

  function select(id) {
    if (!project || itemIndex(id) < 0) return;
    var old = $('row_' + selId);
    if (old) setClass(old, 'sel', false);
    selId = id;
    var row = $('row_' + id);
    if (row) setClass(row, 'sel', true);
    renderSelected();
    scrollToSel();
  }
  function scrollToSel() {
    var row = $('row_' + selId), list = $('slideList');
    if (!row) return;
    var top = row.offsetTop, bottom = top + row.offsetHeight + 24;
    if (top < list.scrollTop) list.scrollTop = top - 6;
    else if (bottom > list.scrollTop + list.clientHeight) list.scrollTop = bottom - list.clientHeight;
  }
  function selectBy(delta) {
    var i = itemIndex(selId) + delta;
    if (project && i >= 0 && i < project.items.length) select(project.items[i].id);
  }
  $('prevSlide').onclick = function () { selectBy(-1); };
  $('nextSlide').onclick = function () { selectBy(1); };

  /* ---------- preview (16:9 frame) ---------- */

  function renderSelected() { renderPreview(); renderPanel(); }

  /*
   * Where the question goes on the slide, as fractions of the slide.
   * fill: as large as possible, centred, starting near the top.
   * top-right: inside the top-right quarter.
   */
  function layoutArea() {
    var m = 0.02;
    if (project.settings.layout === 'top-right') return { x: 0.5, y: m, w: 0.5 - m, h: 0.5 - m, right: true };
    return { x: m, y: m, w: 1 - 2 * m, h: 1 - 2 * m, right: false };
  }

  function renderPreview() {
    var frame = $('frame'), it = selItem(), pane = $('previewPane');
    /* fit the 16:9 frame into the pane, leaving room for the buttons below */
    var W = pane.clientWidth - 34, H = pane.clientHeight - 110;
    if (H > 0 && W * 9 / 16 > H) W = H * 16 / 9;
    W = Math.max(320, Math.floor(W));
    var FH = Math.round(W * 9 / 16);
    frame.style.width = W + 'px';
    frame.style.height = FH + 'px';
    $('previewInfo').parentNode.style.width = W + 'px';
    frame.innerHTML = '';
    if (!it) { $('previewInfo').innerHTML = ''; return; }

    var idx = itemIndex(selId), a = layoutArea();
    $('previewInfo').innerHTML = 'Slide ' + (idx + 1) + ' of ' + project.items.length + (blankAfter(it) ? ' &middot; followed by a blank slide' : '');
    var ax = a.x * W, ay = a.y * FH, aw = a.w * W, ah = a.h * FH;

    if (a.right) {                         // faint outline of the corner area
      var g = el('div', 'guide');
      g.style.left = ax + 'px'; g.style.top = ay + 'px'; g.style.width = aw + 'px'; g.style.height = ah + 'px';
      frame.appendChild(g);
    }

    if (it.render === 'text') {
      var box = el('div', 'area');
      box.style.left = ax + 'px'; box.style.top = ay + 'px'; box.style.width = aw + 'px'; box.style.height = ah + 'px';
      box.style.fontSize = Math.max(10, Math.round(W / (a.right ? 48 : 32))) + 'px';
      var html = '<b>' + esc(it.label) + '</b> ' + esc(it.stem).replace(/\n/g, '<br>');
      var opts = it.options || [];
      for (var i = 0; i < opts.length; i++) html += '<div class="opt">(' + String.fromCharCode(97 + i) + ') ' + esc(opts[i]) + '</div>';
      box.innerHTML = html;
      frame.appendChild(box);
      return;
    }

    var img = new Image();
    img.alt = '';
    var place = function (iw, ih) {
      if (!iw || !ih) return;
      var s = Math.min(aw / iw, ah / ih), w = iw * s, h = ih * s;
      img.style.width = w + 'px';
      img.style.height = h + 'px';
      img.style.top = ay + 'px';
      img.style.left = (a.right ? ax + aw - w : ax + (aw - w) / 2) + 'px';
    };
    if (it.img_w && it.img_h) place(it.img_w, it.img_h);
    else { img.style.visibility = 'hidden'; img.onload = function () { place(img.naturalWidth, img.naturalHeight); img.style.visibility = 'visible'; }; }
    img.onerror = function () { if (img.parentNode === frame) frame.innerHTML = '<p class="muted center">The picture could not be loaded.</p>'; };
    img.src = thumbUrl(it);
    frame.appendChild(img);
  }

  var resizeTimer = null;
  window.onresize = function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      if (project && !hasClass($('scrEditor'), 'hidden')) renderPreview();
      if (crop) layoutCrop();
    }, 150);
  };

  /* ---------- item panel ---------- */

  function renderPanel() {
    var it = selItem();
    show($('panelEmpty'), !it);
    show($('panelBody'), !!it);
    if (!it) return;
    $('pLabel').value = it.label || '';
    var isText = it.render === 'text';
    setClass($('pRenderPic'), 'on', !isText);
    setClass($('pRenderText'), 'on', isText);
    show($('pTextBox'), isText);
    if (isText) {
      $('pStem').value = it.stem || '';
      renderOptionInputs(it.options || []);
    }
    $('pBlank').value = it.blank_after === true ? 'yes' : it.blank_after === false ? 'no' : 'default';
    var notes = '';
    if (it.flags && it.flags.length) notes += '<div>' + chipsHtml(it) + '</div>';
    if (typeof it.confidence === 'number') notes += '<div>Confidence: ' + Math.round(it.confidence * 100) + '%' + (it.confidence < 0.6 ? ' <span class="bad">&mdash; please check this slide</span>' : '') + '</div>';
    if (it.parts && it.parts.length) {
      var pg = [];
      for (var i = 0; i < it.parts.length; i++) pg.push(it.parts[i].page + 1);
      notes += '<div>From page ' + pg.join(' + ') + '</div>';
    }
    $('pNotes').innerHTML = notes;
    $('pMerge').disabled = itemIndex(it.id) >= project.items.length - 1;
    $('pCrop').disabled = !project.pages.length;
  }

  function renderOptionInputs(opts) {
    var box = $('pOptions');
    box.innerHTML = '';
    for (var i = 0; i < opts.length; i++) addOptionInput(opts[i]);
  }
  function addOptionInput(val) {
    var box = $('pOptions'), n = box.children.length;
    var row = el('div', 'opt-row');
    row.innerHTML = '<input type="text" placeholder="Option ' + String.fromCharCode(97 + n) + '"><button type="button" title="Remove this option">&#10005;</button>';
    row.firstChild.value = val || '';
    row.lastChild.onclick = function () { box.removeChild(row); };
    box.appendChild(row);
    return row.firstChild;
  }
  $('pAddOpt').onclick = function () { addOptionInput('').focus(); };

  $('pLabel').onchange = function () {
    var it = selItem();
    if (!it) return;
    edit('PATCH', '/items/' + it.id, { label: trim(this.value) }, { after: refreshSummary });
  };
  $('pLabel').onkeydown = function (e) { if (e.keyCode === 13) this.blur(); };

  function setRender(mode) {
    var it = selItem();
    if (!it || it.render === mode) return;
    edit('PATCH', '/items/' + it.id, { render: mode });
  }
  $('pRenderPic').onclick = function () { setRender('snapshot'); };
  $('pRenderText').onclick = function () { setRender('text'); };

  $('pSaveText').onclick = function () {
    var it = selItem();
    if (!it) return;
    var inputs = $('pOptions').getElementsByTagName('input'), opts = [];
    for (var i = 0; i < inputs.length; i++) { var v = trim(inputs[i].value); if (v) opts.push(v); }
    edit('PATCH', '/items/' + it.id, { stem: $('pStem').value, options: opts });
  };

  $('pBlank').onchange = function () {
    var it = selItem();
    if (!it) return;
    var v = this.value === 'yes' ? true : this.value === 'no' ? false : null;
    edit('PATCH', '/items/' + it.id, { blank_after: v });
  };

  $('pCrop').onclick = function () { var it = selItem(); if (it) openCrop('edit', it); };
  $('pAdd').onclick = $('pAdd2').onclick = function () { openCrop('new', selItem()); };

  $('pReplace').onclick = function () { $('pReplaceInput').click(); };
  $('pReplaceInput').onchange = function () {
    var f = this.files[0], it = selItem();
    this.value = '';
    if (!f || !it) return;
    var fd = new FormData();
    fd.append('file', f, f.name);
    edit('POST', '/items/' + it.id + '/image', fd, { message: 'Picture replaced' });
  };

  $('pDup').onclick = function () {
    var it = selItem();
    if (!it) return;
    var idx = itemIndex(it.id);
    edit('POST', '/items/' + it.id + '/duplicate', null, { exclusive: true, message: 'Copy added below',
      after: function () { if (project.items[idx + 1]) select(project.items[idx + 1].id); } });
  };

  $('pMerge').onclick = function () {
    var it = selItem(), idx = itemIndex(selId);
    if (!it || idx >= project.items.length - 1) return;
    var next = project.items[idx + 1];
    edit('POST', '/items/' + it.id + '/merge', { 'with': next.id }, { exclusive: true, message: 'Merged' });
  };

  function deleteSelected() {
    var it = selItem();
    if (!it) return;
    if (!confirm('Delete slide "' + (it.label || it.id) + '"?')) return;
    var idx = itemIndex(it.id), nextSel = project.items[idx + 1] || project.items[idx - 1];
    selId = nextSel ? nextSel.id : null;
    edit('DELETE', '/items/' + it.id, null, { exclusive: true, message: 'Slide deleted', after: scrollToSel,
      fail: function () { selId = it.id; } });
  }
  $('pDelete').onclick = deleteSelected;

  /* ================================================================
   * 5. Crop editor
   * Coordinates are fractions (0..1) of the page image: [x0, y0, x1, y1].
   * ================================================================ */

  var crop = null;   // { mode:'edit'|'new', itemId, after, parts:[{page,box}], pageIdx, sel, fit:'page'|'width' }
  var drag = null;   // { kind:'move'|'resize'|'draw', idx, h, fx, fy, start:[...] }

  function openCrop(mode, it) {
    if (!project.pages.length) { toast('This deck has no source pages, so there is nothing to crop. Use "Replace picture" instead.', true); return; }
    var parts = [], startPage = 0, i;
    if (mode === 'edit') for (i = 0; i < (it.parts || []).length; i++) parts.push({ page: it.parts[i].page, box: it.parts[i].box.slice(0) });
    var ref = it && it.parts && it.parts.length ? it.parts[it.parts.length - 1].page : null;
    if (mode === 'edit' && parts.length) ref = parts[0].page;
    if (ref !== null) { startPage = pageBySeq(ref); if (startPage < 0) startPage = 0; }
    crop = { mode: mode, itemId: it ? it.id : null, after: it ? it.id : null, parts: parts, pageIdx: startPage, sel: parts.length ? 0 : -1, fit: 'page' };

    $('cropTitle').innerHTML = mode === 'new' ? 'Add a missing question' : 'Adjust crop of ' + esc(it.label || 'this slide');
    $('cropHelp').innerHTML = mode === 'new'
      ? 'Drag on the page to draw a box around the question. Use Next page to add a part that continues on another page.'
      : 'Drag the box or its corner squares. Drag on an empty area to add another part (for example, the rest of the question on the next page).';
    show($('cropLabelWrap'), mode === 'new');
    $('cropLabel').value = '';
    if (mode === 'edit' && it.custom_image) toast('Note: applying a crop will replace the picture you uploaded.', true);
    show($('cropModal'), true);
    showCropPage();
  }

  function closeCrop() { crop = null; drag = null; show($('cropModal'), false); }

  function showCropPage() {
    var pg = project.pages[crop.pageIdx];
    $('cropPageInfo').innerHTML = 'Page ' + (crop.pageIdx + 1) + ' of ' + project.pages.length;
    $('cropPrev').disabled = crop.pageIdx === 0;
    $('cropNext').disabled = crop.pageIdx >= project.pages.length - 1;
    $('cropImg').src = jobPath('/pages/' + pg.seq + '.jpg');
    layoutCrop();
  }

  /* Size the page to fit the modal (whole page, or full width with scrolling). */
  function layoutCrop() {
    if (!crop) return;
    drawBoxes();                 // first: the parts row above changes the space left for the page
    var pg = project.pages[crop.pageIdx], stage = $('cropStage');
    var pw = pg.w || 1086, ph = pg.h || 1536;
    var aw = stage.clientWidth - 40, ah = stage.clientHeight - 24;
    var s = crop.fit === 'width' ? aw / pw : Math.min(aw / pw, ah / ph);
    s = Math.max(s, 0.05);
    var page = $('cropPage');
    page.style.width = Math.round(pw * s) + 'px';
    page.style.height = Math.round(ph * s) + 'px';
    $('cropFit').innerHTML = crop.fit === 'width' ? 'Fit whole page' : 'Fit width (bigger)';
  }

  /* Rebuild the boxes of the current page and the parts list. */
  function drawBoxes() {
    var page = $('cropPage'), seq = project.pages[crop.pageIdx].seq, i;
    for (i = page.childNodes.length - 1; i >= 0; i--) if (page.childNodes[i].tagName !== 'IMG') page.removeChild(page.childNodes[i]);
    for (i = 0; i < crop.parts.length; i++) {
      if (crop.parts[i].page !== seq) continue;
      var b = el('div', 'cbox' + (i === crop.sel ? ' sel' : ''));
      b.id = 'cbox_' + i;
      b.setAttribute('data-idx', i);
      var h = '<span class="cnum">Part ' + (i + 1) + '</span>', hs = ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'];
      for (var k = 0; k < hs.length; k++) h += '<div class="hdl ' + hs[k] + '" data-h="' + hs[k] + '"></div>';
      b.innerHTML = h;
      placeBox(b, crop.parts[i].box);
      page.appendChild(b);
    }
    var list = '';
    for (i = 0; i < crop.parts.length; i++) {
      list += '<span class="part' + (i === crop.sel ? ' sel' : '') + '" data-pi="' + i + '">Part ' + (i + 1) + ' &middot; page ' + (pageBySeq(crop.parts[i].page) + 1) + '</span>';
    }
    $('cropParts').innerHTML = crop.parts.length > 1 || crop.mode === 'new'
      ? (list ? 'Parts (shown one below the other on the slide): ' + list : '<span class="muted">No box yet.</span>') : '';
    $('cropRemove').disabled = crop.sel < 0;
    $('cropApply').disabled = !crop.parts.length;
  }

  function placeBox(b, box) {
    b.style.left = (box[0] * 100) + '%';
    b.style.top = (box[1] * 100) + '%';
    b.style.width = ((box[2] - box[0]) * 100) + '%';
    b.style.height = ((box[3] - box[1]) * 100) + '%';
  }

  function clamp(v) { return v < 0 ? 0 : v > 1 ? 1 : v; }

  /* pointer position as a fraction of the page image */
  function pageFrac(cx, cy) {
    var r = $('cropPage').getBoundingClientRect();
    return { x: clamp((cx - r.left) / r.width), y: clamp((cy - r.top) / r.height) };
  }

  function pointerDown(target, cx, cy) {
    if (!crop) return false;
    var p = pageFrac(cx, cy), t = target, hName = null, idx = -1;
    while (t && t !== $('cropPage')) {
      if (!hName && t.getAttribute('data-h')) hName = t.getAttribute('data-h');
      if (t.getAttribute('data-idx') !== null) { idx = +t.getAttribute('data-idx'); break; }
      t = t.parentNode;
    }
    if (idx >= 0) {
      crop.sel = idx;
      drag = { kind: hName ? 'resize' : 'move', idx: idx, h: hName, fx: p.x, fy: p.y, start: crop.parts[idx].box.slice(0) };
    } else {
      /* draw a new box starting here */
      crop.parts.push({ page: project.pages[crop.pageIdx].seq, box: [p.x, p.y, p.x, p.y] });
      idx = crop.parts.length - 1;
      crop.sel = idx;
      drag = { kind: 'draw', idx: idx, h: 'se', fx: p.x, fy: p.y, start: [p.x, p.y, p.x, p.y] };
    }
    drawBoxes();
    return true;
  }

  function pointerMove(cx, cy) {
    if (!drag) return;
    var p = pageFrac(cx, cy), dx = p.x - drag.fx, dy = p.y - drag.fy, s = drag.start, b;
    if (drag.kind === 'move') {
      var w = s[2] - s[0], h = s[3] - s[1];
      var x0 = Math.min(Math.max(s[0] + dx, 0), 1 - w), y0 = Math.min(Math.max(s[1] + dy, 0), 1 - h);
      b = [x0, y0, x0 + w, y0 + h];
    } else {
      b = s.slice(0);
      var hn = drag.h;
      if (hn.indexOf('w') >= 0) b[0] = clamp(s[0] + dx);
      if (hn.indexOf('e') >= 0) b[2] = clamp(s[2] + dx);
      if (hn.indexOf('n') >= 0) b[1] = clamp(s[1] + dy);
      if (hn.indexOf('s') >= 0) b[3] = clamp(s[3] + dy);
      b = [Math.min(b[0], b[2]), Math.min(b[1], b[3]), Math.max(b[0], b[2]), Math.max(b[1], b[3])];
    }
    crop.parts[drag.idx].box = b;
    var elx = $('cbox_' + drag.idx);
    if (elx) placeBox(elx, b);         // only move the one box while dragging (cheap on old PCs)
  }

  function pointerUp() {
    if (!drag) return;
    var b = crop.parts[drag.idx].box;
    if (b[2] - b[0] < 0.01 || b[3] - b[1] < 0.005) {
      if (drag.kind === 'draw') { crop.parts.splice(drag.idx, 1); crop.sel = crop.parts.length - 1; }
      else crop.parts[drag.idx].box = drag.start;   // too small: put it back
    }
    drag = null;
    drawBoxes();
  }

  /* mouse */
  $('cropPage').onmousedown = function (e) {
    if (e.button !== 0) return;
    if (pointerDown(e.target, e.clientX, e.clientY)) e.preventDefault();
  };
  document.addEventListener('mousemove', function (e) { if (drag) { pointerMove(e.clientX, e.clientY); e.preventDefault(); } });
  document.addEventListener('mouseup', function () { pointerUp(); });

  /* touch (nice to have) */
  $('cropPage').addEventListener('touchstart', function (e) {
    if (e.touches.length !== 1) return;
    var t = e.touches[0];
    if (pointerDown(e.target, t.clientX, t.clientY)) e.preventDefault();
  }, false);
  $('cropPage').addEventListener('touchmove', function (e) {
    if (!drag) return;
    var t = e.touches[0];
    pointerMove(t.clientX, t.clientY);
    e.preventDefault();
  }, false);
  $('cropPage').addEventListener('touchend', function () { pointerUp(); }, false);

  $('cropParts').onclick = function (e) {
    var pi = e.target.getAttribute && e.target.getAttribute('data-pi');
    if (pi === null || pi === undefined || !crop) return;
    crop.sel = +pi;
    var idx = pageBySeq(crop.parts[crop.sel].page);
    if (idx >= 0 && idx !== crop.pageIdx) { crop.pageIdx = idx; showCropPage(); }
    else drawBoxes();
  };
  $('cropPrev').onclick = function () { if (crop && crop.pageIdx > 0) { crop.pageIdx--; showCropPage(); } };
  $('cropNext').onclick = function () { if (crop && crop.pageIdx < project.pages.length - 1) { crop.pageIdx++; showCropPage(); } };
  $('cropFit').onclick = function () { if (crop) { crop.fit = crop.fit === 'width' ? 'page' : 'width'; layoutCrop(); } };
  $('cropRemove').onclick = function () {
    if (!crop || crop.sel < 0) return;
    crop.parts.splice(crop.sel, 1);
    crop.sel = crop.parts.length ? Math.min(crop.sel, crop.parts.length - 1) : -1;
    drawBoxes();
  };
  $('cropCancel').onclick = closeCrop;

  function roundBox(b) {
    var out = [];
    for (var i = 0; i < 4; i++) out.push(Math.round(b[i] * 10000) / 10000);
    return out;
  }

  $('cropApply').onclick = function () {
    if (!crop || !crop.parts.length) return;
    var parts = [];
    for (var i = 0; i < crop.parts.length; i++) parts.push({ page: crop.parts[i].page, box: roundBox(crop.parts[i].box) });
    var c = crop;
    if (c.mode === 'edit') {
      closeCrop();
      edit('PATCH', '/items/' + c.itemId, { parts: parts }, { message: 'Crop saved' });
    } else {
      var label = trim($('cropLabel').value);
      closeCrop();
      /* the new item comes back alone; fetch the project so the list and numbering are right */
      edit('POST', '/items', { after: c.after, label: label, parts: parts }, {
        exclusive: true, message: 'Question added',
        after: function (res) {
          var newId = res && res.item ? res.item.id : null;
          api('GET', jobPath('/project?t=' + new Date().getTime()), null, function (err, p) {
            if (err) { toast(err, true); return; }
            if (newId) selId = newId;
            applyProject(p);
            scrollToSel();
          });
        }
      });
    }
  };

  /* ================================================================
   * 6. Keyboard + start-up
   * ================================================================ */

  document.onkeydown = function (e) {
    e = e || window.event;
    var tag = (e.target && e.target.tagName || '').toLowerCase();
    var typing = tag === 'input' || tag === 'textarea' || tag === 'select';
    if (crop) {
      if (e.keyCode === 27) closeCrop();                                            // Esc
      else if (e.keyCode === 46 && !typing && crop.sel >= 0) $('cropRemove').onclick();   // Delete selected part
      return;
    }
    if (!project || hasClass($('scrEditor'), 'hidden')) return;
    if (typing) return;                                                             // typing in a field
    if (e.keyCode === 38) { selectBy(-1); e.preventDefault(); }                     // Up
    else if (e.keyCode === 40) { selectBy(1); e.preventDefault(); }                 // Down
    else if (e.keyCode === 46) { deleteSelected(); e.preventDefault(); }            // Delete
    else if ((e.ctrlKey || e.metaKey) && (e.keyCode === 90)) { undo(); e.preventDefault(); }   // Ctrl+Z
  };

  window.onhashchange = function () {
    var id = hashJob();
    if (id && id !== jobId) resumeJob(id);
  };

  renderFileList();
  var startId = hashJob();
  if (startId) resumeJob(startId);
  else showScreen('scrUpload');
})();
