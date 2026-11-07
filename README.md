# SchoolHub on Google AI Studio

This repository is optimized to run out-of-the-box in **Google AI Studio (Build mode)** as well as **Google Cloud Run** and standard Node/Python environments.

## Quick Start in Google AI Studio

1. Open [Google AI Studio](https://aistudio.google.com/) and navigate to **Build** (or Create App).
2. Click the **+** (Add files) button in the prompt / file explorer area and select **Upload file as ZIP**.
3. Upload the single ZIP archive of this project (make sure the project files like `package.json`, `server.js`, and `app.py` are at the root level of the ZIP).
4. Google AI Studio will automatically run `npm run dev` (`node server.js`), binding uvicorn to `0.0.0.0:3000`.
5. The live interactive preview will automatically load inside the AI Studio preview pane.

## How It Works

- **`package.json`**: Provides standard `dev`, `start`, and `build` scripts that Google AI Studio's Cloud Run harness expects.
- **`server.js`**: Automatically detects the Python runtime, verifies that required packages (`fastapi`, `uvicorn`, `httpx`, `websockets`, etc.) are installed (or installs them from `requirements.txt`), and launches `uvicorn app:app` bound to `0.0.0.0:3000` (or the environment `PORT`).
- **`Procfile`**: Defines the web process (`web: node server.js`) for one-click Cloud Run deployment directly from AI Studio.
- **Optimized JSON footprints**: Log stores (`activity_events.json`, `openchat_history.json`) are kept lean to prevent context window overflow and serialization errors in AI Studio.

## Local Development

```bash
# Using Node.js runner:
npm install
npm run dev

# Or directly with Python / Uvicorn:
pip install -r requirements.txt
python3 -m uvicorn app:app --host 0.0.0.0 --port 3000
```
