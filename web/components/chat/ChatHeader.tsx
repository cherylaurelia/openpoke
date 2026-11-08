interface ChatHeaderProps {
  onOpenSettings: () => void;
  onClearHistory: () => void;
  onToggleActivity: () => void;
  activityOpen: boolean;
  problems: number;
}

export function ChatHeader({ onOpenSettings, onClearHistory, onToggleActivity, activityOpen, problems }: ChatHeaderProps) {
  return (
    <header className="mb-4 flex items-center justify-between">
      <div className="flex items-center">
        <h1 className="text-lg font-semibold">OpenPoke 🌴</h1>
      </div>
      <div className="flex items-center gap-2">
        <button
          className={`relative rounded-md border px-3 py-2 text-sm hover:bg-gray-50 ${
            activityOpen ? 'border-brand-600 text-brand-900' : 'border-gray-200'
          }`}
          onClick={onToggleActivity}
          aria-pressed={activityOpen}
        >
          Activity
          {problems > 0 && (
            <span
              className="absolute -right-1.5 -top-1.5 min-w-[18px] rounded-full bg-red-500 px-1 text-[11px] font-semibold leading-[18px] text-white"
              title={`${problems} need${problems === 1 ? 's' : ''} a look`}
            >
              {problems}
            </span>
          )}
        </button>
        <button
          className="rounded-md border border-gray-200 px-3 py-2 text-sm hover:bg-gray-50"
          onClick={onOpenSettings}
        >
          Settings
        </button>
        <button
          className="rounded-md border border-gray-200 px-3 py-2 text-sm hover:bg-gray-50"
          onClick={onClearHistory}
        >
          Clear
        </button>
      </div>
    </header>
  );
}
