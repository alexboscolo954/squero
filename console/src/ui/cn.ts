// Class names for the console's components (Phase 21, shadcn/ui's helper): later classes win.
import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export const cn = (...inputs: ClassValue[]) => twMerge(clsx(inputs));
