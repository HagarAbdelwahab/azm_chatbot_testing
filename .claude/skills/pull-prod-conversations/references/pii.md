## This is real citizens' data

Pulled text is unredacted **by design**: the pull is the faithful record, and a replay
run later needs the original wording. Masking happens at **render time** — national IDs,
IBANs, phone numbers and emails are replaced in the HTML, and case numbers keep only
their last four digits so a reviewer can still tell threads apart.

`data/prod_pulls/` is force-added to `.gitignore` by the puller on every run. Do not move
pulls out of it, do not paste raw pulled text into chat, a ticket, or a commit message,
and quote from the **report**, not from `pull.json`.

Verify the mask held before handing a report over:

```bash
.venv/bin/python -c "
import re,pathlib
h=pathlib.Path('<run-dir>/report.html').read_text(encoding='utf-8')
print('ids:', len(re.findall(r'(?<!\d)[12]\d{9}(?!\d)', h)))
print('ibans:', len(re.findall(r'SA\d{22}', h)))
print('emails:', len(re.findall(r'[\w.+-]+@[\w-]+\.[\w.]+', h)))"
```

**Never anchor a masking pattern on `\b`.** Arabic letters and digits are both word
characters, so there is no word boundary in `الهوية1001520665` — a `\b`-anchored rule
misses every identifier typed flush against Arabic text, which is exactly how users write
them. The patterns live in `render_replay_html.py` and are imported, not redefined; if you
add one, test it against an identifier with no space before it and check the rendered HTML.
