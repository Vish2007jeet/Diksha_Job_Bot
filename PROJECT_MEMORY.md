# Project Memory

## User Operating Preferences

- The live target domain is Business Analytics / BI / Controlling / Data / Project Management. Never use the legacy Inventory domain as a source of role content.
- CV and cover letter content must be genuinely re-invented for each JD inside the target domain, not merely reworded from an inventory. Make the case persuasive, job-specific, and naturally include credible, relevant metrics.
- ATS terms should be selected by the model and woven naturally into Experience and Projects. Do not penalize the candidate for not listing every possible tool; only state tools that are credible for the role and profile.
- Cover letters must be newly tailored per JD. Every paragraph must end in a complete sentence and the opening must begin with a concrete work moment rather than a generic company introduction.
- Job discovery must exclude listings older than three days whenever a trustworthy posting date is available.

## Models and Cost Policy

- Standard CV/CL generation uses Sonnet. Dream Apply uses Opus only for CV/CL generation; ATS evaluation stays on Sonnet and humanization on Haiku.
- API cost tracking is enabled against the configured monthly budget. Keep prompt caching intact when editing live keyword settings.

## Gmail Workflow

- `/checkgmail` is the explicit Inbox re-scan: it clears the Gmail seen-message cache, reviews the last 30 days, and sends confirmation cards before changing a job status.
- `/gmailscan` reviews only new messages from both Inbox and the Gmail label named `Rejections`, for the last 30 days. It does not clear the seen-message cache.
- Gmail cards show the application number (for example, `#195`), job, sender, subject, source label, and proposed status. Status changes occur only after the user confirms the card.
- The Gmail scanner discovers the `Rejections` label at runtime; never hard-code its Gmail label ID. Messages without a reliable tracked-application match must not create a status-change card.

## Operational Notes

- LinkedIn requires a current `li_at` session cookie. HTTP 302 means its session has expired; HTTP 429 means LinkedIn rate limiting.
- Telegram polling `httpx.ReadError` is a transient network/Telegram connection failure; the polling retry loop should recover.
- Google Drive upload SSL errors are graceful failures: the local generated file remains available.
