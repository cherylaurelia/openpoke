// Forwards Activity panel requests to the Python server. Only the activity, reminders and
// approvals endpoints pass through, so this can't become a general proxy to the backend.
const serverBase = (process.env.PY_SERVER_URL || 'http://localhost:8001').replace(/\/$/, '');
const ALLOWED = /^(activity(\/[^/]+)?|reminders|reminders\/\d+\/(cancel|resume)|followups|followups\/f[a-f0-9]+\/stop|approvals|approvals\/p[a-f0-9]+\/(approve|decline))$/;

async function forward(method: 'GET' | 'POST', path: string[]) {
  const joined = path.join('/');
  const encoded = path.map(encodeURIComponent).join('/');
  if (!ALLOWED.test(joined)) {
    return Response.json({ error: 'Not found' }, { status: 404 });
  }
  try {
    const res = await fetch(`${serverBase}/api/v1/${encoded}`, {
      method,
      headers: { Accept: 'application/json' },
      cache: 'no-store',
    });
    return new Response((await res.text()) || '{}', {
      status: res.status,
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
    });
  } catch (error: any) {
    return Response.json({ error: error?.message || 'Failed to reach Python server' }, { status: 502 });
  }
}

export async function GET(_req: Request, { params }: { params: { path: string[] } }) {
  return forward('GET', params.path);
}

export async function POST(_req: Request, { params }: { params: { path: string[] } }) {
  return forward('POST', params.path);
}
