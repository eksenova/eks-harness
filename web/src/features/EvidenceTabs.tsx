import { Tabs } from "../components/Misc";

export function EvidenceTabs({ active }: { active: "sessions" | "projects" | "search" | "cleanup" }) {
  return (
    <Tabs
      label="Evidence"
      items={[
        { label: "Projects", to: "/evidence", active: active === "projects" },
        { label: "Sessions", to: "/sessions", active: active === "sessions" },
        { label: "Search", to: "/search", active: active === "search" },
        { label: "Clean up", to: "/cleanup", active: active === "cleanup" },
      ]}
    />
  );
}
