import { useQueryClient } from "@tanstack/react-query";
import { useState, type CSSProperties } from "react";
import { errorText } from "../api/client";
import { useTagCatalog } from "../api/queries";
import { Button } from "../components/Button";
import { Dialog } from "../components/Dialog";
import { TagToken } from "../components/Misc";
import { EmptyState, Notice } from "../components/Notice";
import { formatCount } from "../lib/format";
import { TAG_PALETTE } from "../lib/tags";
import { setTagColor } from "./artifactActions";

export function TagColorDialog({ tag, onClose }: { tag: string; onClose: () => void }) {
  const client = useQueryClient();
  const catalog = useTagCatalog();
  const info = catalog.data?.items.find((item) => item.tag === tag);
  const [color, setColor] = useState<string | null>(info?.color ?? null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const label = info?.label ?? tag;
  const fallback = info?.defaultColor ?? null;

  const save = async (value: string | null) => {
    setBusy(true);
    setError("");
    try {
      await setTagColor(client, tag, value);
      onClose();
    } catch (err) {
      setError(errorText(err, "save", "the tag color"));
      setBusy(false);
    }
  };

  return (
    <Dialog
      title={`Color of ${label}`}
      onClose={onClose}
      busy={busy}
      onSubmit={() => void save(color === fallback ? null : color)}
      footer={
        <>
          <Button onClick={() => void save(null)} disabled={busy || (info?.color ?? null) === fallback}>
            {fallback ? "Reset to default" : "Remove color"}
          </Button>
          <span className="toolbar-spacer" />
          <Button onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" busy={busy} busyLabel={"Saving…"} disabled={!color}>
            Save
          </Button>
        </>
      }
    >
      <div className="stack">
        <p className="field-helper">The color applies to this tag in every project and session.</p>
        <div className="tag-swatches" role="group" aria-label="Colors">
          {TAG_PALETTE.map((swatch) => (
            <button
              key={swatch}
              type="button"
              className="tag-swatch-option"
              style={{ "--tag-color": swatch } as CSSProperties}
              aria-label={swatch}
              aria-pressed={color === swatch}
              onClick={() => setColor(swatch)}
            />
          ))}
          <label className="tag-swatch-custom">
            <span>Custom</span>
            <input type="color" value={color ?? fallback ?? TAG_PALETTE[0]} onChange={(event) => setColor(event.target.value.toLowerCase())} />
          </label>
        </div>
        <div className="tag-preview" style={color ? ({ "--tag-color": color } as CSSProperties) : undefined}>
          <span className="tag" data-colored={color ? "" : undefined}>
            {color ? <span className="tag-swatch" aria-hidden="true" /> : null}
            <span className="tag-label">{label}</span>
          </span>
          <span className="mono muted">{color ?? "no color"}</span>
        </div>
        {error ? <Notice variant="error">{error}</Notice> : null}
      </div>
    </Dialog>
  );
}

export function TagColorsDialog({ editable, onClose }: { editable: boolean; onClose: () => void }) {
  const catalog = useTagCatalog();
  const [editing, setEditing] = useState<string | null>(null);
  const items = catalog.data?.items ?? [];
  if (editing) return <TagColorDialog tag={editing} onClose={() => setEditing(null)} />;
  return (
    <Dialog
      title="Tags"
      onClose={onClose}
      footer={
        <Button variant="primary" onClick={onClose}>
          Done
        </Button>
      }
    >
      {catalog.isError ? <Notice variant="error">{errorText(catalog.error, "load", "the tags")}</Notice> : null}
      {!items.length && !catalog.isPending ? <EmptyState>No tags yet.</EmptyState> : null}
      <ul className="tag-catalog">
        {items.map((item) => (
          <li key={item.tag} className="tag-catalog-row">
            <TagToken tag={item.tag} onColor={editable ? () => setEditing(item.tag) : undefined} />
            <span className="muted">{item.builtin ? `Built in: ${item.description}` : "Custom"}</span>
            <span className="muted tag-catalog-count">{formatCount(item.count)}</span>
          </li>
        ))}
      </ul>
      {editable ? <p className="field-helper">Select a tag's swatch to change its color in every project and session.</p> : null}
    </Dialog>
  );
}
