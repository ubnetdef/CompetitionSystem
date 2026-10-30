document.addEventListener("DOMContentLoaded", () => {
  for (const form of document.querySelectorAll("form[data-builder-mode]")) {
    const selector = form.querySelector('[name="check_type"]');
    if (!selector) continue;

    const hostGroupSelector = form.querySelector('select[name="host_group_id"]');
    const configSections = form.querySelectorAll("[data-check-config]");
    const teamAccessSection = form.querySelector("[data-team-access]");
    const updateCheckTypeOptions = () => {
      if (form.dataset.builderMode !== "true" || !hostGroupSelector) return;
      const selectedGroup = hostGroupSelector.selectedOptions[0];
      let allowedTypes = [];
      try {
        allowedTypes = JSON.parse(selectedGroup?.dataset.checkTypes || "[]");
      } catch (error) {
        console.error("Could not read the selected device group's check types.", error);
      }

      for (const option of selector.querySelectorAll("[data-check-type]")) {
        const allowed = option.dataset.supported === "true"
          && selectedGroup?.value
          && allowedTypes.includes(option.value);
        option.disabled = !allowed;
      }
      if (selector.value && selector.selectedOptions[0]?.disabled) {
        selector.value = "";
      }
    };

    const updateSections = () => {
      for (const section of configSections) {
        const active = section.dataset.checkConfig === selector.value;
        section.hidden = !active;
        for (const field of section.querySelectorAll("input, select, textarea")) {
          field.disabled = !active;
        }
      }
      if (teamAccessSection) {
        const active = ["ssh", "winrm", "ldap", "sql", "ftp"].includes(selector.value);
        teamAccessSection.hidden = !active;
        for (const field of teamAccessSection.querySelectorAll("input")) {
          field.disabled = !active;
        }
      }
    };

    hostGroupSelector?.addEventListener("change", updateCheckTypeOptions);
    selector.addEventListener("change", updateSections);
    updateCheckTypeOptions();
    updateSections();

    const savedUsername = form.querySelector("[data-saved-username]")?.value;
    if (savedUsername) {
      for (const field of form.querySelectorAll('[data-check-config] input[name="username"]')) {
        if (!field.value) field.value = savedUsername;
      }
    }

    const skipHostKeyVerification = form.querySelector(
      '[name="skip_host_key_verification"]',
    );
    const hostKeyFingerprint = form.querySelector('[name="host_key_fingerprint"]');
    const updateSshVerificationFields = () => {
      if (!skipHostKeyVerification || !hostKeyFingerprint) return;
      const sshSection = form.querySelector('[data-check-config="ssh"]');
      hostKeyFingerprint.disabled = skipHostKeyVerification.checked
        || Boolean(sshSection?.hidden);
    };
    skipHostKeyVerification?.addEventListener("change", updateSshVerificationFields);
    selector.addEventListener("change", updateSshVerificationFields);
    updateSshVerificationFields();

    const lookup = form.querySelector('[name="lookup"]');
    const recordType = form.querySelector('[name="record_type"]');
    const dnsSection = form.querySelector('[data-check-config="dns"]');
    const expected = dnsSection?.querySelector('[name="expected_output"]');
    const status = dnsSection?.querySelector('[aria-live="polite"]');
    if (!lookup || !recordType || !expected || !status) continue;

    let autoFilledValue = "";
    let requestController;
    const loadCurrentDnsAnswer = async () => {
      if (selector.value !== "dns" || !lookup.value.trim()) return;
      if (expected.value.trim() && expected.value !== autoFilledValue) return;
      requestController?.abort();
      requestController = new AbortController();
      const requestedLookup = lookup.value.trim();
      const requestedType = recordType.value;
      status.textContent = "Looking up the current DNS answer…";
      try {
        const params = new URLSearchParams({
          lookup: requestedLookup,
          record_type: requestedType,
        });
        const response = await fetch(`${form.dataset.dnsPreviewUrl}?${params}`, {
          signal: requestController.signal,
          headers: { Accept: "application/json" },
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || "DNS lookup failed.");
        if (lookup.value.trim() !== requestedLookup || recordType.value !== requestedType) return;
        expected.value = result.expected_output;
        autoFilledValue = result.expected_output;
        status.textContent = "Expected answer filled from the current DNS response. You can edit it.";
      } catch (error) {
        if (error.name === "AbortError") return;
        status.textContent = error.message || "The current DNS answer could not be resolved.";
      }
    };

    expected.addEventListener("input", () => {
      if (expected.value !== autoFilledValue) autoFilledValue = "";
    });
    lookup.addEventListener("blur", loadCurrentDnsAnswer);
    recordType.addEventListener("change", loadCurrentDnsAnswer);
  }
});
