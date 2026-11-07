# Codex Drive Sync

Choose local Codex projects or individual chats, then pick up the same chats on another Windows PC or Mac. The app copies changed files to a Google Drive for desktop folder. Google Drive handles the upload and download, so there is no separate `rsync` install or Google login in this app.

## Set up the first machine

1. Install current Codex Desktop, Google Drive for desktop, and Python 3.14 or newer with Tk. The standard Python installers for Windows and macOS include Tk. Use the same Codex account on both machines.
2. Clone this repository or use **Code → Download ZIP** on GitHub and extract it. On Windows, double-click **Launch Windows.cmd**. On Mac, double-click **Launch Mac.command**, or open Terminal in the app directory and run `python3.14 app.py`.
3. Create a dedicated folder under **My Drive**, for example `Codex Drive Sync`. In the app, click **Choose folder** and select that folder. Making it available offline helps when Drive is streaming files.
4. Tick the box in the **Sync** column. You can also highlight a row and tick **Sync this project** or **Sync this chat** below the list, or press Space. Projects include existing and future chats. Select **Include project files** to also copy the workspace.
5. Click **Apply selections**, which becomes available once you've marked something and chosen a Drive folder. The app saves the selection and begins transferring the marked items. Leave the sync app open to check every five minutes; **Sync now** checks immediately after setup.

Chats sync first. The first workspace transfer can take several minutes for large projects. The status line shows scanning and transfer progress. Use **Stop sync** to stop the current run and **Sync now** to retry.

## Add another machine

1. Install the same prerequisites, extract and launch the app there, and choose where new project folders should be restored. The default is `~/Codex Sync Projects`. Leave existing Codex project folders in place.
2. Wait for Drive to show the shared folder and its `codex-drive-sync.json` file. Point the app at that same folder. It reads the selections and starts syncing without another Apply.
3. **Quit Codex Desktop and any Codex CLI sessions** to let the sync app import incoming chats. You can click **Sync now** or wait for the next check. Once the import finishes, reopen Codex. Uploads and workspace file transfers can run while Codex is open.

To hand a chat over to another computer, finish the current turn and check that the first machine has synced. Wait for Drive to finish, then run a check on the other machine with Codex closed before opening the chat there. If both copies gained different messages, the app keeps both copies and reports the conflict. Workspace files that were changed independently can merge if the changes are to different files.

## What it copies

- Local Codex workspace projects, individual chats, and archived chats. Restored chats keep their IDs and conversation records. Workspace paths are translated for the destination; paths quoted in old messages and tool output remain historical.
- Workspace working files, when included. Install Git to apply `.gitignore` rules in both repositories and standalone folders. Tracked files in a repository follow Git's normal behavior. Each project is restored as a working folder; the app does not copy `.git`, branches, Git history, file permissions, symlinks, common cache folders, or common secret filenames. Keep Git history in your repository host. Workspace files and chat messages can still contain secrets under other names, so choose a private Drive folder and review what you select.
- ChatGPT cloud projects, remote Codex hosts, attachments stored outside the rollout, and Codex credentials or settings are outside the transfer.

Unchecking an item stops future sync. It does not delete the local copy or previous versions stored in Drive. File deletions also do not propagate. New ignore rules filter future transfers on the machine applying them. To start a smaller Drive copy, use a new sync folder. The dedicated Drive folder stores checksummed files and a history of selection and content changes; it will grow over time. Keep its contents together and avoid editing the sync files by hand.

Chat imports use the installed Codex app server to index rollout files and assign chats to projects. This depends on Codex's local history format and project API. If an installed version rejects an import, keep the shared folder intact and check **Sync details**. `CODEX_HOME` and `CODEX_SYNC_CODEX` can point to a custom Codex data directory and executable.

## Verification

From the extracted directory:

```text
python3.14 -m unittest discover -s tests -v
```

Set `CODEX_SYNC_TEST_NATIVE=1` to additionally test restore and resume with the installed Codex executable in temporary Codex homes. The tests do not use your Drive or Codex history. The app was tested against the installed Windows Codex version; macOS path handling is covered by the portable tests, but a Mac end-to-end run is still needed.
