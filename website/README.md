# Project page

A static page with no build step: `index.html` plus `static/`. It is published to GitHub Pages
by `.github/workflows/pages.yml` on every push to `main` that touches this folder (enable Pages in
the repository settings with **GitHub Actions** as the source).

```bash
python -m http.server --directory website 8000    # then open http://localhost:8000
```

The numbers come from the run directories of two weeks of use (8 to 23 November 2026): each
session's `outcome.json` gives its outcome, cost and duration. The hero condenses one real session's
report (20 November 2026); its incident line is the logged one, trimmed.

`static/fl-page.css` and `static/fl-page.js` are the interactive layer shared by the Forall Labs
project pages (OpenCaP, AutoPolish, CALVIN-MAX, CodeMedic): the hero with a visual beside the title,
the sticky section bar, the compare slider, figure zoom and the hover states. The same two files ship
in each repository, so edit them in one and copy them to the others.
