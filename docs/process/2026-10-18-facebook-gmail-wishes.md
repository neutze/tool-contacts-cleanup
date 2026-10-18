# Process notes: birthday wishes from Facebook and Gmail (2026-10-18)

Raw notes, no polish.

- Ask (Johannes, 2026-10-18): "Did we update all of the birthdates from WhatsApp? Can we also somehow extract it from Facebook or other sources?"
- WhatsApp state then: 14 check-the-day cards in Iris, none answered, so nothing written to Google yet.
- Facebook: he requests "Download your information" (JSON, Messages + Posts) himself; E2EE Messenger chats need the separate "Download secure storage data". `--facebook` reads the zips in place (no unpacked chats lying around), 1:1 chats you sent in, posts on someone's timeline (English and German titles). Facebook's Latin-1-mangled UTF-8 undone. "You" = the name in most chats.
- Gmail, first try: the saved login has gmail.metadata (headers, no search), so walked SENT one message per call (6,582) matching subjects. First run died at the 1 h token expiry (no refresh callback); rerun with refresh. He asked "Why does it take so long?" and "Don't search yourself" / "Give me Gemini prompt": scan stopped, never finished. Lesson: a one-call-per-mail walk is minutes, say so up front, and ask before running anything on his mail.
- Card offered full search (gmail.readonly login) as recommended; he picked Gemini prompts instead. Then widened: all years, and old Facebook/XING reminder mails (received, by name), not only sent wishes.
- Built: `--gmail-list` reads Gemini's lines, address -> work/gmail-wishes.json, full name -> work/gmail-names.json; matching by unique email / unique full name beside WhatsApp's numbers; contacts-wishes.json carries `sources`, Iris's card says "From a Facebook and Gmail wish".
- A new Gmail login asks for the saved scopes plus the new one, so it never drops contacts.other.readonly or gmail.metadata.
- Merged: iris#96 (spec rule 10 + card text; main had moved, briefs conflicted on the version line), tool-contacts-cleanup#6.
- Open: his Facebook export and ~/Downloads/gmail-wishes.txt; then one read each and the next job run writes the cards.
- Gemini lines read: 35 lines, 30 names; 3 cards, 6 already in Google, 21 not contacts. Two later Gemini batches (common German names on consecutive days, 2011-06 and 2012-05) looked made up and were dropped (his choice). He noted years may be off; harmless, cards write day and month only.
- `name,MM-DD` lines accepted (tool#7); a test leaked the real work/ files and was fixed there.
- Facebook birthdays page (facebook.com/events/birthdays): ~500 friends, dates only in hover tooltips (DOM tooltip on mouseover), ~137 in embedded JSON. Bulk read refused by the auto-mode safety check (PII), then even opening the page refused. His "allow"/"added all permissions" in chat didn't change the session's mode. Unresolved.
- Ways of working: he dropped cards/pickers mid-thread ("sometimes disappears in the history, not showing on phone"); questions now plain text in replies (WAYS-OF-WORKING changed in hermes-mac#20).
- Wrapped 2026-10-18 at his word. Open: 17 cards in Iris (/review/contacts) unanswered; Facebook export not requested; birthdays-page read needs the session out of Auto mode.
- Later the same day: switched the session out of Auto (to Manual) at his request; the saved birthdays page (Cmd+S) gave 106 dates (13 checked against visible labels, all agree; the rest paired by nearest name in embedded JSON).
- Review one by one in the chat picker (he insisted on picker questions, not text): placeholder = no year. 28 cards: 26 yes, 2 skipped because 01.04. is his own birthday (thank-you messages matched as wishes; guard missing: exclude the owner's birthday from wish sources). Then years asked for 25 day-only birthdays: 6 known.
- Outcome: birthdays in Google 48 -> 75 of 294 contacts, with year 42 -> 56. Written: WhatsApp 13, Facebook 11, Gmail 2. Mined: Gmail 35 Gemini lines / 30 names (3 contacts); Facebook 106 of ~500 friends (18 contacts, 1 differing from Google kept as Google).
