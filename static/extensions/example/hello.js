// Example Studio extension (docs/extensions.md). Loaded only when an operator
// lists it in SIMPLEAUDIT_EXTENSIONS. Adds a small badge to the top bar of run
// pages showing who is signed in and in which workspace.
Studio.onPage("/runs/*", function (studio) {
  var actions = document.querySelector("header .flex.items-center.gap-2:last-child");
  if (!actions) return;
  var badge = document.createElement("span");
  badge.className = "studio-example-badge";
  badge.textContent = studio.user.username + " · " + (studio.workspace ? studio.workspace.name : "no workspace");
  badge.title = "Added by the example extension (role: " + studio.role + ")";
  actions.prepend(badge);
});
