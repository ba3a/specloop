# specloop landing page: style guide

## Goal

Publish `specloop.md` on the site as a landing page that sells specloop and sends visitors to the GitHub repository. The page content is final and lives in Markdown. This work adds the layout, styles and rendering rules that turn that Markdown into the designed page described below, without changing the site's other pages.

This spec is meant for feature mode: run it with `/sl-feature`.

## Background

- The site renders pages from Markdown. Use the site's existing Markdown pipeline, template system and asset handling; find out how they work before planning. Do not introduce a new framework or build tool.
- `specloop.md` uses only standard Markdown plus two conventions:
  - `<!-- section: <name> -->` comments split the page into sections. The names are `hero`, `problem`, `how-it-works`, `timeline`, `spec-example`, `features`, `dogfood`, `quick-start`, `faq` and `final-cta`.
  - A `######` (h6) heading directly before a section's `##` (or the hero's `#`) heading is an eyebrow label, not a heading.
- The front matter holds `title`, `description`, `slug` and `og_image`. Map these to the site's own front matter keys if they differ.
- `index.html` next to this file is a static reference rendering of the same page. Match its look; it is the source of truth for anything this guide leaves open. Do not copy it into the site as-is.
- If the Markdown renderer cannot see HTML comments or front matter, prefer a small renderer extension or a post-processing step over editing the content. Change `specloop.md` only where the site's pipeline makes a convention impossible, and keep the wording unchanged.

## Requirements

### 1. Design tokens

Define these as CSS custom properties scoped to the landing page (for example on a `.landing` wrapper), so they cannot leak into other pages.

| Token | Light | Dark | Use |
|-------|-------|------|-----|
| `--bg` | `#fbfaf7` | `#121114` | Page background |
| `--bg-alt` | `#f2efe8` | `#1a191d` | Alternating section background, inline code background |
| `--fg` | `#1d1b18` | `#ece9e2` | Body text |
| `--muted` | `#6b665d` | `#a19c92` | Secondary text |
| `--line` | `#e2ddd2` | `#2c2a30` | Borders and dividers |
| `--accent` | `#c2521c` | `#f08a50` | Brand color: eyebrows, links, primary button, numbers |
| `--accent-soft` | `#f6e3d6` | `#2e1f17` | Callout and "You" chat bubble background |
| `--ok` | `#2f7d4f` | `#7fd19b` | Check marks |
| `--term-bg` | `#17161a` | `#0b0a0d` | Code and terminal blocks (dark in both themes) |
| `--term-fg` | `#e9e6df` | `#e9e6df` | Code text |
| `--term-muted` | `#8a857b` | `#8a857b` | Code comments, `[specloop]` lines |
| `--term-ok` | `#7fd19b` | `#7fd19b` | `done`, `APPROVE` in terminal output |
| `--term-warn` | `#f2b45c` | `#f2b45c` | Commit hashes, the "you leave" line |
| `--term-accent` | `#f08a50` | `#f08a50` | Prompt characters `>` and `$`, Markdown headings in code |
| `--radius` | `14px` | `14px` | Cards, code blocks, stat tiles |

Dark values apply under `prefers-color-scheme: dark` and, if the site has a theme switch, under its dark setting. If the site already has a dark mode mechanism, hook into it instead of adding a second one.

### 2. Typography

- Body: Inter (fall back to `system-ui`), 17px, line height 1.6.
- Code and labels: JetBrains Mono (fall back to `ui-monospace, Consolas, monospace`).
- Load fonts the way the site already loads fonts. If it self-hosts, self-host these too.
- Headings: weight 800, letter spacing `-0.02em`, line height 1.15. `h1` scales with `clamp(38px, 6vw, 68px)`, `h2` with `clamp(28px, 4vw, 42px)`, `h3` is 20px weight 600.
- Eyebrow (the h6 convention): JetBrains Mono, 13px, weight 600, uppercase, letter spacing `0.12em`, color `--accent`. It is rendered as a paragraph-level label, not as a heading, so it does not appear in any table of contents or heading outline.
- The first paragraph after a section heading is a lead: 18–21px, color `--muted`, at most 640px wide.

### 3. Layout

- Content width at most 1080px, centered, with a 16px side gutter.
- Every section has 88px vertical padding and a 1px `--line` top border, except the hero.
- Sections alternate background: `problem`, `timeline`, `features`, `quick-start` and `final-cta` use `--bg-alt`; the rest use `--bg`.
- Two-column sections (`hero`, `problem`, `timeline`, `spec-example`, `quick-start`): text on the left, the visual (code block, chat, timeline) on the right from 900px wide (960px for the hero); stacked below that, text first.
- No horizontal page scroll at any width down to 320px. Code blocks scroll horizontally inside themselves.

