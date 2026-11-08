import { useCallback, useEffect, useState } from 'react';

export type TaskStatus = 'working' | 'done' | 'failed';

export interface Task {
  id: string;
  title: string;
  status: TaskStatus;
  latest: string;
  last_activity: string;
  sent_something: boolean;
}

export interface TaskEvent {
  at: string;
  kind: 'asked' | 'approved' | 'declined' | 'waiting' | 'action' | 'sent' | 'send_failed' | 'failed' | 'run_failed' | 'summary';
  text: string;
}

export interface TaskDetail {
  id: string;
  title: string;
  description: string;
  status: TaskStatus;
  events: TaskEvent[];
  technical: { at: string; tag: string; text: string }[];
}

export interface Reminder {
  id: number;
  what: string;
  next_run: string | null;
  repeats: string | null;
  state: 'scheduled' | 'running' | 'didnt_finish';
  last_error: string | null;
}

export interface FollowUp {
  id: string;
  who: string;
  subject: string;
  since: string;
  check_in: string;
  status: 'waiting' | 'nudge_ready' | 'nudged' | 'replied';
  reply: string;
}

export interface Approval {
  id: string;
  to: string;
  subject: string;
  preview: string;
}

const POLL_MS = 4000; // panel open
const SEEN_KEY = 'activity-seen-at';
const BACKGROUND_POLL_MS = 15000; // panel closed: just enough to keep the badge current

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`/api/py/${path}`, { cache: 'no-store' });
  if (!res.ok) throw new Error(`Request failed (${res.status})`);
  return res.json();
}

/** Tasks and reminders, refreshed every few seconds. Polls even while the panel is closed,
 *  so the header badge can say when something needs a look. */
export function useActivity(open: boolean) {
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [reminders, setReminders] = useState<Reminder[] | null>(null);
  const [approvals, setApprovals] = useState<Approval[] | null>(null);
  const [followUps, setFollowUps] = useState<FollowUp[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // When the panel was last open: a failure the user has already seen stops counting in the badge.
  const [seenAt, setSeenAt] = useState(() => {
    try {
      return Number(window.localStorage.getItem(SEEN_KEY)) || 0;
    } catch {
      return 0;
    }
  });

  const refresh = useCallback(async () => {
    try {
      const [t, r, a, f] = await Promise.all([
        getJson<{ tasks: Task[] }>('activity'),
        getJson<{ reminders: Reminder[] }>('reminders'),
        getJson<{ approvals: Approval[] }>('approvals'),
        getJson<{ followups: FollowUp[] }>('followups'),
      ]);
      setTasks(t.tasks);
      setReminders(r.reminders);
      setApprovals(a.approvals);
      setFollowUps(f.followups);
      setError(null);
    } catch (err: any) {
      setError(err?.message || 'Could not load activity');
    }
  }, []);

  useEffect(() => {
    void refresh();
    const id = window.setInterval(() => void refresh(), open ? POLL_MS : BACKGROUND_POLL_MS);
    return () => window.clearInterval(id);
  }, [refresh, open]);

  useEffect(() => {
    if (!open) return;
    const markSeen = () => {
      const now = Date.now();
      setSeenAt(now);
      try {
        window.localStorage.setItem(SEEN_KEY, String(now));
      } catch {}
    };
    markSeen();
    return markSeen; // also on close, so what was on screen counts as seen
  }, [open]);

  const setReminder = useCallback(
    async (id: number, action: 'cancel' | 'resume') => {
      const res = await fetch(`/api/py/reminders/${id}/${action}`, { method: 'POST' });
      if (!res.ok) throw new Error(action === 'cancel' ? 'Could not cancel' : 'Could not undo');
      await refresh();
    },
    [refresh],
  );

  const stopFollowUp = useCallback(
    async (id: string) => {
      const res = await fetch(`/api/py/followups/${id}/stop`, { method: 'POST' });
      if (!res.ok && res.status !== 404) throw new Error('Could not stop');
      await refresh();
    },
    [refresh],
  );

  /** Returns why it didn't go through, or null. A send that fails is never retried here. */
  const decide = useCallback(
    async (id: string, choice: 'approve' | 'decline'): Promise<string | null> => {
      const res = await fetch(`/api/py/approvals/${id}/${choice}`, { method: 'POST' });
      const body = await res.json().catch(() => ({}));
      await refresh();
      if (res.status === 409) return null; // already handled elsewhere (chat or another tab)
      if (!res.ok) return body.error || 'Could not update. Nothing was sent.'; // the server's error handler uses "error"
      return body.status === 'failed' ? `Couldn't send: ${body.result}` : null;
    },
    [refresh],
  );

  // What needs a look: emails waiting for you, reminders that didn't run, and failures since you last looked.
  const problems =
    (approvals?.length ?? 0) +
    (reminders?.filter(r => r.state === 'didnt_finish').length ?? 0) +
    (tasks?.filter(t => t.status === 'failed' && logTime(t.last_activity) > seenAt).length ?? 0);

  return { tasks, reminders, approvals, followUps, error, problems, refresh, setReminder, stopFollowUp, decide };
}

export function useTaskDetail(id: string | null) {
  const [detail, setDetail] = useState<TaskDetail | null>(null);

  useEffect(() => {
    setDetail(null);
    if (!id) return;
    let alive = true;
    const load = () =>
      getJson<TaskDetail>(`activity/${id}`)
        .then(d => alive && setDetail(d))
        .catch(() => {});
    void load();
    const timer = window.setInterval(load, POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [id]);

  return detail;
}

/** Log times are local "YYYY-MM-DD HH:MM:SS"; reminder times are ISO. */
function logTime(value: string): number {
  return new Date(value.includes('T') ? value : value.replace(' ', 'T')).getTime();
}

/** "9:34 PM" today, "Nov 6, 9:34 PM" otherwise, in the reader's locale. */
export function clock(value: string): string {
  const date = new Date(logTime(value));
  if (Number.isNaN(date.getTime())) return value;
  const today = date.toDateString() === new Date().toDateString();
  return new Intl.DateTimeFormat(undefined, {
    ...(today ? {} : { month: 'short', day: 'numeric' }),
    hour: 'numeric',
    minute: '2-digit',
  }).format(date);
}

/** "2m ago", "Tue 10:02", "Nov 3". Log times are local "YYYY-MM-DD HH:MM:SS"; reminder times are ISO. */
export function when(value: string | null, future = false): string {
  if (!value) return '';
  const date = new Date(logTime(value));
  if (Number.isNaN(date.getTime())) return value;
  const diffMin = Math.round((date.getTime() - Date.now()) / 60000);
  if (!future && diffMin > -1) return 'just now';
  if (!future && diffMin > -60) return `${-diffMin}m ago`;
  if (!future && diffMin > -24 * 60) return `${Math.round(-diffMin / 60)}h ago`;
  const sameWeek = Math.abs(diffMin) < 6 * 24 * 60;
  const time = date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  if (sameWeek) return `${date.toLocaleDateString([], { weekday: 'short' })} ${time}`;
  return date.toLocaleDateString([], { month: 'short', day: 'numeric' });
}
