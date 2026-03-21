import { useState, useRef, useEffect } from "react";
import { searchBuildings } from "../../api/client";
import type { SearchResult } from "../../types/building";

interface Props {
  onSelect: (result: SearchResult) => void;
}

export default function SearchBar({ onSelect }: Props) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [isOpen, setIsOpen] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout>>();

  useEffect(() => {
    if (query.length < 2) {
      setResults([]);
      setIsOpen(false);
      return;
    }
    clearTimeout(timerRef.current);
    timerRef.current = setTimeout(async () => {
      try {
        const data = await searchBuildings(query);
        setResults(data.results);
        setIsOpen(data.results.length > 0);
      } catch {
        setResults([]);
      }
    }, 300);
    return () => clearTimeout(timerRef.current);
  }, [query]);

  return (
    <div className="absolute top-4 left-1/2 -translate-x-1/2 z-10 w-96">
      <input
        type="text"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="건물명 또는 주소 검색..."
        className="w-full px-4 py-3 rounded-lg shadow-lg bg-white/95 backdrop-blur text-gray-800 text-sm outline-none focus:ring-2 focus:ring-blue-400"
      />
      {isOpen && (
        <ul className="mt-1 bg-white/95 backdrop-blur rounded-lg shadow-lg max-h-64 overflow-y-auto">
          {results.map((r) => (
            <li
              key={r.pnu}
              className="px-4 py-2 hover:bg-blue-50 cursor-pointer text-sm text-gray-700 border-b border-gray-100 last:border-0"
              onClick={() => {
                onSelect(r);
                setQuery(r.building_name || "");
                setIsOpen(false);
              }}
            >
              <div className="font-medium">{r.building_name || r.pnu}</div>
              <div className="text-xs text-gray-500">{r.usage_type}</div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
