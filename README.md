# tool-contacts-cleanup

Cleans up Google Contacts and fills in what's missing, straight through the Google People API. It reads your live contacts, decides what to delete, merge and fix, shows you a dry run, and then changes only the fields that differ. Photos, stars, birthdays, addresses and contact ids stay unless a rule names them. There are no exports, re-imports or files to move around.

Sources, newest first: WhatsApp, Signal, Telegram and Gmail, then your own lists (class lists, vCards), then what's on the card. Proper names win, while nicknames, single words and emoji never replace a name.

Python 3.9+, standard library only. The routine for a full pass, and what the People API cannot do, is in [HOWTO.md](HOWTO.md).

## Setup (once)

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create a project, e.g. `contacts-sync`.
2. Under **APIs & Services → Library**, enable **People API**, plus **Gmail API** for `--gmail`.
3. Under **Google Auth Platform**:
   - **Branding:** enter an app name and your email.
   - **Audience:** choose External, then add your Gmail address as a test user.
4. Under **Clients → Create client → Desktop app**, create a client and save its JSON outside this repo, e.g. `~/.config/contacts-sync/client_secret.json`.
5. Copy `config.example.json` to `~/.config/contacts-sync/config.json` and edit it. See [Config](#config).
6. For `--signal`, run `brew install sqlcipher`. For `--whatsapp` and `--signal`, give Terminal Full Disk Access.

The first run opens your browser to log in, and the token is saved in `~/.config/contacts-sync/token.json`. On a machine without a browser, `people_sync.py --login-url` and then `--login-code <address>` log in by pasting the address the login ended on. While the app is in testing mode, Google expires the login after 7 days; set **Audience → Publishing status** to *In production* and the saved login keeps working without a browser.

## Run

```bash
python3 pipeline.py --client-secret ~/.config/contacts-sync/client_secret.json \
  --config ~/.config/contacts-sync/config.json --work ~/.cache/contacts-sync \
  --gmail --other-contacts --whatsapp --signal --gravatar [--telegram result.json] [--linkedin Connections.csv]
# check work/sync_report.txt, then the same with --apply
python3 pipeline.py ... --merge [--apply]      # merge the duplicate groups the triage found
```

| Flag | What it adds |
|---|---|
| `--gmail` | Treats contacts you've **sent** mail to recently as active. Reads only the To/Cc/Bcc headers of the SENT label (`gmail.metadata`). |
| `--other-contacts` | **Other contacts** (Gmail's auto-collected addresses): copies people you've written to recently, attaches an address to the one card it clearly belongs to ("Gizem K.", `kocgizem@`), and gives a name-only card the email of an Other contact with the same name. The API can't delete Other contacts; clear them at contacts.google.com/other. |
| `--whatsapp` | Reads WhatsApp for Mac's database: activity, profile names, groups and profile photos. Never reads message text. |
| `--signal` | Reads Signal Desktop's profile names (first and last, set by the person), numbers, last activity and profile photos. The SQLCipher key comes from the macOS keychain. |
| `--telegram result.json` | Telegram Desktop → Settings → Advanced → Export Telegram data → *Contacts list*, JSON. |
| `--linkedin Connections.csv` | Current company and title, profile link, and an email where shared. A new last name is taken only on an email match. |
| `--gravatar` | Public Gravatar photos for cards still without one. |
| `--maps-key KEY` | Checks and formats addresses with Google's Address Validation API (needs billing). |
| `--merge` | Merges the duplicate groups: keeps the fullest card and gives it everyone's emails, numbers, links, labels, the longest name and missing fields. |
| `--names` / `--reach` / `--group-labels FILE` | Hands back a suggestion sheet from `work/fix/` once you've filled it in. |

Other tools:
- `add_people.py list.csv --label X`: creates or completes people from a list (class list, guest list). Data from an old list is tagged with its year, and a recorded maiden-to-married name change is applied.
- `vcard.py contacts.vcf`: merges an iCloud, iPhone or any vCard export, filling gaps only.

Everything a run writes goes into `--work`:
- `sync_report.txt`: every DELETE, UPDATE, SKIPPED and FAILED.
- `triage/report.md`: why each card was flagged.
- `incomplete.csv`: cards still missing a first name, last name, or any way to reach them.
- `undo.json`: the full before-state of every touched card, written with `--apply`.

Google Contacts → Settings → **Undo changes** covers 30 days. A second run right after `--apply` should report `delete 0, update 0`.

## Iris sync

`iris_sync.py` keeps Iris's people and Google Contacts in step (Iris's spec: *Sync People with Google Contacts*). It runs on the Mac as the job `de.neutze.iris.contacts` (job-status name `iris-contacts`), hourly and whenever Iris saves an answer, with the login above. Planned 2026-10-17, not built yet.

```bash
python3 iris_sync.py --client-secret ~/.config/contacts-sync/client_secret.json --pictures ~/Pictures/_inbox [--apply]
```

- **Export:** every contact that is a person goes into `<pictures>/.iris/contacts.vcf` as vCard 4.0 with only its full name, given and family name, birthday and Google id (`UID`). A birthday without a year stays `--MM-DD`; a custom field `Birth year` fills in the year, or stands alone as `BDAY:YYYY`. Written to a temporary file, then moved over the old one.
- **Answers:** Iris writes one line per yes or no into `<pictures>/.iris/contacts-answers.jsonl`. For each yes not yet in `contacts-sent.jsonl` the contact is read live; only when it still lacks the value is the birthday (or `Birth year`) added, or a new contact with just a name and birthday created. Nothing is ever overwritten or deleted, and each result is a line in `contacts-sent.jsonl`. Without `--apply` it only says what it would send.
- **Never in this repo:** the vCard and the answers hold names and birthdays; they live in the pictures folder on the Mac only.

## What it decides

**Deleted**
- Blank cards and no-reply addresses (including `noreply.github.com`).
- Google+ leftovers without a proper name, and Google+ brand pages.
- Email-only cards you haven't written to in `active_years`.
- Cards whose name is just their email address, and handles like `W33d`, `Airbnb2`, emoji or one or two letters.
- With `delete_unreachable`: every card without a phone and an email, after the sources above have had a chance to give it one.

**Fixed**
- Dead links removed, and a country code added to numbers that start with `0`.
- Names typed entirely in lower or upper case recapitalised.
- Old student addresses removed, and former-employer addresses typed `old`.
- Old Facebook-sync notes and "old number" entries removed.
- Missing email and phone types set (home or work, mobile or other).
- Labels added by domain or company.
- The most recently used email or number moved first, and stale ones typed `old`.

**Names**
- A proper WhatsApp, Signal or Telegram profile name with the card's first name sets or updates the last name (marriage).
- LinkedIn does the same on an email match.
- A still-missing first or last name comes from the nickname, a profile name or the company.

**Duplicates**
- A shared landline doesn't make two people one, while nickname pairs (Dimi / Dimitris) are grouped.
- `--merge` merges the groups.

## Config

`~/.config/contacts-sync/config.json` (see `config.example.json`):

```json
{
  "country": "49", "region": "DE", "active_years": 3,
  "delete_unreachable": true, "own_domains": ["example.com"], "own_names": ["Your Name"],
  "drop_email_domains": ["mytum.de"], "old_email_domains": ["former-employer.com"],
  "labels": [["domain", "tum.de", "TUM"], ["org", "former employer", "Former Employer"]]
}
```

A `domain` rule matches that domain and its subdomains. An `org` rule is a case-insensitive match anywhere in the company name. `own_domains` and `own_names` are you: your aliases and your name are never copied as contacts. `keep_names` lists cards (by their full name) whose name is never changed, for a profile name you decided against.

## How it stays safe

- **Matching:** every change names its card by a fingerprint of name, emails, phones and links, matched against the live contacts at apply time. A card that changed in between is skipped. Identical twins are only touched when the live count equals the planned count.
- **Narrow writes:** only the named fields are sent, and labels are only ever added.
- **Per-contact results:** each contact's own status inside a batch is checked. Quota answers (429, Gmail's 403) are retried.
- **Matching rules:** a parents' landline never links siblings, and a list's old address never replaces one on the card.

## Files

| File | Role |
|---|---|
| `pipeline.py` | The one command: fetch → rescue → triage → plan → dry run / apply → photos. |
| `contacts_cleanup.py` | Triage rules and report. |
| `contacts_fix.py` | Fixes, labels, suggestions, `plan.json`. |
| `live_plan.py` | Live-data passes: types, recency, names, signatures, birthdays. |
| `people_sync.py` | People API client, login, matching, apply, photos, Gravatar. |
| `merge.py` | Merging duplicate groups. |
| `google_extras.py` | Other contacts, Address Validation. |
| `messengers.py` | Signal and Telegram readers. |
| `whatsapp_activity.py` | WhatsApp for Mac reader, hidden ids (`@lid`) included. |
| `add_people.py`, `vcard.py` | Lists and vCards. |

## Tests

```bash
python3 -m unittest
```

The tests run against a fake People API, so no account is needed.
