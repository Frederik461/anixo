# Mouse accessibility checklist

This checklist is for the pointer-first pass completed on 2026-11-24. It is a
manual companion to `tests/test_mouse_interactions.py`; automated tests cover
the event wiring, but a human should still confirm hover/press feedback,
clipping, and terminal-specific scrolling on each supported platform.

Supported targets for this pass:

- Windows 10 x64 and Windows 11 x64
- macOS 13+ Apple Silicon (`arm64`)
- macOS 13+ Intel (`x86_64`, CI-validated only)

Use the packaged build or `python main.py`. Terminal size controls the
responsive tiers:

| Tier | Size |
| --- | --- |
| Compact | width `< 96` or height `< 34` |
| Medium | width `96–139` and height `>= 34` |
| Wide | width `>= 140` and height `>= 40` |

Resize with `pilot.resize_terminal()` in automation or by resizing the terminal
window manually.

## 1. Shell, navigation, and history

- [ ] At startup the left `←` and right `→` arrows in the top bar are disabled.
- [ ] Click every nav rail item (Home, Discover, Watchlist, History, Settings).
  The matching screen opens and its nav item is highlighted.
- [ ] After visiting at least two screens, `←` becomes enabled. Click it: the
  previous screen opens and `→` becomes enabled.
- [ ] Click `→`: the next screen opens and the forward state updates again.
- [ ] `Alt+←` and `Alt+→` do the same as the two buttons.
- [ ] Clicking a nav item while the search/filter input has focus still
  navigates, and the input is blurred. Clicking the search field again restores
  focus and typing works.
- [ ] Opening details from a card and then clicking a nav item returns to a
  clean primary screen; no stale details screen remains underneath.

## 2. Cards, posters, and featured panel

- [ ] In Dashboard, Discover, and Watchlist, a left-click on a card, poster, or
  featured artwork opens that exact anime's details.
- [ ] The clicked card becomes selected; its poster image does not flicker or
  recompose when only selection changes.
- [ ] Right-click on a card/artwork does nothing.
- [ ] Hover on a card shows the accent border/boosted background; focus and
  selected states remain distinguishable.
- [ ] The Watch button on the featured panel and on details works by click.
- [ ] Disabled action buttons (for example Mark with no progress) do not react
  to clicks and render visibly dimmed.

## 3. Filters, seasons, and episodes

- [ ] Discover's Category, Genre, and Sort chips respond to left-click and
  reload results with the clicked value.
- [ ] In the season/movie screen, left-click on a different provider row selects
  that row and Choose uses that exact provider entry.
- [ ] In the episode browser, left-click on a different episode cell updates the
  selection and the detail pane (number, title, status, description).
- [ ] Right-click on an episode cell does not change the selection.
- [ ] Mouse wheel scrolling over the episode list moves the scroll offset but
  never changes the selected episode.
- [ ] The manual episode jump field still accepts a typed number and Enter.
- [ ] Provider-released episodes only are listed. A `RELEASING` series with a
  known `nextAiringEpisode` stops at `episode - 1`; no planned future episodes
  appear. The manual jump remains available.

## 4. Dialogs, settings, setup, and maintenance

- [ ] Confirm dialog: both Confirm and Cancel buttons work by click; Y/N/Enter/
  Escape still work.
- [ ] Help dialog: Close button works; `?`, `F1`, and Escape still work.
- [ ] Settings categories and rows respond to click; text rows keep Enter/Escape
  edit/cancel behavior.
- [ ] History rows start playback when clicked; keyboard Enter on a selected row
  still works.
- [ ] Setup dependency rows can be clicked and show the selected/hover state.
- [ ] Diagnostics Refresh and Export buttons work by click.
- [ ] Uninstall mode options (ANIXO only / complete removal) respond to click
  and update the active marker and warnings.

## 5. Keyboard and mixed input regression

- [ ] `D/G/W/H/S`, `Tab`, `Shift+Tab`, `Enter`, `Escape`, `↑↓←→`, `PgUp/PgDn`,
  `Home/End`, `/`, `A`, `E`, `M`, `Shift+M`, `U`, `V`, `X`, `?`, and `Q` still
  behave as documented in `README.md`.
- [ ] Mixing keyboard selection with a mouse click selects the clicked item and
  opens its details.
- [ ] Typing in a search field does not trigger route actions until a deliberate
  nav click or shortcut.
- [ ] Rapid resize and filtering do not leave stale results or duplicate
  posters on screen.

## 6. Responsive layout

Repeat sections 1–5 at compact, medium, and wide sizes:

- [ ] Top bar arrows, title, and nav rail remain visible and non-overlapping.
- [ ] Cards, chips, rows, and action buttons have visible hit targets.
- [ ] No clipped text or hidden buttons in compact mode.
- [ ] Scroll containers respond to the wheel without moving selection.

Record the terminal emulator, OS, architecture, size tier, and any failure in
`TESTING.md` before release. Do not mark an item VERIFIED on macOS unless it was
actually run on a Mac; headless CI only verifies the automated tests.
