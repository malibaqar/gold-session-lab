GOLD SESSION LAB v4.1 — COMPLETE MT5 DEMO BOT
Includes the full executor, rules, journal, capital guard, news calendar, control commands,
TradingView bridge/Pine file, sample utility and a new live CMD dashboard. No earlier patch is needed.

UPDATE YOUR EXISTING C:\gold_bot_v2 (recommended)
1. Stop the running bot with Ctrl+C. Keep MT5 logged into the pinned DEMO account.
2. Extract ALL ZIP contents directly into C:\gold_bot_v2.
   install_updated_bot.py and its payload folder must be directly inside that folder.
   Do not run bot.py from inside payload.
3. In Command Prompt, run each line separately:
   cd /d C:\gold_bot_v2
   .venv\Scripts\activate
   python install_updated_bot.py
   python -m pip install -r requirements.txt
   python bot.py
4. Maximize the command window for the full dashboard. Ctrl+C stops the runner.
   After installation you can also double-click START_BOT.cmd to start it using the existing .venv.

The installer creates backup_before_complete_DATE_TIME and preserves your account/server,
magic, clock correction, execution mode, runtime.sqlite3, existing calendar and exit-plan cutoff.
Existing managed positions keep their recorded plans; the 0.01 entry cap does not force-close them.
It does NOT clear daily gates, uncertain sends or emergency pauses. Never delete the journal to restart.
Only one runner may hold the configured local port. No order is sent by installation.
Your existing DEMO ORDER/DRY RUN flag is preserved; read the printed mode before starting.
Do not run the old patch installers after this complete update.
The included sample utility is not part of installation; no further sample trade is required.

FRESH INSTALL INTO AN EMPTY WINDOWS FOLDER
Use Windows Python 3.12 64-bit and desktop MT5. Open CMD in the extracted folder:
   python -m venv .venv
   .venv\Scripts\activate
   python install_updated_bot.py
   python -m pip install -r requirements.txt
Edit the generated config.json: set account_login and account_server to the exact DEMO identity;
select your broker's exact gold symbol and correct terminal_path if needed.
Verify broker_time_offset_hours from actual feed/tick timestamps. Do not copy another broker's offset.
Fresh installation defaults to DRY RUN. Verify setup/quotes and then set send_demo_orders=true for demo.
Start with python bot.py. Real accounts are refused.

LOT SIZE IS NOT A FIXED MONEY RISK
New entries are capped at max_lot_size=0.01. This is a maximum, not a guaranteed order size.
The bot calculates stop loss in account currency using MT5 order_calc_profit, adds configured
commission and an adverse-entry allowance, then floors volume to broker increments.
Base risk_fraction is at most 0.005 (0.5% equity), with the existing 10% reserve retained.
Risk may be reduced further by market context. It is never increased above that base budget.
Example: equity 1,000 -> base full budget 5.00, usable budget 4.50 before lot rounding.
If the broker's minimum 0.01 position has an estimated loss of 10.00, the bot SKIPS it.
It does not tighten the strategy stop or raise the risk limit merely to force a trade.
If the broker supports a smaller valid volume, the risk-based size can be below 0.01.
Account-currency loss can exceed estimates because of gaps, slippage or charges.

ADAPTIVE MARKET RULES (initial, unvalidated thresholds)
Use 50 prior ATR values from the signal's own timeframe to calculate a median baseline.
- Signal ATR >=2x prior median: skip entry.
- Signal ATR >=1.5x but <2x median: halve the cash budget.
- EMA20/50 separation <0.25 signal ATR: skip weak trend context.
- Closing price >1.5 ATR away from EMA20: skip extended/chasing entry.
- Sweep-rejection setup: multiply the remaining budget by 0.75.
All other contexts use the base budget, never a multiplier above 1.
Exact signal-candle context must be present; failed/missing data blocks the new entry.
These are transparent risk heuristics, not measured win probabilities or a promise of higher accuracy.

STRATEGY AND POSITION LIMITS
Completed candles only: M5 every five minutes, H1 once per hour. The MT5 chart tab does not control scanning.
Conservative profile enables session_pullback, breakout_retest and sweep_rejection on M5/H1.
EMA reclaim, scalping, inside breakout and impulse remain in engine.py but are disabled in this profile.
All-hours entry eligibility is retained, subject to broker availability and every safety gate.
Maximum one gold position. Combined estimated stop-risk ceiling at most 1% equity.
Existing daily loss gate (normally 2%), six daily entry attempts, entry cooldown, spread/margin/stops
checks, account pinning, reconnect revalidation and duplicate-send protection remain active.
Manual/foreign gold positions, pending orders, uncertain outcomes and stale data can block entries.
Daily gates block NEW orders and do not guarantee the eventual maximum realized daily loss.

