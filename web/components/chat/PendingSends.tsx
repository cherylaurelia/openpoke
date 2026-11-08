import { useState } from 'react';

import type { Approval } from '../activity/useActivity';

interface PendingSendsProps {
  approvals: Approval[] | null;
  onDecide: (id: string, choice: 'approve' | 'decline') => Promise<string | null>;
  onOpenActivity: () => void;
}

/** Send / Don't send under the conversation, like suggested replies in a text thread. The full
 *  email and every recipient are in Activity; this is the quick yes or no. */
export function PendingSends({ approvals, onDecide, onOpenActivity }: PendingSendsProps) {
  const [busy, setBusy] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  if (!approvals?.length) return null;
  const [newest] = approvals.slice(-1);
  const more = approvals.length - 1;

  const act = async (choice: 'approve' | 'decline') => {
    setBusy(newest.id);
    setProblem(null);
    try {
      setProblem(await onDecide(newest.id, choice));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="mb-3 rounded-lg border border-brand-300 bg-brand-50 px-3 py-2">
      {problem && <p className="mb-2 text-xs text-red-700">{problem}</p>}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        {/* Never truncated: every recipient, bcc included, must be visible before Send. */}
        <p className="min-w-0 flex-1 break-words text-xs text-gray-600">
          ✉ Ready to send to <span className="font-medium text-gray-900">{newest.to}</span>
          {newest.subject && <>: {newest.subject}</>}
        </p>
        <div className="flex gap-2">
          <button className="btn px-3 py-1.5 text-xs" disabled={busy !== null} onClick={() => act('approve')}>
            {busy ? 'Sending…' : 'Send email'}
          </button>
          <button
            className="rounded-md border border-gray-200 bg-white px-3 py-1.5 text-xs hover:bg-gray-50 disabled:opacity-50"
            disabled={busy !== null}
            onClick={() => act('decline')}
          >
            Don&apos;t send
          </button>
        </div>
      </div>
      <button className="mt-1 text-xs text-gray-500 underline hover:text-gray-700" onClick={onOpenActivity}>
        {more > 0 ? `Review the full email, and ${more} more waiting, in Activity` : 'Review the full email in Activity'}
      </button>
    </div>
  );
}
