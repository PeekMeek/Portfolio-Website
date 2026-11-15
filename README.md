# Engineering portfolio

A static site: no build step, no server code.

## Run it locally
From this folder: `python -m http.server`, then open http://localhost:8000.
(Opening index.html directly won't work because the browser blocks loading the data file.)

## Deploy
Push this folder to a GitHub repo and enable GitHub Pages, or drop it into Cloudflare Pages / Netlify.

## Add a real project
1. Put STEP files in `projects/<project-id>/` (for example `projects/robot-arm/v1.step`).
2. Add an entry to `data/projects.json`. For each version, use `"file": "projects/robot-arm/v1.step"` instead of `"demo": [...]`.
3. Optional change data per version, keyed by part name (the name inside the STEP file):
   - `"changes": {"Bracket": "modified", "Bolt": "added"}`
   - `"removed": ["Old Pin"]` (shown as a translucent red ghost from the previous version)

The sample projects use `demo` parts (simple boxes and cylinders) so you can see everything working before you add files.

## Notes
- The viewer loads three.js and occt-import-js from the jsDelivr CDN, so visitors need internet access.
- Large STEP files load slowly in the browser. The Windows app will convert them to a lighter format later.
- "Open a STEP file" on a project page previews a local file without saving anything.
