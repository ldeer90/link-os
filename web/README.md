# LINK OS operations console

React/Vite/TypeScript console compiled into `web/dist` for FastAPI to serve at the existing LINK OS origin.

## Commands

```bash
npm install
npm run dev
npm test
npm run build
```

The Vite development server proxies `/api` to `http://127.0.0.1:8766`. Production requests are same-origin.

## API contract

The client uses versioned endpoints beneath `/api/v1`:

- `GET /overview`, `/health`, `/domains`, `/jobs`, `/campaigns`, `/replies`, `/offers`, `/listings`, `/agencies`
- `GET /imports/{id}`, `/domains/{id}`
- `POST /imports`, `/jobs/{id}/retry`, `/outreach/pause`, `/outreach/resume`
- `PATCH /offers/{id}`, `/listings/{id}`
- `GET /events` as a server-sent event stream

List views accept bare arrays or common `{items, total}` / `{data}` envelopes. Missing or unhealthy endpoints render explicit loading, empty, or error states; they never infer a healthy outreach state.
