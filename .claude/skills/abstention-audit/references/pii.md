## Real citizens' text

`audit.json` holds unredacted transcripts by design — you cannot audit what the bot
was asked without the question. `report.html` masks national IDs, IBANs, phones and
emails at render time, and shortens `ED-x-…` case numbers to their last four digits.

Everything lands under `reports/`, which this repo gitignores wholesale. Do not move
either file out of it, do not paste raw `audit.json` text into chat or a ticket, and
quote from the **report** when writing findings.

**Bare numeric request numbers are NOT masked.** The mask patterns cover 10-digit
national IDs (`[12]\d{9}`), IBANs, phones and emails; a 9-digit request number like
`411082220` passes through, same as in the replay report. If a window is going to be
shared outside the QA group, skim it first. Verify masking on the rendered HTML rather
than trusting the regex:

```bash
.venv/bin/python -c "
import re,pathlib
h=pathlib.Path('<run-dir>/report.html').read_text(encoding='utf-8')
print('unmasked ids:', len(re.findall(r'(?<!\d)[12]\d{9}(?!\d)', h)))"
```

Never anchor a new masking pattern on `\b` — Arabic letters and digits are both word
characters, so there is no boundary in `الهوية1001520665`. Use digit lookarounds.
