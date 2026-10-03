/* Studio admin embed masking script.
 *
 * Injected inline by the chat proxy into Open WebUI HTML documents that are
 * marked ?__studio_admin=1 (workspace embeds: Knowledge, Tools). The
 * stylesheet chat/embed_admin.css cannot express these rules in pure CSS
 * (the targets are identified by text content, and :text-is() is not a real
 * CSS selector), so the masking lives here.
 *
 * Reads from disk per marked-document request, so edits apply on the next
 * page load without a server restart (the proxy is not StatReloader-managed).
 * Keep this file browser-safe and dependency-free: it runs in the Open WebUI
 * origin, where Studio owns no other assets.
 */
(function () {
  // No hover tooltip: the browser shows the frame's document title when the
  // pointer sits on the iframe, and Open WebUI names its pages "Open WebUI
  // <section>". Pin the title empty so nothing shows. The <title> element
  // exists at injection time (we are in <head>); a MutationObserver keeps it
  // pinned because the SPA re-sets the title after boot.
  var titleEl = document.querySelector('title');
  if (titleEl) {
    titleEl.textContent = '';
    new MutationObserver(function () {
      if (titleEl.textContent.trim() !== '') {
        titleEl.textContent = '';
      }
    }).observe(titleEl, { childList: true, characterData: true, subtree: true });
  }
  function text(el) {
    return el.textContent.trim();
  }
  function maskModal(modal) {
    // Only leaf elements carry the exact texts; iterate once.
    var divs = modal.querySelectorAll('div');
    for (var i = 0; i < divs.length; i++) {
      var el = divs[i];
      if (el.childElementCount > 0) {
        continue;
      }
      var t = text(el);
      // The "Access List" row in the KB create/edit modal (label + Add Access).
      if (t === 'Access List') {
        var row = el.closest('.flex.items-center.justify-between');
        if (row && row.style.display !== 'none') {
          row.style.display = 'none';
        }
      } else if (t === 'No access grants. Private to you.') {
        // The empty-state wrapper directly under it.
        var wrap = el.parentElement;
        if (wrap && wrap !== modal && wrap.style.display !== 'none') {
          wrap.style.display = 'none';
        }
      }
    }
  }
  function maskBranding() {
    // The "Made by Open WebUI Community" credit + "Discover a tool" promo
    // card shown in empty workspace states: pure branding, no function here.
    // Find the promo link, then climb to the smallest ancestor that also
    // holds the credit text — that is the card — and hide it.
    var link = document.querySelector('main#main-content a[href*="openwebui.com"]');
    if (!link) {
      return;
    }
    var a = link;
    var aborted = false;
    while (a && a.parentElement) {
      a = a.parentElement;
      var t = a.textContent || '';
      if (t.indexOf('Made by Open WebUI Community') !== -1) {
        break;
      }
      // Safety: if the credit is no longer next to the link, do not keep
      // climbing towards <main> and hide the whole section.
      if (t.length > 800) {
        aborted = true;
        break;
      }
    }
    if (!aborted && a && a !== document.body && a.style.display !== 'none') {
      a.style.display = 'none';
    }
  }
  function maskUploadMenu() {
    // "Upload directory" and "Sync directory" use the File System Access API
    // (showDirectoryPicker), which browsers only allow in a top-level or
    // same-origin frame. This embed is a cross-origin subframe, so those two
    // rows would throw a SecurityError. They live in the full workspace (linked
    // from the Studio page), so hide the rows here and keep the rest of the "+"
    // menu (Upload files, New directory, Add webpage, Add text content).
    var rows = document.querySelectorAll('button');
    for (var i = 0; i < rows.length; i++) {
      var t = rows[i].textContent.trim();
      if (t === 'Upload directory' || t === 'Sync directory') {
        if (rows[i].style.display !== 'none') {
          rows[i].style.display = 'none';
        }
      }
    }
  }
  function scan() {
    var modals = document.querySelectorAll('div.modal');
    for (var k = 0; k < modals.length; k++) {
      maskModal(modals[k]);
    }
    maskBranding();
    maskUploadMenu();
  }
  var observer = new MutationObserver(scan);
  function start() {
    scan();
    observer.observe(document.body, { childList: true, subtree: true });
  }
  if (document.body) {
    start();
  } else {
    // Injected into <head>: the body may not exist yet.
    document.addEventListener('DOMContentLoaded', start);
  }
  // ?create=1 — deep link from Studio's agent form: once the SPA has rendered
  // the section header, click Create so the user lands directly in the form.
  if (new URLSearchParams(location.search).get('create') === '1') {
    var attempts = 0;
    var poll = setInterval(function () {
      attempts += 1;
      var btns = document.querySelectorAll('main#main-content nav button');
      for (var i = 0; i < btns.length; i++) {
        if (btns[i].textContent.trim() === 'Create') {
          clearInterval(poll);
          btns[i].click();
          return;
        }
      }
      if (attempts > 100) { // ~25 s: the SPA failed to boot; give up
        clearInterval(poll);
      }
    }, 250);
  }
})();
