# tool-contacts-cleanup

Cleans up Google Contacts and fills in what's missing, straight through the Google People API. It reads your live contacts, decides what to delete and fix, shows you a dry run, and then changes only the fields that differ. Photos, stars, birthdays, addresses and contact ids stay as they are. There are no exports, re-imports or files to move around.

Python 3.9+, standard library only.

## Setup (once)

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create a project, e.g. `contacts-sync`.
2. Under **APIs & Services → Library**, enable **People API**. Also enable **Gmail API** if you want `--gmail`.
3. Under **Google Auth Platform**:
   - **Branding:** enter an app name and your email.
   - **Audience:** choose External, then add your Gmail address as a test user.
4. Under **Clients → Create client → Desktop app**, create a client and download the JSON. Keep it outside this repo, e.g. `~/.config/contacts-sync/client_secret.json`.
5. Run `cp config.example.json config.json` and edit it. See [Config](#config).

The first run opens your browser to log in. The token is saved in `~/.config/contacts-sync/token.json`, readable only by you. While the app is in testing mode, Google expires the login after 7 days, and the next run simply asks again.

## Run

```bash
python3 pipeline.py --client-secret ~/.config/contacts-sync/client_secret.json --config config.json            # dry run
python3 pipeline.py --client-secret ~/.config/contacts-sync/client_secret.json --config config.json --apply    # write
```

Optional extras:

| Flag | What it adds |
|---|---|
| `--gmail` | Treats contacts you've **sent** mail to recently as active. Reads only To/Cc/Bcc headers (`gmail.metadata` scope). |
| `--whatsapp` | Reads WhatsApp for Mac's local database: activity, profile names (for last-name and phone suggestions), groups, and profile photos for contacts that have none. Never reads message text. Terminal needs Full Disk Access. |
| `--linkedin Connections.csv` | Fills in company, title, email and profile link, matched on full name. Request the file at LinkedIn → Settings → Data privacy → Get a copy of your data → Connections. |
| `--names` / `--reach` / `--group-labels FILE` | Hands back a suggestion sheet from `work/fix/` after you've filled it in (see below). |

Everything a run writes goes into `--work` (default `./work`, gitignored):

- `work/sync_report.txt`: every DELETE, UPDATE and SKIPPED, plus a FAILED line for anything Google rejected.
- `work/triage/report.md`: why each contact was flagged.
- `work/undo.json`: written only with `--apply`. It holds the full before-state of every contact the run touches.

Google Contacts → Settings → **Undo changes** rolls back anything from the last 30 days.

A second run right after `--apply` should report `delete 0, update 0`. If it doesn't, something changed in between, for example a Merge & fix in Google Contacts.

## What it decides

**Delete candidates**
- Blank cards.
- No-reply addresses, including sending domains like `noreply.github.com`.
- Google+ leftovers: a name plus a dead `google.com/profiles` link and nothing else, when the name isn't a proper first and last name or is a brand page (Google Deutschland, SPIEGEL ONLINE, …).
- With activity data: email-only cards you haven't written to in `active_years`. Gmail used to save everyone you emailed as a contact.
- Cards whose "name" is just their email address.

**Kept**
- Anything with a proper first and last name, anything you've written to recently, and anything with a phone number.

**Fixes**
- Dead Google+ links removed.
- Country code added to numbers that start with `0`, and the same number twice on one card kept once.
- Names typed entirely in lower or upper case recapitalised. Mixed names like `ADAC Pannenhilfe` are left alone.
- Old student addresses removed (`drop_email_domains`).
- Former-employer addresses relabelled `old` (`old_email_domains`).
- Old Facebook-sync notes cleared, and numbers labelled "old number" removed.
- Emails labelled home or work, and phones labelled mobile or other, where they had no label.
- Labels added by email domain or company (`labels`).

**Duplicates**
- Listed in `work/triage/report.md` for Google's Merge & fix. Neither the API nor this tool merges.
- A shared landline doesn't make two people one.
- Nickname pairs (Dimi / Dimitris) are listed.

**Suggestions you confirm**, in `work/fix/`:
- `first_name_only.csv`: contacts that are only a first name, with suggested last names from WhatsApp profile names. Fill in `last_name`, `company` or `delete`.
- `reach_suggestions.csv`: name-only contacts with a phone number found through an exact WhatsApp profile-name match. Set `apply` to `y`.
- `groups.csv`: your WhatsApp groups. Fill in `label` for the ones worth a label.

Pass a filled-in file back with `--names`, `--reach` or `--group-labels`.

## Config

`config.json` (see `config.example.json`):

```json
{
  "country": "49",
  "active_years": 3,
  "drop_email_domains": ["mytum.de"],
  "old_email_domains": ["former-employer.com"],
  "labels": [["domain", "tum.de", "TUM"], ["org", "former employer", "Former Employer"]]
}
```

A `domain` rule matches that domain and its subdomains. An `org` rule is a case-insensitive match anywhere in the company name.

## How it stays safe

- **Matching:** each planned change names its contact by a fingerprint of name, emails, phones and links. That fingerprint is matched against the live contacts at apply time. A contact that changed in between doesn't match, so it's skipped. Identical twins are only touched when the live count equals the planned count.
- **Narrow writes:** only the fields the plan names are sent, and labels are only ever added.
- **Per-contact results:** Google's batch update answers 200 even when single contacts fail, so each contact's own status is checked and reported.

## Files

| File | Role |
|---|---|
| `pipeline.py` | The one command: fetch → triage → plan → dry run / apply. |
| `contacts_cleanup.py` | Triage rules and report (also works on a Google CSV export). |
| `contacts_fix.py` | Fixes, labels, suggestions, and `plan.json`. |
| `live_plan.py` | Streamlining and enrichment from live data (types, signatures, birthdays, WhatsApp). |
| `people_sync.py` | People API client, login, matching, apply, photos. |
| `whatsapp_activity.py` | Reads WhatsApp for Mac's database (activity, profile names, groups, photos). |

## Tests

```bash
python3 -m unittest
```

The tests run against a fake People API, so no Google account is needed.
