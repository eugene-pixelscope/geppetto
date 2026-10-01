const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const valueText = value => value === undefined ? '—' : typeof value === 'string' ? value : JSON.stringify(value);

function fields(values) {
  return Object.entries(values || {}).map(([key, value]) => {
    const nested = value !== null && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).length;
    return nested
      ? `<div class="run-property-group"><details open><summary>${esc(key)}</summary><dl class="run-properties">${fields(value)}</dl></details></div>`
      : `<div class="run-property"><dt>${esc(key)}</dt><dd><code>${esc(valueText(value))}</code></dd></div>`;
  }).join('');
}

export function runOverview(run) {
  const overview = {
    'Run name': run.name,
    'Run ID': run.id,
    'State': run.state,
    ...(run.provenance_note ? {'Source': run.provenance_note} : {}),
    ...(run.frozen?.config_sha256 ? {'Config hash': run.frozen.config_sha256} : {}),
    ...(run.airflow && Object.keys(run.airflow).length ? {'Airflow': run.airflow} : {}),
    ...(run.mlflow && Object.keys(run.mlflow).length ? {'MLflow': run.mlflow} : {}),
  };
  const config = run.frozen?.config;
  const links = (run.links || []).filter(link => /^https?:\/\//i.test(link.url));
  return `<section class="card run-overview" aria-label="Run overview"><h2>Overview</h2><dl class="run-properties">${fields(overview)}</dl>${links.length ? `<div class="run-overview-links">${links.map(link => `<a class="button secondary" href="${esc(link.url)}" target="_blank" rel="noopener">${esc(link.name)} ↗</a>`).join('')}</div>` : ''}</section><section class="card run-overview" aria-label="Run config"><h2>Config</h2>${config && Object.keys(config).length ? `<div class="run-property-heading"><span>Key</span><span>Value</span></div><dl class="run-properties">${fields(config)}</dl>` : '<p class="muted">No configuration recorded.</p>'}</section>`;
}
