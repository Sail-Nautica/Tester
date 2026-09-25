# MaizeTix Price Tracker

Tracks student ticket prices on [maizetix.com](https://www.maizetix.com) every 5 minutes
to help decide when, and at what price, to sell.

- **Dashboard:** published to GitHub Pages by the workflow each run
- **Collector:** `.github/workflows/collect.yml` runs `maizetix_tracker.py` on a schedule and commits the results
- **Data:**
  - `data/snapshots.csv`: per game, per run: listed, sold, cheapest ask, median sale
  - `data/listing_events.csv`: each listing's lifecycle (listed, price changes, removed) with section/row/seat.
    A removal while the sold count rose is counted as a likely sale.
  - `data/games.json`: schedule
  - Seller initials and names are intentionally not recorded.

Prices on MaizeTix include the buyer fee ($3 + 10%). Seller payout ≈ (price − 3) ÷ 1.1.

Run locally (Python 3, no dependencies):

```
python3 maizetix_tracker.py collect
python3 maizetix_tracker.py report   # writes docs/index.html
```
