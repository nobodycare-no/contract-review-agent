const j = (url, opt) => fetch(url, opt).then(r => r.json())

export const api = {
  health: () => j('/health'),
  queue: () => j('/app/queue'),
  detail: id => j(`/agent/tasks/${id}`),
  pullForms: () => j('/tools/list_pending', { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: '{"limit":20}' }),
  createForms: (fd) => fetch('/app/forms', { method: 'POST', body: fd }).then(r => r.json()),
  batchReview: ids => j('/app/batch_review', { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ task_ids: ids }) }),
  runAgent: body => j('/agent/run', { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  retry: id => fetch(`/agent/tasks/${id}/retry`, { method: 'POST' }).then(r => r.json()),
  adminHeaders: () => ({ 'X-Admin-Token': localStorage.getItem('cra_admin') || '' })
}
