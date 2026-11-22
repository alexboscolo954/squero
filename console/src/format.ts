const MI = 2 ** 20;
const GI = 2 ** 30;

export const cores = (v: number) => `${v.toFixed(2)} cores`;
export const gib = (v: number) => `${(v / GI).toFixed(2)} GiB`;
export const cpu = (v: number | null | undefined) =>
  v == null ? "unset" : `${Math.round(v * 1000)}m`;
export const memory = (v: number | null | undefined) =>
  v == null ? "unset" : `${Math.round(v / MI)}Mi`;
export const pct = (v: number) => `${Math.round(v * 100)}%`;

export function when(at: number | null | undefined): string {
  if (at == null) return "never";
  return new Date(at * 1000).toISOString().slice(0, 16).replace("T", " ") + " UTC";
}

export const amount = (v: number, currency: string) =>
  `${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${currency}`;
export const money = (v: number, currency: string) => `${amount(v, currency)}/month`;
/** A whole amount, for a number tile (slice 21.2); the pages give the cents. */
export const whole = (v: number, currency: string) =>
  `${v.toLocaleString("en-US", { maximumFractionDigits: 0 })} ${currency}`;

const sign = (v: number) => (v < 0 ? "-" : "+");

/** A change since the compared run: money when priced, else cores and GiB. */
export function change(c: { cost?: { requests: number; waste: number } | null; requests: { cpu?: number; memory?: number }; waste: { cpu?: number; memory?: number } }, key: "requests" | "waste", currency: string | null | undefined): string {
  if (currency && c.cost) return sign(c.cost[key]) + money(Math.abs(c.cost[key]), currency);
  const cpu = c[key].cpu ?? 0;
  const mem = c[key].memory ?? 0;
  return `${sign(cpu)}${cores(Math.abs(cpu))}, ${sign(mem)}${gib(Math.abs(mem))}`;
}

/** One number to sort by: the waste's monthly cost when the Contract has prices, else
 * cores and GiB with a core weighing as much as 4 GiB. */
export const rank = (w: {
  waste: { cpu?: number; memory?: number };
  cost?: { waste: number } | null;
}) => (w.cost ? w.cost.waste : (w.waste.cpu ?? 0) + (w.waste.memory ?? 0) / GI / 4);