### 4. Section rendering

| Section | Markdown | Rendered as |
|---------|----------|-------------|
| `hero` | `#` heading | The headline. Each sentence on its own line; the second sentence ("Go make tea.") in `--accent`. |
| `hero` | Paragraph of two links | Call-to-action buttons. The first is primary (`--accent` background, white text), the second is secondary (`--bg` background, `--line` border). 12px × 20px padding, 10px radius, weight 600, lift 1px on hover. |
| `hero` | Bullet list | Badge row: inline, small (14px), `--muted`, each item prefixed with a `--ok` check mark. |
| `hero` | `console` code block | Terminal window: `--term-bg`, a title bar with three grey dots and the label "claude — my-project", large soft shadow, a blinking block cursor after the last line. |
| `problem` | Blockquote of `**Claude:**` / `**You:**` lines | Chat bubbles. Claude bubbles left-aligned on `--bg` with a `--line` border; You bubbles right-aligned on `--accent-soft`. The speaker labels are hidden. At most 85% wide, 12px radius. |
| `how-it-works` | Table | Three cards side by side (stacked on narrow screens). The phase cell is the card's label in `--accent` mono; the middle cell is the body; the gate cell sits under a dashed divider as "Gate: …". |
| `timeline` | Bullet list | Vertical timeline with a 2px `--line` rail. The bold time is a mono label. Items marked `*you*` get a filled `--accent` dot and the marker text is hidden; the others get a hollow dot. |
| `spec-example` | `markdown` code block | Code block with Markdown headings in `--term-accent`. |
| `features` | Bullet list | Card grid, `repeat(auto-fit, minmax(260px, 1fr))`. The emoji is the card icon above the title; the bold text is the card title; the rest is the body in `--muted`. |
| `dogfood` | Bullet list of `**number** · text` | Stat tiles: the number at 36px weight 800 in `--accent`, the text below at 14px `--muted`. |
| `dogfood` | `console` code block | Terminal styling: hashes in `--term-warn`, `specloop step …` lines in `--term-muted`. |
| `dogfood` | Blockquote | Callout: 4px `--accent` left border, `--accent-soft` background, rounded right corners. |
| `quick-start` | `bash` code block | Code block with comments in `--term-muted` and slash commands in `--term-accent`. |
| `faq` | `###` headings with paragraphs | Accordion: each question is a collapsed `<details>` whose summary is the question, with `+` / `–` in `--accent` on the right. Content width at most 780px. Without JavaScript all answers still work. |
| `final-cta` | Everything | Centered. The link paragraph becomes buttons, the first primary. |
| after `---` | Paragraph | Small footer text in `--muted`. |

Code highlighting: if the site already has a syntax highlighter, theme it with the `--term-*` tokens for this page. Otherwise color only what the table names; do not add a highlighting library for this page.

### 5. Navigation and metadata

- Brand assets are in `logo/` next to this file; `logo/README.md` says which file goes where. Copy the ones you use into the site's static assets, at the paths the front matter names (`og_image`, `logo`).
- Favicons: `favicon.ico`, `favicon.svg` and `png/apple-touch-icon.png`. If the site has a global favicon, keep it and use these on this page only, unless the site is specloop's own.
- A sticky top bar for this page: the 32 px `logo.svg` (`logo-dark.svg` in dark mode), then the wordmark `spec` + `loop` (the second half in `--accent`) in JetBrains Mono, links to `#how-it-works`, `#dogfood` and `#quick-start`, and a small "★ GitHub" button. Below 640px only the wordmark and the button remain. If the site has a global header, put this bar under it or replace it on this page only, whichever the site's templates support.
- Each section gets an `id` equal to its section name, so the in-page links work.
- `title`, `description` and `og_image` go into `<title>`, `<meta name="description">` and Open Graph tags.

### 6. Accessibility

- Text contrast meets WCAG AA in both themes.
- The terminal and chat visuals carry an `aria-label` describing them.
- The blinking cursor and the button hover lift are disabled under `prefers-reduced-motion: reduce`.
- Buttons and accordion summaries are keyboard reachable with a visible focus ring in `--accent`.

## Acceptance

- The page is reachable on the site at the `slug` from the front matter, and the site's existing pages look and behave exactly as before.
- Every section in the table of section 4 renders as described, in light and dark mode, at 375px, 768px and 1280px wide.
- `specloop.md` is the only place the page's text lives; the templates and styles contain no copy from it.
- The site's own build, link check and tests (if any) pass.
- The repository's own documentation, if it has any for adding pages, mentions the landing layout and its two Markdown conventions.
