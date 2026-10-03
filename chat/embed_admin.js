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
  function scan() {
    var modals = document.querySelectorAll('div.modal');
    for (var k = 0; k < modals.length; k++) {
      maskModal(modals[k]);
    }
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
})();
