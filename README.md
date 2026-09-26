# HawkDrop 🦅

A smart price tracker for Israeli shoppers (and anyone else). It tracks one item across
several online stores, local or worldwide, and compares the **real delivered price**:
shipping, customs duty, VAT and courier fees included. It then tells you whether to
**buy now or wait** for an upcoming sales day, with a confidence score.

- **Multi-store tracking**: KSP, Ivory, Bug, LastPrice, Payngo, ACE, Zap, Amazon (US/UK/DE),
  AliExpress, eBay, B&H, Newegg. Any other shop works too, with country and currency guessed
  from the domain.
- **Landed cost**: Israeli import rules by default: 18% VAT above the personal-import
  exemption, customs duty per category above $500, and a courier clearance fee when taxes
  are collected on arrival. Stores that charge VAT at checkout (Amazon, AliExpress) are
  handled. US, EU and UK destination profiles are also included.
- **Sales-day calendar**: Black Friday/Cyber Monday, 11.11, Prime Day, Prime Big Deal Days,
  AliExpress Anniversary, 618, Boxing Day, Back to School, and the Israeli pre-Rosh Hashana
  and pre-Passover sales, computed from the Hebrew calendar.
- **Buy/wait advice with confidence**: a Monte-Carlo model that combines the item's price
  trend and volatility with how likely each upcoming event is to discount it, and by how
  much. It learns from the item's own behaviour in past events.
- **Web app / iOS PWA** that works fully offline and syncs changes when you're back online.
- No dependencies. Python 3.11+ and SQLite.

## Install

```bash
pip install .          # or run in place: python -m hawkdrop ...
```

## Quick start

```bash
hawkdrop demo                     # a demo item with 14 months of synthetic history

hawkdrop track "Sony WH-1000XM5" --category electronics --target 1100 \
    --url https://ksp.co.il/web/item/XXXX \
    --url https://www.amazon.com/dp/XXXX \
    --url https://www.aliexpress.com/item/XXXX.html

hawkdrop check                    # fetch prices now (put it in cron: 0 */6 * * * hawkdrop check)
hawkdrop compare "WH-1000XM5"     # landed-cost table
hawkdrop advise  "WH-1000XM5"     # buy now or wait?
hawkdrop history "WH-1000XM5"     # sparkline of the best landed price
hawkdrop events --region IL       # upcoming sales days
```

Some sites block bots (captcha) or have no machine-readable price. You can either:

```bash
hawkdrop price "WH-1000XM5" ivory 1449 --shipping 29       # record a price manually
hawkdrop add-offer "WH-1000XM5" https://shop.co.il/p/1 --regex 'class="price">([\d,.]+)'
```

`--shipping` on `add-offer` or `price` overrides a store's shipping policy. Unknown shipping is
flagged with `?` in `compare`.

## Example advice

```
  Recommendation : WAIT until Nov 23 (Black Friday / Cyber Monday)
  Confidence     : 80% (high)
  Best offer now : AliExpress - 1,307 ILS delivered
  Expected price : ~1,169 ILS  (80% range 1,046 ILS - 1,305 ILS)

  Why:
   • Watch through Amazon Prime Big Deal Days (October), Singles' Day 11.11, Black Friday /
     Cyber Monday (...): 83% chance one of them beats today's price by more than the cost
     of waiting; expected net saving ~106 ILS.
   • Buy at the first sale where the landed price drops below: 1,253 ILS (...), 1,232 ILS
     (Singles' Day 11.11), 1,221 ILS (Black Friday / Cyber Monday).
   • This item dropped in 1/1 past Black Friday / Cyber Monday events.
```

## Web app and iPhone app (PWA)

```bash
hawkdrop serve                                    # http://localhost:8765
hawkdrop serve --host 0.0.0.0 --token MYSECRET --check-every 6   # reachable from your phone
```

The web client lets you see your items, the buy/wait advice with its confidence, a price
chart with past sales shaded, the delivered-price breakdown for each store, and the sales
calendar. From it you can log prices, add stores and track new items.

**Offline.** The app is offline-first:

