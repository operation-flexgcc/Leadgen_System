(() => {
  const form = document.querySelector("[data-prospect-form]");
  if (!form) return;

  const status = document.querySelector("#id_status");
  const meetingFields = [...document.querySelectorAll("[data-meeting-field]")];
  const meetingInputs = [
    document.querySelector("#id_meeting_scheduled_at"),
    document.querySelector("#id_meeting_timezone"),
    document.querySelector("#id_meeting_participants"),
  ].filter(Boolean);
  const workstream = document.querySelector("#id_workstream");
  const owner = document.querySelector("#id_owner");
  const workstreamSections = [...document.querySelectorAll("[data-workstream-section]")];
  const guidanceBlocks = [...document.querySelectorAll("[data-guidance]")];
  const sectionNumbers = [...document.querySelectorAll("[data-section-number]")];
  const escalationToggle = document.querySelector("#id_founder_escalation_required");
  const escalationNotes = document.querySelector("[data-escalation-notes]");
  const linkedInRequiredIds = [
    "#id_contact_linkedin_url",
    "#id_founder_account",
    "#id_linkedin_connection_status",
    "#id_personalization_note",
  ];

  const selectedWorkstream = () => workstream?.value || form.dataset.initialWorkstream || "intern";

  const setRequired = (input, required) => {
    input.required = required;
    input.setAttribute("aria-required", required ? "true" : "false");
    const label = input.closest(".field")?.querySelector("label");
    if (!label) return;
    let marker = label.querySelector(".required");
    if (required && !marker) {
      marker = document.createElement("span");
      marker.className = "required";
      marker.textContent = "*";
      label.append(marker);
    }
    if (marker) marker.hidden = !required;
  };

  const updateMeetingVisibility = () => {
    if (!status) return;
    const shouldShow = status.value === "meeting_scheduled" || status.value === "meeting_done";
    const shouldRequire = status.value === "meeting_scheduled";
    meetingFields.forEach((field) => { field.hidden = !shouldShow; });
    meetingInputs.forEach((input) => {
      setRequired(input, shouldRequire);
    });
  };

  const updateOwnerOptions = () => {
    if (!owner) return;
    const role = selectedWorkstream();
    [...owner.options].forEach((option) => {
      const matches = !option.value || option.dataset.role === role;
      option.hidden = !matches;
      option.disabled = !matches;
    });
    const selected = owner.options[owner.selectedIndex];
    if (selected?.value && selected.dataset.role !== role) owner.value = "";
  };

  const updateWorkstreamVisibility = () => {
    const role = selectedWorkstream();
    workstreamSections.forEach((section) => {
      section.hidden = section.dataset.workstreamSection !== role;
    });
    guidanceBlocks.forEach((block) => {
      block.hidden = block.dataset.guidance !== role;
    });
    sectionNumbers.forEach((number) => {
      number.textContent = role === "linkedin_outreach" ? number.dataset.linkedinNumber : number.dataset.defaultNumber;
    });
    linkedInRequiredIds.forEach((selector) => {
      const input = document.querySelector(selector);
      if (!input) return;
      setRequired(input, role === "linkedin_outreach");
    });
    updateOwnerOptions();
  };

  const updateEscalationVisibility = () => {
    if (!escalationToggle || !escalationNotes) return;
    escalationNotes.hidden = !escalationToggle.checked;
  };

  status?.addEventListener("change", updateMeetingVisibility);
  workstream?.addEventListener("change", updateWorkstreamVisibility);
  escalationToggle?.addEventListener("change", updateEscalationVisibility);
  updateMeetingVisibility();
  updateWorkstreamVisibility();
  updateEscalationVisibility();
})();
