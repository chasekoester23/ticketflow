# Review speed and field intake (November 2026)

Goal: a project engineer with 300 AI-read tickets verifies a clean ticket in roughly
10-15 seconds and a few keystrokes, without approving unreliable values blindly.

**Every timing here comes from a script driving a browser against a mocked API with
fictional tickets. A script doesn't read a scan or decide anything, so these figures are
the software's share of the work, never a person's throughput.** No real-world reviewer
was measured.

## What already worked (kept)

Ctrl/Cmd+Enter accept-and-next, Enter walking the fields, Alt+D same-as-previous,
J/K/N/F navigation, zoom/pan/rotate, a `?` shortcut list, resume-where-you-left-off,
unsaved-work guards, a session pace readout, and prefetching of the next scan.

## Friction found

1. **The next scan was downloaded twice, and the person waited for the second copy.**
   The API sends `Cache-Control: private, no-store` on every `/api/` response
   (backend/app/main.py), so the `new Image()` prefetch could not be reused by the
   `<img>` that showed the ticket. Measured: 2 image requests per ticket, and the full
   image delay after every Accept.
2. **A value the reader warned about could be approved with one chord, unseen.** On a
   ticket flagged "Tonnage unusually large (> 60)", the cursor went to tons, but
   Ctrl+Enter approved it straight away.
3. **On a clean ticket the cursor went nowhere** (the page body), so plain Enter did
   nothing; only the chord approved.
4. No way to narrow the queue to one kind of problem, no flag/skip keys, no rotate-left
   or one-press fit, no time-left estimate, and batch corrections only on the Records page.
5. Phone intake: no direct camera button; an upload error appeared at the top of a long
   phone page, out of sight of the button, and didn't say whether anything was saved.

## Changes

- **Cursor placement** (ReviewForm): the first field needing a person (blank, or holding
  a value that arrived with a warning: a reader flag, tons above 60, an implausible date,
  a ticket number that may be the job's); otherwise the Accept button. One Enter then
  approves and opens the next ticket.
- **Warned values must be looked at before Accept.** Pressing Enter or Tab in the field,
  clicking into it, changing it, or "Matches the scan" (for touch) counts as looking; the
  cursor merely landing there does not. Accept or Ctrl+Enter with an unchecked value moves
  the cursor to it and highlights the request. The server's own checks are unchanged.
- **Enter in a field** now moves to the next field still needing a person, then Accept,
  instead of every field. Tab still visits every field. A held (repeating) Enter, or an
  Enter within 350 ms of a ticket opening, is ignored on Accept, so a double press can't
  approve the ticket that just appeared.
- **Scans fetched ahead as blobs** (page.tsx `useScanCache`): the next ticket, the next one
  Accept would open, and the next with the chosen issue, held as object URLs and released
  once out of range. If the fetch fails the viewer falls back to the plain address, so its
  "don't approve without seeing the original" message still works.
- **Shortcuts** (listed under `?`): I next open ticket with an issue, X flag with the typed
  note, S skip (asks first), Shift+R rotate left, and the zoom level is now a one-press
  "fit" control. Existing keys unchanged.
- **Queue issue filter**: has warnings, missing tons, possible duplicate, no rate, unit not
  tons, each with its open count. While a filter is chosen, Accept moves to the next open
  ticket with that issue; when none are left the filter clears.
- **Progress**: "n / N resolved" stays; after three accepts the session line adds
  "about X min left for N open at this pace (an estimate)", from this session's own
  open-to-accept times.
- **Correct several**: choose tickets in the queue (or "Choose all shown"), set supplier,
  material, job location or delivery date. Uses the existing `POST /api/tickets/bulk-edit`,
  which changes only needs-review and flagged tickets, audits each, and never approves;
  approved tickets can't be ticked. Each ticket still needs its own Accept.
- **Phone/tablet intake** (UploadPanel, minimal): below 1024 px a "Take a photo of a ticket"
  button opens the camera (`accept="image/jpeg,image/png" capture="environment"`); each
  photo adds to the list. Upload errors scroll into view and say what to do; a dropped
  connection says the upload may or may not have arrived and to check Upload history
  before sending again (the files stay chosen).

## Measured before and after

Script: `frontend/tests/browser/zz-measure.spec.mjs` was run on the old and new code
(Next dev server, same machine, same script), then removed. 12 AI-read tickets, every
sixth with tons of 64.000 and the reader's "Tonnage unusually large" flag. The mocked image
endpoint answers after 300 ms with the server's real `private, no-store` header. The
script waits 800 ms per ticket (standing in for a person looking), then takes the fewest
keys the screen offers.

| | Before | After |
|---|---|---|
| Image requests per ticket | 2 (1 for the first) | 1 |
| Median wait from the accepting key to the next scan and form ready (clean tickets) | 369 ms | 59 ms |
| Keys to approve a clean ticket | 1 (Ctrl+Enter chord; cursor on page body) | 1 (Enter; cursor on Accept) |
| Warned ticket approved by one chord without visiting the value | yes | no |
| Keys to approve a warned ticket, the intended way | 1 (blind) | 2 (Enter on the value, Enter on Accept) |

The same script on the new code under the production build (`next build && next start`,
as CI runs it): 1 image request per ticket, median wait 40 ms (29-63 ms) on clean tickets,
no warned ticket approved by one chord. The old code was not re-measured under the
production build, so compare the before and after columns only with each other.

What these numbers don't show: how long a person takes to read a scan. With the software
wait near zero, the 10-15 s target depends on reading, which no script here measures.
`tests/e2e/review-bench.sh` (real backend) was not run in this work; its readiness check was
updated to read the ticket from the image's `data-ticket` attribute, because a prefetched
scan is now shown from a `blob:` address.

## Tests

- `frontend/tests/browser/review-speed.spec.mjs`: cursor on Accept and 1 key per clean
  ticket; warned value refused unseen, then 2 keys; touch confirmation; correcting counts as
  looking; held Enter approves one ticket; Enter skips clean fields; shortcuts and `?` list;
  issue filter and Accept staying within it; time-left estimate; bulk correction never
  sends approved tickets and approves nothing; each scan requested once with `no-store`.
- `frontend/tests/browser/mobile-intake.spec.mjs` at 390x844 and 820x1180: camera input,
  two photos in one upload, project required, slow upload (1.2 s) shows "Uploading…" and
  can't be sent twice, the upload is in Upload history after leaving, a dropped upload is
  explained in view and resent, touch review with "Matches the scan", no sideways scroll.

## Not done

- Byte-level upload progress: uploads go through `fetch` in `lib/api.ts`, which reports
  no progress; it needs XMLHttpRequest there and a bar in UploadPanel (owned elsewhere).
  An upload in flight can't survive closing the page; once sent it's in Upload history.
- "Zoom to the field": the reader returns no field positions, so there's nothing cheap to
  zoom to. Zoom and scroll position already carry from one ticket to the next.
- Real network throttling (CDP) doesn't apply to mocked routes; slow and dropped
  connections were simulated with route delays and `route.abort`.
- Photos default to the four-per-page layout in UploadPanel; a phone user must pick
  "One ticket per page" (a note says so). Defaulting photos to single is worth doing there.
