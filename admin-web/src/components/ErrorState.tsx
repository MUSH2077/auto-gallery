export default function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="bg-red-50 border border-red-200 rounded-lg p-6 text-center">
      <p className="text-red-700 mb-3">{message}</p>
      {onRetry && <button onClick={onRetry} className="px-4 py-2 bg-red-600 text-white rounded text-sm hover:bg-red-700">Retry</button>}
    </div>
  );
}
