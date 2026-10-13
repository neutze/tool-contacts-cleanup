# How to run a cleanup

The way a full pass over the contacts is done, in order. [README.md](README.md) explains the flags and the rules; this is the routine.

## Where things are

| What | Where | In git? |
|---|---|---|
| The tool | this folder | yes |
| Everything a run writes, the undo files, old exports | `work/` in this folder | no, ignored |
| Pre-cleanup export of the contacts | `work/backup/` | no, ignored |
| OAuth client, login token, settings | `~/.config/contacts-sync/` (`client_secret.json`, `token.json`, `config.json`) | no, outside the repo |

This repository is public. Nothing with a name, a number or an address in it is ever committed: `work/` holds other people's data. Never print or copy `client_secret.json` or `token.json`.

## The login

The login is stored in `token.json` with a refresh token, so a run needs no browser. That holds as long as the Google Cloud project's **Google Auth Platform → Audience → Publishing status** is *In production*; in *Testing* the login ends after 7 days. A browser sign-in is needed again only after access was revoked, after about six months without a run, or when a run asks for a permission the login doesn't have yet (`--gmail`, `--other-contacts`).

## One pass

All commands from this folder:

```bash
python3 pipeline.py --client-secret ~/.config/contacts-sync/client_secret.json --config ~/.config/contacts-sync/config.json --work work --whatsapp --signal --gravatar
```

1. **Dry run** with the command above. Nothing is changed.
2. **Read `work/sync_report.txt`.** Every `DELETE` must be one you agree with. Read `work/streamline_review.csv` for the name changes: a proper full name is taken, a nickname or a joke name is not. A name to keep goes into `keep_names` in `config.json`.
3. **Apply** with the same command plus `--apply`. `work/undo.json` gets the state before.
4. **Verify** with the dry run again. It must say `delete 0, update 0`.
5. **Tests** with `python3 -m unittest` before committing a change to the tool.

## Photos

A card that has a photo keeps it. A card without one gets, in this order, the WhatsApp profile picture, the Signal one, then a public Gravatar. The picture of a person's Google account shows on the card by itself and is not a photo of the card.

## Reviewing by hand

Some groups no rule can decide. They are read from the live contacts and WhatsApp for Mac's files, and each card is decided on its own:

- cards whose numbers are all unknown to WhatsApp: no entry in `ContactsV2.sqlite`, no chat, no profile name, no cached picture
- cards on WhatsApp that were never written to
- cards with several mobile numbers of which one is not on WhatsApp, which is usually an old number
- numbers that were written to and have no card

Rules for it:

- Check every number on a card against WhatsApp, not only the first.
- Delete nothing without an answer for that card. Save the full card first (`work/review_undo.json`).
- A landline is never on WhatsApp; that says nothing about the person.
- "Not on WhatsApp" is what this Mac's copy shows. It is a linked device and can lag the phone.
- No new contact labels.

## What the People API cannot do

- Move a saved contact to *Other contacts*: taking it out of *My Contacts* is refused (`canNotRemoveLastContactGroupResourceNames`).
- Delete or merge *Other contacts*. Their address can be put on the saved card instead.
- Restore from the Bin. That is contacts.google.com → Bin, for 30 days.
- Show Google's *Merge and fix* suggestions. That is contacts.google.com/suggestions.

## WhatsApp for Mac's files

In `~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/`. The terminal needs Full Disk Access.

- `ChatStorage.sqlite`: chats, groups, profile names, the list of cached profile pictures. Message text is never read.
- `ContactsV2.sqlite`: the phone's address book as WhatsApp sees it, with each person's number and hidden id (`@lid`). Most pictures, group members and some chats are filed under the hidden id, so this file is what turns them back into numbers.
- `Media/Profile/`: the cached pictures.

Message types 6 and 10 are system notices, not messages; leave them out when counting who was written to.
