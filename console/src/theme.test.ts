// The chart colours (slice 21.5b): a token at an opacity, and the axes the brief asks for.
import { expect, test } from "vitest";
import { alpha, axes } from "./theme";

test("a #rrggbb colour at an opacity; anything else stays as it is", () => {
  expect(alpha("#4fbfaf", 0.1)).toBe("rgba(79, 191, 175, 0.1)");
  expect(alpha("#0E1216", 1)).toBe("rgba(14, 18, 22, 1)");
  expect(alpha("var(--x)", 0.5)).toBe("var(--x)");
});

test("the axes: horizontal rules only, no ticks, the unit on each value", () => {
  const [x, y] = axes((v) => `${v} cores`);
  expect(x.grid).toEqual({ show: false });
  expect([x.ticks, y.ticks]).toEqual([{ show: false }, { show: false }]);
  expect(y.grid?.width).toBe(1);
  expect(y.values?.(null, [1, 2.5])).toEqual(["1 cores", "2.5 cores"]);
});
