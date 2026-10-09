export type IconName =
  | "play"
  | "pause"
  | "close"
  | "download"
  | "more"
  | "grid"
  | "list"
  | "previous"
  | "next"
  | "check"
  | "chevron-down"
  | "external"
  | "copy"
  | "expand"
  | "upload"
  | "stop"
  | "record"
  | "step-back"
  | "step-forward"
  | "zoom-in"
  | "zoom-out"
  | "snap"
  | "lock"
  | "eye"
  | "loop"
  | "home"
  | "back"
  | "rotate"
  | "camera";

const PATHS: Record<IconName, string> = {
  play: "M4.75 2.75v10.5L12.5 8z",
  pause: "M5.25 3v10M10.75 3v10",
  close: "M3.5 3.5l9 9M12.5 3.5l-9 9",
  download: "M8 2v8.5M4.25 6.75L8 10.5l3.75-3.75M2.75 13.25h10.5",
  more: "M2.75 7.25h1.5v1.5h-1.5zM7.25 7.25h1.5v1.5h-1.5zM11.75 7.25h1.5v1.5h-1.5z",
  grid: "M2.75 2.75h4v4h-4zM9.25 2.75h4v4h-4zM2.75 9.25h4v4h-4zM9.25 9.25h4v4h-4z",
  list: "M2.75 4h10.5M2.75 8h10.5M2.75 12h10.5",
  previous: "M10.25 3.25L5.5 8l4.75 4.75",
  next: "M5.75 3.25L10.5 8l-4.75 4.75",
  check: "M3.25 8.25l3 3 6.5-6.5",
  "chevron-down": "M3.25 5.75L8 10.5l4.75-4.75",
  external: "M9.25 2.75h4v4M13.25 2.75l-6 6M11.25 9.5v3.75h-8.5v-8.5h3.75",
  copy: "M5.75 5.75h7.5v7.5h-7.5zM10.25 5.75v-3h-7.5v7.5h3",
  expand: "M9.75 2.75h3.5v3.5M13.25 2.75l-4 4M6.25 13.25h-3.5v-3.5M2.75 13.25l4-4",
  upload: "M8 10.5V2M4.25 5.75L8 2l3.75 3.75M2.75 13.25h10.5",
  stop: "M3.75 3.75h8.5v8.5h-8.5z",
  record: "M8 3.25a4.75 4.75 0 1 0 0 9.5a4.75 4.75 0 1 0 0-9.5z",
  "step-back": "M3.75 3v10M12.25 3.5L6.75 8l5.5 4.5z",
  "step-forward": "M12.25 3v10M3.75 3.5L9.25 8l-5.5 4.5z",
  "zoom-in": "M7 2.75a4.25 4.25 0 1 0 0 8.5a4.25 4.25 0 1 0 0-8.5zM10.25 10.25l3 3M7 5v4M5 7h4",
  "zoom-out": "M7 2.75a4.25 4.25 0 1 0 0 8.5a4.25 4.25 0 1 0 0-8.5zM10.25 10.25l3 3M5 7h4",
  snap: "M2.75 8h4M9.25 8h4M8 2.75v10.5",
  lock: "M4.25 7.25h7.5v6h-7.5zM5.75 7.25v-2a2.25 2.25 0 0 1 4.5 0v2",
  eye: "M1.75 8s2.25-4.25 6.25-4.25S14.25 8 14.25 8s-2.25 4.25-6.25 4.25S1.75 8 1.75 8zM8 6.25a1.75 1.75 0 1 0 0 3.5a1.75 1.75 0 1 0 0-3.5z",
  loop: "M3.25 7V5.25h8.5M9.75 3.25l2 2-2 2M12.75 9v1.75h-8.5M6.25 12.75l-2-2 2-2",
  home: "M2.75 7.5L8 3l5.25 4.5M4.25 6.5v6.75h7.5V6.5",
  back: "M6.25 4L2.75 7.5l3.5 3.5M2.75 7.5h7a3.5 3.5 0 0 1 0 7h-1.5",
  rotate: "M12.75 6.25A5 5 0 1 0 13 9.5M12.75 2.75v3.5h-3.5",
  camera: "M2.75 5.25h2.5l1-1.5h3.5l1 1.5h2.5v7.5h-10.5zM8 6.75a2 2 0 1 0 0 4a2 2 0 1 0 0-4z",
};

export function Icon({ name, size = 16, className }: { name: IconName; size?: number; className?: string }) {
  return (
    <svg
      viewBox="0 0 16 16"
      width={size}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="square"
      strokeLinejoin="miter"
      aria-hidden="true"
      focusable="false"
      className={className}
    >
      <path d={PATHS[name]} />
    </svg>
  );
}
