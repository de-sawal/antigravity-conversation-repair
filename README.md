# antigravity conversation repair

antigravity sometimes saves a chat but then it disappears from the sidebar. this little script checks your local conversation files and repairs the sidebar indexes when it safely can.

needs python 3.10 or newer. no extra packages to install. it supports the usual antigravity locations on windows, macos, and linux, though not every install/version has been tested, tested on ubuntu 26.04 lts with default installation directories.

## use it

quit antigravity first. open a terminal in this folder and preview the changes:

```bash
python antigravity_conversation_repair.py
```

if the plan looks right, apply it:

```bash
python antigravity_conversation_repair.py --apply
```

on mac/linux, use `python3` if `python` isn't found. it makes a database backup before writing anything. keep the backup until you've reopened antigravity and checked your chats.

if it can't tell which project a chat belongs to, you can give it the project folder:

```bash
python antigravity_conversation_repair.py --workspace "/path/to/your/project" --apply
```

if your database is somewhere unusual, pass its path with `--db "/path/to/state.vscdb"`.

if something goes wrong, open an issue. please don't upload your database or transcripts,a as they can contain private conversations and paths.
