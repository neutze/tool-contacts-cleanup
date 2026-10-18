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
