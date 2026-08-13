# Marketplace Deal Watcher v2

This version is designed for Windows and has a GUI. You should not need to edit
JSON manually.

## Quick start

1. Extract the ZIP.
2. Double-click `SETUP.bat`.
3. When setup finishes, double-click `START.bat`.
4. Paste your Discord webhook and click **Test Discord**.
5. Click **Facebook Login** and log into Facebook in the browser that opens.
6. In Facebook Marketplace, build a search with the location/radius/category you
   want and copy the search-results URL.
7. In the app, click **Add Search** and paste that URL.
8. Click **Run Once** to test.
9. Click **Start Watcher** to keep checking.

## Improvements over v1

- Windows GUI
- No manual config.json editing required
- Multiple Marketplace searches
- Discord webhook test button
- Better Discord embeds
- Listing thumbnail when Facebook exposes one
- Automatic median-based price baseline
- Manual estimated-value override
- Required/preferred/excluded keywords
- Maximum price
- Minimum discount and deal score
- Price-drop alerts
- Duplicate protection with SQLite
- Limited scrolling to load more result cards
- Start/stop controls and live logs
- Maximum alerts per scan to avoid notification floods

## Automatic price baseline

When **Automatically estimate normal price** is enabled, the watcher uses the
median price of the visible listings in that search as a rough baseline. This is
most useful when your Marketplace search is specific, for example:

- `RTX 4070 gaming PC`
- `PS5 disc edition`
- `Ryzen 7 7800X3D`

It is less useful for a very broad search like `computer`, because the products
are too different.

You can instead enter a manual estimated value for a search.

## Important limitation

Facebook can change Marketplace page structure, login behavior, or access rules.
If extraction suddenly finds zero listings while the browser clearly shows
results, the selectors may need an update.

This project intentionally does not include CAPTCHA bypassing, login bypassing,
stealth plugins, proxy rotation, or code intended to defeat Facebook security
controls.

Keep the `browser_profile` folder private because it contains local browser
session data.
