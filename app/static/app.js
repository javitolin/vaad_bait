// CloudFront signs origin requests to the Lambda URL with SigV4, and Lambda
// rejects unsigned payloads, so the body hash has to come from the client.
// Without this every form post returns 403 with a signature mismatch.
async function bodyHash(body) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(body));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (form.method.toLowerCase() !== "post") return;
  event.preventDefault();
  if (form.dataset.confirm && !confirm(form.dataset.confirm)) return;

  const body = new URLSearchParams(new FormData(form)).toString();
  const buttons = form.querySelectorAll("button");
  buttons.forEach((b) => (b.disabled = true));
  try {
    const response = await fetch(form.action, {
      method: "POST",
      headers: {
        "content-type": "application/x-www-form-urlencoded",
        "x-amz-content-sha256": await bodyHash(body),
      },
      body,
    });
    if (response.status === 401) {
      location.href = "/auth/login?next=" + encodeURIComponent(location.pathname + location.search);
      return;
    }
    if (response.redirected) {
      location.href = response.url;
      return;
    }
    document.open();
    document.write(await response.text());
    document.close();
  } finally {
    buttons.forEach((b) => (b.disabled = false));
  }
});

// On a phone the share sheet sends the PDF itself; WhatsApp takes it from there.
// Desktop browsers mostly can't share files, so download it and open the chat.
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-share]");
  if (!button) return;
  const { pdf, name, text, phone } = button.dataset;
  const blob = await (await fetch(pdf)).blob();
  const file = new File([blob], name, { type: "application/pdf" });
  if (navigator.canShare && navigator.canShare({ files: [file] })) {
    try {
      await navigator.share({ files: [file], text });
    } catch (error) {
      if (error.name !== "AbortError") throw error;
    }
    return;
  }
  const link = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: name });
  link.click();
  window.open(`https://wa.me/${phone}?text=${encodeURIComponent(text)}`, "_blank");
});

function syncToggles() {
  const chosen = document.querySelector("[data-toggles] input:checked");
  document.querySelectorAll("[data-show-for]").forEach((el) => {
    el.hidden = !chosen || el.dataset.showFor !== chosen.value;
  });
}

// Picking another apartment brings its own fee and usual payment method.
function applyApartment(select) {
  const option = select.selectedOptions[0];
  const form = select.form;
  if (!option || !option.dataset.fee) return;
  form.elements.amount.value = option.dataset.fee;
  const method = form.querySelector(`input[name="method"][value="${option.dataset.method}"]`);
  if (method) method.checked = true;
  syncToggles();
}

document.addEventListener("change", (event) => {
  if (event.target.closest("[data-toggles]")) syncToggles();
  if (event.target.matches("select[data-apartment]")) applyApartment(event.target);
});
syncToggles();
