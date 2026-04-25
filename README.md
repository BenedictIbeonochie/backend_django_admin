# Aqua AI — Admin Control Plane

A separate Django project that replaces the ad-hoc `api.aquaai.uk/admin/` workflow with an **AI-driven approval control plane** for Breeder and Consultant signups on the main Aqua AI backend.

This project **does not replace** the existing Django admin on the main API — the developer can keep using that. It adds a second, more controlled UI where:

1. New breeder / consultant profiles are auto-reviewed by GPT‑4 and approved or rejected on verifiable grounds.
2. When the AI flags an issue, **Steven@humara.io** and **Ben@humara.io** are notified via **email + Slack** with recommended remediations, which the AI also applies.
3. A daily analytics report summarises approvals / rejections with full drill-down into the AI's reasoning for every decision.
4. Only **Steven@humara.io** and **Ben@humara.io** are super admins. They are the only users who can invite or remove other admin accounts.

The legacy `/admin/` continues to work unchanged.

## How it talks to the main backend

Both projects share the same Postgres database (pointed at by `DATABASE_URL`). The control plane exposes its own auth / UI tables (prefixed `admin_portal_*`) and reaches into the main app's tables via **unmanaged mirror models** — so migrations from this project will never touch `user_auth_*`, `breeders_*` or `consultant_*`.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # then fill in the values
python manage.py migrate
python manage.py bootstrap_superadmins
python manage.py runserver 0.0.0.0:8001
```

Then browse to `http://localhost:8001/admin-portal/` and log in with the password you set for `steven@humara.io` / `ben@humara.io` during bootstrap.

## Scheduled jobs

Run these on a cron / celery-beat / systemd timer:

| Job | Frequency | Command |
|---|---|---|
| Scan main DB for new profiles and run AI review | every 5 min | `python manage.py process_pending_reviews` |
| Build the end-of-day analytics summary | daily 23:55 UTC | `python manage.py generate_daily_report` |

## Environment variables

See `.env.example` for the full list. The ones you must set:

- `DATABASE_URL` — same Postgres used by the main API.
- `OPENAI_API_KEY` — your GPT‑4 key. Leave the placeholder in `.env.example` alone until you have the real key.
- `SLACK_BOT_TOKEN` and `SLACK_CHANNEL` — for AI issue alerts.
- `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` — for email alerts.
- `SUPERADMIN_EMAILS` — comma-separated list; defaults to `steven@humara.io,ben@humara.io`.

## Migration / go-live plan from current admin

1. Keep the legacy admin at `http://api.aquaai.uk/admin/` available during rollout.
2. Deploy this project as a separate service (example URL: `https://admin-control.aquaai.uk/admin-portal/`).
3. Point `DATABASE_URL` to the same production Postgres used by the main backend.
4. Run:
   - `python manage.py migrate`
   - `python manage.py bootstrap_superadmins --password '<temporary-strong-password>'`
5. Set production secrets:
   - `OPENAI_API_KEY=<your key>`
   - `SLACK_BOT_TOKEN=<bot token>` and `SLACK_CHANNEL=<channel>`
   - SMTP settings (`EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, etc.)
6. Add schedules:
   - `python manage.py process_pending_reviews` every 5 minutes
   - `python manage.py generate_daily_report` daily at `23:55 UTC`
7. Validate with one breeder + one consultant test signup before enforcing full automation.
8. After validation, instruct ops/developers to use only the new control-plane UI for approvals.

## Troubleshooting

- `ModuleNotFoundError: No module named 'dj_database_url'`
  - Run `pip install -r requirements.txt` in the same Python environment used for `manage.py`.
  - If using Windows PowerShell, activate your venv first (for example: `\.venv\Scripts\Activate.ps1`).
- `Your models in app(s): 'admin_portal' have changes that are not yet reflected in a migration`
  - Pull the latest repo changes and run `python manage.py migrate` again.
  - This repo now includes `admin_portal` migration `0002` to align model state.
