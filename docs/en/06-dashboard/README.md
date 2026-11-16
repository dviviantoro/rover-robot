# Dashboard and panel: the two screens

The robot has two optional screens. Both are **clients of `robot-core`** and nothing depends on
them: kill either one and the robot keeps listening, answering and driving.

| | The dashboard (`robot-ui`) | The panel (`robot-face`) |
|---|---|---|
| Where | A browser on your network, `http://<jetson>:8081` | A display attached to the robot |
| For | Setting up, debugging, measuring | Showing the conversation and the face to the person in front of it |
| Language | Python (NiceGUI as the server) and hand-written HTML, CSS and JavaScript | **Rust and Slint**, the one approved exception to "Python on the Jetson" |
| Cost | about 60 MiB | **11.0 MiB** resident, plus 8.4 MiB of display buffers |
| Start with | `./run.sh all` | `./run.sh slint` |
| Needs a login | yes | no input device attached yet |

> **No screenshots yet.** These pages describe the screens from their source. Screenshots need the
> stack running with a browser attached, and the Jetson's memory is shared with the stack, so they
> are a task for someone with the robot running. Each page below says what to look for.

## Pages

| Chapter page | Covers |
|---|---|
| [The dashboard, page by page](pages.md) | The sign-in and landing pages, then Dashboard, Conversation, Knowledge, Memory, Monitor, Services, Logs and Config |
| [The panel on the robot](panel.md) | What `robot-face` shows, how it connects, what it costs, and what is still open |

## Rules that shape both screens

* **One sidebar, defined once** (`NAV` in `static/app.js`). Adding a page used to mean editing ten
  files, and the tenth was always missed. A page is in the shell because it sets `data-page`.
* **`/` is public by not asking for the guard.** It is the only public page. Everything else needs
  the session; `robot-ui` **refuses to start** without `ROBOT_UI_PASSWORD`.
* **Nothing polls on its own.** The numbers ride the orchestrator's 3 s health poll; only the graphs
  have their own timer. Nothing is computed in the browser from something it guessed.
* **Shared components live in `style.css`**, never in a page's own `<style>`, and a test
  (`tests/test_ui_styles.py`) fails when a page uses a class nobody defines.
* **One page width, 72 rem**, with a reading measure inside it for prose.
* **Charts are hand-written SVG**, with three series colours validated for colour-blind readers.
  A glyph and a label carry identity, never colour alone, and there is never a second y-axis.

The wire behind both screens is described in [The WebSocket](../05-interfaces/websocket.md).
