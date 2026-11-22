// A value kept in the page's address (slice 9.5a; its own module since 21.6d, for the pages'
// filter chips): a link to the page opens it as it was left.
import { useState } from "react";

export function useParam(name: string): [string, (v: string) => void] {
  const [value, set] = useState(() => new URLSearchParams(location.search).get(name) ?? "");
  const update = (v: string) => {
    const q = new URLSearchParams(location.search);
    if (v) q.set(name, v);
    else q.delete(name);
    history.replaceState(null, "", `${location.pathname}?${q}`);
    set(v);
  };
  return [value, update];
}
