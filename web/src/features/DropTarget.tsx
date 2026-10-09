import { useState, type ReactNode } from "react";
import { filesFromDrop, type PickedFile } from "./UploadDialog";

export function DropTarget({ label, enabled, onDrop, children }: { label: string; enabled: boolean; onDrop: (files: PickedFile[], folder: boolean) => void; children: ReactNode }) {
  const [over, setOver] = useState(0);
  const hasFiles = (event: React.DragEvent) => Array.from(event.dataTransfer.types).includes("Files");
  return (
    <div
      className={`drop-target ${over > 0 ? "is-over" : ""}`}
      onDragEnter={(event) => {
        if (!enabled || !hasFiles(event)) return;
        event.preventDefault();
        setOver((n) => n + 1);
      }}
      onDragOver={(event) => {
        if (!enabled || !hasFiles(event)) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = "copy";
      }}
      onDragLeave={() => enabled && setOver((n) => Math.max(0, n - 1))}
      onDrop={async (event) => {
        if (!enabled || !hasFiles(event)) return;
        event.preventDefault();
        setOver(0);
        const { files, folder } = await filesFromDrop(event.dataTransfer);
        if (files.length) onDrop(files, folder);
      }}
    >
      {over > 0 ? <p className="drop-label">Drop to upload to {label}</p> : null}
      {children}
    </div>
  );
}
