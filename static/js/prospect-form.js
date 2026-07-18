(() => {
  const status = document.querySelector("#id_status");
  const meetingField = document.querySelector("[data-meeting-field]");
  const meetingInput = document.querySelector("#id_meeting_scheduled_at");
  if (!status || !meetingField || !meetingInput) return;

  const updateMeetingVisibility = () => {
    const shouldShow = status.value === "meeting_scheduled" || status.value === "meeting_done";
    meetingField.hidden = !shouldShow;
    meetingInput.required = status.value === "meeting_scheduled";
    meetingInput.setAttribute("aria-required", meetingInput.required ? "true" : "false");
  };

  status.addEventListener("change", updateMeetingVisibility);
  updateMeetingVisibility();
})();
