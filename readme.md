<p align="center">
  <a href="https://lovable.dev">
    <img src="https://lovable.dev/favicon.ico" width="36" alt="Lovable">
  </a>
  &nbsp;&nbsp;
  <a href="https://www.snapchat.com/topic/hilarious-cat-workout-videos">
    <img src="https://cf-st.sc-cdn.net/o/LFs6XtEBKvLYzv7IDUdL0.256.IRZXSOY?mo=GkYaCTIBD0gCUC5gAVCgAVoQRGZMYXJnZVRodW1ibmFpbKIBEAiAAiILEgAqB0lSWlhTT1miARAImgoiCxIAKgdJUlpYU09Z&amp;uc=46" width="160" alt="Hilarious cat workout videos">
  </a>
</p>

<h1 align="center">Lovable Pull</h1>

<p align="center">
  Paste a <a href="https://lovable.dev">Lovable</a> project link.<br>
  Get the full source tree on disk.
</p>

```bash
pip install lovable-pull ; lovable-pull https://lovable.dev/projects/YOUR-PROJECT-ID
```

Stay signed in to Lovable in Chrome. Files save in the folder where you run it.

It skipped Chrome because your existing login was enough.

1. Read the Lovable session token already stored in Chrome.
2. Ask Lovable’s API for the full file list (81 files).
3. Download those files in parallel (16 at a time).
4. Write each one to the matching path, e.g. `.lovable/project.json`.
5. Open the browser only if some files were still missing. All 81 succeeded, so Chrome never opened.

## Read the Lovable session token already stored in Chrome

1. Find Chrome’s user-data folder on this PC (Default / last-used profile).
2. Scan IndexedDB, Local Storage, and Session Storage files there.
3. Pull out JWT strings (`eyJ...`) from those files.
4. Keep only tokens that look like a Lovable / Firebase login.
5. Use the newest one that has not expired.

## Ask Lovable’s API for the full file list

1. Take the project id from the URL (`c71ba7f9-...`).
2. Call `https://api.lovable.dev/projects/{id}/git/files?ref=main`.
3. Send `Authorization: Bearer <token>` so Lovable treats it as your login.
4. Parse the JSON into paths like `.gitignore` and `.lovable/project.json`.
5. Deduplicate and keep going with pagination until the list is complete (81 files).

## Download those files in parallel (16 at a time)

1. Cap workers at 16 (`min(16, file count)`).
2. For each path, request `.../git/file?path=...&ref=main` with the same token.
3. Run many of those requests at once on a thread pool, not one-by-one.
4. Write each response into the matching folder, e.g. `.lovable/project.json`.
5. Count successes as they finish; all 81 succeeded, so Chrome was never needed.

[pypi.org/project/lovable-pull](https://pypi.org/project/lovable-pull/1.0.0/)
