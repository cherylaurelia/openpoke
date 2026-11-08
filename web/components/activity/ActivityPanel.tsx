import clsx from 'clsx';
import { useState } from 'react';

import { Approval, FollowUp, Reminder, Task, TaskEvent, clock, useActivity, useTaskDetail, when } from './useActivity';

type Activity = ReturnType<typeof useActivity>;

/** The trust layer behind the chat: what needs you, what's coming up, and what was actually done. */
export function ActivityPanel({ activity, onClose }: { activity: Activity; onClose: () => void }) {
  const [openTask, setOpenTask] = useState<string | null>(null);

  return (
    <aside className="card flex h-full flex-col overflow-hidden">
      <div className="flex items-center justify-between border-b border-gray-200 px-4 py-3">
        {openTask ? (
          <button className="text-sm font-medium text-brand-900 hover:underline" onClick={() => setOpenTask(null)}>
            ← Activity
          </button>
        ) : (
          <h2 className="text-sm font-semibold">Activity</h2>
        )}
        <button aria-label="Close activity" className="rounded p-1 text-gray-500 hover:bg-gray-100" onClick={onClose}>
          ✕
        </button>
      </div>

      <div className="flex-1 overflow-y-auto overscroll-contain px-4 py-3">
        {activity.error && (
          <p className="mb-3 rounded-md bg-red-50 px-3 py-2 text-xs text-red-700">
            Can&apos;t reach the server right now. Showing the last update.
          </p>
        )}
        {openTask ? (
          <TaskTimeline id={openTask} />
        ) : (
          <>
            <NeedsYou approvals={activity.approvals} onDecide={activity.decide} />
            <WaitingOn followUps={activity.followUps} onStop={activity.stopFollowUp} />
            <ComingUp reminders={activity.reminders} onChange={activity.setReminder} />
            <Recent tasks={activity.tasks} onOpen={setOpenTask} />
          </>
        )}
      </div>
    </aside>
  );
}

function SectionTitle({ children }: { children: string }) {
  return <h3 className="mb-2 mt-1 text-xs font-semibold uppercase tracking-[0.05em] text-gray-500">{children}</h3>;
}

/** Long text shows a few lines and a button for the rest, so nothing is cut off out of reach. */
function Expandable({ text, lines, more, className }: { text: string; lines: 3 | 4; more: string; className: string }) {
  const [open, setOpen] = useState(false);
  const long = text.length > 160 || text.split('\n').length > lines;
  const clamp = lines === 3 ? 'line-clamp-3' : 'line-clamp-4';
  return (
    <div>
      <p className={clsx(className, 'whitespace-pre-wrap', long && !open && clamp)}>{text}</p>
      {long && (
        <button
          type="button"
          aria-expanded={open}
          className="mt-1 text-xs font-medium text-brand-900 hover:underline"
          onClick={() => setOpen(v => !v)}
        >
          {open ? 'Show less' : more}
        </button>
      )}
    </div>
  );
}

