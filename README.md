# @MausamMitra — v0.3 (overhaul)

SIH 2026 · Ministry of Earth Sciences (MoES) · India Meteorological Department

This is a rebuild of your original submission, focused on the three things
you flagged: **response speed**, **UI quality**, and **mobile-friendliness**,
plus the feature set you asked for (charts, map, voice, multi-language,
push-style alerts, installable PWA).

```
backend/     FastAPI + Gemini agent (rewritten: async, cached, fewer LLM round trips)
frontend/    New mobile-first PWA (vanilla JS, no build step)
```

## ⚠️ Before anything else: rotate your Gemini key

Your uploaded zip included `weathergpt_gemini.py` with a **real API key
hardcoded in plain text** (`AQ.Ab8RN6...`). If that file was ever committed to
GitHub, submitted in a zip to judges, or shared anywhere, treat that key as
compromised: go to https://aistudio.google.com/apikey , delete/rotate it, and
never hardcode keys in files you share — use the `GEMINI_API_KEY` environment
variable, which is what the real backend already does. I did not carry that
file forward into this rebuild.

## What was actually making it slow

1. **4 LLM round trips per question.** The old agent let Gemini call
   `geocode_location`, `get_forecast`, `evaluate_alerts`, and
   `generate_crop_advisory` as four *separate* tool calls. Each tool call is
   a full extra generation from the model — that's most of your latency,
   not the weather API itself. **Fix:** added `get_weather_briefing()`, one
   Python function that does all four steps internally. The system prompt
   now tells Gemini to prefer it, so a typical question is 1 round trip
   instead of 4.
2. **No caching.** Every question re-hit Open-Meteo's geocoder and forecast
   API, even for the same city seconds apart. **Fix:** small in-memory TTL
   cache (24h for geocoding, 10min for forecasts).
3. **Blocking I/O.** `requests` + sync functions meant each request tied up
   a worker thread. **Fix:** rewritten on `httpx.AsyncClient` with a shared
   connection pool, and the whole FastAPI app is now `async def` end to end.
4. **No streaming — the UI waited for the entire answer.** Added
   `POST /chat/stream` (Server-Sent Events) so the new frontend shows text
   as Gemini generates it, which fixes the *perceived* speed even on
   questions that still take a few seconds.

None of this changes the "never guess a number" grounding guarantee — it's
the same tool-calling design, just fewer trips and less waiting.

## What changed in the UI

The old `weathergpt_demo.html` (from your original WeatherGPT submission) was a single dark "AI chat" page with fairly
small touch targets and no real mobile layout. The new `frontend/` is a
proper mobile-first PWA:

- **Bottom tab bar** (Chat / Forecast / Alerts) sized for thumbs, not a
  sidebar that only worked on wide screens.
- **Forecast tab**: tap-to-pick location on a live map (Leaflet), a 5-day
  temperature + rainfall chart (Chart.js), current-conditions readouts, and
  a crop advisory picker.
- **Alerts tab**: a scrolling alert ticker plus a toggle for real browser
  push-style notifications when a severe alert is fetched.
- **Voice in + voice out**: mic button for speech-to-text input, and a
  "🔊 Listen" link under replies using the browser's built-in
  text-to-speech — no external voice API needed for the MVP.
- **Multi-language**: a language selector (Hindi, Gujarati, Marathi, Tamil,
  Telugu, Kannada, Bengali, Punjabi, Malayalam, English). Gemini itself
  replies directly in the chosen language — no separate translation service
  needed for text; swap in Bhashini later only if you need Indic **speech**
  recognition beyond what Chrome's built-in speech API covers.
- **Installable PWA**: manifest + service worker cache the app shell, so it
  installs to a home screen and the interface itself loads instantly even
  on a flaky connection (live weather data still needs network, by design).
- A distinct visual identity instead of a generic dark AI-chat look: a
  "printed IMD bulletin" aesthetic (cream chart-paper background, grid
  lines, serif headings, monospace data readouts) so it doesn't look like
  every other hackathon chatbot demo.

## Running it

**Backend:**
```bash
cd backend
pip install -r requirements.txt
export GEMINI_API_KEY="AQ.your-real-key-here"     # macOS/Linux
# setx GEMINI_API_KEY "AQ.your-real-key-here"      # Windows, persists
uvicorn main:app --reload
```

**Frontend** — serve it with any static server (needed for the service
worker and microphone permission to work; opening the HTML file directly
with `file://` will disable those two features):
```bash
cd frontend
python3 -m http.server 5500
# then open http://localhost:5500 in your browser
```
On first load it'll ask for your backend's URL (default
`http://localhost:8000`) — tap the ⚙ icon any time to change it, e.g. once
you deploy the backend somewhere real.

