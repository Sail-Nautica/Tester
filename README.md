# MaizeTix Price Tracker

Tracks student ticket prices on [maizetix.com](https://www.maizetix.com) every 15 minutes
to help decide when, and at what price, to sell.

- **Dashboard:** GitHub Pages serves `docs/index.html`
- **Collector:** `.github/workflows/collect.yml` runs `maizetix_tracker.py` on a schedule and commits the results
- **Data:** `data/snapshots.csv` (history), `data/latest.json` (current listings), `data/games.json`

Prices on MaizeTix include the buyer fee ($3 + 10%). Seller payout ≈ (price − 3) ÷ 1.1.

Run locally (Python 3, no dependencies):

```
python3 maizetix_tracker.py collect
python3 maizetix_tracker.py report   # writes docs/index.html
```