function NeedsYou({ approvals, onDecide }: { approvals: Approval[] | null; onDecide: Activity['decide'] }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  if (!approvals?.length && !problem) return null; // only takes space when something is waiting

  const act = async (id: string, choice: 'approve' | 'decline') => {
    setBusy(id);
    setProblem(null);
    try {
      setProblem(await onDecide(id, choice));
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="mb-6">
      <SectionTitle>Needs you</SectionTitle>
      {problem && <p className="mb-2 rounded-md bg-red-50 px-3 py-2 text-xs text-red-700">{problem}</p>}
      <ul className="space-y-3">
        {(approvals ?? []).map(a => (
          <li key={a.id} className="rounded-lg border border-brand-300 bg-brand-50 p-3">
            <p className="text-xs text-gray-600">
              ✉ Ready to send to <span className="font-medium text-gray-900">{a.to}</span>
            </p>
            {a.subject && <p className="mt-1 text-sm font-medium text-gray-900">{a.subject}</p>}
            {a.preview && (
              <div className="mt-1">
                <Expandable text={a.preview} lines={4} more="Show full email" className="text-sm text-gray-700" />
              </div>
            )}
            <div className="mt-3 flex gap-3">
              <button className="btn px-3 py-1.5 text-xs" disabled={busy === a.id} onClick={() => act(a.id, 'approve')}>
                {busy === a.id ? 'Sending…' : 'Send'}
              </button>
              <button
                className="rounded-md border border-gray-200 bg-white px-3 py-1.5 text-xs hover:bg-gray-50 disabled:opacity-50"
                disabled={busy === a.id}
                onClick={() => act(a.id, 'decline')}
              >
                Don&apos;t send
              </button>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** "today", "tomorrow" or "Mon". Not a clock time: checks wait out quiet hours (10pm to 8am). */
function day(iso: string): string {
  const d = new Date(iso);
  const days = Math.round((new Date(d.toDateString()).getTime() - new Date(new Date().toDateString()).getTime()) / 864e5);
  if (days <= 0) return 'today';
  if (days === 1) return 'tomorrow';
  return new Intl.DateTimeFormat(undefined, { weekday: 'short' }).format(d);
}

// What needs you first, then what's still open, then replies that just came in.
const FOLLOW_UP_ORDER = { nudge_ready: 0, waiting: 1, nudged: 1, replied: 2 } as const;

function followUpLine(f: FollowUp): string {
  if (f.status === 'nudge_ready') return 'A short nudge is ready in Needs you.';
  if (f.status === 'nudged') return `Nudged. Stops waiting ${day(f.check_in)}.`;
  if (f.status === 'replied') return f.reply ? `Replied: “${f.reply}”` : 'Replied.';
  return `Sent ${when(f.since)}. No reply by ${day(f.check_in)} and a nudge is drafted.`;
}

/** People you're waiting on. Poke checks for their reply and nudges once, only with your OK. */
function WaitingOn({ followUps, onStop }: { followUps: FollowUp[] | null; onStop: Activity['stopFollowUp'] }) {
  const [busy, setBusy] = useState<string | null>(null);
  return (
    <section className="mb-6">
      <SectionTitle>No reply yet</SectionTitle>
      {followUps === null ? (
        <Loading />
      ) : followUps.length === 0 ? (
        <p className="text-sm text-gray-500">
          When you email someone, their reply is tracked here. No reply in 2 days and a nudge is drafted for you.
        </p>
      ) : (
        <ul className="space-y-3">
          {[...followUps].sort((a, b) => FOLLOW_UP_ORDER[a.status] - FOLLOW_UP_ORDER[b.status]).map(f => (
            <li key={f.id} className="flex items-start justify-between gap-3 rounded-lg border border-gray-200 p-3">
              <div className="min-w-0">
                <p className="truncate text-sm font-medium text-gray-900" title={f.who}>
                  {f.status === 'replied' && <span className="text-green-600">✓ </span>}
                  {f.who}
                </p>
                <p className="truncate text-xs text-gray-500" title={f.subject}>{f.subject}</p>
                <p className={clsx('mt-1 text-xs', f.status === 'replied' ? 'text-gray-900' : 'text-gray-600')}>
                  {followUpLine(f)}
                </p>
              </div>
              {f.status !== 'replied' && (
                <button
                  className="shrink-0 rounded-md border border-gray-200 px-2.5 py-1 text-xs hover:bg-gray-50 disabled:opacity-50"
                  disabled={busy === f.id}
                  onClick={async () => {
                    setBusy(f.id);
                    try {
                      await onStop(f.id);
                    } finally {
                      setBusy(null);
                    }
                  }}
                >
                  {busy === f.id ? 'Stopping…' : 'Stop'}
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

const plain = (text: string) =>
  text.replace(/\bthe user's\b/gi, 'your').replace(/\bthe user\b/gi, 'you').replace(/^./, c => c.toUpperCase());

function ComingUp({ reminders, onChange }: { reminders: Reminder[] | null; onChange: Activity['setReminder'] }) {
  const [busy, setBusy] = useState<number | null>(null);
  const [cancelled, setCancelled] = useState<Reminder[]>([]); // kept on screen so Cancel can be undone

  const run = async (r: Reminder, action: 'cancel' | 'resume') => {
    setBusy(r.id);
    try {
      await onChange(r.id, action);
      setCancelled(list => (action === 'cancel' ? [...list, r] : list.filter(x => x.id !== r.id)));
    } finally {
      setBusy(null);
    }
  };

  const active = (reminders ?? []).filter(r => !cancelled.some(c => c.id === r.id));
  return (
    <section className="mb-6">
      <SectionTitle>Coming up</SectionTitle>
      {reminders === null ? (
        <Loading />
      ) : active.length === 0 && cancelled.length === 0 ? (
        <p className="text-sm text-gray-500">
          Nothing scheduled. Try texting <span className="italic">“remind me every Friday to pay rent”</span>.
        </p>
      ) : (
        <ul className="space-y-3">
          {active.map(r => (
            <li key={r.id} className="rounded-lg border border-gray-200 p-3">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  {r.state === 'didnt_finish' && (
                    <p className="text-xs font-medium text-amber-700">⚠ Didn&apos;t finish last time</p>
                  )}
                  {r.state === 'running' ? (
                    <p className="text-xs font-medium text-brand-900">● Running now</p>
                  ) : (
                    r.next_run && (
                      <p className="text-xs tabular-nums text-gray-500">
                        ⏰ {r.state === 'didnt_finish' ? 'Next: ' : ''}
                        {when(r.next_run, true)}
                        {r.repeats && ` · ${r.repeats}`}
                      </p>
                    )
                  )}
                  <p className="mt-1 text-sm text-gray-900">{plain(r.what)}</p>
                  {r.state === 'didnt_finish' && r.last_error && <p className="mt-1 text-xs text-gray-600">{r.last_error}</p>}
                </div>
                <button
                  className="shrink-0 rounded-md border border-gray-200 px-2.5 py-1 text-xs hover:bg-gray-50 disabled:opacity-50"
                  disabled={busy === r.id}
                  onClick={() => run(r, 'cancel')}
                >
                  {busy === r.id ? 'Cancelling…' : 'Cancel'}
                </button>
              </div>
            </li>
          ))}
          {cancelled.map(r => (
            <li key={r.id} className="flex items-center justify-between gap-3 rounded-lg bg-gray-50 px-3 py-2">
              <p className="min-w-0 truncate text-sm text-gray-600">
                Cancelled: <span className="text-gray-900">{plain(r.what)}</span>
              </p>
              <button
                className="shrink-0 text-xs font-medium text-brand-900 hover:underline disabled:opacity-50"
                disabled={busy === r.id}
                onClick={() => run(r, 'resume')}
              >
                Undo
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

const STATUS = {
  working: { icon: '●', label: 'Working', className: 'text-brand-800 motion-safe:animate-pulse' },
  done: { icon: '✓', label: 'Done', className: 'text-green-600' },
  failed: { icon: '✕', label: "Couldn't finish", className: 'text-red-600' },
} as const;

function Recent({ tasks, onOpen }: { tasks: Task[] | null; onOpen: (id: string) => void }) {
  return (
    <section>
      <SectionTitle>Recent</SectionTitle>
      {tasks === null ? (
        <Loading />
      ) : tasks.length === 0 ? (
        <p className="text-sm text-gray-500">No tasks yet. Ask for something in the chat and it shows up here.</p>
      ) : (
        <ul className="divide-y divide-gray-100">
          {tasks.map(t => {
            const s = STATUS[t.status];
            return (
              <li key={t.id}>
                <button className="flex w-full items-start gap-2 py-2.5 text-left hover:bg-gray-50" onClick={() => onOpen(t.id)}>
                  <span aria-hidden className={clsx('mt-0.5 w-4 text-center text-sm', s.className)}>
                    {s.icon}
                  </span>
                  <span className="sr-only">{s.label}:</span>
                  <span className="min-w-0 flex-1">
                    <span className="flex items-baseline justify-between gap-2">
                      <span className="truncate text-sm font-medium text-gray-900">
                        {t.title}
                        {t.sent_something && <span title="Sent something on your behalf"> ✉</span>}
                      </span>
                      <span className="shrink-0 text-xs tabular-nums text-gray-500">{when(t.last_activity)}</span>
                    </span>
                    <span className="block truncate text-xs text-gray-500">{t.latest}</span>
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

interface Run {
  at: string;
  request: string | null; // null for anything logged before the first request
  steps: (TaskEvent & { count: number })[];
  result: TaskEvent | null;
}

/** One block per request: what was asked, the steps taken, and how it ended. */
function toRuns(events: TaskEvent[]): Run[] {
  const runs: Run[] = [];
  for (const e of events) {
    if (e.kind === 'asked' || runs.length === 0) {
      runs.push({ at: e.at, request: e.kind === 'asked' ? e.text : null, steps: [], result: null });
      if (e.kind === 'asked') continue;
    }
    const run = runs[runs.length - 1];
    if (e.kind === 'summary' || e.kind === 'run_failed') {
      run.result = e;
      continue;
    }
    const last = run.steps[run.steps.length - 1];
    if (last && last.kind === e.kind && last.text === e.text) last.count += 1;
    else run.steps.push({ ...e, count: 1 });
  }
  return runs;
}

const STEP_STYLE: Partial<Record<TaskEvent['kind'], string>> = {
  sent: 'text-sm font-semibold text-gray-900',
  send_failed: 'text-xs text-red-700',
  failed: 'text-xs text-red-700',
};

function TaskTimeline({ id }: { id: string }) {
  const detail = useTaskDetail(id);
  if (!detail) return <Loading />;
  const runs = toRuns(detail.events);

  return (
    <div>
      <h3 className="text-sm font-semibold text-gray-900">{detail.title}</h3>
      {detail.description && <p className="mt-0.5 text-xs text-gray-500">{detail.description}</p>}

      <ol className="mt-5 space-y-6">
        {runs.map((run, i) => {
          const isLast = i === runs.length - 1;
          return (
            <li key={`${run.at}-${i}`}>
              <div className="flex items-baseline justify-between gap-3">
                {/* Poke's instructions can run long; two lines say what it was, the raw log has all of it. */}
                <p className="line-clamp-2 text-sm font-medium text-gray-900" title={run.request ?? undefined}>
                  {run.request ?? 'Earlier activity'}
                </p>
                <time className="shrink-0 text-xs tabular-nums text-gray-500">{clock(run.at)}</time>
              </div>
              {run.steps.length > 0 && (
                <ul className="mt-2 space-y-1">
                  {run.steps.map((s, j) => (
                    <li key={j} className={STEP_STYLE[s.kind] ?? 'text-xs text-gray-600'}>
                      {s.kind === 'sent' && '✉ '}
                      {s.text}
                      {s.count > 1 && ` · ${s.count}×`}
                    </li>
                  ))}
                </ul>
              )}
              <Outcome run={run} pending={isLast ? detail.status : 'failed'} />
            </li>
          );
        })}
      </ol>

      <details className="mt-8">
        <summary className="cursor-pointer text-xs text-gray-500 hover:text-gray-700">Raw log</summary>
        <pre className="mt-2 max-h-64 overflow-auto rounded-md bg-gray-50 p-2 text-xs leading-snug text-gray-600">
          {detail.technical.map(t => `${t.at}  ${t.tag}  ${t.text}`).join('\n')}
        </pre>
      </details>
    </div>
  );
}

/** How a request ended: the result, the reason it failed, or that it's still going. */
function Outcome({ run, pending }: { run: Run; pending: Task['status'] }) {
  const failed = run.result?.kind === 'run_failed' || (!run.result && pending === 'failed');
  if (!run.result && pending === 'working') {
    return <p className="mt-2 text-sm text-brand-900 motion-safe:animate-pulse">● Working on it</p>;
  }
  if (!run.result && pending === 'done') return null; // finished by an approval or a reminder, no summary
  const text = run.result?.text ?? "Didn't finish. Ask again in the chat.";
  return (
    <div className="mt-2 flex gap-2">
      <span aria-hidden className={clsx('text-sm', failed ? 'text-red-600' : 'text-green-600')}>
        {failed ? '✕' : '✓'}
      </span>
      <span className="sr-only">{failed ? "Couldn't finish:" : 'Result:'}</span>
      <div className="min-w-0 flex-1">
        <Expandable
          text={text}
          lines={3}
          more="Show full result"
          className={clsx('text-sm', failed ? 'text-red-700' : 'text-gray-900')}
        />
        {failed && run.steps.some(s => s.kind === 'sent') && (
          // "Ask again" alone would send it twice: this is the case the panel exists for.
          <p className="mt-1 text-sm font-medium text-gray-900">
            The email above was already sent. Check your Sent folder before asking again.
          </p>
        )}
      </div>
    </div>
  );
}

function Loading() {
  return <p className="text-sm text-gray-500">Loading…</p>;
}
