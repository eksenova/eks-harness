import { useMemo } from "react";
import { useTagCatalog } from "../api/queries";
import type { TagInfo } from "../api/types";

export interface TagLook {
  label: string;
  color: string | null;
  builtin: boolean;
  description: string;
}

export const TAG_PALETTE = [
  "#2f6fdb", "#4c8bf5", "#0f9d8a", "#3a9e4f", "#7cb342", "#e0a100",
  "#ef8f00", "#e2572a", "#d93f5c", "#c2185b", "#7b4fd6", "#5f6b7a",
];

export function useTagLook(): (tag: string) => TagLook {
  const catalog = useTagCatalog();
  const index = useMemo(() => {
    const map = new Map<string, TagInfo>();
    for (const item of catalog.data?.items ?? []) map.set(item.tag, item);
    return map;
  }, [catalog.data]);
  return (tag: string) => {
    const info = index.get(tag);
    return { label: info?.label ?? tag, color: info?.color ?? null, builtin: Boolean(info?.builtin), description: info?.description ?? "" };
  };
}

export function parseTagList(value: string | undefined): string[] {
  return Array.from(new Set((value ?? "").split(",").map((t) => t.trim()).filter(Boolean)));
}
