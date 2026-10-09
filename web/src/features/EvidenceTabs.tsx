import { Tabs } from "../components/Misc";

export function EvidenceTabs({ active }: { active: "sessions" | "projects" | "search" }) {
  return (
    <Tabs
      label="Evidence"
      items={[
        { label: "Sessions", to: "/evidence", active: active === "sessions" },
        { label: "Projects", to: "/projects", active: active === "projects" },
        { label: "Search", to: "/search", active: active === "search" },
      ]}
    />
  );
}
