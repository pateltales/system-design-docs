# HLD System Design Docs

A static documentation site for High Level Design (HLD) system design topics — browsable via a sidebar with Markdown rendering.

**Live site:** http://pateltales.com/system-design-docs/

## Local Development

Requires [Node.js](https://nodejs.org/).

```bash
npm install
npm start
```

Open http://localhost:3000 in your browser.

## Adding or Updating Docs

1. Add or edit `.md` files in any topic directory (e.g. `twitter/`, `uber/`, `ratelimiter/`).
2. Regenerate `tree.json` so the sidebar picks up your changes:

```bash
python3 generate_tree.py
```

3. Commit and push:

```bash
git add .
git commit -m "Update docs"
git push
```

GitHub Pages redeploys automatically within ~30 seconds of each push.

## Hosting

The site is hosted on **GitHub Pages** from the root of the `main` branch. It is fully static — `index.html` loads `tree.json` for the sidebar and fetches `.md` files directly, so no server is needed.

To enable GitHub Pages on a fork:
1. Go to **Settings → Pages**
2. Set source to **Deploy from a branch**, branch `main`, folder `/`
3. Save — your site will be live at `https://<your-username>.github.io/system-design-docs/`
