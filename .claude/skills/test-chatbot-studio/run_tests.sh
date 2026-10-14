#!/usr/bin/env bash
# Drive the Agent Studio playground: one question per conversation.
#
#   ./run_tests.sh <agent_id> <questions.txt> [out.json]
#
# Each question gets a page reload first — the playground has no "new conversation"
# button, and a reload is the only way to clear the thread. Without it every question
# inherits the previous answer's context and the run is not isolated.
#
# Run this from the SAME directory the browser was opened in: playwright-cli keeps
# session state in ./.playwright-cli/, and from anywhere else every command fails with
# "browser is not open" — silently, producing a full set of empty rows.
set -uo pipefail

AGENT="${1:?agent id required}"
QFILE="${2:?questions file required}"
OUT="${3:-studio_results.json}"
SESSION="${STUDIO_SESSION:-studio}"
BASE="${STUDIO_URL:-https://chatbot-studio.azm-cit.com}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WAIT="${STUDIO_WAIT:-30}"          # seconds to wait for a reply; raise for slow models
HYDRATE="${STUDIO_HYDRATE:-6}"     # seconds for React to hydrate after a reload; 3 is too few

TMP="$(mktemp)"; trap 'rm -f "$TMP"' EXIT
META='{}'
first=1
n=0

while IFS= read -r q; do
  [ -z "$q" ] && continue
  n=$((n+1))
  printf '  %2d. %s\n' "$n" "${q:0:60}"

  playwright-cli -s="$SESSION" goto "$BASE/dashboard/agents/$AGENT?tab=test" >/dev/null 2>&1
  sleep "$HYDRATE"
  playwright-cli -s="$SESSION" fill "input[placeholder='Ask something to test…']" "$q" >/dev/null 2>&1

  # Filling before React hydrates leaves the DOM value set but component state empty,
  # so Send does nothing and the row comes back blank. Verify, then retry once.
  val=$(playwright-cli -s="$SESSION" --raw eval \
        "() => document.querySelector(\"input[placeholder='Ask something to test…']\")?.value || ''" 2>/dev/null)
  if [ "${#val}" -lt 3 ]; then
    echo "      input did not take — waiting and retrying"
    sleep "$HYDRATE"
    playwright-cli -s="$SESSION" fill "input[placeholder='Ask something to test…']" "$q" >/dev/null 2>&1
  fi

  playwright-cli -s="$SESSION" click "button:has-text('Send')" >/dev/null 2>&1
  sleep "$WAIT"

  # The "Running as" strip (agent / knowledge base / model) does NOT exist on a freshly
  # loaded playground — it renders only once a message has been sent. So capture it after
  # the first answer, never before, or every field comes back null.
  if [ "$META" = '{}' ]; then
    META=$(playwright-cli -s="$SESSION" --raw eval "() => {
      const t=(document.querySelector('main')||{}).textContent||'';
      const g=re=>{const x=t.match(re);return x?x[1].trim():null;};
      const tn=document.querySelector('aside button, [class*=sidebar] button');
      return JSON.stringify({
        agent: g(/Agent:\s*([^]*?)(?:Knowledge base:|Model:|Test conversation)/),
        knowledge_base: g(/Knowledge base:\s*([^]*?)(?:Model:|Test conversation)/),
        model: g(/Model:\s*([^]*?)(?:Test conversation|Running as)/),
        tenant: tn ? tn.textContent.trim() : null});
    }" 2>/dev/null)
  fi

  res=$(playwright-cli -s="$SESSION" --raw eval "$(cat "$HERE/extract.js")" 2>/dev/null)
  [ $first -eq 1 ] || echo "," >> "$TMP"
  first=0
  # --raw hands back a JSON-encoded string; unwrap one level so the file holds objects.
  python3 -c "
import json,sys
raw=sys.argv[1]
try: obj=json.loads(json.loads(raw))
except Exception: obj={'error':'extract failed','raw':raw[:300]}
obj['asked']=sys.argv[2]
sys.stdout.write(json.dumps(obj,ensure_ascii=False,indent=2))
" "$res" "$q" >> "$TMP"
done < "$QFILE"

python3 -c "
import json,sys
meta_raw, agent, tmp, out = sys.argv[1:5]
try: meta=json.loads(json.loads(meta_raw))
except Exception: meta={}
meta['agent_id']=agent
results=json.loads('['+open(tmp,encoding='utf-8').read()+']') if open(tmp,encoding='utf-8').read().strip() else []
json.dump({'meta':meta,'results':results}, open(out,'w',encoding='utf-8'),
          ensure_ascii=False, indent=2)
if not meta.get('agent'):
    print('  note: agent/model strip not captured — report will not name the model')
" "$META" "$AGENT" "$TMP" "$OUT"

echo "wrote $n result(s) -> $OUT"
python3 "$HERE/render_report.py" "$OUT"
