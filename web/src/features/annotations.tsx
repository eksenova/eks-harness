import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, encodeSegment, errorText, projectPath, uploadForm } from "../api/client";
import { keys, normalizeAssets, useAnnotationVersions, useProjectAssets } from "../api/queries";
import type {
  AnnotateResponse,
  AnnotationCrop,
  AnnotationMeta,
  AnnotationVersionOut,
  ArtifactOut,
  ProjectAssetOut,
} from "../api/types";
import { Button } from "../components/Button";
import { ConfirmDialog } from "../components/Dialog";
import { TextInput } from "../components/Form";
import { DefList, Mono, RelTime } from "../components/Misc";
import { Notice, ResultText } from "../components/Notice";
import { formatSize } from "../lib/format";
import { localPath } from "../lib/url";

export function annotationMeta(artifact: ArtifactOut): AnnotationMeta | null {
  const meta = (artifact.meta ?? {}) as Record<string, unknown>;
  const value = meta.annotation;
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as AnnotationMeta;
}

export function AnnotationBadge({ artifact }: { artifact: ArtifactOut }) {
  if (!annotationMeta(artifact)) return null;
  return <span className="muted">Annotated</span>;
}

function recordOf(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function textOf(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

function specItems(spec: unknown): unknown[] {
  const record = recordOf(spec);
  if (!record) return [];
  return Array.isArray(record.items) ? record.items : [];
}

interface RuleRow {
  rule: string;
  ok: boolean;
  message: string;
}

function ruleRows(report: unknown): RuleRow[] {
  const record = recordOf(report);
  const raw = record ? record.rules ?? record.results ?? [] : Array.isArray(report) ? report : [];
  if (!Array.isArray(raw)) return [];
  const rows: RuleRow[] = [];
  for (const entry of raw) {
    const row = recordOf(entry);
    if (!row) continue;
    const rule = textOf(row.rule) ?? textOf(row.id) ?? textOf(row.name);
    if (!rule) continue;
    rows.push({
      rule,
      ok: row.ok === true && row.pass !== false && row.failed !== true,
      message: textOf(row.message) ?? textOf(row.detail) ?? "",
    });
  }
  return rows;
}

interface ContrastRow {
  label: string;
  ratio: string;
}

function contrastRows(report: unknown): ContrastRow[] {
  const record = recordOf(report);
  const raw = record ? record.contrast ?? record.contrastReadings ?? [] : [];
  if (!Array.isArray(raw)) return [];
  const rows: ContrastRow[] = [];
  for (const entry of raw) {
    const row = recordOf(entry);
    if (!row) continue;
    const ratio = row.ratio ?? row.value;
    if (typeof ratio !== "number" && typeof ratio !== "string") continue;
    rows.push({ label: textOf(row.itemId) ?? textOf(row.rule) ?? "Text", ratio: String(ratio) });
  }
  return rows;
}

export function metaCrops(meta: AnnotationMeta): AnnotationCrop[] {
  if (!Array.isArray(meta.crops)) return [];
  const crops: AnnotationCrop[] = [];
  for (const entry of meta.crops) {
    const row = recordOf(entry);
    if (!row) continue;
    const url = textOf(row.url) ?? textOf(row.rawUrl);
    if (!url) continue;
    crops.push({
      itemId: textOf(row.itemId) ?? textOf(row.id) ?? `Crop ${crops.length + 1}`,
      url,
      rawUrl: textOf(row.rawUrl) ?? url,
    });
  }
  return crops;
}

export interface AnnotationState {
  meta: AnnotationMeta | null;
  crops: AnnotationCrop[];
  storedCropCount: number;
  versions: AnnotationVersionOut[];
  versionsPending: boolean;
  last: AnnotateResponse | null;
  busy: "rerender" | "restore" | null;
  error: string;
  result: string;
  rerender: (style?: string) => Promise<void>;
  restore: (version: number) => Promise<boolean>;
}

export function useAnnotationState(artifact: ArtifactOut | null): AnnotationState {
  const client = useQueryClient();
  const id = artifact?.id ?? null;
  const versionsQuery = useAnnotationVersions(id);
  const [last, setLast] = useState<AnnotateResponse | null>(null);
  const [busy, setBusy] = useState<"rerender" | "restore" | null>(null);
  const [error, setError] = useState("");
  const [result, setResult] = useState("");

  useEffect(() => {
    setLast(null);
    setError("");
    setResult("");
  }, [id]);

  const meta = artifact ? annotationMeta(artifact) : null;
  const stored = meta ? (Array.isArray(meta.crops) ? meta.crops.length : 0) : 0;
  const crops = last?.crops?.length ? last.crops : meta ? metaCrops(meta) : [];

  const refresh = async () => {
    if (id) {
      await client.invalidateQueries({ queryKey: keys.artifact(id) });
      await client.invalidateQueries({ queryKey: keys.annotations(id) });
    }
  };

  const rerender = async (style?: string) => {
    if (!id) return;
    setBusy("rerender");
    setError("");
    setResult("");
    try {
      const response = await api.post<AnnotateResponse>(`/api/artifacts/${encodeSegment(id)}/annotate/rerender`, style ? { style } : {});
      setLast(response);
      setResult(`Re-rendered as version ${response.version}.`);
      await refresh();
    } catch (err) {
      setError(errorText(err, "re-render", "the annotation"));
    } finally {
      setBusy(null);
    }
  };

  const restore = async (version: number): Promise<boolean> => {
    if (!id) return false;
    setBusy("restore");
    setError("");
    setResult("");
    try {
      const response = await api.post<AnnotateResponse>(`/api/artifacts/${encodeSegment(id)}/annotate/restore`, { version });
      setLast(response);
      setResult(`Restored version ${version} as version ${response.version}.`);
      await refresh();
      return true;
    } catch (err) {
      setError(errorText(err, "restore", "the annotation version"));
      return false;
    } finally {
      setBusy(null);
    }
  };

  return {
    meta,
    crops,
    storedCropCount: stored,
    versions: versionsQuery.data?.versions ?? [],
    versionsPending: versionsQuery.isPending,
    last,
    busy,
    error,
    result,
    rerender,
    restore,
  };
}

export function AnnotationCrops({ crops }: { crops: AnnotationCrop[] }) {
  if (!crops.length) return null;
  return (
    <section className="crop-strip" aria-label="Annotation crops">
      <h2 className="inspector-title">Crops</h2>
      <div className="crop-row">
        {crops.map((crop) => (
          <a key={crop.itemId} href={localPath(crop.rawUrl)} target="_blank" rel="noopener noreferrer" className="crop-link" title={`Open full crop ${crop.itemId}`}>
            <img src={localPath(crop.url)} alt={`Crop ${crop.itemId}`} loading="lazy" decoding="async" />
            <span className="crop-id mono">{crop.itemId}</span>
          </a>
        ))}
      </div>
    </section>
  );
}

function SpecPanel({ meta }: { meta: AnnotationMeta }) {
  if (meta.spec === undefined || meta.spec === null) return null;
  const items = specItems(meta.spec);
  return (
    <section className="inspector-section">
      <h2 className="inspector-title">Annotation spec</h2>
      <p className="muted">
        {items.length === 1 ? "1 mark" : `${items.length} marks`}
        {meta.styleName ? `, style ${meta.styleName}` : ""}
      </p>
      <pre className="annotation-spec">{JSON.stringify(meta.spec, null, 2)}</pre>
    </section>
  );
}

function snapshotText(snapshot: Record<string, unknown> | null | undefined, key: string): string | null {
  if (!snapshot) return null;
  return textOf(snapshot[key]);
}

function StylePanel({ meta }: { meta: AnnotationMeta }) {
  if (!meta.styleName && !meta.styleSnapshot) return null;
  const snapshot = recordOf(meta.styleSnapshot);
  const fonts = recordOf(snapshot?.fonts);
  const badge = recordOf(snapshot?.badge);
  const callout = recordOf(snapshot?.callout);
  return (
    <section className="inspector-section">
      <h2 className="inspector-title">Style</h2>
      <DefList
        items={[
          meta.styleName ? { label: "Style", value: meta.styleName } : null,
          snapshotText(snapshot, "refWidth") !== null || typeof snapshot?.refWidth === "number" ? { label: "Reference width", value: `${String(snapshot?.refWidth)} px` } : null,
          fonts && textOf(fonts.family) ? { label: "Font", value: textOf(fonts.family) as string } : null,
          badge ? { label: "Badge", value: `${textOf(badge.shape) ?? "circle"}, ${String(badge.size ?? "?")} px` } : null,
          callout && callout.fontSize !== undefined ? { label: "Callout text", value: `${String(callout.fontSize)} px` } : null,
          meta.cleanId ? { label: "Clean source", value: <Mono title={meta.cleanId}>{meta.cleanId.slice(0, 8)}</Mono>, copy: meta.cleanId } : null,
        ]}
      />
    </section>
  );
}

function ReportPanel({ meta }: { meta: AnnotationMeta }) {
  if (meta.report === undefined || meta.report === null) return null;
  const rules = ruleRows(meta.report);
  const contrast = contrastRows(meta.report);
  const record = recordOf(meta.report);
  const ok = record?.ok;
  return (
    <section className="inspector-section">
      <h2 className="inspector-title">Validation report</h2>
      {typeof ok === "boolean" ? <p className={ok ? undefined : "strong danger"}>{ok ? "All rules pass." : "Some rules fail."}</p> : null}
      {meta.anchorFallback ? <p className="muted">Some marks use coordinate fallback instead of a measured element.</p> : null}
      {rules.length ? (
        <ul className="rule-list">
          {rules.map((row) => (
            <li key={row.rule}>
              <span className={row.ok ? undefined : "strong danger"}>{row.ok ? "Pass" : "Fail"}</span> <Mono>{row.rule}</Mono>
              {row.message ? <span className="muted"> {row.message}</span> : null}
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">No rule results stored.</p>
      )}
      {contrast.length ? (
        <DefList compact items={contrast.map((row) => ({ label: row.label, value: <span className="mono">{row.ratio}:1</span> }))} />
      ) : null}
    </section>
  );
}

function VersionsPanel({ artifactId, state, editable, projectId }: { artifactId: string; state: AnnotationState; editable: boolean; projectId: string }) {
  const [style, setStyle] = useState("");
  const [restoring, setRestoring] = useState<AnnotationVersionOut | null>(null);
  useEffect(() => {
    setStyle("");
    setRestoring(null);
  }, [artifactId]);
  return (
    <section className="inspector-section">
      <h2 className="inspector-title">Annotation versions</h2>
      {state.versionsPending ? (
        <p className="muted">Loading versions…</p>
      ) : state.versions.length ? (
        <ul className="version-list">
          {state.versions.map((entry) => (
            <li key={entry.version} className="version-row">
              <a href={localPath(entry.url)} target="_blank" rel="noopener noreferrer" className="link">
                Version {entry.version}
              </a>
              <span className="muted">
                {entry.kind}
                {entry.styleName ? `, ${entry.styleName}` : ""}
                {entry.createdBy ? `, by ${entry.createdBy}` : ""}
              </span>
              <span className="muted">
                <RelTime value={entry.createdAt} />
              </span>
              {editable ? (
                <button type="button" className="link" onClick={() => setRestoring(entry)}>
                  Restore
                </button>
              ) : null}
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">No earlier versions.</p>
      )}
      {editable ? (
        <div className="rerender-row">
          <TextInput value={style} onChange={(event) => setStyle(event.target.value)} placeholder="Style, empty keeps current" aria-label="Style for re-render" />
          <Button busy={state.busy === "rerender"} busyLabel={"Re-rendering…"} onClick={() => void state.rerender(style.trim() || undefined)}>
            Re-render
          </Button>
        </div>
      ) : null}
      {editable ? <p className="field-helper">Built-ins: kb, review. A custom style name must be registered first.</p> : null}
      {state.error ? <p className="field-error">{state.error}</p> : null}
      {state.result ? <ResultText>{state.result}</ResultText> : null}
      {restoring ? (
        <ConfirmDialog
          title={`Restore version ${restoring.version}?`}
          body={`Version ${restoring.version} becomes the current render. Earlier versions stay in history.`}
          confirmLabel="Restore version"
          busyLabel={"Restoring…"}
          destructive={false}
          onClose={() => setRestoring(null)}
          errorFor={(err) => errorText(err, "restore", "the annotation version", projectId)}
          onConfirm={async () => {
            const done = await state.restore(restoring.version);
            if (done) setRestoring(null);
            else throw new Error(state.error || "Could not restore the annotation version.");
          }}
        />
      ) : null}
    </section>
  );
}

export function AnnotationPanels({ artifact, state, editable, projectId }: { artifact: ArtifactOut; state: AnnotationState; editable: boolean; projectId: string }) {
  const { meta } = state;
  if (!meta && !state.versions.length && !state.versionsPending && !state.last) return null;
  return (
    <>
      {meta ? <SpecPanel meta={meta} /> : null}
      {meta ? <StylePanel meta={meta} /> : null}
      {meta ? <ReportPanel meta={meta} /> : null}
      <VersionsPanel artifactId={artifact.id} state={state} editable={editable} projectId={projectId} />
    </>
  );
}

export function AnnotationAssets({ projectId, editable }: { projectId: string; editable: boolean }) {
  const client = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState("");
  const [removing, setRemoving] = useState<ProjectAssetOut | null>(null);
  const assetsQuery = useProjectAssets(projectId);
  const assets = assetsQuery.data ?? [];

  const add = async () => {
    setError("");
    setResult("");
    if (!file) {
      setError("Choose a file to upload.");
      return;
    }
    setBusy(true);
    try {
      const form = new FormData();
      form.set("file", file);
      if (name.trim()) form.set("name", name.trim());
      await uploadForm(`${projectPath(projectId)}/assets`, form, () => undefined);
      setFile(null);
      setName("");
      setResult("Asset added.");
      await client.invalidateQueries({ queryKey: keys.assets(projectId) });
    } catch (err) {
      setError(errorText(err, "add", "the asset", projectId));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="section">
      <h2 className="section-title">Annotation assets</h2>
      <p className="field-helper">Custom icons and images that this project's annotation specs reference by name. PNG, JPEG, WebP or SVG.</p>
      {assetsQuery.isPending ? (
          <p className="muted">Loading assets…</p>
        ) : assetsQuery.isError ? (
          <p className="muted">Assets are unavailable.</p>
        ) : assets.length ? (
          <ul className="version-list">
            {normalizeAssets(assets).map((asset) => (
              <li key={asset.name} className="version-row">
                <Mono title={asset.filename}>{asset.name}</Mono>
                <span className="muted">
                  {asset.filename}
                  {typeof asset.size === "number" ? `, ${formatSize(asset.size)}` : ""}
                </span>
                {editable ? (
                  <button type="button" className="link" onClick={() => setRemoving(asset)}>
                    Remove
                  </button>
                ) : null}
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">No assets in this project.</p>
      )}
      {editable ? (
        <div className="rerender-row">
          <input type="file" accept=".png,.jpg,.jpeg,.webp,.svg" aria-label="Asset file" onChange={(event) => setFile(event.target.files?.[0] ?? null)} />
          <TextInput value={name} onChange={(event) => setName(event.target.value)} placeholder="Name, empty uses filename" aria-label="Asset name" />
          <Button busy={busy} busyLabel={"Adding…"} onClick={() => void add()}>
            Add asset
          </Button>
        </div>
      ) : null}
      {error ? (
        <Notice variant="error">{error}</Notice>
      ) : null}
      {result ? <ResultText>{result}</ResultText> : null}
      {removing ? (
        <ConfirmDialog
          title={`Remove ${removing.name}?`}
          body="Annotation specs that use this asset stop rendering it."
          confirmLabel="Remove asset"
          busyLabel={"Removing…"}
          onClose={() => setRemoving(null)}
          errorFor={(err) => errorText(err, "remove", "the asset", projectId)}
          onConfirm={async () => {
            await api.delete(`${projectPath(projectId)}/assets/${encodeSegment(removing.name)}`);
            setRemoving(null);
            setResult(`Removed ${removing.name}.`);
            await client.invalidateQueries({ queryKey: keys.assets(projectId) });
          }}
        />
      ) : null}
    </section>
  );
}