- A service worker caches the app itself, so it opens with no connection.
- The last data synced from the server is stored on the device (IndexedDB).
- While offline you can browse everything, **log prices, add stores, track new items, edit
  or delete them**. Changes wait in a queue ("2 pending") and are sent when the server is
  reachable again. Each queued price carries a unique ID, so a retry never records it twice.
- Only fetching prices from store websites ("Check") and loading the demo need a connection.

**Installing on iPhone / iPad:**

1. Run the server where your phone can reach it, **over HTTPS**. iOS only enables service
   workers (offline mode) on HTTPS or `localhost`. Some easy ways:
   - [Tailscale](https://tailscale.com/kb/1312/serve): `tailscale serve 8765` gives you
     `https://<machine>.<tailnet>.ts.net`;
   - a Cloudflare Tunnel;
   - your own certificate: `hawkdrop serve --host 0.0.0.0 --cert cert.pem --key key.pem`.
     [mkcert](https://github.com/FiloSottile/mkcert) works, but you need to install its root
     CA on the iPhone.
2. Open the URL in **Safari** (add `?token=…` if you started the server with `--token`).
3. Tap **Share → Add to Home Screen**. The home-screen app keeps the token.
4. Open the app once while online. After that it works offline and syncs when it opens,
   when it returns to the foreground and when the connection comes back. iOS has no
   background sync, so syncing only happens while the app is open.

`--check-every HOURS` makes the server fetch every tracked store page on a schedule, so
prices keep updating while your phone is offline.

## How the advice works

1. **Daily best price.** Each day, the cheapest in-stock landed price across all stores. A
   store's last reading counts for up to 14 days.
2. **Trend and noise.** Log-linear regression over the last 90 days, damped and capped so a
   short slide is not extrapolated too far.
3. **Events.** For every sales event in the next 75 days that one of the item's stores takes
   part in, HawkDrop starts from a category prior: P(item discounted), and the typical
   discount depth. It then updates that prior with what this item did in past occurrences of
   the event: the drop against the median of the 35 days before it.
4. **Simulation.** 4,000 correlated price paths. The events share a price-level random walk
   and a "this item gets promoted" propensity. The strategy "watch through events 1..k and
   buy at the first one below the trigger price" is scored by its expected saving *net of
   waiting cost*. The waiting cost (0.06%/day by default) stands for the risks of rising
   prices, stock-outs and going without the item.
5. **Decision.** HawkDrop recommends WAIT when the best strategy saves money on average
   *and* is more likely than not to beat today's price. Otherwise it says BUY NOW. Hitting
   your `--target`, or being at the historical low, favours buying.
6. **Confidence.** The probability behind the decision, shrunk toward 50% when data is thin:
   short history, few readings, few stores, or no past events observed.

## Configuration

`~/.hawkdrop/config.toml` (set `HAWKDROP_HOME` to move it):

```toml
[destination]
code = "IL"              # IL, US, EU, UK (or use --dest)
vat_rate = 0.18
vat_exempt_usd = 75      # personal-import VAT exemption, change it if the law changes
duty_exempt_usd = 500
clearance_fee = 35       # ILS, charged by couriers when taxes are due on arrival
threshold_includes_shipping = false

[destination.duty_rates]
clothing = 0.12

[advisor]
max_wait_days = 75
min_saving = 0.03        # only wait for savings of at least 3% ...
wait_cost_per_day = 0.0006  # ... plus 0.06% per day of waiting

[stores.amazon_us]
shipping_flat = 15
shipping_free_over = 49
```

> Tax thresholds, rates and store shipping policies change. The built-in values are
> estimates, not tax advice. Check them and override them in the config.

Exchange rates come from the ECB via frankfurter.app and are cached for 12 hours. If you're
offline, HawkDrop falls back to built-in approximate rates.

## Development

```bash
python -m unittest discover -s tests          # unit + API tests

# browser test of the web app, including full offline use (needs Playwright + Chromium)
HAWKDROP_HOME=$(mktemp -d) hawkdrop --offline serve --port 8799 &
HAWKDROP_URL=http://localhost:8799 NODE_PATH=$(npm root -g) node tests/e2e/offline.cjs

python scripts/make_icons.py                  # regenerate the app icons
```

When you change files in `hawkdrop/web/`, bump `VERSION` in `sw.js` so installed apps pick
up the update. They show a "new version ready, Reload" prompt.
