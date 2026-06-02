// ---------- Global settings save ---------------------------------------------
document.querySelectorAll('[data-action="save"]').forEach(btn => {
  btn.addEventListener('click', async (e) => {
    e.preventDefault();
    const row = btn.closest('.setting-row');
    const key = row.dataset.key;
    const type = row.dataset.type;
    const isSecret = row.dataset.secret === '1';
    const input = row.querySelector('[data-value]');
    let value = input.value;
    if (type === 'int') value = parseInt(value, 10);
    else if (type === 'float') value = parseFloat(value);
    else if (type === 'bool') value = value === 'true';
    else if (type === 'json') value = JSON.parse(value);

    const resp = await fetch('/api/settings', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        key, value, value_type: type, is_secret: isSecret,
      }),
    });
    flash(btn, resp.ok);
    if (isSecret && resp.ok) input.value = '';
  });
});

// ---------- Project settings save --------------------------------------------
document.querySelectorAll('[data-action="save-project"]').forEach(btn => {
  btn.addEventListener('click', async (e) => {
    e.preventDefault();
    const form = btn.closest('form');
    const projectId = form.dataset.projectId;
    const row = btn.closest('.setting-row');
    const key = row.dataset.key;
    const type = row.dataset.type;
    const input = row.querySelector('[data-value]');
    let value = input.value;
    if (type === 'int') value = parseInt(value, 10);
    else if (type === 'float') value = parseFloat(value);
    else if (type === 'bool') value = value === 'true';

    const resp = await fetch(`/api/projects/${projectId}/settings`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ key, value, value_type: type }),
    });
    flash(btn, resp.ok);
  });
});

function flash(btn, ok) {
  const original = btn.textContent;
  btn.textContent = ok ? 'Saved' : 'Failed';
  btn.style.background = ok ? 'var(--ok)' : 'var(--danger)';
  setTimeout(() => {
    btn.textContent = original;
    btn.style.background = '';
  }, 1200);
}

// ---------- Add / delete project ---------------------------------------------
const addBtn = document.getElementById('add-project-btn');
const dialog = document.getElementById('project-dialog');
if (addBtn && dialog) {
  addBtn.addEventListener('click', () => dialog.showModal());
  dialog.querySelectorAll('[data-close]').forEach(b =>
    b.addEventListener('click', () => dialog.close()));

  // Swap visible field-sets when broker selection changes.
  const brokerSel = document.getElementById('broker-type-select');
  function applyBrokerVisibility() {
    const v = brokerSel ? brokerSel.value : 'alpaca';
    document.querySelectorAll('[data-broker-fields]').forEach(el => {
      const show = el.dataset.brokerFields === v;
      el.hidden = !show;
      // Toggle 'required' so hidden fields don't block form submit.
      el.querySelectorAll('[data-broker-required]').forEach(inp => {
        inp.required = (inp.dataset.brokerRequired === v);
      });
    });
  }
  if (brokerSel) {
    brokerSel.addEventListener('change', applyBrokerVisibility);
    applyBrokerVisibility();
  }

  document.getElementById('project-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const brokerType = fd.get('broker_type') || 'alpaca';
    const payload = {
      project_id: fd.get('project_id'),
      project_name: fd.get('project_name'),
      broker_type: brokerType,
      // Alpaca fields (sent as empty strings for ETrade projects;
      // backend accepts and stores empty for the other broker).
      alpaca_api_key: fd.get('alpaca_api_key') || '',
      alpaca_secret_key: fd.get('alpaca_secret_key') || '',
      alpaca_base_url: fd.get('alpaca_base_url') || 'https://paper-api.alpaca.markets',
      alpaca_data_feed: fd.get('alpaca_data_feed') || 'iex',
      // ETrade fields
      etrade_consumer_key: fd.get('etrade_consumer_key') || '',
      etrade_consumer_secret: fd.get('etrade_consumer_secret') || '',
      etrade_environment: fd.get('etrade_environment') || 'sandbox',
      max_equity_allocation: parseFloat(fd.get('max_equity_allocation')),
      is_active: fd.get('is_active') === 'on',
    };
    const resp = await fetch('/api/projects', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    if (resp.ok) {
      dialog.close();
      toast.ok('Project saved');
      // For ETrade projects, redirect to OAuth start so the user can
      // mint access tokens immediately. Otherwise just reload.
      if (brokerType === 'etrade') {
        const pid = encodeURIComponent(payload.project_id);
        setTimeout(() => {
          window.location.href = `/etrade/connect?project_id=${pid}`;
        }, 400);
      } else {
        setTimeout(() => location.reload(), 400);
      }
    } else {
      toast.error('Failed to save project');
    }
  });
}

document.querySelectorAll('[data-action="delete"]').forEach(btn => {
  btn.addEventListener('click', async () => {
    if (!await confirmModal('Delete this project and all its data?',
        { title: 'Delete project', okLabel: 'Delete' })) return;
    const id = btn.dataset.id;
    const resp = await fetch(`/api/projects/${id}`, { method: 'DELETE' });
    if (resp.ok) location.reload();
    else toast.error('Delete failed');
  });
});