THREE TARGETS AND THE 0.01-LOT LIMITATION
New guarded positions retain their initial broker SL and original final TP.
TP1 = halfway from actual entry to the original final TP; TP2 = three-quarters of that distance;
TP3 = the original final TP. For a standard 2R setup: 1R, 1.5R, 2R.
TP1: close 50%, rounded down; then move remaining SL to entry when broker rules allow.
TP2: close 75% of the REMAINING volume, rounded down; TP3: close the rest.
No widening of stops; a better existing stop is preserved. Entry breakeven excludes fees/slippage.
If three valid volume portions cannot be formed, the explicit fallback is FULL CLOSE AT TP1.
On a 0.01-minimum/0.01-step broker, every new 0.01 entry uses that fallback. It cannot be split.
The bot will NOT open 0.03/0.08 or another larger volume merely to manufacture three portions.
With a smaller minimum/step, some <=0.01 positions may support partial exits.
Broker SL/final TP remain if the software disconnects; TP1/TP2 software exits require it online.
Price gaps can bypass intermediate actions. Old unguarded positions are not adopted automatically.

LIVE CMD DASHBOARD
Gold/cyan header, live account and risk panels, floating/booked P/L cards, open-position table,
entry/current/SL/TP detail, guarded exit plans and recent decisions/execution events.
Snapshots refresh approximately every 2 seconds while connected, with their age displayed.
A stale snapshot is marked; it is not presented as a fresh market reading.
MT5 history is refreshed every 60 seconds. Floating includes swap, excludes entry commission.
Today's booked bot P/L includes attributed deal profit, commission, swap and fee, including entry fees
on positions still open. It is not a closed-trades-only performance statistic. Day boundary is UTC.
Whole-account balance/equity can differ from bot-only P/L; manual/foreign positions are listed separately.
Rich is used only for display. If unavailable or the stream is not a terminal, text output remains usable.
Use a large/maximized window; shorter windows use a compact layout and fewer recent event rows.
All original diagnostic logs remain in rotating bot.log files. No MT5 calls run in the display thread.
Set dashboard_enabled=false in config.json and restart for plain log output.
Display clock defaults to Asia/Karachi; change display_timezone if desired. Broker timestamps remain
normalized using broker_time_offset_hours, and news/calendar/daily risk boundaries remain UTC.

NEWS CALENDAR — SAVED SNAPSHOT, NOT AN AUTOMATIC NEWS FEED
The included selected-event calendar covers remaining major US releases through 31 December 2026:
CPI, NFP, PPI, PCE, GDP, FOMC statement/press conference/minutes, JOLTS and ECI.
Default blackout is 30 minutes before and after saved events; FOMC press conferences have 60 minutes after.
It is not an exhaustive high-impact calendar: speeches, ISM/PMI, retail sales, geopolitical surprises,
other central banks and additional events require separate verified additions.
Missing/malformed/expired calendar blocks new entries; known protective exits can continue.
First snapshot review deadline: 30 October 2026 00:00 UTC / 05:00 Pakistan time.
An existing installed calendar is preserved, including any earlier review deadline.
Review official dates weekly and after schedule changes:
   python review_calendar.py
After actually checking/correcting dates and coverage, stop the bot and record your review:
   python review_calendar.py --confirm-official-dates-reviewed
Then restart. That command records YOUR review and extends its deadline at most seven days;
it does not fetch, verify or add event dates. Do not use it to bypass a needed review.

EMERGENCY VOLATILITY HALT AND RESUME
Current/latest M5 true range >=3x preceding ATR, or observed mid-price move >=0.50% within
60 seconds, latches a persistent pause on NEW entries. These thresholds are unvalidated.
It detects price behavior, not war/headline content, and cannot protect against every initial gap.
It never resumes automatically. Known protective exits continue unless management is uncertain.
After inspecting conditions, Ctrl+C, then:
   python guard_control.py
   python guard_control.py --resume
   python bot.py
Resume is your explicit approval and refuses if detected shock/data/news/management checks still block it.
For uncertain management outcomes: inspect MT5 Trade AND History, manually reconcile and close ALL
bot-owned gold positions, then python guard_control.py --ack-management. Approve --resume separately.
An original entry-send halt still needs its original reconciliation. Never blindly clear an unknown halt.

TRADINGVIEW
bridge.py and tradingview_confirmation.pine are included for compatibility. The bot does not read your
browser/chart directly. The existing Pine alert only covers its original session_pullback setup/session
filter. Default require_tradingview_confirmation=false lets enabled Python rules work independently.
If true is preserved from your old config, that confirmation requirement can still restrict other entries.

TESTING AND PRACTICAL LIMITS
This is a rule-based DEMO bot with no calibrated win-probability model and no verified profitability claim.
The new lot/risk rules change behavior, so evaluate this version separately from earlier results.
Backtest 2–3 years with realistic costs, bid/ask or tick data and unseen holdout periods; then at least
8–12 weeks and roughly 100–200 strategy entries on demo, whichever takes longer, without changing rules.
The old research.py replay, if still present in your folder, is not a valid simulator for this exit manager.
Offline fake-broker checks cover lot caps, risk reductions, skip filters, guard/management recovery,
account enforcement, journal/config preservation and dashboard rendering. No real MT5 fill, Windows
terminal integration or historical profitability was validated here. Keep this version on demo.
