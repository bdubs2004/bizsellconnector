// Connect page: copy buttons, and the dev plan switcher submits on change.
document.addEventListener("click", function (e) {
  var b = e.target.closest("[data-copy]");
  if (!b) return;
  var el = document.getElementById(b.dataset.copy);
  if (!el || !navigator.clipboard) return;
  navigator.clipboard.writeText(el.textContent.trim()).then(function () {
    var t = b.textContent; b.textContent = "Copied"; setTimeout(function () { b.textContent = t; }, 1500);
  });
});
document.addEventListener("change", function (e) {
  if (e.target.matches("select[data-autosubmit]")) e.target.form.submit();
});
