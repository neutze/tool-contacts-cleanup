# Process notes: birthday wishes from the Android WhatsApp backup (2026-10-17 to 2026-10-18)

Raw notes, no polish.

- Ask: find birthday wishes in the phone's full WhatsApp history, not only the 2025-2026 slice WhatsApp for Mac has.
- First brief assumed an iPhone (Finder backup, Manifest.db). Phone is Android, so that whole route was dropped before any code.
- Android options weighed: local msgstore.db.crypt15 with a 64-digit end-to-end key (decryptable with wa-crypt-tools), per-chat exports (one by one, too slow), Google Drive backup (not readable). Picked the 64-digit key.
- Spec first: Iris note rule 9 and this README, then code (`--android-db`, query over message/chat/jid, jid_map for hidden ids, LIKE only, text never selected). Only number + date kept in work/android-wishes.json so the daily job keeps the wishes after the decrypted file is gone; considered keeping the decrypted db at a fixed path for the job, rejected (full chat history lying around).
- Snags:
  - He first chose a passkey. Passkey backups keep the key on WhatsApp's servers: not decryptable. Turning E2E off doesn't help either (crypt14, key in the app's private folder, root only). Had to switch to the 64-digit key and back up again.
  - Switching encryption removed the older local backup files; Drive keeps only the latest anyway. Chats themselves were never at risk.
  - The first copy in Downloads was the passkey backup; spotted by timing (copied before the switch) and kept under its name; the new one pulled under another name.
  - The auto-mode safety check refused `adb pull` from Claude; he ran it himself.
  - The key ended up pasted into the chat once (inside a command). Asked him to delete the message and generate a new key afterwards.
  - `python3 -I` broke the tool's own imports (script dir dropped from sys.path).
- Result 2026-10-18: 265 wishes (249 distinct number+day) in 66 chats on the phone; all 23 of the Mac's were among them. 229 matched a contact, 49 contacts. Cards 7 -> 15 (14 without a birthday, 1 beside Google's). 6 fell on Google's own day. 28 contacts gave wishes on several days and no card; 19 of those have one day (+-1) recurring across years ("alles Gute" is broad and catches other occasions).
- Decrypted db and both crypt15 copies set aside in ~/whatsapp-set-aside for him to trash.
