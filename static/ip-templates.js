(() => {
  const form = document.querySelector("[data-ip-template-form]");
  if (!form) return;

  let dirty = false;
  const redirectStep = form.querySelector('[name="redirect_step"]');

  form.addEventListener("input", () => {
    dirty = true;
  });
  form.addEventListener("change", () => {
    dirty = true;
  });
  form.addEventListener("submit", () => {
    dirty = false;
  });

  document.querySelectorAll(".ip-template-nav").forEach((link) => {
    link.addEventListener("click", (event) => {
      if (!dirty) return;

      event.preventDefault();
      if (!window.confirm("Save all IP template changes before leaving this step?")) {
        return;
      }

      const destination = new URL(link.href, window.location.href);
      redirectStep.value = destination.searchParams.get("step") || "templates";
      form.requestSubmit();
    });
  });

  window.addEventListener("beforeunload", (event) => {
    if (!dirty) return;
    event.preventDefault();
    event.returnValue = "";
  });
})();
