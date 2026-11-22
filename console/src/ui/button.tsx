// A button (Phase 21, after shadcn/ui's): one place for its variants and sizes.
import { cva, type VariantProps } from "class-variance-authority";
import type { ComponentProps } from "react";
import { cn } from "./cn";

const button = cva(
  "inline-flex shrink-0 cursor-pointer items-center justify-center gap-2 rounded-md border-0 text-sm " +
    "font-medium whitespace-nowrap transition-colors disabled:pointer-events-none disabled:opacity-50 " +
    "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent " +
    "[&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default: "bg-accent text-white hover:opacity-90",
        outline: "border border-line bg-panel text-ink hover:bg-page",
        ghost: "bg-transparent text-ink hover:bg-page",
        danger: "bg-bad text-white hover:opacity-90",
        "danger-outline": "border border-bad bg-transparent text-bad hover:bg-bad-bg",
      },
      size: { default: "h-9 px-4", sm: "h-8 px-3", icon: "size-9" },
    },
    defaultVariants: { variant: "default", size: "default" },
  },
);

export function Button({
  className,
  variant,
  size,
  type = "button",
  ...props
}: ComponentProps<"button"> & VariantProps<typeof button>) {
  return <button type={type} className={cn(button({ variant, size }), className)} {...props} />;
}
