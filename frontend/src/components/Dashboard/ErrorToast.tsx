import { useAppStore } from "../../store/appStore";

export default function ErrorToast() {
  const error = useAppStore((s) => s.error);
  if (!error) return null;

  return (
    <div className="absolute top-16 left-1/2 -translate-x-1/2 z-30 bg-red-600 text-white px-4 py-2 rounded-lg shadow-lg text-sm animate-fade-in">
      {error}
    </div>
  );
}
