const pending = document.querySelector('[data-poll]');
if (pending) {
  const poll = async () => {
    try {
      const response = await fetch(`/api/cases/${pending.dataset.poll}`);
      if (response.ok) {
        const data = await response.json();
        if (['ready', 'manual', 'reviewed'].includes(data.status)) { location.reload(); return; }
      }
    } catch (_) { /* Keep retry controls usable when the service is temporarily unavailable. */ }
    setTimeout(poll, 2000);
  };
  setTimeout(poll, 2000);
}
