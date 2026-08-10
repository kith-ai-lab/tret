# bench for non-technical humans: the easy start

You don't need to know anything about programming to run bench on your own
computer. There are three steps: install Docker Desktop (once), get the bench
files, and double-click a file. This page walks through all of it.

## Step 1 — Install Docker Desktop (once)

Docker Desktop is a free app that runs bench in a tidy box on your computer,
with everything bench needs already inside the box.

1. Go to <https://www.docker.com/products/docker-desktop/> and download it
   for your computer (Mac or Windows — it picks the right one for you).
2. Install it like any other app.
3. **Open Docker Desktop once** and let it finish its own first-time setup.
   It may ask you to accept its terms or restart — that's normal. You do not
   need a Docker account; if it asks you to sign in, you can skip that.
4. You'll know it's ready when the little whale icon (in the menu bar on a
   Mac, or the taskbar corner on Windows) stops animating.

That's the only "installing software" you'll ever do for bench.

## Step 2 — Get the bench files

Two ways; pick whichever sounds friendlier.

**The no-tools way:** on the bench page on GitHub, click the green **Code**
button, then **Download ZIP**. When it downloads, double-click the ZIP to
unpack it, and put the resulting folder somewhere you'll find it again
(Documents is a fine home).

**If you're comfortable with a terminal:** `git clone` the repository as
usual.

## Step 3 — Double-click the start file

Open the bench folder and double-click the one for your computer:

| Your computer | Double-click this   |
| ------------- | ------------------- |
| Mac           | `start-bench.command` |
| Windows       | `start-bench.bat`   |
| Linux         | `start-bench.sh`    |

A text window opens and narrates what's happening. It checks that Docker is
awake (and wakes it if not), sets things up, and opens bench in your browser
when it's ready.

### "My computer is warning me about this file!"

Both Mac and Windows are suspicious of downloaded files the first time. This
is normal and only happens once:

- **Mac:** if you see "cannot be opened because it is from an unidentified
  developer", don't double-click — instead **right-click (or
  Control-click) the file and choose Open**, then click **Open** in the
  dialog. After that first time, double-clicking works normally. On newer
  versions of macOS you may instead need to go to **System Settings →
  Privacy & Security**, scroll down, and click **Open Anyway**.
- **Windows:** if a blue "Windows protected your PC" screen appears, click
  **More info**, then **Run anyway**.

### The first start is slow — that's expected

The very first time, your computer downloads and assembles everything bench
needs. Depending on your internet connection this takes **several minutes**
— you'll see a lot of text scroll by and then dots while it waits. Every
start after this one takes only a few seconds. If the window says it timed
out but nothing looks wrong, just wait another minute or two and open
<http://localhost:5180> in your browser yourself.

## Step 4 — Log in

Your browser opens at <http://localhost:5180>. Sign in with:

- **Email:** `admin@example.com`
- **Password:** `bench-admin`

This is fine for playing on your own computer. If other people can reach
this machine, change the email and password in the `.env` file that the
start script created in the bench folder (open it with any text editor, edit
the `BENCH_ADMIN_EMAIL` and `BENCH_ADMIN_PASSWORD` lines, then stop and
start bench again).

## Step 5 — Add an AI provider key

bench talks to AI models on your behalf, so it needs at least one API key —
a long code you get from an AI provider (a single OpenRouter key is enough
to try everything).

bench asks for this itself: right after your first login, a **Welcome to
bench** window appears with a place to pick your provider, paste your key,
and save. If you closed it, the yellow bar at the top brings it back — or go
to **Settings** and find the **Provider keys** section, which is the same
form and where keys live from then on.

You never need to put keys in any file — the app is the place.

## Stopping bench

Double-click the matching stop file: `stop-bench.command` (Mac),
`stop-bench.bat` (Windows), or `stop-bench.sh` (Linux). Everything shuts
down and **all your data is kept** — runs, settings, keys. Double-click the
start file whenever you want it back. Quitting Docker Desktop afterwards is
optional, but frees up memory.

## If something goes wrong

- The start window tells you what it's stuck on — read its last few lines.
- Most problems are Docker not being fully awake yet. Open Docker Desktop,
  wait for the whale to settle, and double-click the start file again.
- Still stuck? Send whoever helps you with computers the last lines from the
  start window — they'll know what to do. (For them: `docker compose logs
  backend` from the bench folder shows the details.)
