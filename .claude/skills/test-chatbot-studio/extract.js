// Pull the last answer plus the evidence Studio renders around it.
// Anchors: user bubbles are `.justify-end`; source chips and metric spans are
// leaf nodes. Avoid matching on full Tailwind class strings — they churn.
() => {
  const main = document.querySelector('main');
  if (!main) return JSON.stringify({error: 'no main'});
  const txt = e => (e.textContent || '').trim();
  const leaves = sel => [...main.querySelectorAll(sel)].filter(e => e.children.length === 0);

  const users = [...main.querySelectorAll('div')].filter(e => /justify-end/.test(e.className));
  const question = users.length ? txt(users[users.length - 1]) : '';

  const sources = [...new Set(
    leaves('div,span,li').map(txt).filter(t => /\.(md|txt|docx|pdf)$/i.test(t)))];

  const tools = [...new Set(
    leaves('div,span,li,code').map(txt).filter(t => /^[a-z][a-z0-9_]{3,}$/.test(t)
      && /search_|get_|resolve_|send_|create_|diagnose_/.test(t)))];

  const metric = re => {
    const hit = leaves('span,div').map(txt).filter(t => re.test(t)).pop();
    return hit ? hit.split(':').pop().trim() : null;
  };

  // Answer = longest leaf-ish block that is not the question, chrome, or evidence.
  const noise = /Test conversation|Running as|unsaved changes|Review and commit|Ask something|^Sources|^Quality|^Faithfulness/;
  let answer = '';
  main.querySelectorAll('div,p').forEach(e => {
    const t = txt(e);
    if (!t || t === question || noise.test(t)) return;
    if (e.querySelectorAll('button').length) return;
    if (t.includes(question) || /\.(md|txt|docx)$/i.test(t)) return;
    if (t.length > answer.length && t.length < 8000) answer = t;
  });

  // The evidence panels share a wrapper with the prose, so the longest-block
  // heuristic drags them in. Cut at the first panel label.
  answer = answer.split(/Sources \(\d+\)|TOOLS USED|Quality:/)[0].trim();

  return JSON.stringify({question, answer, sources, tools,
                         quality: metric(/^Quality:/), faithfulness: metric(/^Faithfulness:/)});
}
