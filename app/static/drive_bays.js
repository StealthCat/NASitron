/* Native dialogs provide keyboard focus, Escape dismissal and modal semantics. */
(() => {
  document.querySelectorAll('[data-assign-bay]').forEach(button => {
    button.addEventListener('click', () => {
      const dialog = document.getElementById(button.dataset.dialog);
      const form = dialog.querySelector('form');
      form.reset();
      form.action = button.dataset.action;
      form.elements.slot.value = button.dataset.slot;
      dialog.querySelector('[data-bay-location]').textContent = button.dataset.location;
      dialog.querySelector('[data-bay-error]').hidden = true;
      dialog.showModal();
    });
  });
  document.querySelectorAll('.bay-dialog').forEach(dialog => {
    dialog.querySelector('[data-close-bay-dialog]').addEventListener('click', () => dialog.close());
    dialog.querySelector('form').addEventListener('submit', async event => {
      event.preventDefault();
      const form = event.currentTarget, submit = form.querySelector('[type=submit]');
      if (!form.reportValidity() || submit.disabled) return;
      const error = dialog.querySelector('[data-bay-error]');
      error.hidden = true; submit.disabled = true;
      const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 15000);
      try {
        const response = await fetch(form.action, {method:'POST', body:new FormData(form), signal:controller.signal});
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          throw new Error(typeof data.detail === 'string' ? data.detail : 'Assignment failed. Refresh the page and try again.');
        }
        // Reload from the server so every available-drive list reflects the assignment.
        location.reload();
      } catch (e) {
        error.textContent = e.name === 'AbortError' ? 'Request timed out. Refresh to check whether the drive was assigned.' : e.message;
        error.hidden = false;
      } finally { clearTimeout(timeout); submit.disabled = false; }
    });
  });
})();
