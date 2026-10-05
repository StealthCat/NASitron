(() => {
  const input = document.querySelector('input[name="confirm_text"]');
  if (!input) return;
  const form = input.form, button = form.querySelector('button[type="submit"]');
  let submitted = false;
  input.addEventListener('input', () => input.setCustomValidity(input.value === input.dataset.confirmation ? '' : 'Type the confirmation exactly as shown.'));
  form.addEventListener('submit', event => {
    if (submitted) { event.preventDefault(); return; }
    submitted = true;
    button.disabled = true;
    button.textContent = 'Submitting…';
    document.getElementById('zfs-submit-status').textContent = 'Waiting for the NAS. Keep this page open; inspect pool status if the connection is interrupted.';
  });
  window.addEventListener('pageshow', () => { submitted = false; button.disabled = false; button.textContent = 'Execute confirmed action'; });
})();
