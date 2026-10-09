import { useState } from "react";
import { ApiError } from "../api/client";
import { driverAct, driverCapture, driverObserve, useDriver } from "../api/system";
import { Button } from "../components/Button";
import { Icon, type IconName } from "../components/Icon";
import { StateLine } from "../components/Workbench";
import { PluginSlot } from "../shell/plugins";

interface Result {
  label: string;
  ok: boolean;
  text: string;
}

function show(value: unknown): string {
  if (value === null || value === undefined) return "done";
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2);
}

export function ToolRail({ sid, platform, device }: { sid: string | null; platform: "web" | "ios" | "android"; device?: { kind: string; index: number } }) {
  const driver = useDriver(sid);
  const [target, setTarget] = useState("");
  const [text, setText] = useState("");
  const [route, setRoute] = useState("");
  const [code, setCode] = useState("");
  const [fake, setFake] = useState("camera");
  const [recording, setRecording] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [result, setResult] = useState<Result | null>(null);
  const mobile = platform !== "web";

  if (!sid) {
    return (
      <aside className="tool-rail" aria-label="Driver tools">
        <h2 className="tool-rail-title">Drive</h2>
        <p className="ink-2">Lease this machine to drive it.</p>
      </aside>
    );
  }
  const ready = Boolean(driver.data?.worker);
  const run = async (label: string, work: () => Promise<{ value?: unknown }>) => {
    setBusy(label);
    try {
      const response = await work();
      setResult({ label, ok: true, text: show(response.value) });
    } catch (error) {
      setResult({ label, ok: false, text: error instanceof ApiError ? error.message : String(error) });
    } finally {
      setBusy(null);
    }
  };
  const act = (label: string, action: string, params: Record<string, unknown> = {}) => run(label, () => driverAct(sid, action, params));
  const verb = (icon: IconName, label: string, action: () => void, disabled = false) => (
    <button type="button" className="tool" onClick={action} disabled={!ready || busy !== null || disabled}>
      <Icon name={icon} size={14} />
      <span>{label}</span>
    </button>
  );

  return (
    <aside className="tool-rail" aria-label="Driver tools">
      <div className="tool-rail-head">
        <h2 className="tool-rail-title">Drive</h2>
        {driver.isPending ? <span className="ink-3">checking</span> : ready ? <StateLine state="worker ready" tone="ok" /> : <StateLine state="no worker" tone="wait" />}
      </div>
      {!ready && !driver.isPending ? (
        <p className="tool-rail-note">
          No driver worker runs for <span className="mono">{sid}</span>. Start one with <span className="mono">eks-harness flow run</span> or <span className="mono">eks-harness driver</span>.
        </p>
      ) : null}
      <div className="tool-group">
        {mobile ? verb("home", "Home", () => act("Home", "navigate", { route: "Home" })) : verb("home", "Home", () => act("Home", "goto", { route: "/" }))}
        {verb("back", "Back", () => act("Back", "back"))}
        {verb("rotate", "Reload", () => act("Reload", "reload"))}
        {verb("camera", "Screenshot", () => run("Screenshot", () => driverCapture(sid, "screenshot", { name: `lab-${Date.now()}` })))}
        {recording
          ? verb("stop", "Stop recording", () => run("Recording", async () => {
              const response = await driverCapture(sid, "video.stop");
              setRecording(false);
              return response;
            }))
          : verb("record", "Record", () => run("Recording", async () => {
              const response = await driverCapture(sid, "video.start", { name: `lab-${Date.now()}` });
              setRecording(true);
              return response;
            }))}
      </div>
      <form
        className="tool-form"
        onSubmit={(event) => {
          event.preventDefault();
          void act(`Press ${target}`, "press", { target });
        }}
      >
        <label className="field-label" htmlFor="tool-target">
          Target
        </label>
        <input id="tool-target" className="input mono" value={target} placeholder={mobile ? "#testID or text" : "css=, role=, text= or #id"} onChange={(e) => setTarget(e.target.value)} />
        <div className="tool-row">
          <Button size="sm" type="submit" disabled={!ready || !target || busy !== null}>
            Press
          </Button>
          <Button size="sm" disabled={!ready || !target || busy !== null} onClick={() => void run(`Text of ${target}`, () => driverObserve(sid, "text", { target }))}>
            Read text
          </Button>
        </div>
        <label className="field-label" htmlFor="tool-text">
          Fill with
        </label>
        <div className="tool-row">
          <input id="tool-text" className="input" value={text} onChange={(e) => setText(e.target.value)} />
          <Button size="sm" disabled={!ready || !target || busy !== null} onClick={() => void act(`Fill ${target}`, "fill", { target, text })}>
            Fill
          </Button>
        </div>
      </form>
      <form
        className="tool-form"
        onSubmit={(event) => {
          event.preventDefault();
          void act(`Go to ${route}`, mobile ? "navigate" : "goto", { route });
        }}
      >
        <label className="field-label" htmlFor="tool-route">
          {mobile ? "Screen" : "Path"}
        </label>
        <div className="tool-row">
          <input id="tool-route" className="input mono" value={route} placeholder={mobile ? "Checkout" : "/checkout"} onChange={(e) => setRoute(e.target.value)} />
          <Button size="sm" type="submit" disabled={!ready || !route || busy !== null}>
            Go
          </Button>
        </div>
      </form>
      {mobile ? (
        <div className="tool-form">
          <label className="field-label" htmlFor="tool-fake">
            Arm a fake
          </label>
          <div className="tool-row">
            <select id="tool-fake" className="input" value={fake} onChange={(e) => setFake(e.target.value)}>
              <option value="camera">camera</option>
              <option value="gallery">gallery</option>
              <option value="document">document</option>
              <option value="nfc">nfc</option>
            </select>
            <Button size="sm" disabled={!ready || busy !== null} onClick={() => void act(`Arm ${fake}`, "arm", { fake, payload: { mode: "success" } })}>
              Arm
            </Button>
          </div>
        </div>
      ) : null}
      <div className="tool-form">
        <label className="field-label" htmlFor="tool-code">
          Evaluate
        </label>
        <textarea id="tool-code" className="input mono tool-code" rows={3} value={code} placeholder={mobile ? "return nav.getCurrentRoute()" : "return document.title"} onChange={(e) => setCode(e.target.value)} />
        <div className="tool-row">
          <Button size="sm" disabled={!ready || !code || busy !== null} onClick={() => void act("Evaluate", "evaluate", { script: code })}>
            Run
          </Button>
          <Button size="sm" variant="quiet" disabled={!ready || busy !== null} onClick={() => void run("Tree", () => driverObserve(sid, "tree"))}>
            Tree
          </Button>
          <Button size="sm" variant="quiet" disabled={!ready || busy !== null} onClick={() => void run("Logs", () => driverObserve(sid, "logs"))}>
            Logs
          </Button>
        </div>
      </div>
      {busy ? <p className="ink-3">{busy}...</p> : null}
      {result ? (
        <div className="tool-result" data-ok={result.ok || undefined} role="status">
          <div className="tool-result-head">
            <span className={result.ok ? "strong" : "danger strong"}>{result.label}</span>
            <button type="button" className="link" onClick={() => setResult(null)}>
              Clear
            </button>
          </div>
          <pre>{result.text}</pre>
        </div>
      ) : null}
      <PluginSlot slot="device.controls" props={{ device: { kind: device?.kind ?? platform, index: device?.index ?? 0, sid, platform } }} />
    </aside>
  );
}
