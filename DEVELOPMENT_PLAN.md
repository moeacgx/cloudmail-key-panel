# CloudMail Registration Workbench Development Plan

## Goal

Reduce the manual registration support flow from repeated admin filtering, opening mailbox pages, copying addresses, editing categories, and waiting for page refreshes into a single backend workbench page.

The workbench should let an admin claim one mailbox from a selected category, copy the address, watch recent verification mail, copy detected codes, change the mailbox category to a selected completion category, and immediately move to the next mailbox without full page reloads.

## Scope

- Add first-party workflow support inside this CloudMail panel.
- Keep category as the business label, such as `未使用`, `已使用`, `GPT废号`, or `Pro20X`.
- Add a separate lifecycle status field only for temporary workbench occupancy.
- Use Tailwind CSS and daisyUI on existing Jinja templates instead of introducing a React/shadcn frontend.
- Do not automate third-party account creation or submit forms on third-party websites.

## Data Model

Add workbench lifecycle fields to `access_mappings`:

- `status`: `idle` or `in_progress`
- `claimed_at`: UTC timestamp when a mailbox is claimed for registration work
- `used_at`: UTC timestamp when a workbench completion action updates the category
- `last_seen_email_id`: reserved for future mail tracking
- `target_site`: optional context label for the site/workflow using the mailbox

Legacy terminal status values are released back to `idle` on startup. Category values are kept as business labels and are not migrated into status.

- `unused` -> `idle`
- `used` -> `idle`
- `skipped` -> `idle`
- `failed` -> `idle`
- `in_progress` stays `in_progress`

## Backend Tasks

- Extend `AccessMapping` and SQLite migration logic.
- Add store methods for:
  - claiming the next available mapping in a selected category
  - reading the current in-progress mapping
  - completing a workbench item by updating category and releasing status
- Add admin page route:
  - `GET /admin/workbench`
- Add JSON API routes:
  - `GET /api/workbench/current`
  - `POST /api/workbench/claim-next`
  - `GET /api/workbench/current/mailbox`
  - `POST /api/workbench/current/mark-used`
  - `POST /api/workbench/current/skip`

## Frontend Tasks

- Add `admin_workbench.html`.
- Add a visible entry point from the existing admin dashboard.
- Implement no-refresh controls:
  - claim next
  - copy email
  - copy code
  - refresh mailbox
  - save completion category and move next
  - cancel current claim
- Poll mailbox data while a mapping is in progress.

## Tests

- Store tests for status migration and claim/update workflow.
- App/API tests for admin-only workbench actions.
- Existing test suite must continue to pass.

## Acceptance Criteria

- Admin can claim one available mailbox from a selected category in the workbench.
- Claimed mailbox changes to `in_progress`.
- Mailbox preview and extracted codes are available through the workbench API.
- Admin can change the current mailbox category to `已使用`, `GPT废号`, `Pro20X`, or a custom category and automatically receive the next available mailbox.
- Admin can cancel the current claim without changing category.
- Terminal business labels remain categories, not status values, so the same mailbox can be reused by selecting that category later.
- Existing admin dashboard, mailbox pages, and CloudMail lookup flow still work.

## Progress

- [x] Plan written
- [x] Data model and store methods
- [x] Workbench routes and APIs
- [x] Workbench template and styles
- [x] Tests and verification
- [x] Status reset from dashboard and workbench
- [x] Category-based completion with idle/in-progress status semantics
- [x] Tailwind CSS + daisyUI template refresh
- [x] Removed local custom CSS and stale style classes
- [x] Browser-session isolated workbench claims with global mailbox locking
- [x] Workbench clears stale current mailbox when a claim is released elsewhere
