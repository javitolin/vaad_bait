// CloudFront signs origin requests to the Lambda URL with SigV4, and Lambda
// rejects unsigned payloads, so the body hash has to come from the client.
// Without this every form post returns 403 with a signature mismatch.
async function bodyHash(body) {
  const bytes = typeof body === "string" ? new TextEncoder().encode(body) : body;
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (form.method.toLowerCase() !== "post") return;
  event.preventDefault();
  if (form.dataset.confirm && !confirm(form.dataset.confirm)) return;

  // Only the ones enabled now, so a button greyed out on purpose stays that way.
  const buttons = [...form.querySelectorAll("button:not(:disabled)")];
  buttons.forEach((b) => (b.disabled = true));
  try {
    const { body, type } = await encode(form);
    const response = await fetch(form.action, {
      method: "POST",
      headers: { "content-type": type, "x-amz-content-sha256": await bodyHash(body) },
      body,
    });
    if (response.status === 401) {
      location.href = "/auth/login?next=" + encodeURIComponent(location.pathname + location.search);
      return;
    }
    if (response.redirected) {
      // Back to the same page: reload keeps the scroll position while marking a long list.
      if (response.url === location.href) location.reload();
      else location.href = response.url;
      return;
    }
    document.open();
    document.write(await response.text());
    document.close();
  } finally {
    buttons.forEach((b) => (b.disabled = false));
  }
});

async function encode(form) {
  if (form.enctype !== "multipart/form-data") {
    return { body: new URLSearchParams(new FormData(form)).toString(), type: "application/x-www-form-urlencoded" };
  }
  const data = new FormData(form);
  const files = data.getAll("files").filter((f) => f.size);
  data.delete("files");
  for (const file of files) data.append("files", await shrink(file));
  // One Request, so the multipart boundary in the header matches the bytes that get hashed.
  const request = new Request(form.action, { method: "POST", body: data });
  return { body: await request.arrayBuffer(), type: request.headers.get("content-type") };
}

// Phone photos run to several MB and the Lambda behind CloudFront accepts at most 6.
async function shrink(file) {
  if (!file.type.startsWith("image/") || file.size < 700 * 1024) return file;
  try {
    const bitmap = await createImageBitmap(file);
    const scale = Math.min(1, 1800 / Math.max(bitmap.width, bitmap.height));
    const canvas = Object.assign(document.createElement("canvas"), {
      width: Math.round(bitmap.width * scale),
      height: Math.round(bitmap.height * scale),
    });
    canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.85));
    return new File([blob], file.name.replace(/\.\w+$/, "") + ".jpg", { type: "image/jpeg" });
  } catch {
    return file;
  }
}

document.addEventListener("input", (event) => {
  const hint = event.target.form?.querySelector("[data-dirty-hint]");
  if (hint) hint.hidden = false;
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

// One sheet serves every cell: it is filled from the clicked cell's data attributes.
function openSheet(cell) {
  const d = cell.dataset;
  const sheet = document.getElementById("cell-sheet");
  const flags = {
    bounced: Boolean(d.bounced),
    cheque: d.method === "cheque",
    "no-cheque-no": d.method === "cheque" && !d.cheque,
    receipt: Boolean(d.receipt),
    manual: d.pane === "paid" && !d.receipt,
  };
  const fill = (template) => template.replace(/\{(\w+)\}/g, (_, key) => encodeURIComponent(d[key] ?? ""));
  const text = { when: `דירה ${d.apt} · ${d.when}`, name: d.name, "method-he": d.methodHe };
  sheet.querySelectorAll("[data-f]").forEach((el) => (el.textContent = text[el.dataset.f] ?? ""));
  sheet.querySelectorAll("[data-pane]").forEach((el) => (el.hidden = el.dataset.pane !== d.pane));
  sheet.querySelectorAll("[data-show-if]").forEach((el) => (el.hidden = !flags[el.dataset.showIf]));
  sheet.querySelectorAll("[data-action]").forEach((form) => (form.action = fill(form.dataset.action)));
  sheet.querySelectorAll("[data-href]").forEach((link) => (link.href = fill(link.dataset.href)));
  sheet.querySelectorAll("input[name=apt_id]").forEach((input) => (input.value = d.apt));
  sheet.querySelectorAll("input[name=period]").forEach((input) => (input.value = d.period));
  sheet.querySelectorAll("input[name=back]").forEach((input) => (input.value = location.pathname + location.search));
  sheet.querySelectorAll("input[name=cheque_no]").forEach((input) => (input.value = d.cheque || ""));
  sheet.querySelectorAll("[data-needs-cheque]").forEach((button) => (button.disabled = flags["no-cheque-no"]));
  sheet.querySelectorAll(".method-btn").forEach((button) => button.classList.toggle("suggested", button.value === d.default));
  sheet.showModal();
}

document.addEventListener("click", (event) => {
  const cell = event.target.closest("[data-cell]");
  if (cell) openSheet(cell);
  // A click on the backdrop lands on the dialog element itself, outside its box.
  if (event.target.matches("dialog.sheet")) {
    const box = event.target.getBoundingClientRect();
    const inside = event.clientX >= box.left && event.clientX <= box.right && event.clientY >= box.top && event.clientY <= box.bottom;
    if (!inside) event.target.close();
  }
});
document.addEventListener("keydown", (event) => {
  const cell = event.target.closest("li[data-cell]");
  if (cell && (event.key === "Enter" || event.key === " ")) {
    event.preventDefault();
    openSheet(cell);
  }
});