## Fixes in this pass

- **Renamed WeatherGPT → @MausamMitra** everywhere it's user-visible: page
  title, header, PWA install prompt, manifest (so the home-screen icon
  label updates too), settings dialog, and the system prompt Gemini itself
  is given (so the assistant introduces itself with the new name).
- **Install prompt was covering the search bar / chat input.** It was a
  `position:fixed` toast anchored near the bottom of the screen, in the
  same zone as the chat input row and just above the tab bar. It's now an
  in-flow banner docked right under the header, so it pushes content down
  instead of floating on top of it — it can no longer overlap anything you
  need to tap. It also now has its own dismiss (✕) button.
- **"Backend not connected" on the Forecast tab.** This almost always means
  exactly what it says — the FastAPI server in `backend/` isn't reachable
  at the URL configured in ⚙ settings. Two things changed to make this
  clearer instead of failing silently:
  - The Forecast tab's empty state now shows "Backend not connected" *up
    front* (as soon as the periodic health check fails) with an **Open
    backend settings** button, rather than only after you search or tap
    the map and nothing happens.
  - The place-search box now shows the same message inline in its dropdown
    on a failed lookup, instead of just closing with no feedback.

  If you're still seeing it after that: confirm `uvicorn main:app --reload`
  is actually running, and — the most common gotcha — if you **installed
  the PWA on a phone**, the default backend URL `http://localhost:8000`
  refers to *that phone*, not your laptop. Deploy the backend somewhere
  reachable from the phone (or use your laptop's LAN IP, e.g.
  `http://192.168.x.x:8000`, while both are on the same Wi-Fi) and set that
  URL in ⚙ settings.

## Fixes in this second pass (map tap / speed)

- **Forecast + alerts merged into one request.** The Forecast tab used to
  fire two parallel requests (`/forecast` and `/alerts`) for the *same*
  underlying data. Besides being wasted work, `Promise.all` meant that if
  either one failed — e.g. a flaky mobile connection dropped just the
  second request — the whole update was thrown away and nothing rendered,
  which is the most likely reason tapping the map sometimes looked like it
  "did nothing." `/forecast` now returns `alerts` alongside the forecast in
  a single response, computed from data it already fetched, so there's one
  request instead of two and one less way for it to fail.
- **No more silent hang on a slow backend.** Forecast/search requests now
  time out (15s for forecast, 8s for search) with an explicit message
  instead of leaving "Loading forecast…" on screen indefinitely — a slow
  backend used to be indistinguishable from a broken one.
- **Gemini replies tuned for speed.** Thinking is turned off for the chat
  model where the SDK/model supports it (this app grounds every number in
  a tool call, not model reasoning, so extra "thinking" time doesn't buy
  accuracy here) and replies are capped to a sensible length, which speeds
  up how fast the streamed reply finishes — both changes are defensively
  guarded so they no-op instead of breaking chat if your exact model/SDK
  version doesn't support them.
- Reduced background chatter: the periodic backend health check now waits
  for the previous check to finish before firing again and backs off to
  15s, so it doesn't compete with your actual requests on a slow
  connection.

If chat replies are still slow after this, it's most likely the Gemini API
call itself (model choice, or network distance from wherever you deployed
the backend to Google's API) rather than anything in this app's code —
worth trying a smaller/faster model via the `GEMINI_MODEL` env var to
compare.

## What's still roadmap (be upfront with judges about this, same as before)

| Feature | This build | Production roadmap |
|---|---|---|
| Weather/forecast data | **Live**, Open-Meteo (GFS model output) | Direct IMD/NWP feed via WIS2.0 |
| Alerts | Rule-based engine, CAP-shaped output | Ingest NDMA SACHET CAP feed directly |
| Text multilingual | **Live** — Gemini replies natively in 9 Indian languages | Add Bhashini for languages Gemini doesn't cover well |
| Voice | **Live** — browser Web Speech API (Chrome/Edge) | Bhashini ASR/TTS for full-device + more-language coverage |
| Mobile app | **Live** — installable PWA | Native Flutter/React Native if you need deep OS integration (background push, etc.) |
| Scale | Single FastAPI instance, in-memory sessions + cache | Redis session/cache store, Kubernetes, PostgreSQL+PostGIS |

Being explicit about this table is a strength in front of judges — it shows
you understand the real system and scoped the build sensibly.
